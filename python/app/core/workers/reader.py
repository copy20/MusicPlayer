"""读线程（视频/音频共用一份实现，各起一个实例）。

职责极简：**循环调用后端的 read_xxx()，把帧塞进有界队列**。
它不做任何时间判断，也不接触任何 Qt 部件——这样"解码跟不上"永远只会导致
自己的循环被队列背压住，不会波及 UI。

关键点是 generation(serial) 的处理：seek 时队列会被 clear() 并且 serial 自增，
此时 put() 返回 False，读线程就去取新的 serial 继续工作（后端已经在 seek()
里把流挪到了新位置），从而避免"上一代的帧混进新时间线"。
"""
from __future__ import annotations

import logging
import time

from app.core.workers.base import StoppableThread
from app.qtcompat import Signal

log = logging.getLogger("core.reader")


class MediaReadThread(StoppableThread):
    """把一个流（video / audio）从后端搬到帧队列。"""

    stream_eof = Signal(str)            # kind
    stats_updated = Signal(str, dict)   # kind, {..}

    def __init__(self, kind: str, backend, frame_queue, parent=None):
        super().__init__("read-" + kind, parent)
        self.kind = kind
        self.backend = backend
        self.queue = frame_queue
        self._frames_read = 0
        self._t0 = 0.0

    def run(self) -> None:  # noqa: D102
        fetch = self.backend.read_video if self.kind == "video" else self.backend.read_audio
        serial = self.queue.serial
        self._t0 = time.monotonic()
        log.debug("[%s] 读线程启动 serial=%s", self.objectName(), serial)

        while not self.stopping:
            try:
                frame = fetch()
            except Exception as exc:      # 读过程中的异常不应该拖垮整个播放器
                log.exception("[%s] 读取异常: %s", self.objectName(), exc)
                self.error_occurred.emit("%s 读取失败: %s" % (self.kind, exc))
                break

            if frame is None:             # EOS
                self.queue.mark_eos()
                log.debug("[%s] EOS, 共读 %d 帧", self.objectName(), self._frames_read)
                break

            frame.serial = serial
            self._frames_read += 1
            if self.queue.put(frame, serial=serial):
                continue

            # put 失败：要么队列关了，要么发生过 seek
            if self.stopping or self.queue.is_closed:
                break
            new_serial = self.queue.serial
            if new_serial != serial:
                log.debug("[%s] generation %d -> %d，切换新时间线", self.objectName(), serial, new_serial)
                serial = new_serial

        self.stream_eof.emit(self.kind)
        self.finished_ok.emit(self.objectName())

    def stats(self) -> dict:
        elapsed = max(1e-6, time.monotonic() - self._t0)
        return {
            "frames": self._frames_read,
            "fps": self._frames_read / elapsed,
            "running": self.isRunning(),
        }
