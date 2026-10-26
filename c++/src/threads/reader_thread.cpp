#include "threads/reader_thread.h"

#include <QDebug>
#include <QLoggingCategory>
#include <QMutexLocker>

Q_LOGGING_CATEGORY(lcReader, "player.reader")

ReaderThread::ReaderThread(Kind kind, IDecoder *decoder, FrameQueue<VideoFrame> *vq,
                           FrameQueue<AudioFrame> *aq, std::atomic<bool> *playFlag,
                           QObject *parent)
    : StoppableThread(parent)
    , kind_(kind)
    , decoder_(decoder)
    , vq_(vq)
    , aq_(aq)
    , playFlag_(playFlag)
    , serial_(kind == Kind::Video ? vq->serial() : aq->serial())
{
    setObjectName(kind == Kind::Video ? QStringLiteral("read-video")
                                      : QStringLiteral("read-audio"));
}

void ReaderThread::requestSeek(double seconds)
{
    QMutexLocker lk(&seekMutex_);
    seekTarget_ = seconds;
    seekPending_ = true;
    serial_ = (kind_ == Kind::Video) ? vq_->serial() : aq_->serial();  // 新世代
}

void ReaderThread::requestSpeed(double speed)
{
    QMutexLocker lk(&seekMutex_);
    speedRequest_ = speed;
}

void ReaderThread::handleSeek()
{
    double target = 0.0;
    {
        QMutexLocker lk(&seekMutex_);
        if (!seekPending_)
            return;
        target = seekTarget_;
        seekPending_ = false;
    }
    decoder_->seek(target);
    serial_ = (kind_ == Kind::Video) ? vq_->serial() : aq_->serial();
    qCDebug(lcReader) << objectName() << "seek ->" << target;
}

void ReaderThread::run()
{
    decoder_->setAbortFlag(&stop_);

    while (!stop_.load()) {
        // 暂停时不消费：队列很快被填满 -> 背压 -> 解码器也不再往前跑
        if (playFlag_ != nullptr && !playFlag_->load()) {
            if (sleepFor(10))
                break;
            continue;
        }

        handleSeek();

        // 变速请求同样由读线程执行：有 atempo 的后端会重建音频管道，
        // 侧线程直接调 setSpeed() 会和正在读管道的自己打架
        {
            double requested = -1.0;
            {
                QMutexLocker lk(&seekMutex_);
                requested = speedRequest_;
                speedRequest_ = -1.0;
            }
            if (requested > 0.0)
                decoder_->setSpeed(requested);
        }

        if (kind_ == Kind::Video) {
            const VideoFrame frame = decoder_->readVideo();
            if (!frame.isValid()) {
                if (stop_.load())
                    break;
                vq_->setEos(true);
                emit eosReached();
                if (sleepFor(30))            // 等 seek / 停止，别空转烧 CPU
                    break;
                continue;
            }
            const PushResult r = vq_->push(frame, serial_, 500);
            if (r == PushResult::WrongSerial) {
                serial_ = vq_->serial();      // seek 了：换新世代继续
            } else if (r == PushResult::Closed) {
                break;
            }
        } else {
            const AudioFrame frame = decoder_->readAudio();
            if (!frame.isValid()) {
                if (stop_.load())
                    break;
                aq_->setEos(true);
                emit eosReached();
                if (sleepFor(30))
                    break;
                continue;
            }
            const PushResult r = aq_->push(frame, serial_, 500);
            if (r == PushResult::WrongSerial) {
                serial_ = aq_->serial();
            } else if (r == PushResult::Closed) {
                break;
            }
        }
    }
    qCDebug(lcReader) << objectName() << "退出";
}
