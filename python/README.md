# MiniPlayer —— 多线程视频播放器（FFmpeg + Qt）

一个以 **FFmpeg 为解码底层**、**Qt 绘制界面** 的视频播放器，支持播放 / 暂停 / 停止 /
快进快退 / 任意倍速播放 / 循环 / 音量 / 拖拽 / 全屏。

项目的重点不在于"按钮长得像不像"，而在于这三件事：**多线程设计、缓存队列设计、变速播放**。
代码里每一处相关决定都写了"为什么"。

---

## 1. 快速开始

```powershell
# 1) 安装 Qt 绑定（必须）
C:\Users\qw\AppData\Local\Programs\Python\Python312\python.exe -m pip install PyQt5

# 2) 运行（先用内置合成演示源，不需要任何媒体文件）
python main.py synthetic://demo

# 3) 也可以直接带一个视频文件
python main.py D:\videos\test.mp4
```

界面打开后：`文件 -> 打开合成演示源`，或直接把视频文件拖进窗口。

> 快捷键：`空格` 播放/暂停 · `S` 停止 · `←/→` ±5 秒 · `↑/↓` 音量 ·
> `Ctrl+↑/↓` 变速 · `M` 静音 · `F` 全屏 · `Ctrl+I` 监控面板 · `Ctrl+L` 循环

### 关于解码后端

程序不是"看谁存在就挑谁"，而是**逐个候选后端真的打开一次，按实测能力打分**
（`app/core/backends/factory.py`）：

| 后端 | 条件 | 能力 |
| --- | --- | --- |
| `FFmpeg (子进程)` | 能找到 `ffmpeg.exe` | 真实文件解码、**atempo 保音变速** |
| `PyAV (libav)` | `python -m pip install av`（推荐，自带完整 FFmpeg 8.0） | 真实文件解码，变速走 CPU 侧 OLA |
| `内置合成演示源` | 永远可用，但只服务 `synthetic://` 这种伪协议 | 程序化生成画面+音频，用于演示整条管线 |

每个候选在 open() 时都会**实测**：能不能解出视频帧、能不能解出音频块、有没有
atempo 滤镜（见 `FFmpegCLIBackend._verify_decode_capability`）。所以像
`--disable-everything` 编出来的极简 `ffmpeg.exe`（没有音频解码器、连 `-f rawvideo`
输出都被裁掉）会被识别出来并自动让位给 PyAV，而不是"看起来在播，其实是黑屏"。

想启用 FFmpeg 后端，把**完整版** `ffmpeg.exe`(+可选 `ffprobe.exe`) 放到下面任一位置
即可，**无需改代码**：

```
python/tools/ffmpeg/bin/ffmpeg.exe      # 推荐
python/tools/ffmpeg.exe
加入 PATH
设置环境变量 FFMPEG=D:\path\to\ffmpeg.exe
```

菜单 `帮助 -> 关于 / 后端信息` 可以随时看到当前后端状态和探测结果。

### 没有素材也能试真解码

`tools/make_sample_clip.py` 会用 PyAV 自带的编码器现生成一段测试视频
（H.264 + AAC / MP4：移动色块 + 进度条 + 每秒一声"嘀"），用来验证真实解码链路：

```powershell
python tools/make_sample_clip.py assets/sample.mp4 20 640 360
python main.py assets/sample.mp4
```

---

## 2. 架构总览

```
                        ┌────────────────────── GUI 主线程 ──────────────────────┐
                        │  菜单/按钮/进度条/监控面板   VideoWidget::paintEvent     │
                        └──────▲──────────────────────────────────│──────────────┘
                   跨线程信号 │ frame_ready(QImage)         调用 │ play/seek/set_speed
                               │                                 ▼
   ┌───────────────────────────┴──────────────────────────────────────────────┐
   │                          PlayerController（状态机）                        │
   │           MediaClock   +   两个有界 FrameQueue   +   线程编排              │
   └───┬────────────┬──────────────────────┬──────────────┬───────────────────┘
       │            │                      │              │
   read-video   read-audio           render-video    render-audio
   (QThread)    (QThread)             (QThread)       (QThread)
       │            │                      │              │
       ▼            ▼                      ▲              ▲
  ┌────────┐  ┌──────────┐          ┌──────────┐   ┌──────────┐
  │video_q │  │ audio_q  │  ──────► │ video_q  │   │ audio_q  │
  │ 12 frames│ │ 64 frames│          └──────────┘   └──────────┘
  └────────┘  └──────────┘                 │              │
       ▲            ▲                      │              ▼
       │            │                  QImage 投递      QAudioSink → 声卡
   ┌───┴────────────┴───┐
   │   DecodeBackend     │  = ffmpeg 子进程×2  /  PyAV(demux线程+packet队列)  /  合成源
   └────────────────────┘
```

