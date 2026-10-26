"""
MiniPlayer - 一个以 FFmpeg 为底层的多线程视频播放器（Qt 界面）。

模块划分
--------
app/qtcompat.py            Qt 绑定兼容层（PyQt5 / PySide6 / PyQt6 自动选择）
app/core/frames.py         帧与媒体信息的数据结构
app/core/clock.py          媒体时钟（变速、暂停、seek 的核心是它的分段线性映射）
app/core/frame_queue.py    有界缓存队列（背压 / 丢弃策略 / generation 机制）
app/core/dsp/              纯 Python 信号处理（变速兜底用的 OLA 时域拉伸）
app/core/backends/         解码后端：ffmpeg 子进程 / PyAV / 内置合成源
app/core/workers/          线程：读线程（视频/音频）、渲染线程（视频/音频）
app/core/player.py         播放器状态机，负责编排以上所有线程
app/ui/                    界面
"""
__version__ = "1.0.0"
__all__ = ["__version__"]
