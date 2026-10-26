// FFmpeg 子进程解码后端：把 ffmpeg.exe 当"解码器外挂"用。
//
// 为什么走子进程而不是链接 libav*：
//   * 用户机器上通常只有 ffmpeg.exe，没有开发库（libavcodec.lib / .a）；
//   * 子进程崩了不会带崩播放器；
//   * 代价是多一层管道拷贝，且 seek / 变速要靠"重启子进程"实现（约 100ms）。
//
// 视频管道：ffmpeg -ss T -i F -map 0:v:0 -f rawvideo -pix_fmt rgb24 -vsync 0 -an -sn -
// 音频管道：ffmpeg -ss T -i F -map 0:a:0 -f s16le -acodec pcm_s16le -ar 48000 -ac 2 -vn -
//           （有 atempo 且倍速 != 1 时插入 -af atempo=...，实现保音变速）
//
// 注意：这是个"能力优先"的后端 —— 打开时会**实测**自己能不能真的吐出
// rawvideo / s16le 数据，缺解码器的精简构建（--disable-everything）会被识别出来。
#pragma once

#include "core/child_process.h"
#include "core/decoder.h"

#include <QString>

class FfmpegCliDecoder : public IDecoder {
public:
    FfmpegCliDecoder() = default;
    ~FfmpegCliDecoder() override;

    /// 找 ffmpeg.exe：环境变量 FFMPEG -> ./tools/ffmpeg/bin -> ./tools -> PATH
    static QString locateFfmpeg();

    /// 粗探：能解析出至少一条音视频流就算候选
    static bool canOpen(const QString &path);

    /// 是否支持 atempo 滤镜（决定能不能做"保音调"变速）
    static bool hasAtempo();

    QString displayName() const override { return QStringLiteral("FFmpeg 子进程"); }

    MediaInfo open(const QString &path) override;
    void close() override;

    VideoFrame readVideo() override;
    AudioFrame readAudio() override;

    void seek(double seconds) override;
    void setSpeed(double speed) override;
    bool supportsNativeTempo() const override { return nativeTempo_; }

    qint64 decodedBytes() const override { return videoBytes_ + audioBytes_; }

private:
    struct StreamInfo {
        bool present = false;
        int index = -1;
        QString codec;
    };

    static bool parseInfo(const QString &ffmpeg, const QString &path,
                          MediaInfo &info, StreamInfo &video, StreamInfo &audio);

    bool startVideo(double from);
    bool startAudio(double from);
    void stopProcess(ChildProcess &proc);
    bool readExact(ChildProcess &proc, char *buf, int bytes, int timeoutMs);

    QString ffmpeg_;
    QString path_;
    StreamInfo vStream_;
    StreamInfo aStream_;

    // 注意：这两个是纯 Win32 管道封装，不是 QProcess —— 它们由读线程持有并读取，
    // 而 QProcess 只能在创建它的线程里用（跨线程会崩）。
    ChildProcess vProc_;
    ChildProcess aProc_;
    bool nativeTempo_ = false;
    bool videoEos_ = false;
    bool audioEos_ = false;
    bool closed_ = true;

    double vStart_ = 0.0;      // 当前视频子进程的起点（pts 靠帧序号推算）
    double aStart_ = 0.0;
    double aMedia_ = 0.0;      // 音频当前媒体时间（atempo 输出 1s = 媒体 speed 秒）
    qint64 vFrames_ = 0;
    qint64 aUnits_ = 0;
    qint64 videoBytes_ = 0;
    qint64 audioBytes_ = 0;
};
