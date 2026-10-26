#include "core/player.h"

#include "core/config.h"
#include "core/decoder_factory.h"
#include "threads/audio_renderer.h"
#include "threads/reader_thread.h"
#include "threads/video_renderer.h"

#include <QDebug>
#include <QElapsedTimer>
#include <QLoggingCategory>
#include <QTimer>
#include <algorithm>
#include <chrono>

Q_LOGGING_CATEGORY(lcPlayer, "player.controller")

namespace {
double nowSec()
{
    using namespace std::chrono;
    static const steady_clock::time_point t0 = steady_clock::now();
    return duration<double>(steady_clock::now() - t0).count();
}
} // namespace

Player::Player(QObject *parent)
    : QObject(parent)
    , clock_(std::make_shared<MediaClock>())
{
    auto *timer = new QTimer(this);
    timer->setInterval(60);
    connect(timer, &QTimer::timeout, this, &Player::emitPosition);
    timer->start();
}

Player::~Player()
{
    stopThreads();
    if (decoder_)
        decoder_->close();
}

// ----------------------------------------------------------------- 打开 / 关闭
bool Player::open(const QString &path, bool autoplay)
{
    stopThreads();
    if (decoder_)
        decoder_->close();
    decoder_.reset();
    info_ = MediaInfo();
    path_ = path;
    audioEnabled_ = false;
    audioStale_ = 0;
    audioSynced_ = false;
    audioInControl_ = false;
    audioOffset_ = 0.0;
    lastAudioPos_ = 0.0;
    setState(State::Stopped);

    try {
        decoder_ = DecoderFactory::open(path);
    } catch (const std::exception &ex) {
        emit errorOccurred(QString::fromUtf8(ex.what()));
        return false;
    }

    info_ = decoder_->info();
    videoQueue_.reset(new FrameQueue<VideoFrame>(cfg::VIDEO_QUEUE_FRAMES,
                                                 cfg::VIDEO_QUEUE_SECONDS));
    audioQueue_.reset(new FrameQueue<AudioFrame>(cfg::AUDIO_QUEUE_FRAMES,
                                                 cfg::AUDIO_QUEUE_SECONDS));
    audioEnabled_ = info_.hasAudio;

    clock_->setSpeed(1.0);
    clock_->setTime(0.0);
    clock_->pause();

    qCInfo(lcPlayer) << "打开" << path << "后端=" << info_.backend
                     << "时长=" << info_.duration << "视频=" << info_.hasVideo
                     << "音频=" << info_.hasAudio;

    emit mediaOpened(info_);
    emit positionChanged(0.0);

    if (autoplay)
        play();
    return true;
}

void Player::closeMedia()
{
    stopThreads();
    if (decoder_) {
        decoder_->close();
        decoder_.reset();
    }
    videoQueue_.reset();
    audioQueue_.reset();
    info_ = MediaInfo();
    setState(State::Stopped);
}

// ----------------------------------------------------------------- 传输控制
void Player::play()
{
    if (!decoder_)
        return;

    if (state_ == State::Playing || state_ == State::Buffering)
        return;

    if (state_ == State::Paused) {
        playFlag_.store(true);
        // 恢复播放时重新学一次音频偏移：声卡被 suspend 过，位置基准已经变了
        audioSynced_ = false;
    audioInControl_ = false;
        audioOffset_ = 0.0;
        audioStale_ = 0;
        if (clock_->isPaused())
            clock_->resume();
        setState(State::Playing);
        return;
    }

    startThreads();
    playFlag_.store(true);
    startPreroll();
}

void Player::pause()
{
    if (state_ != State::Playing && state_ != State::Buffering)
        return;
    ++prerollToken_;             // 作废正在进行中的起播流程
    playFlag_.store(false);      // 读线程 / 渲染线程都会停下来（队列随之堆满=背压）
    clock_->pause();
    setState(State::Paused);
    emit bufferingChanged(false);
}

void Player::togglePause()
{
    if (state_ == State::Playing || state_ == State::Buffering)
        pause();
    else
        play();
}

