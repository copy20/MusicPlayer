"""内置合成演示源。

存在的意义：**在没有 ffmpeg / PyAV 的机器上，播放器依然是一个"能跑的真东西"**。

它程序化生成 RGB24 视频帧和 s16le 音频，接口完全遵循 DecodeBackend，
所以上面所有讨论过的机制（两个读线程、有界帧队列、媒体时钟、变速、丢帧、
两级背压）在它身上一样会触发。换句话说：用它也能验证多线程设计，
只是解码器换成了 Python 函数。

打开方式：open("synthetic://demo")  —— 见 app/core/backends/factory.py
"""
from __future__ import annotations

import logging
import math
import threading
from array import array
from typing import Optional

from app.core import config
from app.core.backends.base import DecodeBackend, DecodeError
from app.core.frames import (
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE,
    VIDEO_BYTES_PER_PIXEL,
    AudioFrame,
    MediaInfo,
    VideoFrame,
)

log = logging.getLogger("backend.synthetic")

SYNTHETIC_SCHEME = "synthetic://"


class SyntheticBackend(DecodeBackend):
    name = "synthetic"
    display_name = "内置合成演示源"
    supports_seek = True
    supports_native_tempo = False      # 变速走 CPU 侧 OLA 兜底

    def __init__(self, width: int = 0, height: int = 0, fps: float = 0.0, duration: float = 0.0):
        super().__init__()
        self._w = width or config.SYNTHETIC_WIDTH
        self._h = height or config.SYNTHETIC_HEIGHT
        self._fps = fps or config.SYNTHETIC_FPS
        self._duration = duration or config.SYNTHETIC_DURATION
        self._frame_duration = 1.0 / self._fps
        self._audio_units = 2048           # 每次产出的采样帧数（约 42ms）
        self._v_pts = 0.0
        self._a_pts = 0.0
        self._vlock = threading.Lock()
        self._alock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------ 能力
    @classmethod
    def is_available(cls) -> bool:
        return True

    @classmethod
    def can_open(cls, path: str) -> bool:
        return (not path) or path.startswith(SYNTHETIC_SCHEME) or path == "demo"

    # ------------------------------------------------------------ 打开
    def open(self, path: str = SYNTHETIC_SCHEME + "demo") -> MediaInfo:
        self._v_pts = 0.0
        self._a_pts = 0.0
        self._closed = False
        info = MediaInfo(
            path=path or (SYNTHETIC_SCHEME + "demo"),
            duration=self._duration,
            width=self._w, height=self._h, fps=self._fps,
            has_video=True, has_audio=True,
            sample_rate=AUDIO_SAMPLE_RATE, channels=AUDIO_CHANNELS,
            backend=self.name, title="合成演示源",
            notes="程序化生成画面与音频，用于在没有 ffmpeg 时演示播放管线",
        )
        self.info = info
        return info

    # ------------------------------------------------------------ 读帧
    def read_video(self) -> Optional[VideoFrame]:
        if self._closed:
            return None
        with self._vlock:
            if self._v_pts >= self._duration - 1e-6:
                return None
            pts = self._v_pts
            self._v_pts += self._frame_duration
        data = self._render_frame(pts)
        return VideoFrame(pts=pts, duration=self._frame_duration,
                          width=self._w, height=self._h, data=data)

    def read_audio(self) -> Optional[AudioFrame]:
        if self._closed:
            return None
        with self._alock:
            if self._a_pts >= self._duration - 1e-6:
                return None
            pts = self._a_pts
            units = self._audio_units
            self._a_pts += units / float(AUDIO_SAMPLE_RATE)
        data = self._render_audio(pts, units)
        return AudioFrame(pts=pts, duration=units / float(AUDIO_SAMPLE_RATE),
                          data=data, sample_rate=AUDIO_SAMPLE_RATE, channels=AUDIO_CHANNELS)

    # ------------------------------------------------------------ 生成器
    def _render_frame(self, t: float) -> bytes:
        w, h = self._w, self._h
        phase = t * 0.9
        rows = []
        for y in range(h):
            a = y / max(1.0, (h - 1))
            r = 128 + 96 * math.sin(phase + 2.0 * math.pi * a)
            g = 128 + 96 * math.sin(phase + 2.0 * math.pi * a + 2.094)
            b = 128 + 96 * math.sin(phase + 2.0 * math.pi * a + 4.188)
            rows.append(bytes((_clamp255(r), _clamp255(g), _clamp255(b))) * w)

        # 弹跳方块：方便肉眼判断是否真的在播放/变速
        bw, bh = max(8, w // 8), max(8, h // 8)
        bx = int((0.5 + 0.5 * math.sin(t * 1.7)) * (w - bw))
        by = int((0.5 + 0.5 * math.cos(t * 1.1)) * (h - bh))
        box = bytes((250, 236, 90))
        # 行缓冲是字节串，切片下标必须是"字节偏移"= 像素坐标 × 3
        head, tail = bx * 3, (bx + bw) * 3
        for y in range(by, by + bh):
            rows[y] = rows[y][:head] + box * bw + rows[y][tail:]

        # 底部进度条
        frac = (t % self._duration) / max(1e-6, self._duration)
        pw = int(w * frac)
        for y in range(max(0, h - 8), h):
            rows[y] = bytes((48, 200, 130)) * pw + bytes((26, 28, 34)) * (w - pw)

        return b"".join(rows)

    def _render_audio(self, t: float, units: int) -> bytes:
        sr = AUDIO_SAMPLE_RATE
        out = array("h")
        # 用相位积分保证拼接处连续：phase = 2π * ∫f(t)dt
        # f(t) = 220 + 110*sin(0.5t)  =>  ∫f = 220t + 220*(1 - cos(0.5t))
        for i in range(units):
            tt = t + i / float(sr)
            ph = 2.0 * math.pi * (220.0 * tt + 220.0 * (1.0 - math.cos(0.5 * tt)))
            env = 0.45 + 0.35 * math.exp(-8.0 * (tt % 1.0))   # 每秒一个轻微重音
            v = 0.32 * env * math.sin(ph)
            iv = int(max(-1.0, min(1.0, v)) * 32000)
            out.append(iv)
            if AUDIO_CHANNELS >= 2:
                vr = 0.32 * env * math.sin(ph + 0.6)          # 立体声：右声道相位偏移
                out.append(int(max(-1.0, min(1.0, vr)) * 32000))
        return out.tobytes()

    # ------------------------------------------------------------ seek / close
    def seek(self, t: float) -> None:
        t = max(0.0, min(self._duration, float(t)))
        with self._vlock:
            self._v_pts = math.floor(t * self._fps) / self._fps   # 对齐到帧边界
        with self._alock:
            self._a_pts = t

    def close(self) -> None:
        self._closed = True
        log.debug("SyntheticBackend closed")


def _clamp255(v: float) -> int:
    iv = int(v)
    return 0 if iv < 0 else (255 if iv > 255 else iv)
