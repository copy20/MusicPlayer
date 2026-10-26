// 可停止线程基类：所有工作线程都从这里继承，保证"能停下来"这件事只写一次。
//
// 关键点：**所有等待都必须带超时**。只要有一处 wait() 是无限的，
// 关闭窗口时就会卡住，最后只能 terminate（后果是整个进程状态不可控）。
#pragma once

#include <QThread>
#include <atomic>
#include <chrono>

class StoppableThread : public QThread {
    Q_OBJECT
public:
    explicit StoppableThread(QObject *parent = nullptr) : QThread(parent) {}

    ~StoppableThread() override
    {
        // 兜底：子类应主动 stopAndWait()，这里防止忘记
        stopAndWait(1500);
    }

    void requestStop() { stop_.store(true); }

    bool isStopping() const { return stop_.load(); }

    /// QThread 可以被复用（wait() 之后再 start()），但停止标志要先清掉，
    /// 否则新的一轮 run() 一进去就以为该退出了。
    void prepareStart()
    {
        wait();
        stop_.store(false);
    }

    /// 请求停止并等待退出；超时则 terminate 兜底
    void stopAndWait(int timeoutMs = 3000)
    {
        requestStop();
        if (!isRunning())
            return;
        if (!wait(static_cast<unsigned long>(timeoutMs))) {
            terminate();              // 最后手段：只应在"线程里有不可中断阻塞"时发生
            wait(1000);
        }
    }

protected:
    /// 可被 stop 打断的睡眠；返回 true 表示该退出了
    bool sleepFor(int ms)
    {
        const auto deadline = std::chrono::steady_clock::now()
                              + std::chrono::milliseconds(ms);
        while (!stop_.load()) {
            if (std::chrono::steady_clock::now() >= deadline)
                return false;
            QThread::msleep(2);
        }
        return true;
    }

    std::atomic<bool> stop_{ false };
};
