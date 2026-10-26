#include "core/ffmpeg_cli_decoder.h"

#include <QCoreApplication>
#include <QDir>
#include <QElapsedTimer>
// 只在主线程做一次性的同步探测（-i 解析 / -filters），用 QProcess 没问题；
// 真正被读线程反复读的两条管道走 ChildProcess。
#include <QProcess>
#include <QFileInfo>
#include <QRegularExpression>
#include <QStandardPaths>
#include <QStringList>
#include <QThread>

#include <algorithm>
#include <cmath>

namespace {

QStringList atempoChain(double speed)
{
    // atempo 单级只支持 0.5 ~ 2.0，超出范围要级联：4x => [2, 2]，0.25x => [0.5, 0.5]
    QStringList chain;
    double s = speed;
    while (s > 2.0 + 1e-6) {
        chain << QStringLiteral("atempo=2.0");
        s /= 2.0;
    }
    while (s < 0.5 - 1e-6) {
        chain << QStringLiteral("atempo=0.5");
        s /= 0.5;
    }
    chain << QStringLiteral("atempo=%1").arg(s, 0, 'f', 4);
    return chain;
}

double parseHms(const QString &h, const QString &m, const QString &s)
{
    return h.toDouble() * 3600.0 + m.toDouble() * 60.0 + s.toDouble();
}

} // namespace

FfmpegCliDecoder::~FfmpegCliDecoder()
{
    close();
}

QString FfmpegCliDecoder::locateFfmpeg()
{
    static QString cached;
    static bool cachedOnce = false;
    if (cachedOnce)
        return cached;

    const QString env = qEnvironmentVariable("FFMPEG");
    if (!env.isEmpty() && QFileInfo::exists(env)) {
        cached = QFileInfo(env).canonicalFilePath();
        cachedOnce = true;
        return cached;
    }

    QStringList candidates;
#ifdef MINIPLAYER_SOURCE_DIR
    // 编译期就知道源码目录，临时构建/任意工作目录下都能找到 tools/ffmpeg
    candidates << QStringLiteral(MINIPLAYER_SOURCE_DIR "/tools/ffmpeg/bin/ffmpeg.exe")
               << QStringLiteral(MINIPLAYER_SOURCE_DIR "/tools/ffmpeg.exe");
#endif
    // 再从可执行文件往上找几层（build/msvc/Release -> build/msvc -> build -> 项目根）
    QDir dir(QCoreApplication::applicationDirPath());
    for (int i = 0; i < 5; ++i) {
        candidates << dir.absoluteFilePath(QStringLiteral("tools/ffmpeg/bin/ffmpeg.exe"))
                   << dir.absoluteFilePath(QStringLiteral("tools/ffmpeg.exe"))
                   // 同仓库 python 版放的那份也认一下，省得重复下载
                   << dir.absoluteFilePath(QStringLiteral("../python/tools/ffmpeg/bin/ffmpeg.exe"))
                   << dir.absoluteFilePath(QStringLiteral("python/tools/ffmpeg/bin/ffmpeg.exe"));
        if (!dir.cdUp())
            break;
    }

    for (const QString &p : candidates) {
        if (QFileInfo::exists(p)) {
            cached = QFileInfo(p).canonicalFilePath();
            cachedOnce = true;
            return cached;
        }
    }

    cached = QStandardPaths::findExecutable(QStringLiteral("ffmpeg"));
    cachedOnce = true;
    return cached;
}

bool FfmpegCliDecoder::hasAtempo()
{
    static int cached = -1;
    if (cached >= 0)
        return cached == 1;

    const QString ffmpeg = locateFfmpeg();
    if (ffmpeg.isEmpty()) {
        cached = 0;
        return false;
    }
    QProcess proc;
    proc.setProcessChannelMode(QProcess::MergedChannels);
    proc.start(ffmpeg, { QStringLiteral("-hide_banner"), QStringLiteral("-filters") });
    if (!proc.waitForFinished(8000)) {
        proc.kill();
        cached = 0;
        return false;
    }
    const QByteArray out = proc.readAll();
    cached = out.contains("atempo") ? 1 : 0;
    return cached == 1;
}

