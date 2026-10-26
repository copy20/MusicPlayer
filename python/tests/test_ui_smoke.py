"""界面/管线冒烟测试（离屏运行，不需要显示器也不需要 ffmpeg）。

    python -m tests.test_ui_smoke

覆盖一次完整的播放循环：
    打开合成源 -> 出帧 -> 计时推进 -> 暂停/恢复 -> 变速 -> 跳转 -> 停止
并顺带校验：线程都能正常退出、缓存队列被清理、状态机迁移正确。
"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "*.debug=false")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.player import PlayerState  # noqa: E402
from app.qtcompat import QtCore, QtWidgets  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

_FAILURES = []
_STEPS = 0


def check(condition: bool, message: str) -> bool:
    global _STEPS
    _STEPS += 1
    if condition:
        print("  [OK]   %s" % message)
    else:
        print("  [FAIL] %s" % message)
        _FAILURES.append(message)
    return condition


def spin(app, seconds: float) -> None:
    """手动推进 Qt 事件队列（不使用 exec_，便于脚本控制）。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents(QtCore.QEventLoop.AllEvents, 20)
        time.sleep(0.004)


def wait_until(app, predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents(QtCore.QEventLoop.AllEvents, 20)
        if predicate():
            return True
        time.sleep(0.005)
    return False


def main() -> int:
    print("== MiniPlayer 界面冒烟测试 ==")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MainWindow()
    window.show()

    player = window.player
    counters = {"frames": 0}

    def _on_frame(image):
        if image is not None:
            counters["frames"] += 1

    player.frame_ready.connect(_on_frame)

    # ---------------------------------------------------------------- 1. 打开
    check(player.open("synthetic://demo", autoplay=True), "打开合成演示源")
    check(wait_until(app, lambda: counters["frames"] > 10, 8.0),
          "开始出帧 (frames=%d)" % counters["frames"])
    check(player.is_playing(), "进入播放状态 (%s)" % player.state_text)

    # ---------------------------------------------------------------- 2. 计时推进
    spin(app, 0.2)
    p0 = player.position()
    t0 = time.monotonic()
    spin(app, 1.0)
    p1 = player.position()
    elapsed = time.monotonic() - t0
    rate = (p1 - p0) / elapsed if elapsed > 0 else 0
    check(0.75 < rate < 1.35, "1x 播放时媒体时间推进速率正常 (%.2f x)" % rate)

    # ---------------------------------------------------------------- 3. 缓存队列被填满并受背压
    stats = player.collect_stats()
    vq = stats["video_queue"]
    check(vq["size"] > 0, "视频队列有缓存 (%d 帧, %.2fs)" % (vq["size"], vq["buffered_sec"]))
    check(vq["size"] <= vq["max_size"], "视频队列不超过容量上限 (%d <= %d)"
          % (vq["size"], vq["max_size"]))
    check(stats["threads"]["read-video"]["alive"], "视频读线程在跑")
    if stats["audio_enabled"]:
        check(stats["threads"]["render-audio"]["alive"], "音频渲染线程在跑")
    else:
        print("  [SKIP] 当前环境无可用声卡，已降级为静音+画面主时钟")

    # ---------------------------------------------------------------- 4. 暂停/恢复
    player.pause()
    spin(app, 0.15)
    pp = player.position()
    spin(app, 0.4)
    check(abs(player.position() - pp) < 0.06, "暂停期间位置不动")
    check(player.state == PlayerState.PAUSED, "状态=暂停")

    player.play()
    spin(app, 0.4)
    check(player.position() > pp, "恢复后位置继续推进")

    # ---------------------------------------------------------------- 5. 变速
    player.set_speed(2.0)
    spin(app, 0.3)
    q0 = player.position()
    t1 = time.monotonic()
    spin(app, 1.0)
    q1 = player.position()
    rate2 = (q1 - q0) / (time.monotonic() - t1)
    check(1.55 < rate2 < 2.6, "2x 播放时媒体时间推进约两倍 (%.2f x)" % rate2)
    check(abs(player.speed - 2.0) < 1e-6, "倍速属性已更新")

    player.set_speed(0.5)
    spin(app, 0.3)
    s0 = player.position()
    t2 = time.monotonic()
    spin(app, 1.0)
    rate3 = (player.position() - s0) / (time.monotonic() - t2)
    check(0.3 < rate3 < 0.75, "0.5x 慢放时媒体时间推进约一半 (%.2f x)" % rate3)

    player.set_speed(1.0)
    spin(app, 0.2)

    # ---------------------------------------------------------------- 6. 跳转
    player.seek(30.0)
    spin(app, 0.8)
    check(abs(player.position() - 30.0) < 1.2, "跳转后位置接近目标 (%.2fs)" % position(player))
    before = counters["frames"]
    spin(app, 0.6)
    check(counters["frames"] > before, "跳转后继续出帧")

    # ---------------------------------------------------------------- 7. UI 控件联动
    window._step_speed(0.25)
    window.volume.setValue(35)
    window.btn_mute.toggle()
    window.chk_loop.setChecked(True)
    spin(app, 0.3)
    check(True, "UI 控件操作未抛异常")

    # ---------------------------------------------------------------- 8. 停止
    player.stop()
    spin(app, 0.3)
    stats = player.collect_stats()
    alive = [n for n, info in stats["threads"].items() if info["alive"]]
    check(not alive, "所有工作线程已停止 (残留: %s)" % (alive or "无"))
    check(player.state == PlayerState.STOPPED, "状态=已停止")
    check(abs(player.position()) < 0.6, "停止后位置归零附近 (%.2f)" % player.position())

    # ---------------------------------------------------------------- 9. 再次打开（复用性）
    check(player.open("synthetic://demo", autoplay=True), "二次打开同一源")
    check(wait_until(app, lambda: counters["frames"] > before + 5, 6.0),
          "二次打开后仍能出帧")
    player.stop()

    window.close()
    print("\n共 %d 项检查，失败 %d 项" % (_STEPS, len(_FAILURES)))
    for f in _FAILURES:
        print("  - %s" % f)
    return 1 if _FAILURES else 0


def position(player):
    return player.position()


if __name__ == "__main__":
    sys.exit(main())
