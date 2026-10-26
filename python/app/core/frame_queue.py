"""有界缓存队列（参考 ffplay 的 FrameQueue 思路做 Python 化改造）。

为什么要自己写，而不是直接用 queue.Queue
------------------------------------------
1. 需要**按缓存时长**（而不只是帧数）限流：音频 1 秒是 192KB，视频 1 秒才几帧，
   单一的 maxsize 对两种流都不合适；
2. 需要 peek() —— 视频渲染线程要"先看一眼下一帧的 pts 再决定是睡还是丢帧"，
   queue.Queue 不支持；
3. 需要 generation(serial) —— seek 要清空并作废上一代帧，防止旧帧污染新时间线；
4. 需要统计与水位的接口 —— 界面要显示队列状态，卡顿判断要依赖"缓存时长"。

三种背压/丢帧策略
-----------------
* ``block``（默认）：队列满则阻塞生产者。解码线程会自然被拖住，内存可控。
  这是"缓存"的本质：宁可让解码慢一点，也不要丢数据。
* ``drop_oldest``：队列满时丢最旧的一帧。适合实时性优先的场景。
* 视频侧另有**迟到丢帧**：渲染线程发现当前帧已经过期很久，且后面还有帧，
  就跳过它（见 workers/video_renderer.py），这是 ffplay 的做法，
  比在生产者侧丢更能保证不"白解码"。
"""
from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Deque, Optional

__all__ = ["FrameQueue", "QueueStats"]

log = logging.getLogger("core.queue")


@dataclass
class QueueStats:
    """队列运行时统计，用于监控面板 / 判断卡顿丢帧。"""

    put_count: int = 0
    get_count: int = 0
    dropped_by_producer: int = 0   # 生产者侧因满而丢
    dropped_by_consumer: int = 0   # 消费者侧因迟到而丢
    full_events: int = 0
    starve_events: int = 0         # 消费者空手而归的次数（= 卡顿倾向）
    peak_frames: int = 0
    wait_put_seconds: float = 0.0
    wait_get_seconds: float = 0.0
    last_activity: float = field(default_factory=time.monotonic)


