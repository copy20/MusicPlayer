#include "core/media_clock.h"

#include <cmath>

MediaClock::MediaClock()
    : anchorWall_(std::chrono::steady_clock::now())
{
}

double MediaClock::nowLocked() const
{
    using namespace std::chrono;
    return duration<double>(steady_clock::now() - anchorWall_).count();
}

double MediaClock::timeLocked() const
{
    if (paused_ || !running_)
        return anchorMedia_;
    return anchorMedia_ + nowLocked() * speed_;
}

double MediaClock::time() const
{
    std::lock_guard<std::mutex> lk(m_);
    return timeLocked();
}

void MediaClock::setTime(double t)
{
    std::lock_guard<std::mutex> lk(m_);
    anchorMedia_ = t;
    anchorWall_ = std::chrono::steady_clock::now();
}

void MediaClock::setSpeed(double speed)
{
    std::lock_guard<std::mutex> lk(m_);
    const double cur = timeLocked();          // 先按旧斜率取当前位置
    anchorMedia_ = cur;                       // 再以它为锚点
    anchorWall_ = std::chrono::steady_clock::now();
    if (speed > 0.0)
        speed_ = speed;
}

double MediaClock::speed() const
{
    std::lock_guard<std::mutex> lk(m_);
    return speed_;
}

void MediaClock::pause()
{
    std::lock_guard<std::mutex> lk(m_);
    if (!paused_) {
        anchorMedia_ = timeLocked();
        anchorWall_ = std::chrono::steady_clock::now();
        paused_ = true;
    }
}

void MediaClock::resume()
{
    std::lock_guard<std::mutex> lk(m_);
    if (paused_) {
        anchorWall_ = std::chrono::steady_clock::now();
        paused_ = false;
        running_ = true;
    }
}

bool MediaClock::isPaused() const
{
    std::lock_guard<std::mutex> lk(m_);
    return paused_;
}

void MediaClock::correct(double target, double bigDiff, double factor)
{
    std::lock_guard<std::mutex> lk(m_);
    const double cur = timeLocked();
    const double diff = target - cur;
    anchorWall_ = std::chrono::steady_clock::now();
    if (std::fabs(diff) > bigDiff)
        anchorMedia_ = target;                        // 偏差太大：硬对齐
    else
        anchorMedia_ = cur + diff * factor;           // 小偏差：追一部分，避免抖动
}

void MediaClock::setRunning(bool running)
{
    std::lock_guard<std::mutex> lk(m_);
    running_ = running;
    anchorMedia_ = running ? timeLocked() : anchorMedia_;
    anchorWall_ = std::chrono::steady_clock::now();
}

bool MediaClock::isRunning() const
{
    std::lock_guard<std::mutex> lk(m_);
    return running_;
}
