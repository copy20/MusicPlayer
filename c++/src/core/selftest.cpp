// 无头自测：不开窗口，直接驱动 Player，验证"多线程 + 队列 + 变速 + seek"是否真的对。
//
// 为什么必须有它：这类播放器最容易出的问题是"看起来在播，其实在空转/漂移/卡死"，
// 光靠肉眼看窗口很难发现。这里用媒体时间的**推进速率**和**出帧连续性**来量化。
#include "core/selftest.h"

#include "core/player.h"

#include <QCoreApplication>
#include <QElapsedTimer>
#include <QEventLoop>
#include <QImage>
#include <QTextStream>
#include <QThread>

#include <cmath>
#include <functional>

namespace {

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

void pump(int ms)
{
    QElapsedTimer timer;
    timer.start();
    while (timer.elapsed() < ms) {
        QCoreApplication::processEvents(QEventLoop::AllEvents, 10);
        QThread::msleep(2);
    }
}

bool waitUntil(const std::function<bool()> &pred, int timeoutMs)
{
    QElapsedTimer timer;
    timer.start();
    while (timer.elapsed() < timeoutMs) {
        QCoreApplication::processEvents(QEventLoop::AllEvents, 10);
        if (pred())
            return true;
        QThread::msleep(2);
    }
    return pred();
}

/// 测量媒体时间推进速率。先等画面真的动起来再计时 ——
/// 声卡设备初始化/起播缓冲的耗时不能算进倍速里。
double measureRate(Player *player, int windowMs = 900, int timeoutMs = 12000)
{
    // 先等"时钟稳定推进"：声卡设备初始化、起播缓冲都可能让开始那一下变慢，
    // 直接计时会得到假失败。连续两次 120ms 采样都推进够多才算真的跑起来。
    bool moving = false;
    for (int i = 0; i < timeoutMs / 120 && !moving; ++i) {
        const double a = player->position();
        pump(120);
        const double b = player->position();
        const double need = 0.12 * std::max(0.25, player->speed()) * 0.6;
        moving = (b - a) > need;
    }
    const double p0 = player->position();
    QElapsedTimer timer;
    timer.start();
    pump(windowMs);
    const double elapsed = double(timer.elapsed()) / 1000.0;
    if (elapsed <= 0.0)
        return 0.0;
    return (player->position() - p0) / elapsed;
}

} // namespace