### 2.1 多线程设计

**为什么要切成这些线程？**

| 线程 | 代码 | 职责 | 不能做什么 |
| --- | --- | --- | --- |
| GUI 主线程 | `app/ui/*` | 画界面、收输入 | 不做 IO、不 sleep、不解帧 |
| `read-video` | `workers/reader.py` | 循环 `backend.read_video()` → 塞进队列 | 不做任何时间判断 |
| `read-audio` | 同上 | 同上（音频独立，互不阻塞） | 同上 |
| `render-video` | `workers/video_renderer.py` | 按媒体时钟出帧、迟到丢帧、缓冲恢复 | 不碰 QWidget（`QPainter` 只能在主线程） |
| `render-audio` | `workers/audio_renderer.py` | 写声卡、回报音频时钟（主时钟） | 设备操作只能在本线程 |
| `preroll` | `player._start_async` | 异步等待起播缓冲，避免卡 UI | —— |
| ffmpeg 子进程 ×2 | `backends/ffmpeg_cli.py` | 真正的解封装+解码（视频/音频各一个进程） | —— |

几个刻意的取舍：

1. **读线程只管"读"，不碰时间**。如果让它去判断"这一帧该不该显示"，
   解码速度就和显示节奏耦合在一起了，暂停/变速会变成跨线程状态同步的地狱。
   现在的分工是：**队列负责"有什么"，时钟负责"什么时候"，渲染线程负责"执刑"**。
2. **音频输出用推模式（显式 write）而不是 QIODevice 拉模式**，
   这样可以在自己的循环里同时做变速拉伸、暂停 suspend、卡顿统计，逻辑集中可读。
3. **PyAV 后端内部多一条 demux 线程**：`AVFormatContext` 不允许多线程并发访问，
   所以后端自己用 "demux 线程 + 两条有界 packet 队列" 对外伪装成线程安全的
   `read_video()/read_audio()` —— 上层完全无感（`backends/pyav_backend.py`）。
   **连 seek 也必须做成请求投递给这个线程执行**（`_seek_request` 队列）：
   如果外部线程一边 seek、demux 线程一边 `demux()`，轻则乱序，重则解封装直接报错挂掉
   ——现象就是"往中间跳转之后再也出不了帧"。
4. **所有等待都可中断**：`StoppableThread.sleep()` 内部是 `Event.wait()`，
   停止请求能立即生效，而不是傻等到超时。

### 2.2 两层背压（防止内存被解码器吃光）

```
        队列满 → put() 阻塞 → 读线程停 → 管道缓冲区写满 → ffmpeg 写阻塞 → ffmpeg 自然降速
```

这意味着**队列的容量就是整个播放管线的限速器**，不需要额外的流控协议。
把 `config.VIDEO_QUEUE_FRAMES` 从 12 调大到 100，你会立刻看到内存曲线变陡。

---

## 3. 缓存队列设计（`app/core/frame_queue.py`）

没有直接用 `queue.Queue`，因为它缺三样东西：**peek()、按时长限流、generation 失效**。

| 能力 | 说明 |
| --- | --- |
| 帧数上限 `max_frames` | 视频 12 帧（1080p RGB24 约 72MB 上限），音频 64 块 |
| 时长上限 `max_duration` | 视频 1.0s / 音频 1.0s；音频天然适合按时长限流 |
| `overflow` 策略 | `block`（默认，背压） / `drop_oldest`（实时性优先） |
| `serial`（generation） | `clear()` 时自增；旧 generation 的帧 `put()` 直接返回 False → **杜绝 seek 后的"脏帧"** |
| `peek(i)` | 渲染线程需要先看下一帧的 pts 才能决定"丢还是等" |
| `wait_ready()` | 起播/恢复播放前的缓冲等待（异步执行，不卡 UI） |
| 统计 `QueueStats` | 写入/取出/生产者丢帧/迟到丢帧/空队列次数/峰值，全部显示在监控面板 |

界面右侧的"运行时监控"面板实时显示以上所有指标，**做性能实验时主要就是看它**：
卡顿变多是 `starve` 涨还是 `late_dropped` 涨，一眼就能区分是"解码慢"还是"渲染慢"。

---

## 4. 变速播放设计

### 4.1 画面侧：改时钟斜率就够了

媒体时钟不是简单粗暴地把 pts 除以倍速，而是采用 **分段线性重新锚定**：

```
media_time(t) = anchor_media + (t - anchor_wall) × speed
```

