#include "threads/audio_renderer.h"

#include "core/config.h"

#include <QAudioDeviceInfo>
#include <QAudioFormat>
#include <QDebug>
#include <QLoggingCategory>
#include <algorithm>
#include <chrono>
#include <cmath>

Q_LOGGING_CATEGORY(lcAudRender, "player.render.audio")

namespace {
double nowSec()
{
    using namespace std::chrono;
    static const steady_clock::time_point t0 = steady_clock::now();
    return duration<double>(steady_clock::now() - t0).count();
}
} // namespace

AudioRenderer::AudioRenderer(std::shared_ptr<MediaClock> clock, FrameQueue<AudioFrame> *queue,
                             std::atomic<bool> *playFlag, QObject *parent)
    : StoppableThread(parent)
    , clock_(std::move(clock))
    , queue_(queue)
    , playFlag_(playFlag)
    , stretcher_(cfg::AUDIO_CHANNELS, cfg::AUDIO_SAMPLE_RATE, 1.0, cfg::OLA_FRAME_MS)
{
    setObjectName(QStringLiteral("render-audio"));
}

void AudioRenderer::setVolume(qreal volume)
{
    volume_ = qBound<qreal>(0.0, volume, 1.0);
}

void AudioRenderer::setMuted(bool muted)
{
    muted_ = muted;
}

void AudioRenderer::setNativeTempo(bool native)
{
    nativeTempo_ = native;
}

bool AudioRenderer::openDevice(int sampleRate, int channels)
{
    QAudioFormat format;
    format.setSampleRate(sampleRate);
    format.setChannelCount(channels);
    format.setSampleSize(16);
    format.setCodec(QStringLiteral("audio/pcm"));
    format.setByteOrder(QAudioFormat::LittleEndian);
    format.setSampleType(QAudioFormat::SignedInt);

    const QAudioDeviceInfo device = QAudioDeviceInfo::defaultOutputDevice();
    if (device.isNull()) {
        emit audioUnavailable(QStringLiteral("系统没有可用的音频输出设备"));
        return false;
    }
    if (!device.isFormatSupported(format)) {
        emit audioUnavailable(QStringLiteral("设备不支持 %1Hz/%2ch/16bit 输出")
                                  .arg(sampleRate)
                                  .arg(channels));
        return false;
    }

    sampleRate_ = sampleRate;
    channels_ = channels;

    // 不设 parent：让它归属当前工作线程，避免跨线程使用 Qt 对象
    output_ = new QAudioOutput(device, format);
    output_->setVolume(muted_ ? 0.0f : float(volume_));
    // 设备缓冲给 0.4 秒：太小会频繁 underrun，太大会让音画不同步更难校正
    output_->setBufferSize(int(sampleRate_ * channels_ * 2 * 0.4));
    io_ = output_->start();
    if (io_ == nullptr) {
        emit audioUnavailable(QStringLiteral("音频设备打开失败"));
        delete output_;
        output_ = nullptr;
        return false;
    }
    deviceRunning_ = true;
    qCDebug(lcAudRender) << "声卡就绪" << sampleRate_ << channels_
                         << "bufferSize=" << output_->bufferSize();
    return true;
}

void AudioRenderer::closeDevice()
{
    if (output_ != nullptr) {
        output_->stop();
        delete output_;
        output_ = nullptr;
    }
    io_ = nullptr;
    deviceRunning_ = false;
}

QByteArray AudioRenderer::applySpeed(const AudioFrame &frame)
{
    const double speed = clock_->speed();
    // 后端自带 atempo 或本来就是原速 -> 直通（不花 CPU、不引入延迟）
    if (nativeTempo_ || std::fabs(speed - 1.0) <= 1e-3)
        return frame.data;

    stretcher_.setSpeed(speed);
    stretcher_.push(frame.data.constData(), frame.data.size());
    return stretcher_.pull(frame.data.size());
}

void AudioRenderer::reportClock(double expected, double bufferedSeconds)
{
    const double speed = clock_->speed();
    const double pos = expected - bufferedSeconds * speed;
    emit audioClock(pos);
}

