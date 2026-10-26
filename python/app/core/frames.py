"""帧与媒体信息的数据结构。

统一的约定让后端、队列、渲染线程之间解耦：

* 视频帧像素格式统一为 **RGB24**（打包，行优先），这样可以直接被 QImage 包起来，
  不需要额外转换层。
* 音频统一为 **s16le 交错**（16bit 有符号小端，2ch/48k），与 FFmpeg 的 `-f s16le`
  输出完全一致，因此 walk-through 到 QAudioOutput 时是零拷贝写入。
* 每个帧都带 pts（秒）/ duration（秒）/ serial（第几代时间线）。
  serial 用于把 seek 之前的"旧帧"识别出来丢掉。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ---- 统一格式常量（所有后端都必须产出这种格式） ----
VIDEO_PIXEL_FORMAT = "rgb24"
VIDEO_BYTES_PER_PIXEL = 3

AUDIO_SAMPLE_FORMAT = "s16"      # signed 16bit 交错
AUDIO_SAMPLE_RATE = 48000        # Hz
AUDIO_CHANNELS = 2
AUDIO_BYTES_PER_SAMPLE = 2
AUDIO_FRAME_BYTES = AUDIO_BYTES_PER_SAMPLE * AUDIO_CHANNELS  # 一个采样帧(unit)的字节数


@dataclass
class VideoFrame:
    """一帧视频。data 必须是 width*height*3 字节的 RGB24。"""

    pts: float
    duration: float
    width: int
    height: int
    data: bytes
    serial: int = 0

    @property
    def end(self) -> float:
        return self.pts + max(0.0, self.duration)

    @property
    def nbytes(self) -> int:
        return len(self.data)


@dataclass
class AudioFrame:
    """一段音频。data 为 s16le 交错的裸字节。"""

    pts: float
    duration: float
    sample_rate: int = AUDIO_SAMPLE_RATE
    channels: int = AUDIO_CHANNELS
    data: bytes = b""
    serial: int = 0

    @property
    def end(self) -> float:
        return self.pts + max(0.0, self.duration)

    @property
    def n_samples(self) -> int:
        """采样帧数（一个采样帧含 channels 个采样值）。"""
        return len(self.data) // (self.channels * AUDIO_BYTES_PER_SAMPLE)


@dataclass
class MediaInfo:
    """打开媒体后拿到的元信息。"""

    path: str
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 25.0
    has_video: bool = False
    has_audio: bool = False
    sample_rate: int = AUDIO_SAMPLE_RATE
    channels: int = AUDIO_CHANNELS
    backend: str = ""
    title: str = ""
    notes: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def aspect_ratio(self) -> float:
        if self.height <= 0:
            return 0.0
        return self.width / float(self.height)


EOS = "__EOS__"  # 队列/线程之间使用的流结束哨兵（不是帧对象）


def is_valid_video_frame(frame: Optional[VideoFrame]) -> bool:
    if frame is None:
        return False
    return len(frame.data) == frame.width * frame.height * VIDEO_BYTES_PER_PIXEL
