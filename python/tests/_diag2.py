"""seek 压力诊断：8 次跳转（含播到尾部后回跳），失败时打印内部状态。

    python -X utf8 tests/_diag2.py

历史：曾用它定位过"连续 seek 偶发卡死"（已修复，根因有三：
PyAV packet 跨线程持有、demux 线程 EOS 后退出、管线 EOS 后整体死亡）。
留着做回归压测：任何一条 BAD 都意味着回归。
"""
from __future__ import annotations

import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.qtcompat import QtCore, QtWidgets  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "assets", "sample.mp4")
SEEKS = [3.0, 8.0, 15.0, 2.0, 12.0, 5.0, 18.0, 7.0]   # 最后一次刻意落在"播到尾之后回跳"


def spin(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents(QtCore.QEventLoop.AllEvents, 20)
        time.sleep(0.004)


def state(player) -> str:
    b = getattr(player, "_backend", None)
    return ("vpend=%s apend=%s vpk=%s apk=%s v_eos=%s seek_req=%s vq=%s v_alive=%s "
            "target_v=%s pos=%.2f" % (
                len(getattr(b, "_pending_video", []) or []),
                len(getattr(b, "_pending_audio", []) or []),
                getattr(b, "_v_packets").qsize() if getattr(b, "_v_packets", None) else "-",
                getattr(b, "_a_packets").qsize() if getattr(b, "_a_packets", None) else "-",
                getattr(b, "_v_eos", None) and b._v_eos.is_set(),
                getattr(b, "_seek_request", None) and b._seek_request.qsize(),
                player.video_queue.size if player.video_queue else "-",
                player.v_reader is not None and player.v_reader.isRunning(),
                getattr(b, "_seek_target_v", None),
                player.position()))


def main() -> int:
    app = QtWidgets.QApplication([])
    window = MainWindow()
    window.show()
    player = window.player
    counters = {"frames": 0}
    player.frame_ready.connect(lambda img: counters.__setitem__(
        "frames", counters["frames"] + 1) if img is not None else None)

    player.open(SAMPLE, autoplay=True)
    spin(app, 2.0)
    print("初始: " + state(player))

    failures = 0
    for target in SEEKS:
        before = counters["frames"]
        player.seek(target)
        spin(app, 0.3)
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            app.processEvents(QtCore.QEventLoop.AllEvents, 20)
            if counters["frames"] - before >= 5:
                break
            time.sleep(0.005)
        new_frames = counters["frames"] - before
        if new_frames >= 5:
            print("OK  seek(%.0f) 新出帧=%d  %s" % (target, new_frames, state(player)))
        else:
            failures += 1
            print("BAD seek(%.0f) 新出帧=%d  %s" % (target, new_frames, state(player)))
            # 失败时把所有线程的栈打到文件（faulthandler 输出会被 PowerShell 错误流截断）
            with open("_diag_stack.txt", "w", encoding="utf-8") as fh:
                for tid, fr in sys._current_frames().items():
                    import traceback
                    fh.write("===== thread %d =====\n" % tid)
                    fh.write("".join(traceback.format_stack(fr)))

    player.stop()
    spin(app, 0.3)
    window.close()
    print("结果: %d/%d 失败" % (failures, len(SEEKS)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
