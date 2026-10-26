// 所有"能调"的常量集中在这里，方便做参数实验
#pragma once

namespace cfg {

// ---------------- 缓存队列 ----------------
// 12 帧 1080p RGB24 ≈ 72MB，别贪；按帧数和时长双重限流
constexpr int VIDEO_QUEUE_FRAMES = 12;
constexpr double VIDEO_QUEUE_SECONDS = 1.0;
constexpr int AUDIO_QUEUE_FRAMES = 64;
constexpr double AUDIO_QUEUE_SECONDS = 0.60;

// ---------------- 起播 / 再缓冲 ----------------
constexpr double START_VIDEO_SECONDS = 0.08;   // 起播前至少攒这么多视频
constexpr double START_AUDIO_SECONDS = 0.12;
constexpr double RESTART_VIDEO_SECONDS = 0.05; // 卡顿后攒够这么多再恢复
constexpr double STARVE_PAUSE_AFTER = 0.60;    // 队列空这么久就停钟，别让画面空转

// ---------------- 渲染节拍 ----------------
constexpr int RENDER_TICK_MS = 4;
constexpr double MAX_FRAME_DELAY = 0.50;       // 帧比时钟超前这么多就先等等
constexpr double MAX_FRAME_LATE = 0.30;        // 比时钟落后这么多就直接丢
constexpr double CLOCK_STALL_HEAL = 1.0;       // 时钟卡住超过这么久就按队首帧重锚

// ---------------- 音画同步 ----------------
// 刻意设成"音频只能温和微调"：音频位置里混着设备缓冲、晚启动、时钟域漂移，
// 拿它做硬对齐会演成周期性回退（肉眼可见"倒着跳一下"）。
constexpr double AV_RELEARN_DIFF = 1.0;        // 偏差超过它就重新学偏移，而不是跳
constexpr double AV_SOFT_LIMIT = 0.12;         // 只有这么小的漂移才允许音频微调画面
constexpr double AV_SOFT_FACTOR = 0.05;        // 每次只追 5%，几乎看不出来
constexpr double AV_BIG_DIFF = 0.25;           // （保留：其它场合的大偏差阈值）
constexpr double AV_FACTOR = 0.15;
constexpr double AV_MAX_WAIT = 2.0;

// ---------------- 音频 ----------------
constexpr double AUDIO_START_TIMEOUT = 1.5;    // 声卡这么久没消耗数据就判定不可用
constexpr int AUDIO_SAMPLE_RATE = 48000;
constexpr int AUDIO_CHANNELS = 2;
constexpr double OLA_FRAME_MS = 20.0;

} // namespace cfg
