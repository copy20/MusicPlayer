// 极简"子进程 + stdout 管道"封装，专供解码器使用。
//
// 为什么不用 QProcess：
//   QProcess 是 QObject，内部依赖 socket notifier，只能在创建它的线程里使用。
//   而解码器对象由两条读线程并发驱动（读视频 / 读音频），从别的线程调
//   read()/waitForReadyRead() 属于跨线程操作 QObject —— 实测直接访问冲突崩溃
//   （0xC0000005；合成源没有子进程，所以这个问题一直没暴露）。
//
// 顺带解决另一个隐患：ffmpeg 会把日志写到 stderr，如果没人读，管道塞满 64KB 后
//   ffmpeg 自己会阻塞；这里把 stderr 也接过来，读数据时顺手抽干，并保留尾部若干
//   字节用于报错。
#pragma once

#include <QByteArray>
#include <QString>
#include <QStringList>
#include <QtGlobal>

#include <atomic>

class ChildProcess {
public:
    ChildProcess() = default;
    ~ChildProcess();
    ChildProcess(const ChildProcess &) = delete;
    ChildProcess &operator=(const ChildProcess &) = delete;

    /// 启动子进程（隐藏窗口）。program 建议给完整路径。
    bool start(const QString &program, const QStringList &args);

    /// 带超时地读一段：>0 = 本次读到的字节数，0 = 超时，-1 = 已结束/出错。
    int readSome(char *buf, int maxBytes, int timeoutMs, const std::atomic<bool> *abort);

    /// 读满 bytes；超时或管道结束返回 false。
    bool readExact(char *buf, int bytes, int timeoutMs, const std::atomic<bool> *abort);

    void kill();
    bool isRunning() const;

    /// 最近一次失败原因（含子进程 stderr 尾部）
    QString lastError() const { return lastError_; }

private:
    void takeStderr();

    void *process_ = nullptr;      // HANDLE
    void *outRead_ = nullptr;      // 标准输出读端
    void *errRead_ = nullptr;      // 标准错误读端
    bool started_ = false;
    QString lastError_;
    QByteArray stderrTail_;
};
