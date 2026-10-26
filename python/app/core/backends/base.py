"""解码后端抽象层。

设计目标：**让上层（读线程）完全不知道底层是 ffmpeg 子进程、PyAV 还是合成源。**

接口刻意做得很薄：

    open(path) -> MediaInfo        拿元信息
    read_video() -> VideoFrame?    阻塞读一帧视频（在"视频读线程"里被调用）
    read_audio() -> AudioFrame?    阻塞读一段音频（在"音频读线程"里被调用）
    seek(t)                        跳到 t 秒（后端内部决定是重启进程还是局部 seek）
    apply_audio_speed(r, t)        变速：从 t 秒处以 r 倍速重建音频流（可选实现）

两个约定很重要：
1. read_video / read_audio **各自运行在独立线程**，后端必须保证线程安全
   （ffmpeg 后端靠"两个独立子进程"天然隔离；PyAV 后端靠内部 demux 线程 +
   两个 packet 队列来串行化解封装）；
2. 返回 None 表示 EOS（流结束）。
"""
from __future__ import annotations

import abc
import logging
from typing import Optional

from app.core.frames import AudioFrame, MediaInfo, VideoFrame


class DecodeError(Exception):
    """打开/解码失败。"""


class DecodeBackend(abc.ABC):
    # ---- 描述信息 ----
    name = "abstract"
    display_name = "抽象后端"
    supports_native_tempo = False   # 是否内建了 ffmpeg 的 atempo 这类高质量变速
    supports_seek = False

    def __init__(self):
        self.info: Optional[MediaInfo] = None
        self.log = logging.getLogger("backend." + self.name)

    # ---- 能力探测（用于自动选择后端） ----
    @classmethod
    def is_available(cls) -> bool:
        return False

    @classmethod
    def can_open(cls, path: str) -> bool:
        return cls.is_available()

    # ---- 生命周期 ----
    @abc.abstractmethod
    def open(self, path: str) -> MediaInfo:
        ...

    @abc.abstractmethod
    def read_video(self) -> Optional[VideoFrame]:
        """阻塞读一帧；返回 None 表示 EOS。运行在视频读线程。"""

    @abc.abstractmethod
    def read_audio(self) -> Optional[AudioFrame]:
        """阻塞读一段音频；返回 None 表示 EOS 或无音轨。运行在音频读线程。"""

    @abc.abstractmethod
    def close(self) -> None:
        ...

    def request_stop(self) -> None:
        """只"请求停止"，不释放资源。

        停止时必须先做这一步：读线程可能正阻塞在后端的内部队列上等 packet，
        而 close() 里紧跟着就会关掉容器 —— 如果读线程还在 decode 就被关，
        轻则报错、重则直接把进程 abort 掉。所以顺序必须是
        request_stop() -> 等读线程退出 -> close()。
        """
        return None

    # ---- 可选能力：变速 ----
    def apply_audio_speed(self, speed: float, resume_at: float) -> bool:
        """把音频流从 resume_at 处按 speed 倍速重建（atempo 方案专用）。

        返回 True 表示后端已处理好，输入输出 pts 依旧是"媒体时间"，
        上层不需要再在 CPU 侧做拉伸。
        """
        return False

    # ---- 可选能力：跳转 ----
    def seek(self, t: float) -> None:
        raise NotImplementedError("%s 不支持 seek" % self.name)

    # ---- 供上层查询当前音频流的倍速标签 ----
    @property
    def audio_speed(self) -> float:
        return 1.0