void Player::stop()
{
    if (!decoder_)
        return;
    ++prerollToken_;
    playFlag_.store(false);
    clock_->pause();
    clock_->setTime(0.0);
    stopThreads();

    // 线程都停了，此时在主线程操作解码器是安全的
    if (decoder_)
        decoder_->seek(0.0);
    if (videoQueue_)
        videoQueue_->clear();
    if (audioQueue_) {
        audioQueue_->clear();
        audioQueue_->setEos(false);
    }
    if (videoQueue_)
        videoQueue_->setEos(false);

    lastAudioPos_ = 0.0;
    audioStale_ = 0;
    audioSynced_ = false;
    audioInControl_ = false;
    audioOffset_ = 0.0;
    emit bufferingChanged(false);
    emit positionChanged(0.0);
    setState(State::Stopped);
}

void Player::seek(double seconds)
{
    if (!decoder_)
        return;

    const double target = info_.duration > 0.0
            ? std::clamp(seconds, 0.0, info_.duration)
            : std::max(0.0, seconds);

    ++prerollToken_;
    clock_->pause();
    clock_->setTime(target);
    audioStale_ = 0;
    audioSynced_ = false;
    audioInControl_ = false;          // 音视频管道都重启了，偏移要重新学
    audioOffset_ = 0.0;
    lastAudioPos_ = target;

    // 清队列 = 开启新世代：正在解码途中的旧帧会在 push 时被拒收
    if (videoQueue_)
        videoQueue_->clear();
    if (audioQueue_)
        audioQueue_->clear();

    // seek 与变速都交给读线程执行（解码器不是线程安全的）
    if (videoReader_ != nullptr)
        videoReader_->requestSeek(target);
    else
        decoder_->seek(target);
    if (audioReader_ != nullptr)
        audioReader_->requestSeek(target);

    emit positionChanged(target);

    if (state_ == State::Playing || state_ == State::Buffering)
        startPreroll();
}

void Player::setSpeed(double speed)
{
    // 下限与 OLA 保持一致（0.5）：再慢时内置拉伸填不满输出区间，会变成"半段静音"，
    // 而那种"音画都在动、只是声音断续"的问题最难查，不如直接卡在能力边界上。
    const double s = std::clamp(speed, 0.5, 4.0);
    clock_->setSpeed(s);                    // 画面侧：只改斜率，位置连续
    // 有 atempo 的后端会重建音频管道，位置基准变了 => 偏移重新学
    audioSynced_ = false;
    audioInControl_ = false;
    audioOffset_ = 0.0;
    audioStale_ = 0;
    if (audioReader_ != nullptr)
        audioReader_->requestSpeed(s);      // 音频侧：由读线程重建管道（若有 atempo）
    else if (decoder_)
        decoder_->setSpeed(s);
    if (audioRenderer_ != nullptr)
        audioRenderer_->setNativeTempo(decoder_ && decoder_->supportsNativeTempo());
    emit positionChanged(position());
}

void Player::setVolume(qreal volume)
{
    volume_ = qBound<qreal>(0.0, volume, 1.0);
    if (audioRenderer_ != nullptr)
        audioRenderer_->setVolume(volume_);
}

void Player::setMuted(bool muted)
{
    if (muted_ == muted)
        return;
    muted_ = muted;
    if (audioRenderer_ != nullptr)
        audioRenderer_->setMuted(muted_);
    emit mutedChanged(muted_);
}

// ----------------------------------------------------------------- 查询
QString Player::stateText() const
{
    switch (state_) {
    case State::Stopped:   return QStringLiteral("已停止");
    case State::Buffering: return QStringLiteral("缓冲中…");
    case State::Playing:   return QStringLiteral("播放中");
    case State::Paused:    return QStringLiteral("已暂停");
    }
    return QString();
}

double Player::position() const
{
    if (!clock_)
        return 0.0;
    double t = clock_->time();
    if (info_.duration > 0.0)
        t = std::clamp(t, 0.0, info_.duration);
    return std::max(0.0, t);
}

double Player::speed() const
{
    return clock_ ? clock_->speed() : 1.0;
}

QString Player::clockMaster() const
{
    // 只有音频时钟真的在"当基准"（偏差小、确实在推进）时，主时钟才算 audio
    const bool audioMaster = audioEnabled_ && audioSynced_ && audioInControl_
            && audioStale_ < kAudioStaleIgnore;
    return audioMaster ? QStringLiteral("audio") : QStringLiteral("clock");
}

