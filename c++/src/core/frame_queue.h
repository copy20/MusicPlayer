// 有界帧队列（缓存队列设计的核心）
//
// 为什么不用 std::queue + 信号量：
//   1) 渲染线程需要 **peek()** —— 先看队首帧的 pts 再决定"现在出还是等"，
//      取出来再放回去会破坏时序判断；
//   2) 需要按 **帧数 + 缓存时长** 双重限流（不同分辨率下单帧内存差几十倍）；
//   3) 需要 **代际(serial)** 机制：seek 时自增，旧世代还在解码途中的帧
//      在 push() 时被直接拒收，从根上杜绝"上一代的帧混进新时间线"；
//   4) 需要完整统计（写入/取出/空转/丢帧/峰值）供监控面板显示。
//
// 同时它天然承担"背压"：队列满了 push() 就阻塞，读线程被卡住，
// 内存占用因此被硬性封顶 —— 这本身就是最省事的流控。
#pragma once

#include <QMutex>
#include <QWaitCondition>
#include <QElapsedTimer>
#include <deque>
#include <algorithm>

struct QueueStats {
    qint64 pushed = 0;        // 成功写入
    qint64 popped = 0;        // 被取走
    qint64 rejected = 0;      // 代际不匹配被拒（seek 后）
    qint64 droppedLate = 0;   // 渲染侧判定"太迟了"直接丢弃
    qint64 emptyWaits = 0;    // 渲染侧遇到空队列的次数（卡顿指标）
    int peak = 0;             // 峰值帧数
};

enum class PushResult { Ok, WrongSerial, Closed, Timeout };

template <typename T>
class FrameQueue {
public:
    FrameQueue(int maxFrames = 12, double maxDuration = 1.0)
        : maxFrames_(std::max(1, maxFrames))
        , maxDuration_(maxDuration > 0.0 ? maxDuration : 0.05)
    {
    }

    void setCapacity(int maxFrames, double maxDuration)
    {
        QMutexLocker lk(&m_);
        maxFrames_ = std::max(1, maxFrames);
        maxDuration_ = maxDuration > 0.0 ? maxDuration : 0.05;
        cv_.wakeAll();
    }

    /// 写入一帧。serial 与当前代际不符 => WrongSerial（seek 后的旧帧）。
    /// timeoutMs < 0 表示一直等到有空间（背压）。
    PushResult push(const T &frame, quint64 serial, int timeoutMs = -1)
    {
        QMutexLocker lk(&m_);
        QElapsedTimer timer;
        if (timeoutMs >= 0)
            timer.start();

        for (;;) {
            if (closed_)
                return PushResult::Closed;
            if (serial != serial_) {
                ++stats_.rejected;
                return PushResult::WrongSerial;
            }
            if (!isFullLocked())
                break;

            int waitMs = 20;
            if (timeoutMs >= 0) {
                const int left = timeoutMs - int(timer.elapsed());
                if (left <= 0)
                    return PushResult::Timeout;
                waitMs = std::min(waitMs, left);
            }
            cv_.wait(&m_, static_cast<unsigned long>(waitMs));
        }

        frames_.push_back(frame);
        ++stats_.pushed;
        stats_.peak = std::max(stats_.peak, int(frames_.size()));
        duration_ += frame.duration > 0.0 ? frame.duration : 0.0;
        return PushResult::Ok;
    }

    /// 看一眼队首帧（拷贝；QByteArray 隐式共享，代价极低）
    bool peek(T &out) const
    {
        QMutexLocker lk(&m_);
        if (frames_.empty())
            return false;
        out = frames_.front();
        return true;
    }

    void pop()
    {
        QMutexLocker lk(&m_);
        if (frames_.empty())
            return;
        duration_ -= frames_.front().duration > 0.0 ? frames_.front().duration : 0.0;
        if (duration_ < 0.0)
            duration_ = 0.0;
        frames_.pop_front();
        ++stats_.popped;
        cv_.wakeAll();
    }

    /// seek：清空并开启新一代（旧世代 push 一律拒收）
    void clear()
    {
        QMutexLocker lk(&m_);
        frames_.clear();
        duration_ = 0.0;
        ++serial_;
        eos_ = false;
        cv_.wakeAll();
    }

    quint64 serial() const { QMutexLocker lk(&m_); return serial_; }
    int size() const { QMutexLocker lk(&m_); return int(frames_.size()); }
    bool isEmpty() const { QMutexLocker lk(&m_); return frames_.empty(); }
    double bufferedDuration() const { QMutexLocker lk(&m_); return duration_; }
    int maxFrames() const { QMutexLocker lk(&m_); return maxFrames_; }
    double maxDuration() const { QMutexLocker lk(&m_); return maxDuration_; }

    bool isFull() const { QMutexLocker lk(&m_); return isFullLocked(); }

    void setEos(bool eos) { QMutexLocker lk(&m_); eos_ = eos; cv_.wakeAll(); }
    bool isEos() const { QMutexLocker lk(&m_); return eos_; }

    void close() { QMutexLocker lk(&m_); closed_ = true; cv_.wakeAll(); }
    bool isClosed() const { QMutexLocker lk(&m_); return closed_; }

    void noteDroppedLate() { QMutexLocker lk(&m_); ++stats_.droppedLate; }
    void noteEmptyWait() { QMutexLocker lk(&m_); ++stats_.emptyWaits; }

    QueueStats stats() const { QMutexLocker lk(&m_); return stats_; }

    /// 起播前的"攒够数据"等待：帧数或时长任一达标即返回
    bool waitForReady(int frames, double seconds, int timeoutMs)
    {
        QMutexLocker lk(&m_);
        QElapsedTimer timer;
        timer.start();
        while (!closed_ && !eos_) {
            if (int(frames_.size()) >= frames || duration_ >= seconds)
                return true;
            const int left = timeoutMs - int(timer.elapsed());
            if (left <= 0)
                return false;
            cv_.wait(&m_, static_cast<unsigned long>(std::min(20, left)));
        }
        return !frames_.empty();
    }

private:
    bool isFullLocked() const
    {
        return int(frames_.size()) >= maxFrames_ || duration_ >= maxDuration_;
    }

    mutable QMutex m_;
    QWaitCondition cv_;
    std::deque<T> frames_;
    double duration_ = 0.0;
    int maxFrames_ = 12;
    double maxDuration_ = 1.0;
    quint64 serial_ = 0;
    bool eos_ = false;
    bool closed_ = false;
    QueueStats stats_;
};