```
 媒体时间
   │                         speed=2
   │                    ／
   │              ／
   │        ／  speed=1
   │   ／
   │ ───────────────────────────► 真实时间
        seek=set_time()   变速=改斜率（当前值不跳变）
```

于是：

- 变速 → 只改 `speed`，**当前位置不跳变**，之后自动以新速率前进；
- 暂停 → 把已流逝时间累加到 anchor 后停止累加（`_freeze()`）；
- 跳转 → 直接 `set_time(t)`；
- 帧照常从队列里出，`pts` 是原来的一套时间轴，天然支持任意倍速，**不需要重启解码**。

帧速率不够时也不会越播越慢：渲染线程发现当前帧迟到超过 `FRAME_DROP_THRESHOLD`(0.1s)
且后面还有帧，就丢弃它追赶上去（这是 ffplay 的做法，比在解码侧丢更划算）。

### 4.2 声音侧：三种策略自动切换

变速最麻烦的是声音——简单地改变播放采样率会让音调跑偏。本项目提供三条路径：

| 策略 | 条件 | 质量 | 实现 |
| --- | --- | --- | --- |
| **atempo（首选）** | FFmpeg 后端 | 保音调，最好 | 从当前位置重启音频子进程：`ffmpeg -ss T -af atempo=R …` |
| **OLA（兜底）** | 无 ffmpeg（PyAV / 合成源） | 可接受 | 纯 Python 时域拉伸 `dsp/time_stretch.py` |
| **mute** | `config.AUDIO_SPEED_POLICY = "mute"` | 静音 | 变速时关闭音频，1x 自动恢复 |

`atempo` 单级只支持 0.5~2.0，所以要实现 4x 就得**级联**：
`build_atempo_chain(4.0) -> [2.0, 2.0]`，`0.25 -> [0.5, 0.5]`（见单测 `test_core.py`）。

OLA 的原理：

```
输入：每次取 frame_units 个采样帧，乘 Hann 窗
输出：每次向前 hop_out 个采样帧，与上次的尾巴重叠相加
      hop_out = hop_in / speed   ⇒   输出时长变为原来的 speed 倍
      hop_in  = frame_units / 2  ⇒   50% 重叠，Hann 窗满足恒等叠加(COLA)
```

`hop_out` 必然是小数，实现里用小数累加器保留余数，避免长期倍速漂移。
单测验证了：0.5x/1x/2x 的输出长度比例误差 < 6%，1x 时输出与输入的相关误差 < 15%。

### 4.3 变速时如何保证不错位

改变倍速时 `_restart_audio_speed()` 会让音频流从"**当前听到的位置**"重建，
而这一刻的画面位置由媒体时钟给出 → 音频队列被清空并 generation+1 →
旧倍速的残帧立刻失效。整个过程在一个 100ms 级别的重启动作里完成。

---

## 5. 音画同步

以**音频为主时钟**（听觉比视觉敏感得多）：

```
audio_pos = expected_pts(已写入最后一个采样对应的媒体时间) − 设备缓冲秒数 × speed
clock.correct(audio_pos, big_diff=0.25, factor=0.15)
```

小偏差按比例软校正（避免频繁跳变造成抖动）；> 0.25s 才硬跳（seek / 严重卡顿之后）。

有一条例外值得单独说：**音频落后太多时，宁可冻结画面等它追上来，也不把画面倒回去**
（`PlayerController._on_audio_clock`）。倒回去会有肉眼可见的一次"回退"，比短暂定帧更难看；
等超过 2 秒还追不上才认命并硬对齐。

### 没有声卡也能播

无声卡 / 虚拟设备的服务器、以及 `offscreen` 环境里，Qt 会把设备"打开成功"、
也允许你 write，但底层从来不消耗数据 —— 这时音频时钟是死的，继续拿它当主时钟
只会把画面冻住。所以给声卡设了**出声期限**（`AUDIO_START_TIMEOUT = 1.5s`）：
到期 `processedUSecs` 没变过就判定为不可用，自动切换成"静音 + 画面主时钟"，
画面照常播放（状态栏的 `同步主时钟` 会从 `audio` 变成 `clock`，肉眼可见这个过程）。

暂停同样有讲究：音频侧 `suspend()` 声卡（保留缓冲），恢复时接着播，
不会出现"暂停后放炮音"或"丢一小段"。

---

## 6. 文件清单

