"""媒体时钟。

变速播放的关键：媒体时钟不是"真实时间"，而是把真实时间按播放速度积分出来的
一个虚拟时间轴。做法是**分段线性重新锚定**：

    media_time(t) = anchor_media + (t - anchor_wall) * speed

任何会破坏连续性的事件（暂停/恢复/变速/seek/与外部时钟校正）都会在**保持当前
媒体时间不跳变**的前提下重新设置 anchor。这样：

* 变速：只改 slope，当前位置不变，之后自动以新速度前进 -> 视频天然跟着变快变慢；
* 暂停：把 slope 归零（记录 anchor_media 后停止累加）；
* seek：直接把 anchor_media 设为目标时间。

注意：时钟只回答"现在应该显示到几分几秒"，真正的帧来自缓存队列，
两者解耦。视频渲染线程用 ``clock.get_time()`` 与帧 pts 比较来决定
是等待、显示还是丢弃。
"""
from __future__ import annotations

import threading
import time
from typing import Optional

__all__ = ["MediaClock"]


class MediaClock:
    """线程安全的变速媒体时钟。"""

    def __init__(self, speed: float = 1.0, min_speed: float = 0.05, max_speed: float = 8.0):
        self._lock = threading.RLock()
        self._time = 0.0             # 最近一次 anchor 时的媒体时间
        self._wall = None            # 对应的墙上时间；None 表示暂停
        self._speed = 1.0
        self.min_speed = min_speed
        self.max_speed = max_speed
        self.set_speed(speed)

    # ---------------------------------------------------- 内部
    def _now(self) -> float:
        return time.monotonic()

    def _freeze(self) -> None:
        """把当前 Turn back into anchor（必须在持锁时调用）。"""
        if self._wall is not None:
            now = self._now()
            self._time += (now - self._wall) * self._speed
            self._wall = now

    # ---------------------------------------------------- 读
    def get_time(self) -> float:
        with self._lock:
            if self._wall is None:
                return self._time
            return self._time + (self._now() - self._wall) * self._speed

    def get_speed(self) -> float:
        with self._lock:
            return self._speed

    def is_paused(self) -> bool:
        with self._lock:
            return self._wall is None

    # ---------------------------------------------------- 写
    def set_speed(self, speed: float) -> float:
        """变速。已累积的时间不变，之后的推进速率变为 speed 倍。"""
        speed = max(self.min_speed, min(self.max_speed, float(speed)))
        with self._lock:
            self._freeze()
            self._speed = speed
        return speed

    def set_time(self, t: float) -> None:
        """强行修正媒体时间（seek / 重新同步）。"""
        with self._lock:
            self._time = float(t)
            self._wall = self._now() if self._wall is not None else None

    def pause(self) -> None:
        with self._lock:
            self._freeze()
            self._wall = None

    def resume(self) -> None:
        with self._lock:
            if self._wall is None:
                self._wall = self._now()

    def nudge(self, delta: float) -> None:
        with self._lock:
            self._freeze()
            self._time += delta

    # ---------------------------------------------------- 与外部同步
    def correct(self, external: float, big_diff: float = 0.25, factor: float = 0.2) -> bool:
        """用外部时钟（通常是音频硬件时钟）软校正。

        差值大于 big_diff 时直接跳（seek 后或严重卡顿），否则按比例微调，
        避免频繁大跳导致画面抖动。
        """
        with self._lock:
            diff = external - self.get_time()
            if abs(diff) < 1e-3:
                return False
            self._freeze()
            if abs(diff) > big_diff:
                self._time = external
            else:
                self._time += diff * factor
            return True

    # ---------------------------------------------------- 等待
    def wall_seconds_to(self, media_target: float) -> float:
        """距离媒体时间 media_target 还需要多少**真实**秒（考虑当前速度）。"""
        with self._lock:
            speed = self._speed if self._speed > 1e-6 else 1e-6
            return (media_target - self.get_time()) / speed

    def sleep_until(self, media_target: float, max_sleep: float = 0.05) -> None:
        delay = self.wall_seconds_to(media_target)
        if delay > 0:
            time.sleep(min(delay, max_sleep))


def format_time(seconds: Optional[float]) -> str:
    """把秒格式化为 mm:ss / h:mm:ss。"""
    if seconds is None or seconds < 0:
        seconds = 0.0
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%02d:%02d" % (m, s)
