// 媒体时钟：把"墙上时间"映射成"媒体时间"，并支持倍速与音画软校正。
//
// 设计要点（和 Python 版一致）：
//   * 变速 **不是** 把 pts 除以倍速（那样每次变速都会跳变），
//     而是"分段线性重新锚定"：改斜率前先用当前时间重新设定锚点，
//     于是变速瞬间画面位置完全连续。
//   * 暂停时锚点停在原地，恢复时重新记锚点，位置不会漂。
#pragma once

#include <mutex>
#include <chrono>

class MediaClock {
public:
    MediaClock();

    // 当前媒体时间（秒）
    double time() const;
    // 直接跳到某个媒体时间（seek / 音画硬对齐）
    void setTime(double t);
    // 倍速（0.1 ~ 8.0，1.0 为原速）；切换位置连续
    void setSpeed(double speed);
    double speed() const;

    void pause();
    void resume();
    bool isPaused() const;

    /// 音画偏差软校正：|target-now| > bigDiff 直接跳，否则按 factor 追一部分。
    void correct(double target, double bigDiff, double factor);

    /// 播放/暂停的总开关（stop 时置 false，time() 恒为锚点）
    void setRunning(bool running);
    bool isRunning() const;

private:
    double nowLocked() const;
    double timeLocked() const;

    mutable std::mutex m_;
    std::chrono::steady_clock::time_point anchorWall_;
    double anchorMedia_ = 0.0;
    double speed_ = 1.0;
    bool paused_ = true;
    bool running_ = false;
};