class FrameQueue:
    """线程安全的有界帧队列。

    :param max_frames:    帧数上限
    :param max_duration:  缓存时长上限（秒），按 (最后一帧 end - 第一帧 pts) 计算
    :param overflow:      block | drop_oldest
    """

    def __init__(
        self,
        name: str = "queue",
        max_frames: int = 64,
        max_duration: Optional[float] = None,
        overflow: str = "block",
        high_watermark_ratio: float = 0.8,
    ):
        self.name = name
        self.max_frames = max(1, int(max_frames))
        self.max_duration = max_duration
        self.overflow = overflow
        self.high_watermark_ratio = high_watermark_ratio

        self._cond = threading.Condition(threading.Lock())
        self._frames: Deque = collections.deque()
        self._serial = 0
        self._closed = False
        self._eos = False
        self.stats = QueueStats()

    # ---------------------------------------------------------------- 属性
    @property
    def serial(self) -> int:
        with self._cond:
            return self._serial

    @property
    def size(self) -> int:
        with self._cond:
            return len(self._frames)

    def __len__(self) -> int:  # 方便调试
        return self.size

    @property
    def is_closed(self) -> bool:
        with self._cond:
            return self._closed

    @property
    def is_eos(self) -> bool:
        with self._cond:
            return self._eos

    def capacity_ratio(self) -> float:
        return self.size / float(self.max_frames)

    def nearly_full(self) -> bool:
        return self.capacity_ratio() >= self.high_watermark_ratio

    def is_empty(self) -> bool:
        with self._cond:
            return not self._frames

    def buffered_duration(self) -> float:
        """当前缓存覆盖的媒体时长（秒）。"""
        with self._cond:
            return self._buffered_duration_locked()

    def _buffered_duration_locked(self) -> float:
        if not self._frames:
            return 0.0
        first = self._frames[0]
        last = self._frames[-1]
        pts = getattr(first, "pts", 0.0)
        return max(0.0, getattr(last, "end", getattr(last, "pts", 0.0)) - pts)

    def peek(self, index: int = 0):
        """看第 index 帧但不取出（index=1 就是"下一帧"）。"""
        with self._cond:
            try:
                return self._frames[index]
            except IndexError:
                return None

    # ---------------------------------------------------------------- 状态控制
    def close(self) -> None:
        """关闭并唤醒所有等待者，之后 get() 立刻返回 None（用于线程退出）。"""
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def reopen(self) -> None:
        with self._cond:
            self._closed = False
            self._eos = False
            self._cond.notify_all()

    def clear(self, keep_eos: bool = False) -> int:
        """清空并把 generation +1（seek / 停止时使用）。返回被丢弃的帧数。"""
        with self._cond:
            n = len(self._frames)
            self._frames.clear()
            self._serial += 1
            if not keep_eos:
                self._eos = False
            self._cond.notify_all()
        log.debug("[%s] clear, dropped=%d, serial->%d", self.name, n, self._serial)
        return n

    def mark_eos(self) -> None:
        with self._cond:
            self._eos = True
            self._cond.notify_all()

    # ---------------------------------------------------------------- 生产
    def put(self, frame, serial: Optional[int] = None, timeout: Optional[float] = None) -> bool:
        """放入一帧。

        :param serial: 生产者开始时记录的 generation；与当前不同的话说明
                       期间发生过 seek，这一帧作废（返回 False）。
        :param timeout: None 表示一直等到有空间为止。
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        t0 = time.monotonic()
        with self._cond:
            while True:
                if self._closed:
                    return False
                if serial is not None and serial != self._serial:
                    return False
                if not self._is_full_locked(frame):
                    self._frames.append(frame)
                    self.stats.put_count += 1
                    self.stats.peak_frames = max(self.stats.peak_frames, len(self._frames))
                    self.stats.last_activity = time.monotonic()
                    self._cond.notify_all()
                    return True
                # 到这里说明满了
                self.stats.full_events += 1
                if self.overflow == "drop_oldest":
                    self._frames.popleft()
                    self.stats.dropped_by_producer += 1
                    continue
                if deadline is None:
                    self._cond.wait(0.05)
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._cond.wait(min(remaining, 0.05))
                self.stats.wait_put_seconds += time.monotonic() - t0
                t0 = time.monotonic()

    def _is_full_locked(self, frame=None) -> bool:
        if len(self._frames) >= self.max_frames:
            return True
        if self.max_duration and frame is not None:
            buffered = self._buffered_duration_locked()
            return buffered + max(0.0, getattr(frame, "duration", 0.0)) > self.max_duration
        return False

    # ---------------------------------------------------------------- 消费
    def get(self, timeout: float = 0.05):
        """取出一帧；超时或队列关闭返回 None。"""
        t0 = time.monotonic()
        with self._cond:
            while not self._frames and not self._closed:
                remaining = timeout - (time.monotonic() - t0)
                if remaining <= 0:
                    self.stats.starve_events += 1
                    return None
                self._cond.wait(min(remaining, 0.05))
            if not self._frames:
                self.stats.starve_events += 1
                return None
            frame = self._frames.popleft()
            self.stats.get_count += 1
            self.stats.last_activity = time.monotonic()
            self._cond.notify_all()  # 唤醒因满而阻塞的生产者
            return frame

    def get_nowait(self):
        return self.get(timeout=0.0)

    def drop_front(self) -> bool:
        """消费者侧主动丢掉队首（视频迟到丢帧用）。"""
        with self._cond:
            if not self._frames:
                return False
            self._frames.popleft()
            self.stats.dropped_by_consumer += 1
            self._cond.notify_all()
            return True

    # ---------------------------------------------------------------- 起播/恢复缓冲
    def wait_ready(self, min_frames: int = 1, min_duration: float = 0.0,
                   timeout: float = 2.0) -> bool:
        """等待缓存达到阈值。返回是否达标（False 表示超时或已关闭）。"""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                if self._closed:
                    return False
                if self._eos:
                    return True
                if len(self._frames) >= min_frames and self._buffered_duration_locked() >= min_duration:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, 0.02))

    def snapshot(self) -> dict:
        with self._cond:
            size = len(self._frames)
            buffered = self._buffered_duration_locked()
            head = getattr(self._frames[0], "pts", None) if self._frames else None
            tail = getattr(self._frames[-1], "pts", None) if self._frames else None
        st = self.stats
        return {
            "name": self.name,
            "size": size,
            "max_size": self.max_frames,
            "buffered_sec": buffered,
            "head_pts": head,
            "tail_pts": tail,
            "serial": self._serial,
            "eos": self._eos,
            "put": st.put_count,
            "get": st.get_count,
            "dropped_producer": st.dropped_by_producer,
            "dropped_consumer": st.dropped_by_consumer,
            "starve": st.starve_events,
            "peak": st.peak_frames,
        }