bool FfmpegCliDecoder::parseInfo(const QString &ffmpeg, const QString &path,
                                 MediaInfo &info, StreamInfo &video, StreamInfo &audio)
{
    QProcess proc;
    proc.setProcessChannelMode(QProcess::MergedChannels);
    proc.start(ffmpeg, { QStringLiteral("-hide_banner"), QStringLiteral("-i"), path });
    // 只给输入不给输出，ffmpeg 会以非 0 退出并打印元信息 —— 这正是我们想要的
    proc.waitForFinished(10000);
    const QString text = QString::fromLocal8Bit(proc.readAll());
    proc.kill();

    static const QRegularExpression durRe(
        QStringLiteral("Duration:\\s*(\\d+):(\\d+):(\\d+(?:\\.\\d+)?)"));
    static const QRegularExpression streamRe(
        QStringLiteral("Stream\\s+#(\\d+):(\\d+)(?:\\[[^\\]]*\\])?(?:\\([^)]*\\))?:\\s*"
                       "(Video|Audio):\\s*([^,\\r\\n]+)([^\\r\\n]*)"));
    static const QRegularExpression sizeRe(QStringLiteral("(\\d{2,5})x(\\d{2,5})"));
    static const QRegularExpression fpsRe(QStringLiteral("([\\d.]+)\\s*fps"));
    static const QRegularExpression hzRe(QStringLiteral("(\\d+)\\s*Hz"));
    static const QRegularExpression chRe(
        QStringLiteral("(mono|stereo|quad|5\\.1|7\\.1|(\\d+)\\s*channels)"));

    const auto dm = durRe.match(text);
    if (dm.hasMatch())
        info.duration = parseHms(dm.captured(1), dm.captured(2), dm.captured(3));

    auto it = streamRe.globalMatch(text);
    while (it.hasNext()) {
        const auto m = it.next();
        const QString kind = m.captured(3);
        const QString codec = m.captured(4).trimmed();
        const QString rest = m.captured(5);

        if (kind == QLatin1String("Video") && !video.present) {
            video.present = true;
            video.index = m.captured(2).toInt();
            video.codec = codec;
            info.hasVideo = true;
            const auto sm = sizeRe.match(rest);
            if (sm.hasMatch()) {
                info.width = sm.captured(1).toInt();
                info.height = sm.captured(2).toInt();
            }
            const auto fm = fpsRe.match(rest);
            if (fm.hasMatch() && fm.captured(1).toDouble() > 0.1)
                info.fps = fm.captured(1).toDouble();
        } else if (kind == QLatin1String("Audio") && !audio.present) {
            audio.present = true;
            audio.index = m.captured(2).toInt();
            audio.codec = codec;
            info.hasAudio = true;
            const auto hm = hzRe.match(rest);
            if (hm.hasMatch())
                info.sampleRate = hm.captured(1).toInt();
            const auto cm = chRe.match(rest);
            if (cm.hasMatch()) {
                const QString token = cm.captured(1);
                if (token.startsWith(QLatin1String("mono")))
                    info.channels = 1;
                else if (token.startsWith(QLatin1String("stereo")))
                    info.channels = 2;
                else if (cm.captured(2).toInt() > 0)
                    info.channels = cm.captured(2).toInt();
            }
        }
    }

    info.path = path;
    info.title = QFileInfo(path).fileName();
    return info.hasVideo || info.hasAudio;
}

