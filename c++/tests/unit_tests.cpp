// 纯逻辑单元测试：不建窗口、不开声卡、不解码文件。
//
// 为什么单独做一个可执行文件：
//   selftest.cpp 走的是"整条管线"的集成测试，出问题时要再猜是哪一层；
//   而队列、时钟、变速拼接这三块是纯数据/数学逻辑，可以毫秒级、确定性地验证。
//   它们的 bug 恰恰最隐蔽（队列少丢一代帧、时钟变速时跳变、OLA 把音量拉低），
//   所以这里对每个不变量单独断言。
//
// 用法： miniplayer_unittest.exe      （返回 0 表示全通过）

#include "core/frame.h"
#include "core/frame_queue.h"
#include "core/media_clock.h"
#include "core/time_stretch.h"

#include <QByteArray>
#include <QCoreApplication>
#include <QElapsedTimer>
#include <QTextStream>
#include <QThread>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <thread>

namespace {

// MSVC 默认不定义 M_PI
constexpr double kPi = 3.14159265358979323846;

int g_pass = 0;
int g_fail = 0;

QTextStream &out()
{
    static QTextStream stream(stdout);
    return stream;
}

void check(bool ok, const QString &what, const QString &detail = QString())
{
    out() << (ok ? QStringLiteral("  [OK]   ") : QStringLiteral("  [FAIL] ")) << what;
    if (!detail.isEmpty())
        out() << QStringLiteral("  (") << detail << QStringLiteral(")");
    out() << QStringLiteral("\n");
    out().flush();
    if (ok)
        ++g_pass;
    else
        ++g_fail;
}

QString num(double v, int digits = 3)
{
    return QString::number(v, 'f', digits);
}

VideoFrame makeFrame(double pts, double duration)
{
    VideoFrame f;
    f.pts = pts;
    f.duration = duration;
    f.width = 2;
    f.height = 2;
    f.data = QByteArray(12, '\0');
    return f;
}

// ---------------------------------------------------------------------------
// 1) 有界帧队列：容量、代际、关闭、背压
// ---------------------------------------------------------------------------
void testFrameQueueCapacity()
{
    FrameQueue<VideoFrame> q(3, 10.0);           // 时长上限放到不影响的位置
    const quint64 serial = q.serial();

    bool allOk = true;
    for (int i = 0; i < 3; ++i)
        allOk = allOk && (q.push(makeFrame(0.04 * i, 0.04), serial, 0) == PushResult::Ok);

    check(allOk, "队列未满时写入成功");
    check(q.size() == 3, "队列长度等于写入数", QStringLiteral("size=%1").arg(q.size()));
    check(q.stats().peak == 3, "峰值统计正确", QStringLiteral("peak=%1").arg(q.stats().peak));

    const PushResult r = q.push(makeFrame(0.12, 0.04), serial, 0);
    check(r == PushResult::Timeout, "帧数上限生效：第 4 帧被拒绝",
          QStringLiteral("result=%1").arg(int(r)));
    check(q.size() == 3, "拒绝后长度不变");

    // 时长上限：两帧各 0.06s 就超过 0.10s
    FrameQueue<VideoFrame> qd(100, 0.10);
    qd.push(makeFrame(0.0, 0.06), qd.serial(), 0);
    qd.push(makeFrame(0.06, 0.06), qd.serial(), 0);
    check(qd.isFull(), "时长上限生效（按缓存秒数封顶）",
          QStringLiteral("buffered=%1s").arg(num(qd.bufferedDuration())));
    check(qd.push(makeFrame(0.12, 0.06), qd.serial(), 0) == PushResult::Timeout,
          "时长已满时第 3 帧被拒绝");
}

void testFrameQueueSerial()
{
    FrameQueue<VideoFrame> q(8, 10.0);
    const quint64 oldSerial = q.serial();
    q.push(makeFrame(0.0, 0.04), oldSerial, 0);
    q.push(makeFrame(0.04, 0.04), oldSerial, 0);

    q.clear();                                   // 模拟 seek
    check(q.isEmpty(), "clear() 清空队列");
    check(q.serial() == oldSerial + 1, "clear() 自增代际",
          QStringLiteral("%1 -> %2").arg(oldSerial).arg(q.serial()));

    const PushResult r = q.push(makeFrame(9.9, 0.04), oldSerial, 0);
    check(r == PushResult::WrongSerial, "旧代际的帧被拒收（seek 后不会混入新时间线）",
          QStringLiteral("result=%1").arg(int(r)));
    check(q.stats().rejected == 1, "拒收计数正确",
          QStringLiteral("rejected=%1").arg(q.stats().rejected));
    check(q.isEmpty(), "拒收的帧没有进队列");

    check(q.push(makeFrame(9.9, 0.04), q.serial(), 0) == PushResult::Ok,
          "新代际的帧正常写入");
}

void testFrameQueueCloseAndOrder()
{
    FrameQueue<VideoFrame> q(8, 10.0);
    q.push(makeFrame(1.0, 0.04), q.serial(), 0);
    q.push(makeFrame(2.0, 0.04), q.serial(), 0);

    VideoFrame head;
    check(q.peek(head) && std::fabs(head.pts - 1.0) < 1e-9, "peek() 返回队首帧且不出队");
    check(q.size() == 2, "peek() 不影响队列长度");

    q.pop();
    check(q.peek(head) && std::fabs(head.pts - 2.0) < 1e-9, "pop() 之后队首前移");
    check(q.stats().popped == 1, "取出计数正确");

    q.close();
    check(q.push(makeFrame(3.0, 0.04), q.serial(), 0) == PushResult::Closed,
          "close() 之后写入返回 Closed");
}

void testFrameQueueBackpressure()
{
    // 队列满了之后 push 必须阻塞（这就是流控），pop 之后被唤醒继续写入。
    FrameQueue<VideoFrame> q(2, 10.0);
    const quint64 serial = q.serial();
    q.push(makeFrame(0.0, 0.04), serial, 0);
    q.push(makeFrame(0.04, 0.04), serial, 0);

    std::atomic<bool> finished{false};
    std::atomic<int> result{-1};
    std::thread producer([&q, serial, &finished, &result]() {
        const PushResult r = q.push(makeFrame(0.08, 0.04), serial, 2000);
        result.store(int(r));
        finished.store(true);
    });

    QThread::msleep(60);
    check(!finished.load(), "队列满时写线程被阻塞（背压生效）");

    q.pop();                                     // 释放一个位置
    QElapsedTimer timer;
    timer.start();
    while (!finished.load() && timer.elapsed() < 2000)
        QThread::msleep(2);

    const bool woke = finished.load();
    check(woke, "pop() 之后写线程被唤醒");
    check(woke && result.load() == int(PushResult::Ok), "唤醒后写入成功",
          QStringLiteral("result=%1").arg(result.load()));
    check(q.size() == 2, "长度回到上限", QStringLiteral("size=%1").arg(q.size()));

    if (producer.joinable())
        producer.join();
}

void testFrameQueueWaitForReady()
{
    FrameQueue<VideoFrame> q(8, 10.0);
    check(!q.waitForReady(2, 1.0, 40), "空队列不会假报就绪");

    q.push(makeFrame(0.0, 0.04), q.serial(), 0);
    q.push(makeFrame(0.04, 0.04), q.serial(), 0);
    check(q.waitForReady(2, 1.0, 40), "攒够帧数后就绪");
    check(q.waitForReady(99, 0.07, 40), "时长达标也算就绪");
}

// ---------------------------------------------------------------------------
// 2) 媒体时钟：变速连续、暂停冻结、软校正
// ---------------------------------------------------------------------------
void testMediaClockBasic()
{
    MediaClock c;
    c.setTime(5.0);
    check(std::fabs(c.time() - 5.0) < 1e-9, "未运行时 time() 恒为锚点");

    c.resume();
    const double t0 = c.time();
    QElapsedTimer wallTimer;
    wallTimer.start();
    QThread::msleep(200);
    const double t1 = c.time();
    const double wall = double(wallTimer.elapsed()) / 1000.0;
    const double rate = wall > 0.0 ? (t1 - t0) / wall : 0.0;
    check(std::fabs(rate - 1.0) < 0.15, "1x 时媒体时间按墙钟推进",
          QStringLiteral("rate=%1x").arg(num(rate)));
}

void testMediaClockSpeedContinuity()
{
    MediaClock c;
    c.setTime(10.0);
    c.resume();
    QThread::msleep(100);

    const double before = c.time();
    c.setSpeed(2.0);
    const double after = c.time();
    check(std::fabs(after - before) < 0.01, "变速瞬间位置连续（不跳变）",
          QStringLiteral("before=%1 after=%2").arg(num(before), num(after)));

    QElapsedTimer fastTimer;
    const double p0 = c.time();
    fastTimer.start();
    QThread::msleep(200);
    const double p1 = c.time();
    const double fastWall = double(fastTimer.elapsed()) / 1000.0;
    const double rate = fastWall > 0.0 ? (p1 - p0) / fastWall : 0.0;
    check(std::fabs(rate - 2.0) < 0.3, "2x 时推进速率翻倍",
          QStringLiteral("rate=%1x").arg(num(rate)));

    const double q0 = c.time();
    c.setSpeed(0.5);
    const double q1 = c.time();
    check(std::fabs(q1 - q0) < 0.01, "降速瞬间同样连续");

    QElapsedTimer slowTimer;
    const double r0 = c.time();
    slowTimer.start();
    QThread::msleep(200);
    const double r1 = c.time();
    const double slowWall = double(slowTimer.elapsed()) / 1000.0;
    const double half = slowWall > 0.0 ? (r1 - r0) / slowWall : 0.0;
    check(std::fabs(half - 0.5) < 0.15, "0.5x 时推进速率减半",
          QStringLiteral("rate=%1x").arg(num(half)));
}

void testMediaClockPauseResume()
{
    MediaClock c;
    c.setTime(3.0);
    c.resume();
    QThread::msleep(80);

    c.pause();
    const double frozen = c.time();
    QThread::msleep(120);
    check(std::fabs(c.time() - frozen) < 1e-9, "暂停后时间冻结");

    c.resume();
    QThread::msleep(120);
    check(c.time() - frozen > 0.05, "恢复后继续推进",
          QStringLiteral("+%1s").arg(num(c.time() - frozen)));

    c.setRunning(false);
    const double stopped = c.time();
    QThread::msleep(100);
    check(std::fabs(c.time() - stopped) < 1e-9, "setRunning(false) 后时间冻结");
    c.setRunning(true);
    QThread::msleep(100);
    check(c.time() - stopped > 0.03, "setRunning(true) 后恢复推进");
}

void testMediaClockCorrect()
{
    MediaClock c;
    c.setTime(10.0);                             // 未运行 => time() 恒为锚点，便于确定性断言

    c.correct(10.2, 0.5, 0.5);
    check(std::fabs(c.time() - 10.1) < 1e-6, "小偏差只追一半（软校正，不抖）",
          QStringLiteral("now=%1").arg(num(c.time(), 4)));

    c.correct(20.0, 0.5, 0.5);
    check(std::fabs(c.time() - 20.0) < 1e-6, "超出阈值的偏差直接硬对齐",
          QStringLiteral("now=%1").arg(num(c.time(), 4)));
}

// ---------------------------------------------------------------------------
// 3) OLA 时域拉伸：输出长度、音量守恒
// ---------------------------------------------------------------------------
QByteArray makeSine(int sampleRate, int channels, double seconds, double freq, double amp)
{
    const int frames = int(sampleRate * seconds);
    QByteArray data(frames * channels * int(sizeof(qint16)), '\0');
    qint16 *p = reinterpret_cast<qint16 *>(data.data());
    for (int i = 0; i < frames; ++i) {
        const double t = double(i) / double(sampleRate);
        const qint16 v = qint16(std::lround(amp * 32767.0 * std::sin(2.0 * kPi * freq * t)));
        for (int c = 0; c < channels; ++c)
            p[i * channels + c] = v;
    }
    return data;
}

QByteArray pullAll(OlaStretcher &s, int chunkBytes)
{
    QByteArray all;
    for (;;) {
        const QByteArray part = s.pull(chunkBytes);
        if (part.isEmpty())
            break;
        all.append(part);
    }
    return all;
}

double rmsOf(const QByteArray &pcm, int fromFrame, int toFrame, int channels)
{
    const qint16 *p = reinterpret_cast<const qint16 *>(pcm.constData());
    const int total = pcm.size() / (int(sizeof(qint16)) * channels);
    const int a = std::max(0, fromFrame);
    const int b = std::min(total, toFrame);
    if (b <= a)
        return 0.0;
    double sum = 0.0;
    for (int i = a; i < b; ++i)
        sum += double(p[i * channels]) * double(p[i * channels]);
    return std::sqrt(sum / double((b - a)));
}

void testStretcher()
{
    const int sr = 48000;
    const int ch = 2;
    const QByteArray src = makeSine(sr, ch, 1.0, 440.0, 0.5);
    const int srcFrames = src.size() / (int(sizeof(qint16)) * ch);
    const double srcRms = rmsOf(src, srcFrames / 4, srcFrames * 3 / 4, ch);

    struct Case {
        double speed;
        double expect;      // 输出时长 / 输入时长
        const char *label;
    };
    const Case cases[] = {
        {1.0, 1.00, "1x 旁路"},
        {2.0, 0.50, "2x 加速（时长减半）"},
        {0.5, 2.00, "0.5x 减速（时长加倍）"},
        {1.5, 1.0 / 1.5, "1.5x 变速"},
    };

    for (const Case &c : cases) {
        OlaStretcher s(ch, sr, c.speed);
        s.push(src.constData(), src.size());
        const QByteArray outBytes = pullAll(s, 4096);
        const int outFrames = outBytes.size() / (int(sizeof(qint16)) * ch);
        const double ratio = srcFrames > 0 ? double(outFrames) / double(srcFrames) : 0.0;

        const double tol = c.expect * 0.08;
        check(std::fabs(ratio - c.expect) <= tol,
              QStringLiteral("%1：输出长度符合预期").arg(QString::fromUtf8(c.label)),
              QStringLiteral("ratio=%1 (expect %2)").arg(num(ratio), num(c.expect)));

        if (c.speed < 0.999 || c.speed > 1.001) {
            const double outRms = rmsOf(outBytes, outFrames / 4, outFrames * 3 / 4, ch);
            const double gain = srcRms > 1.0 ? outRms / srcRms : 0.0;
            check(gain > 0.7 && gain < 1.4,
                  QStringLiteral("%1：音量没有被窗函数吃掉").arg(QString::fromUtf8(c.label)),
                  QStringLiteral("rms gain=%1").arg(num(gain)));
        }
    }
}

void testStretcherFormatChange()
{
    OlaStretcher s(2, 48000, 2.0);
    s.setFormat(1, 44100);                       // 切单声道/换采样率后不能残留旧缓冲
    const QByteArray src = makeSine(44100, 1, 0.5, 300.0, 0.4);
    s.push(src.constData(), src.size());
    const QByteArray outBytes = pullAll(s, 1024);
    const int outFrames = outBytes.size() / int(sizeof(qint16));
    const double ratio = double(outFrames) / double(44100 * 0.5);
    check(std::fabs(ratio - 0.5) < 0.06, "换格式后仍按 2x 输出",
          QStringLiteral("ratio=%1").arg(num(ratio)));

    s.setSpeed(1.0);
    check(!s.active(), "速度回到 1x 时 active() 为 false（可旁路）");
}

void testStretcherFlush()
{
    // 尾部残留必须由 flush() 补出来，否则播到结束时结尾会被硬切掉
    OlaStretcher s(2, 48000, 2.0);
    const QByteArray src = makeSine(48000, 2, 0.5, 440.0, 0.5);
    s.push(src.constData(), src.size());

    QByteArray out = pullAll(s, 4096);
    out.append(s.flush());
    const int frames = out.size() / (2 * int(sizeof(qint16)));
    const double ratio = double(frames) / double(48000 * 0.5);
    check(std::fabs(ratio - 0.5) < 0.02, "flush() 把尾部残留补齐（2x）",
          QStringLiteral("ratio=%1").arg(num(ratio)));

    check(s.flush().isEmpty(), "flush() 二次调用返回空（可当退出条件）");
}

} // namespace

int main(int argc, char *argv[])
{
    QCoreApplication app(argc, argv);

    out() << QStringLiteral("== 纯逻辑单元测试 ==\n");
    out().flush();

    out() << QStringLiteral("\n[有界帧队列]\n");
    testFrameQueueCapacity();
    testFrameQueueSerial();
    testFrameQueueCloseAndOrder();
    testFrameQueueBackpressure();
    testFrameQueueWaitForReady();

    out() << QStringLiteral("\n[媒体时钟]\n");
    testMediaClockBasic();
    testMediaClockSpeedContinuity();
    testMediaClockPauseResume();
    testMediaClockCorrect();

    out() << QStringLiteral("\n[变速拉伸]\n");
    testStretcher();
    testStretcherFormatChange();
    testStretcherFlush();

    out() << QStringLiteral("\n== 结果：%1 通过，%2 失败 ==\n").arg(g_pass).arg(g_fail);
    out().flush();
    return g_fail == 0 ? 0 : 1;
}
