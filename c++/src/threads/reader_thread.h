// 读线程：把解码器的输出搬进有界缓存队列。视频/音频各一条。
//
// 职责边界（刻意划清）：
//   * 读线程**只管搬运**，不参与任何时序判断，也不知道时钟走到哪了；
//   * 队列满了 push() 会阻塞 => 天然的背压，内存被硬性封顶；
//   * seek 请求也交给它执行，因为解码器（尤其是 ffmpeg 子进程）不是线程安全的，
//     谁读它谁负责 seek，避免"主线程 seek + 读线程 decode"同时操作同一份状态。
#pragma once

#include "core/decoder.h"
#include "core/frame_queue.h"
#include "threads/stoppable_thread.h"

#include <QMutex>
#include <atomic>

class ReaderThread : public StoppableThread {
    Q_OBJECT
public:
    enum class Kind { Video, Audio };

    ReaderThread(Kind kind, IDecoder *decoder, FrameQueue<VideoFrame> *vq,
                 FrameQueue<AudioFrame> *aq, std::atomic<bool> *playFlag,
                 QObject *parent = nullptr);

    /// 请求跳转（真正的 seek 在读线程里执行）
    void requestSeek(double seconds);

    /// 请求变速（后端可能要重建音频管道，同样不能在别的线程碰解码器）
    void requestSpeed(double speed);

signals:
    void eosReached();
    void decodeFailed(const QString &message);

protected:
    void run() override;

private:
    void handleSeek();

    Kind kind_;
    IDecoder *decoder_;
    FrameQueue<VideoFrame> *vq_;
    FrameQueue<AudioFrame> *aq_;
    std::atomic<bool> *playFlag_;
    quint64 serial_ = 0;

    QMutex seekMutex_;
    double seekTarget_ = 0.0;
    bool seekPending_ = false;
    double speedRequest_ = -1.0;    // <0 表示没有待处理的变速请求
};
