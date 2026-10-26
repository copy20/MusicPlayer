"""变速播放的两种实现：ffmpeg 的 atempo 滤镜链 + 纯 Python OLA 兜底。

方案一 atempo（首选）
-------------------
ffmpeg 的 ``atempo`` 做变调保护的时长缩放：速度变了，音调不变。
它单级只支持 0.5~2.0，超出范围需要**级联**，例如 4.0 = atempo=2.0,atempo=2.0；
0.25 = atempo=0.5,atempo=0.5。``build_atempo_chain`` 负责把任意倍速拆成若干级。

方案二 OLA（Overlap-Add，本模块实现）
-----------------------------------
在没有 ffmpeg 滤镜可用（PyAV 后端、合成源）时的兜底：在 CPU 侧做时域拉伸。

    输入侧：每次取 frame_units 个采样帧，乘 Hann 窗
    输出侧：每次向前 hop_out 个采样帧，与上一次的尾巴重叠相加
    hop_out = hop_in / speed   =>   输出时长变成原来的 speed 倍
    hop_in  = frame_units / 2  =>   50% 重叠，Hann 窗满足 COLA(恒等叠加)

由于 hop_out 是浮点，实现里用小数累加器保留余数，避免倍速不准导致的
音画不同步漂移。
"""
from __future__ import annotations

import math
from array import array
from typing import List

__all__ = ["OlaStretcher", "build_atempo_chain"]

_INT16_MAX = 32767.0
_INT16_MIN = -32768.0


def build_atempo_chain(speed: float, lo: float = 0.5, hi: float = 2.0) -> List[float]:
    """把任意倍速拆成若干级 atempo，每级落在 [lo, hi] 内。

    >>> build_atempo_chain(4.0)
    [2.0, 2.0]
    >>> build_atempo_chain(0.25)
    [0.5, 0.5]
    """
    if speed <= 0:
        raise ValueError("speed must be > 0")
    chain: List[float] = []
    remain = float(speed)
    if lo < remain < hi:
        return [remain]
    for _ in range(8):  # 最多 8 级，足够覆盖 0.5^8 ~ 2^8
        if remain >= hi:
            chain.append(hi)
            remain /= hi
        elif remain <= lo:
            chain.append(lo)
            remain /= lo
        else:
            chain.append(remain)
            remain = 1.0
            break
    # 最后残留的小偏差补一级，保证乘积尽量接近目标倍速
    if abs(remain - 1.0) > 1e-3:
        chain.append(max(lo, min(hi, remain)))
    return chain


def atempo_filter_string(speed: float) -> str:
    parts = ["atempo=%.4f" % v for v in build_atempo_chain(speed)]
    return ",".join(parts)


class OlaStretcher:
    """onn-time-domain 变速（s16le 交错，输入输出都是裸字节）。"""

    def __init__(self, channels: int = 2, sample_rate: int = 48000,
                 speed: float = 1.0, frame_ms: float = 20.0):
        self.channels = max(1, int(channels))
        self.sample_rate = int(sample_rate)
        self.frame_units = max(32, int(sample_rate * frame_ms / 1000.0))
        self.hop_in = max(1, self.frame_units // 2)     # 分析跳步（输入侧）
        self._unit_bytes = self.channels * 2            # 一个采样帧 = ch * int16
        self._win = self._hann(self.frame_units)
        self._buf = bytearray()
        self._pos = 0                                   # 已消费的 unit 数
        self._overlap = array("f", [0.0]) * (self.frame_units * self.channels)
        self._zero = array("f", [0.0]) * (self.frame_units * self.channels)
        self._hop_acc = 0.0
        self._speed = 1.0
        self.out_units = 0
        self.set_speed(speed)

    # ------------------------------------------------------------ 工具
    @staticmethod
    def _hann(n: int) -> List[float]:
        return [0.5 - 0.5 * math.cos(2.0 * math.pi * k / (n - 1)) for k in range(n)]

    # ------------------------------------------------------------ 配置
    def set_speed(self, speed: float) -> None:
        self._speed = max(0.05, min(8.0, float(speed)))
        self._hop_acc = 0.0

    @property
    def speed(self) -> float:
        return self._speed

    def reset(self) -> None:
        """清空内部缓冲（seek / 停止后必须调用，否则会串音）。"""
        self._buf = bytearray()
        self._pos = 0
        self._hop_acc = 0.0
        self._overlap = array("f", [0.0]) * (self.frame_units * self.channels)
        self.out_units = 0

    # ------------------------------------------------------------ 数据流
    def push(self, data: bytes) -> None:
        if data:
            self._buf.extend(data)

    def available_units(self) -> int:
        return len(self._buf) // self._unit_bytes - self._pos

    def _trim(self) -> None:
        if self._pos > self.frame_units * 4:
            cut = self._pos * self._unit_bytes
            del self._buf[:cut]
            self._pos = 0

    def _next_hop_out(self) -> int:
        self._hop_acc += self.hop_in / self._speed
        n = int(self._hop_acc)
        self._hop_acc -= n
        return max(1, n)

    def _process_one(self, out: array) -> None:
        ch = self.channels
        fu = self.frame_units
        start = self._pos * self._unit_bytes
        block = array("h")
        block.frombytes(bytes(self._buf[start:start + fu * self._unit_bytes]))

        hop_out = self._next_hop_out()
        gain = hop_out / float(self.hop_in)   # OLA 的幅度归一化系数
        ov = self._overlap

        for k in range(fu):
            w = self._win[k]
            b = k * ch
            for c in range(ch):
                ov[b + c] += block[b + c] * w

        n_out = min(hop_out, fu)
        i16 = array("h")
        for k in range(n_out):
            b = k * ch
            for c in range(ch):
                v = ov[b + c] * gain
                if v > _INT16_MAX:
                    v = _INT16_MAX
                elif v < _INT16_MIN:
                    v = _INT16_MIN
                i16.append(int(v))
        out.extend(i16)

        del ov[:n_out * ch]
        ov.extend(self._zero[:n_out * ch])
        self._pos += self.hop_in
        self.out_units += n_out

    def pull(self, n_bytes: int) -> bytes:
        """请求 n_bytes 字节的输出（会按内部 unit 对齐，可能略少于请求量）。"""
        want_units = max(1, n_bytes // self._unit_bytes)
        out = array("h")
        while len(out) // self.channels < want_units:
            if self.available_units() < self.frame_units:
                break
            self._process_one(out)
        self._trim()
        return out.tobytes()

    def flush(self) -> bytes:
        """输入结束时把残留重叠区输出完（尾部会淡出）。"""
        out = array("h")
        if self._pos == 0 and not any(self._overlap):
            return b""
        n = self.frame_units
        i16 = array("h")
        for k in range(n):
            b = k * self.channels
            fade = 1.0 - k / float(n)
            for c in range(self.channels):
                v = self._overlap[b + c] * fade
                if v > _INT16_MAX:
                    v = _INT16_MAX
                elif v < _INT16_MIN:
                    v = _INT16_MIN
                i16.append(int(v))
        out.extend(i16)
        self.reset()
        return out.tobytes()
