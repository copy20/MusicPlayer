"""真实媒体文件端到端测试（用 PyAV 后端解码 assets/sample.mp4）。

    python -m tests.test_real_media

没有 PyAV 会自动 skip；没有测试素材会自动用 tools/make_sample_clip.py 生成。
验证点：真实解码的元信息、出帧、计时、seek 到流的新位置、变速、线程回收。
"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "assets", "sample.mp4")

try:
    if os.path.isabs(__file__) is False:  # pragma: no cover
        pass
    import av  # noqa: F401
except Exception:
    av = None

from app.core.player import PlayerState  # noqa: E402
from app.qtcompat import QtCore, QtGui, QtWidgets  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

_FAILURES = []
_STEPS = 0


def check(condition: bool, message: str) -> bool:
    global _STEPS
    _STEPS += 1
    print(("  [OK]   " if condition else "  [FAIL] ") + message)
    if not condition:
        _FAILURES.append(message)
    return condition


def spin(app, seconds: float) -> None:
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


def measure_rate(app, player, window: float = 0.9, timeout: float = 10.0) -> float:
    """测量媒体时间的推进速率。

    注意：声卡设备初始化有时比视频慢（虚拟/无设备环境可达数秒），
    播放器会刻意冻结画面等音频追上来。所以这里先等画面真的开始推进再计时，
    否则会把环境延迟算进倍速，得到假失败。
    """
    wait_until(app, lambda: player.position() > 0.05, timeout)
    p0 = player.position()
    t0 = time.monotonic()
    spin(app, window)
    return (player.position() - p0) / max(1e-6, time.monotonic() - t0)


def ensure_sample() -> str:
    if os.path.isfile(SAMPLE) and os.path.getsize(SAMPLE) > 1024:
        return SAMPLE
    from tools.make_sample_clip import make
    return make(SAMPLE, duration=20, width=640, height=360)


def main() -> int:
    print("== 真实媒体文件播放测试（PyAV 后端）==")
    if av is None:
        print("  [SKIP] 未安装 PyAV（python -m pip install av）")
        return 0

    path = ensure_sample()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MainWindow()
    window.show()
    player = window.player
    counters = {"frames": 0, "image": None}

    def _on_frame(img):
        if img is None:
            return
        counters["frames"] += 1
        if counters["image"] is None:
            counters["image"] = img      # 只留首帧，用来检查像素不是全黑

    player.frame_ready.connect(_on_frame)

    # ---------------------------------------------------------------- 打开
    check(player.open(path, autoplay=True), "打开真实文件: %s" % os.path.basename(path))
    info = player.info
    check(info is not None and info.backend == "pyav", "后端=PyAV (实际=%s)" % info.backend)
    check(19.0 <= info.duration <= 21.0, "时长识别正确 (%.2fs)" % info.duration)
    check(info.width == 640 and info.height == 360, "分辨率正确 (%dx%d)" % (info.width, info.height))
    check(info.has_audio, "识别到音轨")
    check(abs(info.fps - 25.0) < 0.5, "帧率识别正确 (%.2f)" % info.fps)

    check(wait_until(app, lambda: counters["frames"] > 10, 12.0),
          "真实解码出帧 (frames=%d)" % counters["frames"])
    check(player.is_playing(), "进入播放状态")

    # 光有帧数不够：之前 _plane_bytes 用错 API 把画面清零，出帧计数、队列、
    # 位置全都正常，画面上却什么都没有。所以这里必须真的看一眼像素。
    img = counters["image"]
    brightness = 0.0
    if img is not None and not img.isNull():
        total, n = 0.0, 0
        for y in range(0, img.height(), max(1, img.height() // 12)):
            for x in range(0, img.width(), max(1, img.width() // 12)):
                c = QtGui.QColor(img.pixel(x, y))
                total += (c.red() + c.green() + c.blue()) / 3.0
                n += 1
        brightness = total / max(1, n)
    check(brightness > 20.0, "画面不是全黑 (平均亮度 %.1f/255)" % brightness)

    # ---------------------------------------------------------------- 计时
    rate = measure_rate(app, player)
    check(0.75 < rate < 1.35, "1x 播放速率正常 (%.2f x)" % rate)

    stats = player.collect_stats()
    vq = stats["video_queue"]
    check(vq["put"] > 10, "视频队列累计写入 %d 帧" % vq["put"])
    check(vq["size"] <= vq["max_size"], "队列受背压限制 (%d <= %d)" % (vq["size"], vq["max_size"]))
    check(stats["threads"]["read-video"]["alive"], "视频读线程在跑")

    # ---------------------------------------------------------------- seek
    before = counters["frames"]
    player.seek(12.0)
    spin(app, 0.3)
    check(wait_until(app, lambda: 10.5 < player.position() < 13.5, 10.0),
          "seek 到 12s 后位置正确 (%.2f)" % player.position())
    check(counters["frames"] > before, "seek 后继续出帧")

    # ---------------------------------------------------------------- 变速（后端无 atempo -> OLA 兜底）
    player.set_speed(2.0)
    rate2 = measure_rate(app, player)
    st = player.collect_stats()
    check(1.5 < rate2 < 2.7, "2x 变速生效 (%.2f x, 实现=%s)" % (rate2, st.get("speed_tool")))
    check(player.collect_stats()["stretching"] or not player.collect_stats()["audio_enabled"],
          "PyAV 后端已切换到 OLA 音高保持变速")

    player.set_speed(1.0)
    spin(app, 0.5)

    # ---------------------------------------------------------------- 结尾与线程回收
    player.seek(info.duration - 1.0)
    spin(app, 2.0)
    player.stop()
    spin(app, 0.3)
    stats = player.collect_stats()
    alive = [n for n, t in stats["threads"].items() if t["alive"]]
    check(not alive, "所有线程已停止 (残留: %s)" % (alive or "无"))
    check(player.state == PlayerState.STOPPED, "状态=已停止")

    window.close()
    print("\n共 %d 项检查，失败 %d 项" % (_STEPS, len(_FAILURES)))
    for f in _FAILURES:
        print("  - %s" % f)
    return 1 if _FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