int runSelfTest(const QString &source)
{
    out() << QStringLiteral("=== MiniPlayer C++ 无头自测 ===\n");
    out() << QStringLiteral("媒体源：") << source << QStringLiteral("\n\n");
    out().flush();

    Player player;
    int lastFrameCount = 0;
    QString lastError;
    QObject::connect(&player, &Player::frameReady, [&lastFrameCount](const QImage &image) {
        if (!image.isNull())
            ++lastFrameCount;
    });
    QObject::connect(&player, &Player::errorOccurred,
                     [&lastError](const QString &message) { lastError = message; });

    // ---------------------------------------------------------------- 打开
    bool opened = false;
    try {
        opened = player.open(source, true);
    } catch (const std::exception &ex) {
        check(false, QStringLiteral("打开媒体"), QString::fromUtf8(ex.what()));
        return 1;
    }
    check(opened, QStringLiteral("打开媒体"), lastError);
    if (!opened)
        return 1;

    const MediaInfo info = player.mediaInfo();
    check(info.isValid(), QStringLiteral("媒体信息有效"),
          QStringLiteral("%1 · %2x%3 · %4fps · %5s · 后端 %6")
              .arg(info.title)
              .arg(info.width)
              .arg(info.height)
              .arg(info.fps, 0, 'f', 2)
              .arg(info.duration, 0, 'f', 2)
              .arg(info.backend));

    // ---------------------------------------------------------------- 出帧
    const bool gotFrames = waitUntil([&lastFrameCount]() { return lastFrameCount > 3; }, 12000);
    check(gotFrames, QStringLiteral("开始出帧"), QStringLiteral("frames=%1").arg(lastFrameCount));

    // ---------------------------------------------------------------- 1x
    const double rate1 = measureRate(&player);
    {
        const QVariantMap st = player.stats();
        const QVariantMap vq1 = st.value(QStringLiteral("video_queue")).toMap();
        const QVariantMap aq1 = st.value(QStringLiteral("audio_queue")).toMap();
        check(rate1 > 0.75 && rate1 < 1.35, QStringLiteral("1x 播放速率正常"),
              QStringLiteral("%1x  master=%2 audio=%3 clock=%4 vq=%5/%6s aq=%7/%8s")
                  .arg(rate1, 0, 'f', 2)
                  .arg(st.value(QStringLiteral("clock_master")).toString())
                  .arg(st.value(QStringLiteral("audio_pos")).toDouble(), 0, 'f', 2)
                  .arg(st.value(QStringLiteral("clock")).toDouble(), 0, 'f', 2)
                  .arg(vq1.value(QStringLiteral("size")).toInt())
                  .arg(vq1.value(QStringLiteral("seconds")).toDouble(), 0, 'f', 2)
                  .arg(aq1.value(QStringLiteral("size")).toInt())
                  .arg(aq1.value(QStringLiteral("seconds")).toDouble(), 0, 'f', 2));
    }

    // ---------------------------------------------------------------- 2x
    player.setSpeed(2.0);
    const double rate2 = measureRate(&player);
    check(rate2 > 1.5 && rate2 < 2.7, QStringLiteral("2x 变速生效"),
          QStringLiteral("%1x, 实现=%2")
              .arg(rate2, 0, 'f', 2)
              .arg(player.stats().value(QStringLiteral("speed_tool")).toString()));

    // ---------------------------------------------------------------- 0.5x
    player.setSpeed(0.5);
    const double rate05 = measureRate(&player);
    check(rate05 > 0.3 && rate05 < 0.75, QStringLiteral("0.5x 变速生效"),
          QStringLiteral("%1x").arg(rate05, 0, 'f', 2));

    player.setSpeed(1.0);
    pump(120);

    // ---------------------------------------------------------------- 队列背压
    const QVariantMap vq = player.stats().value(QStringLiteral("video_queue")).toMap();
    const int vqMax = vq.value(QStringLiteral("max")).toInt();
    const int vqPeak = vq.value(QStringLiteral("peak")).toInt();
    check(vqMax > 0 && vqPeak <= vqMax, QStringLiteral("视频队列有界（背压生效）"),
          QStringLiteral("peak=%1 max=%2").arg(vqPeak).arg(vqMax));

    // ---------------------------------------------------------------- 暂停
    player.pause();
    pump(80);
    const double pausedAt = player.position();
    pump(500);
    const double pausedAfter = player.position();
    check(std::fabs(pausedAfter - pausedAt) < 0.08, QStringLiteral("暂停期间位置不动"),
          QStringLiteral("%1 -> %2").arg(pausedAt, 0, 'f', 3).arg(pausedAfter, 0, 'f', 3));
    check(player.state() == Player::State::Paused, QStringLiteral("状态=已暂停"));

    player.play();
    pump(120);
    const double resumedAt = player.position();
    pump(400);
    check(player.position() > resumedAt + 0.1, QStringLiteral("恢复播放后位置继续推进"));

    // ---------------------------------------------------------------- seek 精度
    const double target = std::min(30.0, info.duration * 0.5);
    const int beforeSeek = lastFrameCount;
    player.seek(target);
    const bool resumed = waitUntil(
        [&player, target]() {
            const double p = player.position();
            return p > target - 2.0 && p < target + 4.0;
        },
        12000);
    check(resumed, QStringLiteral("seek 落点正确"),
          QStringLiteral("目标 %1s，实际 %2s")
              .arg(target, 0, 'f', 1)
              .arg(player.position(), 0, 'f', 2));
    const bool framesAfterSeek = waitUntil(
        [&lastFrameCount, beforeSeek]() { return lastFrameCount > beforeSeek + 3; }, 8000);
    check(framesAfterSeek, QStringLiteral("seek 后继续出帧"),
          QStringLiteral("新增 %1 帧").arg(lastFrameCount - beforeSeek));

    // ---------------------------------------------------------------- 连续 seek 压力
    // 这是最容易翻车的场景：解码线程正在读、主线程连续改播放位置。
    const double targets[] = { 3.0, 8.0, 15.0, 2.0, 12.0, 5.0, 18.0, 7.0 };
    int seekFails = 0;
    QStringList failDetail;
    for (double t : targets) {
        const double goal = std::min(t, std::max(0.0, info.duration - 3.0));
        const int before = lastFrameCount;
        player.seek(goal);
        const bool ok = waitUntil(
            [&lastFrameCount, before, &player, goal]() {
                return lastFrameCount > before + 3
                        && std::fabs(player.position() - goal) < 4.0;
            },
            4000);
        if (!ok) {
            ++seekFails;
            failDetail << QStringLiteral("%1s->%2s")
                              .arg(goal, 0, 'f', 0)
                              .arg(player.position(), 0, 'f', 1);
        }
    }
    check(seekFails == 0, QStringLiteral("连续 8 次 seek 均能恢复"),
          seekFails == 0 ? QStringLiteral("全部通过") : failDetail.join(QStringLiteral(", ")));

    // ---------------------------------------------------------------- 停止 / 线程回收
    player.stop();
    pump(200);
    const QVariantMap threads = player.stats().value(QStringLiteral("threads")).toMap();
    bool allStopped = true;
    for (auto it = threads.constBegin(); it != threads.constEnd(); ++it)
        allStopped = allStopped && !it.value().toBool();
    check(allStopped, QStringLiteral("所有工作线程已退出"));
    check(player.position() < 0.01, QStringLiteral("停止后位置归零"),
          QStringLiteral("%1").arg(player.position(), 0, 'f', 3));
    check(player.state() == Player::State::Stopped, QStringLiteral("状态=已停止"));

    // ---------------------------------------------------------------- 二次打开
    lastFrameCount = 0;
    try {
        player.open(source, true);
    } catch (const std::exception &) {
    }
    const bool reopened = waitUntil([&lastFrameCount]() { return lastFrameCount > 3; }, 12000);
    check(reopened, QStringLiteral("关闭后再次打开仍能出帧"));
    player.stop();
    pump(150);

    out() << QStringLiteral("\n通过 %1 项，失败 %2 项\n").arg(g_pass).arg(g_fail);
    out().flush();
    return g_fail == 0 ? 0 : 1;
}