QVariantMap Player::stats() const
{
    QVariantMap m;
    m.insert(QStringLiteral("backend"), info_.backend);
    m.insert(QStringLiteral("state"), stateText());
    m.insert(QStringLiteral("clock"), position());
    m.insert(QStringLiteral("audio_pos"), lastAudioPos_);
    m.insert(QStringLiteral("clock_master"), clockMaster());
    m.insert(QStringLiteral("speed"), speed());
    if (!decoder_) {
        m.insert(QStringLiteral("speed_tool"), QStringLiteral("-"));
    } else if (decoder_->supportsNativeTempo()) {
        m.insert(QStringLiteral("speed_tool"), QStringLiteral("atempo(后端)"));
    } else if (audioEnabled_) {
        m.insert(QStringLiteral("speed_tool"), QStringLiteral("OLA(CPU)"));
    } else {
        m.insert(QStringLiteral("speed_tool"), QStringLiteral("无音频"));
    }

    auto queueMap = [](int size, int maxFrames, double seconds, double maxSeconds,
                       const QueueStats &st) {
        QVariantMap q;
        q.insert(QStringLiteral("size"), size);
        q.insert(QStringLiteral("max"), maxFrames);
        q.insert(QStringLiteral("seconds"), seconds);
        q.insert(QStringLiteral("max_seconds"), maxSeconds);
        q.insert(QStringLiteral("pushed"), st.pushed);
        q.insert(QStringLiteral("popped"), st.popped);
        q.insert(QStringLiteral("rejected"), st.rejected);
        q.insert(QStringLiteral("dropped_late"), st.droppedLate);
        q.insert(QStringLiteral("empty_waits"), st.emptyWaits);
        q.insert(QStringLiteral("peak"), st.peak);
        return q;
    };

    if (videoQueue_) {
        m.insert(QStringLiteral("video_queue"),
                 queueMap(videoQueue_->size(), videoQueue_->maxFrames(),
                          videoQueue_->bufferedDuration(), videoQueue_->maxDuration(),
                          videoQueue_->stats()));
    }
    if (audioQueue_) {
        m.insert(QStringLiteral("audio_queue"),
                 queueMap(audioQueue_->size(), audioQueue_->maxFrames(),
                          audioQueue_->bufferedDuration(), audioQueue_->maxDuration(),
                          audioQueue_->stats()));
    } else {
        m.insert(QStringLiteral("audio_queue"), QVariantMap());
    }

    QVariantMap threads;
    threads.insert(QStringLiteral("read-video"), videoReader_ && videoReader_->isRunning());
    threads.insert(QStringLiteral("read-audio"), audioReader_ && audioReader_->isRunning());
    threads.insert(QStringLiteral("render-video"), videoRenderer_ && videoRenderer_->isRunning());
    threads.insert(QStringLiteral("render-audio"), audioRenderer_ && audioRenderer_->isRunning());
    m.insert(QStringLiteral("threads"), threads);
    m.insert(QStringLiteral("frames"), videoRenderer_ ? videoRenderer_->presentedFrames() : 0);
    return m;
}

// ----------------------------------------------------------------- 内部
void Player::setState(State state)
{
    if (state_ == state)
        return;
    state_ = state;
    emit stateChanged(int(state));
}

void Player::emitPosition()
{
    if (decoder_)
        emit positionChanged(position());
}

void Player::startThreads()
{
    if (videoReader_ != nullptr || !decoder_ || !videoQueue_ || !audioQueue_)
        return;

    ++prerollToken_;                 // 作废旧流程
    videoQueue_->clear();
    audioQueue_->clear();
    videoQueue_->setEos(false);
    audioQueue_->setEos(false);

    videoReader_ = new ReaderThread(ReaderThread::Kind::Video, decoder_.get(),
                                    videoQueue_.get(), audioQueue_.get(),
                                    &playFlag_, this);
    connect(videoReader_, &ReaderThread::eosReached, this, &Player::onVideoEos);

    if (audioEnabled_) {
        audioReader_ = new ReaderThread(ReaderThread::Kind::Audio, decoder_.get(),
                                        videoQueue_.get(), audioQueue_.get(),
                                        &playFlag_, this);
        audioRenderer_ = new AudioRenderer(clock_, audioQueue_.get(), &playFlag_, this);
        audioRenderer_->setVolume(volume_);
        audioRenderer_->setMuted(muted_);
        audioRenderer_->setNativeTempo(decoder_->supportsNativeTempo());
        connect(audioRenderer_, &AudioRenderer::audioClock, this, &Player::onAudioClock);
        connect(audioRenderer_, &AudioRenderer::audioUnavailable,
                this, &Player::onAudioUnavailable);
    }

    videoRenderer_ = new VideoRenderer(clock_, videoQueue_.get(), &playFlag_, this);
    connect(videoRenderer_, &VideoRenderer::frameReady, this, &Player::onVideoFrameReady);
    connect(videoRenderer_, &VideoRenderer::bufferingChanged,
            this, &Player::onBufferingChanged);

    videoReader_->prepareStart();
    videoReader_->start();
    if (audioReader_) {
        audioReader_->prepareStart();
        audioReader_->start();
    }
    if (audioRenderer_) {
        audioRenderer_->prepareStart();
        audioRenderer_->start();
    }
    videoRenderer_->prepareStart();
    videoRenderer_->start();
}

