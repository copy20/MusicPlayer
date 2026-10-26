"""线程层：把"读"与"演"彻底分开。

    ┌─────────────┐   raw frames   ┌──────────────┐  signal  ┌────────────┐
    │ 读线程(视频) │ ─────────────► │ 视频渲染线程  │ ───────► │ GUI 主线程  │
    └─────────────┘   FrameQueue   └──────────────┘  QImage  └────────────┘
    ┌─────────────┐   raw pcm      ┌──────────────┐  QtAudio ┌────────────┐
    │ 读线程(音频) │ ─────────────► │ 音频渲染线程  │ ───────► │ 声卡        │
    └─────────────┘   FrameQueue   └──────────────┘          └────────────┘

* 读线程只管"尽快把帧塞进队列"，不做任何时间相关判断；
* 渲染线程负责"按时钟取帧"，包括等待、丢帧、缓冲恢复；
* GUI 线程只负责画，绝不阻塞，也不做 IO。
"""
from app.core.workers.audio_renderer import AudioRenderThread
from app.core.workers.base import StoppableThread
from app.core.workers.reader import MediaReadThread
from app.core.workers.video_renderer import VideoRenderThread

__all__ = ["StoppableThread", "MediaReadThread", "VideoRenderThread", "AudioRenderThread"]
