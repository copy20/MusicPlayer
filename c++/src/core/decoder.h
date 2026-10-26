// 解码后端接口。
//
// 上层（播放器/读线程）只认这个接口，完全不知道底下是 ffmpeg.exe 子进程、
// libav* 进程内解码，还是程序化生成的合成源 —— 这是"后端可插拔"的关键。
#pragma once

#include "core/frame.h"

#include <QString>
#include <atomic>
#include <stdexcept>

class DecodeError : public std::runtime_error {
public:
    explicit DecodeError(const QString &msg)
        : std::runtime_error(msg.toStdString())
        , message_(msg)
    {
    }
    QString message() const { return message_; }

private:
    QString message_;
};

class IDecoder {
public:
    virtual ~IDecoder() = default;

    virtual QString displayName() const = 0;

    /// 打开文件并返回媒体信息；失败抛 DecodeError
    virtual MediaInfo open(const QString &path) = 0;
    virtual void close() = 0;

    /// 读一帧；返回空帧表示 EOS（或已关闭）。允许阻塞（在专用读线程里调用）
    virtual VideoFrame readVideo() = 0;
    virtual AudioFrame readAudio() = 0;

    /// 跳转到指定媒体时间
    virtual void seek(double seconds) = 0;

    /// 变速：有原生 atempo 的后端会重建音频流保音调，其余由上层 OLA 兜底
    virtual void setSpeed(double speed) { speed_ = speed; }
    virtual bool supportsNativeTempo() const { return false; }

    double speed() const { return speed_; }

    /// 已解码字节数（监控用）
    virtual qint64 decodedBytes() const { return 0; }

    /// 读线程把自己的停止标志挂进来，让阻塞的 read 能被打断
    /// （否则关窗口时要等一个完整的 pipe 超时才退出）
    virtual void setAbortFlag(std::atomic<bool> *flag) { abortFlag_ = flag; }

    /// open() 成功后填充的信息（上层据此建队列、建线程、显示状态）
    MediaInfo info() const { return info_; }

protected:
    double speed_ = 1.0;
    std::atomic<bool> *abortFlag_ = nullptr;
    MediaInfo info_;
};
