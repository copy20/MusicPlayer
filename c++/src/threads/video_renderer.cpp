#include "threads/video_renderer.h"

#include "core/config.h"

#include <QDebug>
#include <QLoggingCategory>
#include <chrono>
#include <cmath>
#include <algorithm>

Q_LOGGING_CATEGORY(lcVidRender, "player.render.video")

namespace {
double nowSec()
{
    using namespace std::chrono;
    static const steady_clock::time_point t0 = steady_clock::now();
    return duration<double>(steady_clock::now() - t0).count();
}
} // namespace

VideoRenderer::VideoRenderer(std::shared_ptr<MediaClock> clock, FrameQueue<VideoFrame> *queue,
                             std::atomic<bool> *playFlag, QObject *parent)
    : StoppableThread(parent)
    , clock_(std::move(clock))
    , queue_(queue)
    , playFlag_(playFlag)
{
    setObjectName(QStringLiteral("render-video"));
}

void VideoRenderer::run()
{
    emit bufferingChanged(true);
    qCDebug(lcVidRender) << "启动";

    while (!stop_.load() && !queue_->isClosed()) {
        // 用户暂停：不消费帧，队列自然堆满 -> 背压到解码端
        if (playFlag_ != nullptr && !playFlag_->load()) {
            if (sleepFor(20))
                break;
            continue;
        }

        VideoFrame frame;
        if (!queue_->peek(frame)) {
            // ---------------- 没数据
            if (queue_->isEos())
                break;
            if (!buffering_) {
                buffering_ = true;
                starveStart_ = nowSec();
                queue_->noteEmptyWait();
                emit bufferingChanged(true);
            } else if (starveStart_ >= 0.0
                       && nowSec() - starveStart_ > cfg::STARVE_PAUSE_AFTER
                       && !clock_->isPaused()) {
                // 长时间没帧：把时钟停住，否则位置一直跑、恢复时要跳一大截
                qCDebug(lcVidRender) << "队列长期为空，暂停时钟等待数据";
                clock_->pause();
            }
            if (sleepFor(cfg::RENDER_TICK_MS))
                break;
            continue;
        }

        // ---------------- 缓冲恢复
        if (buffering_) {
            const double buffered = queue_->bufferedDuration();
            if (buffered < cfg::RESTART_VIDEO_SECONDS && !queue_->isEos()) {
                if (sleepFor(cfg::RENDER_TICK_MS))
                    break;
                continue;
            }
            buffering_ = false;
            starveStart_ = -1.0;
            emit bufferingChanged(false);
            clock_->setTime(frame.pts);       // 从当前这一帧重新起跑
            clock_->resume();
            qCDebug(lcVidRender) << "缓冲完成" << buffered << "s，恢复播放";
        }

        // ---------------- 按节奏出帧
        const double delay = frame.pts - clock_->time();
        if (delay > cfg::MAX_FRAME_DELAY) {
            // 时钟停在原地（或跳转后被甩在后面）时不能无限等，超时就按队首帧重锚
            if (farSince_ < 0.0) {
                farSince_ = nowSec();
            } else if (nowSec() - farSince_ > cfg::CLOCK_STALL_HEAL) {
                qCDebug(lcVidRender) << "时钟停滞，按队首帧重新起跑"
                                     << clock_->time() << "->" << frame.pts;
                clock_->setTime(frame.pts);
                clock_->resume();
                farSince_ = -1.0;
                continue;
            }
            if (sleepFor(cfg::RENDER_TICK_MS))
                break;
            continue;
        }
        farSince_ = -1.0;

        if (delay > 0.001) {
            // 还没到该显示的时间：小睡一会儿（上限 50ms，保证 stop 响应）
            const int ms = int(std::min(delay, 0.05) * 1000.0);
            if (sleepFor(std::max(1, ms)))
                break;
            continue;
        }

        if (delay < -cfg::MAX_FRAME_LATE) {
            // 太迟了：直接丢，别让延迟累积（丢帧比卡顿体验好）
            queue_->pop();
            queue_->noteDroppedLate();
            continue;
        }

        // 该出了：拷一份 RGB24 给 GUI 线程（QImage::copy 深拷贝，跨线程安全）
        QImage image(reinterpret_cast<const uchar *>(frame.data.constData()),
                     frame.width, frame.height, frame.width * 3, QImage::Format_RGB888);
        queue_->pop();
        ++presented_;
        emit frameReady(image.copy());
    }

    qCDebug(lcVidRender) << "退出，共出帧" << presented_;
}