void Player::stopThreads()
{
    // 先断信号再停：避免"正在销毁的线程最后一帧信号"打到已失效的槽里
    if (videoRenderer_ != nullptr) {
        disconnect(videoRenderer_, nullptr, this, nullptr);
        videoRenderer_->stopAndWait(2000);
        delete videoRenderer_;
        videoRenderer_ = nullptr;
    }
    if (audioRenderer_ != nullptr) {
        disconnect(audioRenderer_, nullptr, this, nullptr);
        audioRenderer_->stopAndWait(2000);
        delete audioRenderer_;
        audioRenderer_ = nullptr;
    }
    if (videoReader_ != nullptr) {
        disconnect(videoReader_, nullptr, this, nullptr);
        videoReader_->stopAndWait(2000);
        delete videoReader_;
        videoReader_ = nullptr;
    }
    if (audioReader_ != nullptr) {
        disconnect(audioReader_, nullptr, this, nullptr);
        audioReader_->stopAndWait(2000);
        delete audioReader_;
        audioReader_ = nullptr;
    }
    if (decoder_)
        decoder_->setAbortFlag(nullptr);
}

void Player::startPreroll()
{
    if (!decoder_ || !videoQueue_)
        return;

    const int token = ++prerollToken_;
    audioSynced_ = false;
    audioInControl_ = false;
    audioOffset_ = 0.0;
    setState(State::Buffering);
    emit bufferingChanged(true);

    auto *timer = new QTimer(this);
    timer->setInterval(15);
    auto *elapsed = new QElapsedTimer();
    elapsed->start();

    connect(timer, &QTimer::timeout, this, [this, timer, elapsed, token]() {
        if (token != prerollToken_.load()) {
            timer->stop();
            timer->deleteLater();
            delete elapsed;
            return;
        }

        const bool videoReady = !info_.hasVideo
                || videoQueue_->bufferedDuration() >= cfg::START_VIDEO_SECONDS
                || videoQueue_->isEos();
        const bool audioReady = !audioEnabled_ || audioQueue_ == nullptr
                || audioQueue_->bufferedDuration() >= cfg::START_AUDIO_SECONDS
                || audioQueue_->isEos();
        const bool timeout = elapsed->elapsed() > 3000;   // 起播别无限等

        if (!(videoReady && audioReady) && !timeout)
            return;

        if (timeout)
            qCWarning(lcPlayer) << "起播缓冲超时，仍然尝试开始播放";

        timer->stop();
        timer->deleteLater();
        delete elapsed;

        // 从队首帧起跑：避免"时钟已经走了一会儿，画面还在等第一帧"
        VideoFrame head;
        if (videoQueue_->peek(head))
            clock_->setTime(head.pts);

        playFlag_.store(true);
        clock_->resume();
        setState(State::Playing);
    });
    timer->start();
}

void Player::resetQueues()
{
    if (videoQueue_)
        videoQueue_->clear();
    if (audioQueue_)
        audioQueue_->clear();
}

// ----------------------------------------------------------------- 槽
void Player::onVideoFrameReady(const QImage &image)
{
    emit frameReady(image);
}

void Player::onBufferingChanged(bool buffering)
{
    emit bufferingChanged(buffering);
    if (!playFlag_.load())
        return;
    if (buffering && state_ == State::Playing)
        setState(State::Buffering);
    else if (!buffering && state_ == State::Buffering)
        setState(State::Playing);
}

