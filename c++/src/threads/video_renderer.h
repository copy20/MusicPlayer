// 视频渲染线程：**唯一**决定"这一帧什么时候出"的地方。
//
// 它不画图（画图在 GUI 线程的 VideoWidget 里），只做三件事：
//   1) 看队首帧的 pts 与媒体时钟的差，决定"等一会 / 现在出 / 直接丢"；
//   2) 队列空了就进入缓冲态，必要时把时钟停住（不然画面会在原地空转）；
//   3) 把该出的帧通过信号发给 GUI 线程。
#pragma once

#include "core/frame.h"
#include "core/frame_queue.h"
#include "core/media_clock.h"
#include "threads/stoppable_thread.h"

#include <QImage>
#include <atomic>
#include <memory>

class VideoRenderer : public StoppableThread {
    Q_OBJECT
public:
    VideoRenderer(std::shared_ptr<MediaClock> clock, FrameQueue<VideoFrame> *queue,
                  std::atomic<bool> *playFlag, QObject *parent = nullptr);

    qint64 presentedFrames() const { return presented_; }

signals:
    void frameReady(const QImage &image);
    void bufferingChanged(bool buffering);

protected:
    void run() override;

private:
    std::shared_ptr<MediaClock> clock_;
    FrameQueue<VideoFrame> *queue_;
    std::atomic<bool> *playFlag_;
    qint64 presented_ = 0;
    bool buffering_ = true;
    double starveStart_ = -1.0;
    double farSince_ = -1.0;
};