void AudioRenderer::run()
{
    qCDebug(lcAudRender) << "启动";

    QByteArray pending;
    quint64 seenSerial = queue_->serial();
    double lastReport = 0.0;
    double lastDrainCheck = nowSec();
    qint64 lastProcessed = -1;
    bool drainWarned = false;
    int eosStall = 0;          // 收尾数据写不进去的连续次数

    while (!stop_.load() && !queue_->isClosed()) {
        // seek 会清队列并自增代际。上一时间线还没写完的 pending、以及 OLA 内部
        // 的输入/输出缓冲都必须一起作废，否则新时间线的开头会混进 20~40ms 旧音频
        // （听感是"咔"一声或者拖尾）。只有代际变化才清，正常播放零开销。
        const quint64 serial = queue_->serial();
        if (serial != seenSerial) {
            seenSerial = serial;
            pending.clear();
            stretcher_.reset();
        }

        // ---- 暂停：挂起声卡（保留缓冲），恢复时接着播，不会有"放炮音"
        if (playFlag_ != nullptr && !playFlag_->load()) {
            if (output_ != nullptr && output_->state() == QAudio::ActiveState)
                output_->suspend();
            if (sleepFor(15))
                break;
            continue;
        }
        if (output_ != nullptr && output_->state() == QAudio::SuspendedState)
            output_->resume();

        // ---- 没数据可写时从队列取一帧
        if (pending.isEmpty()) {
            if (queue_->isEos() && queue_->isEmpty()) {
                // 收尾：OLA 内部还压着不足一帧的输入（拉伸后最多几十毫秒），
                // 直接退出就把它扔了。flush 第二次调用返回空，正好兼作退出条件。
                // 设备没打开过（纯视频 / 无声卡）就没必要等。
                if (output_ != nullptr && io_ != nullptr)
                    pending = stretcher_.flush();
                if (pending.isEmpty())
                    break;
            } else {
                AudioFrame frame;
                if (!queue_->peek(frame)) {
                    queue_->noteEmptyWait();
                    if (sleepFor(5))
                        break;
                    continue;
                }
                if (output_ == nullptr) {
                    if (!openDevice(frame.sampleRate, frame.channels))
                        break;                   // 设备不可用：交给播放器降级
                    stretcher_.setFormat(frame.sampleRate, frame.channels);
                }
                queue_->pop();
                if (!haveExpected_) {
                    expected_ = frame.pts;
                    haveExpected_ = true;
                }
                pending = applySpeed(frame);
                // 媒体时间按"这一帧实际覆盖的时长"推进，与是否拉伸无关
                expected_ = frame.pts + frame.duration;
                if (pending.isEmpty())
                    continue;
            }
        }

        // ---- 写设备（受设备 buffer 限制，写不进去就等）
        const int freeBytes = output_ != nullptr ? output_->bytesFree() : 0;
        if (freeBytes <= 0) {
            // 收尾阶段设备若一直不收数据（虚拟设备常见），别在这儿卡住退出
            if (queue_->isEos() && ++eosStall > 200)
                break;
            if (sleepFor(3))
                break;
            continue;
        }
        eosStall = 0;
        const int want = std::min(freeBytes, int(pending.size()));
        const qint64 written = io_ != nullptr ? io_->write(pending.constData(), want) : -1;
        if (written > 0)
            pending.remove(0, int(written));

        // ---- 定期上报时钟 + 检测"声卡是否真的在出声"
        const double now = nowSec();
        if (now - lastReport >= 0.05) {
            lastReport = now;
            if (output_ != nullptr) {
                const int bytesPerSecond = sampleRate_ * channels_ * int(sizeof(qint16));
                const int inDevice = output_->bufferSize() - output_->bytesFree();
                const double bufferedWall = bytesPerSecond > 0
                        ? double(inDevice) / double(bytesPerSecond)
                        : 0.0;
                const qint64 processed = output_->processedUSecs();
                // 设备"证明自己在出声"之前，绝对不能拿它当主时钟：
                // 否则画面会被一个还停在 0 的音频时钟拽住（无声卡环境尤其明显）
                if (!verified_ && processed > 0)
                    verified_ = true;
                if (haveExpected_ && verified_)
                    reportClock(expected_, bufferedWall);
                output_->setVolume(muted_ ? 0.0f : float(volume_));
            }
        }

        // 有些环境（无物理声卡 / 虚拟设备）能打开设备、也能 write，但底层从不消耗。
        // 这时音频时钟是死的，继续拿它当主时钟只会把画面冻住 —— 到期就上报不可用。
        if (!drainWarned && output_ != nullptr && now - lastDrainCheck >= cfg::AUDIO_START_TIMEOUT) {
            const qint64 processed = output_->processedUSecs();
            if (processed <= 0 || processed == lastProcessed) {
                drainWarned = true;
                emit audioUnavailable(
                    QStringLiteral("声卡在 %1 秒内没有消耗任何数据，判定为不可用")
                        .arg(cfg::AUDIO_START_TIMEOUT));
                break;
            }
            lastProcessed = processed;
            lastDrainCheck = now;
        }
    }

    // 收尾：设备缓冲里可能还有最后几十毫秒没播完。stop() 会直接丢弃未播放的数据，
    // 所以这里等缓冲空出来再关（最多 250ms；用户主动停止时不等，要的就是立刻静音）。
    if (output_ != nullptr && !stop_.load()) {
        const double deadline = nowSec() + 0.25;
        while (nowSec() < deadline && output_->bytesFree() < output_->bufferSize()) {
            if (sleepFor(5))
                break;
        }
    }

    closeDevice();
    qCDebug(lcAudRender) << "退出";
}