```
python/
├── main.py                     入口（可带文件路径参数）
├── requirements.txt
├── app/
│   ├── qtcompat.py             Qt 绑定兼容层：PyQt5 / PySide6 / PyQt6 自动选择
│   ├── core/
│   │   ├── clock.py            变速媒体时钟（分段线性映射）
│   │   ├── frame_queue.py      有界缓存队列（背压 / 丢帧 / generation / 统计）
│   │   ├── frames.py           帧结构 + 统一格式约定（RGB24 / s16le 48k 立体声）
│   │   ├── config.py           所有可调参数集中在这里
│   │   ├── player.py           播放器状态机 + 线程编排
│   │   ├── dsp/
│   │   │   └── time_stretch.py atempo 级联 + 纯 Python OLA 时域拉伸
│   │   ├── backends/
│   │   │   ├── base.py         DecodeBackend 抽象接口
│   │   │   ├── ffmpeg_cli.py   FFmpeg 子进程后端（真变速）
│   │   │   ├── pyav_backend.py PyAV 后端（自带 demux 线程 + packet 队列）
│   │   │   ├── synthetic.py    内置合成演示源
│   │   │   └── factory.py      后端自动选择
│   │   └── workers/
│   │       ├── base.py         可中断停止的线程基类
│   │       ├── reader.py       读线程（视频/音频共用）
│   │       ├── video_renderer.py  按时钟出帧 + 迟到丢帧 + 缓冲恢复
│   │       └── audio_renderer.py  声卡写入 + 音频主时钟
│   └── ui/
│       ├── main_window.py      菜单 / 控制条 / 状态栏 / 监控面板
│       ├── video_widget.py     画面绘制（QImage + letterbox）
│       └── styles.py           深色主题
├── tools/
│   ├── make_sample_clip.py     用 PyAV 生成 H.264+AAC 测试视频
│   └── ffmpeg/bin/             把完整版 ffmpeg.exe 放这里就会被自动识别
└── tests/
    ├── test_core.py            29 项内核单测（时钟/队列/变速/后端/线程联跑）
    ├── test_ui_smoke.py        22 项离屏冒烟测试（合成源全流程）
    └── test_real_media.py      18 项真实文件测试（PyAV 解码 + seek/变速精度）
```

---

## 7. 调参实验建议

全部集中在 `app/core/config.py`：

| 参数 | 改小会怎样 | 改大会怎样 |
| --- | --- | --- |
| `VIDEO_QUEUE_FRAMES` | 起播更快、内存更小，但更容易卡顿（`starve` 上升） | 抗抖动强，延迟和内存变大 |
| `FRAME_DROP_THRESHOLD` | 更积极丢帧追进度（画面更"跳"但更跟节奏） | 更保守，卡顿时延越拖越多 |
| `AV_SYNC_BIG_DIFF` / `AV_SYNC_FACTOR` | 音画校正更激进（可能有肉眼抖） | 更平滑，但偏差修正慢 |
| `AUDIO_SPEED_POLICY` | `mute` 可换取变速时的 CPU | `auto` 优先用 ffmpeg atempo |
| `AUDIO_START_TIMEOUT` | 更快判定"没声卡"并降级（启动更快） | 慢设备也留机会，代价是起播等更久 |
| `OLA_FRAME_MS` | 拉伸更细腻、CPU 更高 | 更省 CPU，瞬态可能出现轻微回声/金属感 |

---

## 8. 自测

```powershell
python -m tests.test_core          # 内核单测：29 项（时钟/队列/OLA/后端/多线程联跑）
python -m tests.test_ui_smoke      # 离屏冒烟：22 项（合成源：出帧/计时/暂停/变速/seek/停止/线程回收）
python -m tests.test_real_media    # 真实文件：PyAV 解码 assets/sample.mp4 的全流程（18 项）
python main.py --debug             # 运行时看每个线程/队列的 DEBUG 日志
```

实测倍速精度（媒体时间推进速率）：`1x -> 1.00`、`2x -> 2.00`、`0.5x -> 0.50`，
seek 落点精确到第 0 帧（目标 6/12/17 秒 -> 首帧 pts 6.000/12.000/17.000）。

当前环境已验证：Python 3.12.7 + PyQt5 5.15.2 / Qt 5.15.2，两套测试全绿。

---

## 9. 已知限制 / 可以接着做的事

- FFmpeg 后端的 **seek 与变速是"重启子进程"实现的**，代价约 100ms；
  追求极致可以改成"一个常驻进程 + `avformat_seek_file` + 动态 atempo graph"（即用 PyAV 或 C 扩展）。
- 视频画布用 `QImage + QPainter`（CPU 绘制），1080p 60fps 在高分屏上会比较费 CPU；
  可升级为 `QOpenGLWidget` 上传纹理。
- 目前没有字幕轨 / 多轨类型选择 / 硬件解码（`-hwaccel`）入口，接口已留好，
  在 `FFmpegCLIBackend._video_cmd()` 里加参数即可。
- 音频重采样由 ffmpeg / PyAV 负责，播放器内部假设统一 48k/16bit 立体声。
