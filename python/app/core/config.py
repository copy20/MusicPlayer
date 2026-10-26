"""运行时可调参数。

这些数字是多线程 + 缓存设计的调参入口，集中放这里方便做对比实验：
把 VIDEO_QUEUE_FRAMES 从 64 改成 2，就能直观看到起播变快、卡顿变多。
"""
from __future__ import annotations

import os

# ------------------------------------------------------------------ 缓存队列
VIDEO_QUEUE_FRAMES = 12           # 视频队列帧数上限（1920x1080 RGB24 约 6MB/帧，别贪）
VIDEO_QUEUE_MAX_DURATION = 1.0    # 视频队列缓存时长上限（秒）
AUDIO_QUEUE_FRAMES = 64           # 音频队列帧数上限
AUDIO_QUEUE_MAX_DURATION = 1.0    # 音频队列缓存时长上限（秒）

# ------------------------------------------------------------------ 起播/恢复
START_VIDEO_FRAMES = 3            # 起播前至少要缓存的视频帧数
START_AUDIO_SECONDS = 0.12        # 起播前至少要缓存的音频时长
RESTART_VIDEO_SECONDS = 0.05      # 卡顿恢复时需要的视频缓存
RESTART_AUDIO_SECONDS = 0.05

# ------------------------------------------------------------------ 同步
FRAME_DROP_THRESHOLD = 0.10       # 视频帧迟到超过该秒数且后面还有帧 -> 丢帧
STARVE_PAUSE_AFTER = 0.25         # 队列空持续这么久 -> 暂停时钟进入 Buffering
AV_SYNC_BIG_DIFF = 0.25           # 音视频偏差超过此值直接跳
AV_SYNC_FACTOR = 0.15             # 否则按比例软校正
# 声卡"出声期限"：设备能打开但这么久都没消耗过数据，就判定为不可用
# （无音频设备的服务器 / offscreen / 虚拟声卡会命中），自动切静音 + 画面主时钟
AUDIO_START_TIMEOUT = 1.5
RENDER_TICK = 0.005               # 渲染线程最小睡眠
MAX_FRAME_DELAY = 0.5             # 超过该延迟的一律认为是迟到（防止时钟异常卡死）

# ------------------------------------------------------------------ 变速
SPEED_PRESETS = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0)
MIN_SPEED = 0.25
MAX_SPEED = 4.0
ATEMPO_MIN = 0.5                  # ffmpeg atempo 单级下限
ATEMPO_MAX = 2.0                  # ffmpeg atempo 单级上限
OLA_FRAME_MS = 20.0               # CPU 兜底变速的帧长(ms)

# 音频变速策略：auto(首选 ffmpeg atempo) / native / ola / mute
AUDIO_SPEED_POLICY = "auto"

# ------------------------------------------------------------------ 合成演示源
SYNTHETIC_WIDTH = 640
SYNTHETIC_HEIGHT = 360
SYNTHETIC_FPS = 25.0
SYNTHETIC_DURATION = 300.0

# ------------------------------------------------------------------ ffmpeg 查找
def project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def ffmpeg_search_dirs() -> list:
    """ffmpeg.exe / ffprobe.exe 的候选目录（放这里即可被自动识别）。"""
    root = project_root()
    return [
        os.path.join(root, "tools", "ffmpeg", "bin"),
        os.path.join(root, "tools"),
        os.path.join(root, "bin"),
        root,
    ]