bool FfmpegCliDecoder::canOpen(const QString &path)
{
    const QString ffmpeg = locateFfmpeg();
    if (ffmpeg.isEmpty() || !QFileInfo::exists(path))
        return false;

    MediaInfo info;
    StreamInfo v, a;
    if (!parseInfo(ffmpeg, path, info, v, a))
        return false;

    // 能解析出流 ≠ 能解码。极简构建（--disable-everything）既没音频解码器，
    // 也可能没编 rawvideo muxer，这里实读一小段验证一下，别让用户看到黑屏。
    FfmpegCliDecoder probe;
    probe.ffmpeg_ = ffmpeg;
    probe.path_ = path;
    probe.info_ = info;
    probe.vStream_ = v;
    probe.aStream_ = a;
    probe.closed_ = false;
    bool ok = false;
    if (info.hasVideo && probe.startVideo(0.0)) {
        QByteArray buf(info.width * info.height * 3, Qt::Uninitialized);
        ok = probe.readExact(probe.vProc_, buf.data(), buf.size(), 6000);
    }
    probe.stopProcess(probe.vProc_);
    probe.closed_ = true;
    return ok;
}

MediaInfo FfmpegCliDecoder::open(const QString &path)
{
    close();

    ffmpeg_ = locateFfmpeg();
    if (ffmpeg_.isEmpty())
        throw DecodeError(QStringLiteral("没找到 ffmpeg.exe"));

    MediaInfo info;
    StreamInfo v, a;
    if (!parseInfo(ffmpeg_, path, info, v, a))
        throw DecodeError(QStringLiteral("ffmpeg 无法解析该文件: %1").arg(path));

    path_ = path;
    info_ = info;
    vStream_ = v;
    aStream_ = a;
    nativeTempo_ = a.present && hasAtempo();
    closed_ = false;

    info.backend = displayName();
    if (!nativeTempo_ && a.present)
        info.notes = QStringLiteral("当前 ffmpeg 构建没有 atempo 滤镜，变速走 CPU 侧 OLA 拉伸");

    if (v.present && !startVideo(0.0)) {
        info.hasVideo = false;
        info.notes += QStringLiteral("；视频无法解码（该 ffmpeg 构建可能缺 rawvideo 输出/解码器）");
    }
    if (a.present && !startAudio(0.0)) {
        info.hasAudio = false;
        info.notes += QStringLiteral("；音频无法解码（该 ffmpeg 构建可能没编音频解码器）");
    }
    if (!info.hasVideo && !info.hasAudio) {
        close();
        throw DecodeError(QStringLiteral("当前 ffmpeg 构建无法解码声明的音视频流"));
    }
    info_ = info;                    // 能力探测后可能改了 hasVideo/hasAudio/notes
    return info;
}

void FfmpegCliDecoder::close()
{
    closed_ = true;
    stopProcess(vProc_);
    stopProcess(aProc_);
}

void FfmpegCliDecoder::stopProcess(ChildProcess &proc)
{
    proc.kill();                 // 内部会 TerminateProcess + 关管道，重复调用安全
}

bool FfmpegCliDecoder::startVideo(double from)
{
    stopProcess(vProc_);
    videoEos_ = false;
    vStart_ = from;
    vFrames_ = 0;
    if (!vStream_.present || info_.width <= 0 || info_.height <= 0)
        return false;

    QStringList args;
    args << QStringLiteral("-hide_banner") << QStringLiteral("-loglevel")
         << QStringLiteral("error");
    if (from > 0.01)
        args << QStringLiteral("-ss") << QString::number(from, 'f', 3);
    args << QStringLiteral("-i") << path_
         << QStringLiteral("-map") << QStringLiteral("0:%1").arg(vStream_.index)
         << QStringLiteral("-an") << QStringLiteral("-sn") << QStringLiteral("-dn")
         << QStringLiteral("-f") << QStringLiteral("rawvideo")
         << QStringLiteral("-pix_fmt") << QStringLiteral("rgb24")
         << QStringLiteral("-vsync") << QStringLiteral("0")
         << QStringLiteral("-");

    return vProc_.start(ffmpeg_, args);
}

