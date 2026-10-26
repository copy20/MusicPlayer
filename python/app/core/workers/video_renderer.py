"""视频渲染线程 —— "按时钟出帧"。

这是整个播放器最讲究"节奏"的一段循环：

    delay = frame.pts - clock.get_time()      # 这一帧还差多久该显示

* delay > 0        : 睡 min(delay, 50ms)；>> 0 说明时钟还没到（正常情况）
* delay ≈ 0        : 立刻投出去给 GUI 线程画
* delay < -阈值 且后面还有帧 : 丢弃这一帧（迟到丢帧）
                               机器跟不上时，宁可跳帧也不要越播越慢

两条重要的保护：
1. **用户暂停**：直接不动队列，让帧自然堆积 -> 队列满了 -> 读线程被背压住。
   （没有任何额外的同步逻辑，这套因果关系本身就是设计。）
2. **卡顿（队列空）**：先 modestly 暴露 buffering 状态，超过 STARVE_PAUSE_AFTER
   还没数据就把媒体时钟**停下来**，恢复时从第一帧的 pts 重新起跑。
   否则会出现"时钟已经跑到 10s、画面还在 3s"的不可逆错位。

注意：QImage 的构造允许在工作线程里做（QImage 不是 GUI 资源），
但 QPixmap / QWidget::paint 只能在主线程，所以这里通过 Signal 把 QImage
投递给主线程的 imageq 控件（跨线程信号默认走 QueuedConnection）。
"""
from __future__ import annotations

import logging
import time
import threading

from app.core import config
from app.core.workers.base import StoppableThread
from app.qtcompat import QtGui, Signal

log = logging.getLogger("core.video")


class VideoRenderThread(StoppableThread):
    """从视频帧队列按媒体时钟取帧并投递给 GUI。"""

    frame_ready = Signal(object)      # QImage
    buffering = Signal(bool)
    stats_updated = Signal(dict)
    stream_done = Signal()

    def __init__(self, clock, frame_queue, play_event: threading.Event, parent=None):
        super().__init__("render-video", parent)
        self.clock = clock
        self.queue = frame_queue
        self._play = play_event
        self._buffering = True
        self._starve_start = time.monotonic()
        self._presented = 0
        self._late_dropped = 0
        self._fps = 0.0
        self._last_stats = time.monotonic()
        self._shown_since = time.monotonic()
        self._far_since = None          # 一直在"等时钟跟上"的起始时间

    # ---------------------------------------------------------------- 主循环
    def run(self) -> None:  # noqa: D102
        log.debug("[%s] 视频渲染线程启动", self.objectName())
        self._buffering = True
        self._starve_start = time.monotonic()
        self.buffering.emit(True)

        while not self.stopping and not self.queue.is_closed:
            # 用户暂停：不消费帧，让队列自然堆积 -> 触发背压
            if not self._play.is_set():
                if self.sleep(0.02):
                    break
                continue

            frame = self.queue.peek()

            # ---------------- 没数据：缓冲 / 停顿等待
            if frame is None:
                if self.queue.is_eos or self.queue.is_closed:
                    break
                if not self._buffering:
                    self._buffering = True
                    self._starve_start = time.monotonic()
                    self.buffering.emit(True)
                elif time.monotonic() - self._starve_start > config.STARVE_PAUSE_AFTER:
                    if not self.clock.is_paused():
                        log.debug("[%s] 队列长时间为空，暂停时钟等待数据", self.objectName())
                        self.clock.pause()
                if self.sleep(config.RENDER_TICK):
                    break
                continue

            # ---------------- 缓冲恢复
            if self._buffering:
                buffered = self.queue.buffered_duration()
                if buffered < config.RESTART_VIDEO_SECONDS and not self.queue.is_eos:
                    if self.sleep(config.RENDER_TICK):
                        break
                    continue
                self._buffering = False
                self.buffering.emit(False)
                self.clock.set_time(frame.pts)     # 从当前这一帧重新起跑
                self.clock.resume()
                log.debug("[%s] 缓冲完成 %.3fs，恢复播放", self.objectName(), buffered)

            # ---------------- 按节奏出帧
            delay = frame.pts - self.clock.get_time()
            if delay > config.MAX_FRAME_DELAY:
                # 正常情况下这里是"时钟还没走到"，睡一小会儿即可。
                # 但跳转/卡顿之后时钟有可能被停住或被甩在后面，这时不能无限等：
                # 超过 1 秒就以队首帧为基准重新起跑，让播放器自愈。
                if self._far_since is None:
                    self._far_since = time.monotonic()
                elif time.monotonic() - self._far_since > 1.0:
                    log.debug("[%s] 时钟停滞，按队首帧重新起跑 (%.3f -> %.3f)",
                              self.objectName(), self.clock.get_time(), frame.pts)
                    self.clock.set_time(frame.pts)
                    self.clock.resume()
                    self._far_since = None
                    continue
                if self.sleep(config.RENDER_TICK):
                    break
                continue
            self._far_since = None
            if delay > 0.001:
                if self.sleep(min(delay, 0.05)):
                    break
                continue

            # 迟到且后面还有帧 -> 丢（追赶策略）
            if delay < -config.FRAME_DROP_THRESHOLD and self.queue.peek(1) is not None:
                if self.queue.drop_front():
                    self._late_dropped += 1
                continue

            frame = self.queue.get(timeout=0.0)
            if frame is None:
                continue
            self._present(frame)

        log.debug("[%s] 视频渲染线程结束，共出帧 %d，迟到丢弃 %d",
                  self.objectName(), self._presented, self._late_dropped)
        self.stream_done.emit()

    # ---------------------------------------------------------------- 出帧
    def _present(self, frame) -> None:
        try:
            img = QtGui.QImage(frame.data, frame.width, frame.height,
                               frame.width * 3, QtGui.QImage.Format_RGB888)
            if img.isNull():
                log.warning("构造 QImage 失败: %dx%d", frame.width, frame.height)
                return
            self.frame_ready.emit(img.copy())   # copy(): 与bytes解绑，跨线程安全
        except Exception as exc:
            log.exception("投递帧失败: %s", exc)
            return

        self._presented += 1
        now = time.monotonic()
        span = now - self._shown_since
        if span >= 0.5:
            self._fps = self._presented / span if span > 0 else 0.0
            self._presented = 0
            self._shown_since = now
            self.stats_updated.emit(self.stats())

    # ---------------------------------------------------------------- 统计
    def stats(self) -> dict:
        return {
            "render_fps": round(self._fps, 2),
            "late_dropped": self._late_dropped,
            "buffering": self._buffering,
        }