void Player::onAudioClock(double seconds)
{
    const double previous = lastAudioPos_;
    lastAudioPos_ = seconds;
    // 只在"真正播放中"才让音频参与校正：缓冲/暂停阶段的报告会互相打架
    if (!audioEnabled_ || state_ != State::Playing)
        return;

    // ---- 1) 这个音频时钟到底可不可信？
    // 有些环境能打开声卡、也能 write，但底层从不消耗（无物理声卡 / 虚拟设备）。
    // 这种"死时钟"如果继续当主时钟，画面会被周期性冻住 —— 必须识别出来。
    if (playFlag_.load()) {
        if (std::fabs(seconds - previous) < 1e-4) {
            ++audioStale_;
            if (audioStale_ == kAudioStaleIgnore)
                qCDebug(lcPlayer) << "音频时钟不推进，暂以画面时钟为准";
            if (audioStale_ >= kAudioStaleFatal) {
                onAudioUnavailable(QStringLiteral("音频时钟长时间不推进"));
                return;
            }
        } else {
            audioStale_ = 0;
        }
    }
    if (audioStale_ >= kAudioStaleIgnore)
        return;

    // ---- 2) 第一次"可信"的报告：只学偏移，不动画面。
    // 声卡比视频晚启动 0.5~2 秒是常事，这时硬对齐等于把画面倒回去；
    // 把这段差值一次性吸收掉，之后就只剩设备时钟域的正常漂移。
    if (!audioSynced_) {
        audioSynced_ = true;
        audioOffset_ = clock_->time() - seconds;
        qCDebug(lcPlayer) << "音频时钟接管主时钟，初始偏移" << audioOffset_ << "s";
        return;
    }

    // ---- 3) 只做温和微调，绝不让音频把画面"拽回去"
    // 音频位置里混着设备缓冲、晚启动、时钟域漂移等一堆常量偏差，
    // 拿它做硬对齐会演成周期性回退。差得离谱就当成"管道刚重启"，重新学偏移。
    const double target = seconds + audioOffset_;
    const double diff = target - clock_->time();
    if (std::fabs(diff) > cfg::AV_RELEARN_DIFF) {
        audioOffset_ = clock_->time() - seconds;
        audioInControl_ = false;
        qCDebug(lcPlayer) << "音频偏移重新学习:" << audioOffset_ << "s";
        return;
    }
    if (std::fabs(diff) > cfg::AV_SOFT_LIMIT) {
        // 中等偏差：既不该硬跳，也不该让一个不可靠的音频时钟长期拖慢画面。
        // 判它"没资格当基准"，画面按自己的节奏走。
        audioInControl_ = false;
        return;
    }
    audioInControl_ = true;
    clock_->correct(target, cfg::AV_SOFT_LIMIT, cfg::AV_SOFT_FACTOR);
}

void Player::onAudioUnavailable(const QString &reason)
{
    if (!audioEnabled_)
        return;
    audioEnabled_ = false;
    qCWarning(lcPlayer) << "音频输出不可用，切换为静音+画面主时钟:" << reason;

    if (audioQueue_)
        audioQueue_->close();            // 让阻塞在 push 的读线程立刻退出
    if (audioReader_ != nullptr) {
        disconnect(audioReader_, nullptr, this, nullptr);
        audioReader_->stopAndWait(1500);
        delete audioReader_;
        audioReader_ = nullptr;
    }
    if (audioRenderer_ != nullptr) {
        disconnect(audioRenderer_, nullptr, this, nullptr);
        audioRenderer_->stopAndWait(1500);
        delete audioRenderer_;
        audioRenderer_ = nullptr;
    }

    // 之前可能被"等音频"冻过时钟，别让画面卡在暂停态
    audioStale_ = 0;
    if (playFlag_.load() && clock_->isPaused())
        clock_->resume();
    emit errorOccurred(QStringLiteral("音频输出不可用，已切换为静音播放（画面以本地时钟为准）：%1")
                           .arg(reason));
}

void Player::onVideoEos()
{
    if (state_ == State::Stopped)
        return;
    // 视频放完：让最后一帧留在屏幕上，位置停在片尾
    qCInfo(lcPlayer) << "播放结束";
    playFlag_.store(false);
    clock_->pause();
    if (info_.duration > 0.0)
        clock_->setTime(info_.duration);
    emit bufferingChanged(false);
    emit positionChanged(position());
    setState(State::Paused);
}