bool FfmpegCliDecoder::startAudio(double from)
{
    stopProcess(aProc_);
    audioEos_ = false;
    aStart_ = from;
    aUnits_ = 0;
    aMedia_ = from;
    if (!aStream_.present)
        return false;

    QStringList args;
    args << QStringLiteral("-hide_banner") << QStringLiteral("-loglevel")
         << QStringLiteral("error");
    if (from > 0.01)
        args << QStringLiteral("-ss") << QString::number(from, 'f', 3);
    args << QStringLiteral("-i") << path_
         << QStringLiteral("-map") << QStringLiteral("0:%1").arg(aStream_.index)
         << QStringLiteral("-vn") << QStringLiteral("-sn") << QStringLiteral("-dn")
         << QStringLiteral("-acodec") << QStringLiteral("pcm_s16le")
         << QStringLiteral("-ar") << QString::number(info_.sampleRate)
         << QStringLiteral("-ac") << QString::number(info_.channels);

    if (nativeTempo_ && std::fabs(speed_ - 1.0) > 0.01) {
        const QStringList chain = atempoChain(speed_);
        args << QStringLiteral("-af") << chain.join(QLatin1Char(','));
    }
    args << QStringLiteral("-f") << QStringLiteral("s16le") << QStringLiteral("-");

    return aProc_.start(ffmpeg_, args);
}

bool FfmpegCliDecoder::readExact(ChildProcess &proc, char *buf, int bytes, int timeoutMs)
{
    // 纯阻塞轮询读，不依赖 Qt 事件循环，可以从任意线程调用
    return proc.readExact(buf, bytes, timeoutMs, abortFlag_);
}

VideoFrame FfmpegCliDecoder::readVideo()
{
    VideoFrame frame;
    if (closed_ || videoEos_ || !vStream_.present)
        return frame;

    const int need = info_.width * info_.height * 3;
    if (need <= 0)
        return frame;

    frame.data.resize(need);
    if (!readExact(vProc_, frame.data.data(), need, 4000)) {
        videoEos_ = true;
        frame.data.clear();
        return frame;
    }

    // rawvideo 管道里没有 pts，只能按帧序号 + 恒定帧率推算（对 CFR 素材足够，
    // VFR 素材会有偏差 —— 这是子进程方案的已知代价，见 README）
    frame.pts = vStart_ + double(vFrames_) / std::max(1.0, info_.fps);
    frame.duration = 1.0 / std::max(1.0, info_.fps);
    frame.width = info_.width;
    frame.height = info_.height;
    ++vFrames_;
    videoBytes_ += need;
    return frame;
}

AudioFrame FfmpegCliDecoder::readAudio()
{
    AudioFrame frame;
    if (closed_ || audioEos_ || !aStream_.present)
        return frame;

    const int units = 1024;
    const int bytes = units * info_.channels * int(sizeof(qint16));
    frame.data.resize(bytes);
    if (!readExact(aProc_, frame.data.data(), bytes, 4000)) {
        audioEos_ = true;
        frame.data.clear();
        return frame;
    }

    frame.pts = aMedia_;
    frame.duration = double(units) / double(info_.sampleRate);
    frame.sampleRate = info_.sampleRate;
    frame.channels = info_.channels;

    aUnits_ += units;
    aMedia_ += frame.duration * speed_;      // atempo 输出 1 秒 = 媒体时间 speed 秒
    audioBytes_ += bytes;
    return frame;
}

void FfmpegCliDecoder::seek(double seconds)
{
    if (closed_)
        return;
    const double t = std::max(0.0, seconds);
    if (vStream_.present)
        startVideo(t);
    if (aStream_.present)
        startAudio(t);
}

void FfmpegCliDecoder::setSpeed(double speed)
{
    IDecoder::setSpeed(speed);
    if (closed_ || !nativeTempo_ || !aStream_.present)
        return;
    // 有 atempo：从"当前听到的位置"重建音频管道，音调不变、音量不变
    startAudio(aMedia_);
}
