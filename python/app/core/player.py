"""播放器控制器 —— 把所有线程编排起来，并对外提供简洁的播放语义。

线程全景（一次播放会同时存在这些线程）::

    GUI 主线程       界面绘制、用户输入（绝不阻塞）
    read-video      后端的视频读线程：解码 -> video_queue
    read-audio      后端的音频读线程：解码 -> audio_queue
    render-video    按媒体时钟出帧 -> Signal(QImage) -> GUI
    render-audio    写声卡，并回报"听到的位置"用于音画同步
    ffmpeg 子进程   2 个（视频/音频各一个），由 FFmpegCLIBackend 管理

状态机::

    IDLE --open--> PAUSED --play--> (BUFFERING) --> PLAYING
                                        |
    PLAYING <--> PAUSED  <--> PLAYING  |
    PLAYING --seek/stop--> PAUSED/STOPPED
    PLAYING --EOS--> ENDED

三条设计原则
------------
1. **时钟决定何时显示、队列提供显示什么**：二者解耦，变速/暂停/seek 都只用动时钟。
2. **背压优先于丢帧**：队列满 -> 读线程阻塞 -> 管道满 -> ffmpeg 降速。
3. **以音频为主时钟**（可用时），画面服从听觉；没有音频时退回媒体时钟本身。
"""
from __future__ import annotations

import logging
import threading
import time
from enum import IntEnum

from app.core import config
from app.core.backends.base import DecodeError
from app.core.backends.factory import describe_backends, open_backend
from app.core.clock import MediaClock
from app.core.frame_queue import FrameQueue
from app.core.frames import MediaInfo
from app.core.workers.audio_renderer import AudioRenderThread
from app.core.workers.reader import MediaReadThread
from app.core.workers.video_renderer import VideoRenderThread
from app.qtcompat import QtCore, Signal

log = logging.getLogger("core.player")


class PlayerState(IntEnum):
    IDLE = 0
    PAUSED = 1
    PLAYING = 2
    BUFFERING = 3
    STOPPED = 4
    ENDED = 5


_AV_MAX_WAIT = 2.0      # 最多冻结画面等多久音频追上来，超时就硬对齐

STATE_TEXT = {
    PlayerState.IDLE: "就绪",
    PlayerState.PAUSED: "暂停",
    PlayerState.PLAYING: "播放中",
    PlayerState.BUFFERING: "缓冲中",
    PlayerState.STOPPED: "已停止",
    PlayerState.ENDED: "播放结束",
}


class PlayerController(QtCore.QObject):
    """播放器内核：一个状态机 + 若干线程 + 两个有界队列。"""

    # -------------------------------------------------------------- 信号（给 UI）
    state_changed = Signal(int)
    media_changed = Signal(object)          # MediaInfo
    position_changed = Signal(float, float)  # pos, duration
    buffering_changed = Signal(bool)
    speed_changed = Signal(float)
    volume_changed = Signal(float, bool)
    frame_ready = Signal(object)            # QImage
    playback_finished = Signal()
    error_occurred = Signal(str)
    stats_changed = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.clock = MediaClock()
        self._backend = None
        self.info: MediaInfo | None = None

        self.video_queue: FrameQueue | None = None
        self.audio_queue: FrameQueue | None = None

        self.v_reader: MediaReadThread | None = None
        self.a_reader: MediaReadThread | None = None
        self.v_render: VideoRenderThread | None = None
        self.a_render: AudioRenderThread | None = None

        self._play_event = threading.Event()
        self._state = PlayerState.IDLE
        self._speed = 1.0
        self._volume = 0.8
        self._muted = False
        self._loop = False
        self._buffering = False

        self._audio_enabled = False     # 媒体有音轨且已成功打开声卡
        self._audio_stretching = False  # 当前是否在 CPU 侧做时域拉伸
        self._last_reported_speed_tool = "atempo"

        self._video_done = True
        self._audio_done = True
        self._last_audio_pos = 0.0
        self._av_waiting = False        # 正在冻结画面等音频追上来
        self._av_wait_start = 0.0
        self._start_token = 0
        self._lock = threading.RLock()
        self._thread_snapshot: dict = {}

    # =================================================================== 属性
    @property
    def state(self) -> PlayerState:
        return self._state

    @property
    def state_text(self) -> str:
        return STATE_TEXT.get(self._state, "")

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def volume(self) -> float:
        return self._volume

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def loop_enabled(self) -> bool:
        return self._loop

    @property
    def has_media(self) -> bool:
        return self.info is not None

    @property
    def duration(self) -> float:
        return self.info.duration if self.info else 0.0

    def position(self) -> float:
        pos = self.clock.get_time()
        if self.duration > 0:
            return max(0.0, min(self.duration, pos))
        return max(0.0, pos)

    def is_playing(self) -> bool:
        return self._state in (PlayerState.PLAYING, PlayerState.BUFFERING)

    # =================================================================== 打开
    def open(self, path: str, autoplay: bool = True, prefer_backend: str | None = None) -> bool:
        """打开并（默认）开始播放。"""
        self.stop()

        try:
            backend = open_backend(path, prefer_backend)
        except DecodeError as exc:
            log.warning("打开失败: %s", exc)
            self.error_occurred.emit(str(exc))
            self._set_state(PlayerState.IDLE)
            return False

        self._backend = backend
        info = backend.info or MediaInfo(path=path)
        self.info = info
        self.clock.set_speed(1.0)
        self.clock.set_time(0.0)
        self.clock.pause()
        self._speed = 1.0
        self._audio_stretching = False

        # ---- 两条有界缓存队列：限速点是这里，而不是内存上限
        self.video_queue = FrameQueue(
            "video", max_frames=config.VIDEO_QUEUE_FRAMES,
            max_duration=config.VIDEO_QUEUE_MAX_DURATION, overflow="block")
        self.audio_queue = FrameQueue(
            "audio", max_frames=config.AUDIO_QUEUE_FRAMES,
            max_duration=config.AUDIO_QUEUE_MAX_DURATION, overflow="block")

        self._video_done = not info.has_video
        self._audio_done = not info.has_audio
        self._audio_enabled = info.has_audio

        # ---- 线程
        if info.has_video:
            self.v_reader = MediaReadThread("video", backend, self.video_queue, self)
            self.v_render = VideoRenderThread(self.clock, self.video_queue, self._play_event, self)
            self.v_render.frame_ready.connect(self.frame_ready)
            self.v_render.buffering.connect(self._on_buffering_flag)
            self.v_render.stream_done.connect(self._on_video_done)
            self.v_reader.start()
            self.v_render.start()

        if info.has_audio:
            self.a_reader = MediaReadThread("audio", backend, self.audio_queue, self)
            self.a_render = AudioRenderThread(
                self.audio_queue, self._play_event, volume=self._volume, parent=self)
            self.a_render.audio_clock.connect(self._on_audio_clock)
            self.a_render.buffering.connect(self._on_buffering_flag)
            self.a_render.unavailable.connect(self._on_audio_unavailable)
            self.a_render.stream_done.connect(self._on_audio_done)
            self.a_reader.start()
            self.a_render.start()
            # QtMultimedia 不可用时 AudioRenderThread 会立刻退出，这里补一次探测；
            # 另外给声卡一个"出声期限"：迟迟不消耗数据就判定不可用。
            QtCore.QTimer.singleShot(300, self._check_audio_thread)
            QtCore.QTimer.singleShot(int(config.AUDIO_START_TIMEOUT * 1000),
                                     self._check_audio_draining)

        self.media_changed.emit(info)
        self._set_state(PlayerState.PAUSED)
        log.info("已打开: %s [%s] %.1fs %dx%d@%.2f 音轨=%s",
                 info.title, info.backend, info.duration,
                 info.width, info.height, info.fps, info.has_audio)

        if autoplay:
            self.play()
        return True

    # =================================================================== 播放控制
    def play(self) -> None:
        if not self.info:
            return
        if self._state == PlayerState.PLAYING and self._play_event.is_set():
            return
        if self._state == PlayerState.ENDED or self.is_stopped_hard():
            # 播放结束后再按播放 -> 从头开始
            self.seek(0.0)
        self._set_state(PlayerState.BUFFERING)
        self.buffering_changed.emit(True)
        self._start_async()

    def pause(self) -> None:
        if not self.info:
            return
        self._start_token += 1      # 作废可能正在进行的起播流程
        self._play_event.clear()
        self.clock.pause()
        # 音频线程会 suspend 声卡（保留了缓冲，恢复时接着播）
        self._set_state(PlayerState.PAUSED)
        log.debug("暂停 @ %.2fs", self.position())

    def toggle_play(self) -> None:
        if self.is_playing():
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        """彻底停止：停线程、清队列、关后端。"""
        self._play_event.clear()
        self.clock.pause()
        self.clock.set_time(0.0)     # 停止 = 回到片头
        # 先让后端"解除阻塞"再收线程：读线程可能正卡在后端内部队列等 packet，
        # 不先置位停止标志的话它退出不了，最后会被 terminate() 强杀（进程 abort）。
        if self._backend is not None:
            try:
                self._backend.request_stop()
            except Exception as exc:
                log.debug("请求后端停止异常: %s", exc)
        self._teardown_threads()
        if self._backend is not None:
            try:
                self._backend.close()
            except Exception as exc:
                log.debug("关闭后端异常: %s", exc)
            self._backend = None
        self._audio_stretching = False
        self._av_waiting = False
        self._video_done = True
        self._audio_done = True
        self._set_state(PlayerState.STOPPED)
        self.frame_ready.emit(None)

    def is_stopped_hard(self) -> bool:
        return self._state in (PlayerState.STOPPED, PlayerState.IDLE) and not self._play_event.is_set()

    # ------------------------------------------------------------ 起播（异步预缓冲）
    def _start_async(self) -> None:
        """在后台线程里等待"起播缓冲"达标，避免卡住 UI。"""
        self._start_token += 1
        token = self._start_token

        def _preroll():
            ok = True
            if self.video_queue is not None:
                ok &= self.video_queue.wait_ready(config.START_VIDEO_FRAMES, 0.0, 3.0)
            if self._audio_enabled and self.audio_queue is not None:
                ok &= self.audio_queue.wait_ready(1, config.START_AUDIO_SECONDS, 3.0)
                # 声卡设备初始化常常比视频慢（几百毫秒到一两秒）。等它真的开吃
                # 再启动时钟，否则画面会先跑一段，随后再被音画校正拽回去。
                deadline = time.monotonic() + 3.0
                while (time.monotonic() < deadline and self.a_render is not None
                       and self.a_render.isRunning()):
                    if self.a_render.wait_started(0.05):
                        break
            if token != self._start_token:
                return  # 期间发生了 pause/stop/seek，这一次起播作废
            if self._state not in (PlayerState.BUFFERING, PlayerState.PLAYING):
                return
            if not ok:
                log.warning("起播缓冲超时，仍然尝试开始播放")
            start_pts = 0.0
            head = None
            if self.video_queue is not None:
                head = self.video_queue.peek()
            if head is None and self.audio_queue is not None:
                head = self.audio_queue.peek()
            if head is not None:
                start_pts = head.pts
            if abs(self.clock.get_time() - start_pts) > 0.3:
                self.clock.set_time(start_pts)
            self._play_event.set()
            self.clock.set_speed(self._speed)
            self.clock.resume()
            self._set_state(PlayerState.PLAYING)
            log.debug("起播 %.3fs, 倍速 %.2f", start_pts, self._speed)

        threading.Thread(target=_preroll, name="preroll", daemon=True).start()

    # =================================================================== seek
    def seek(self, t: float) -> None:
        if not self.info:
            return
        target = max(0.0, min(t, self.duration if self.duration > 0 else t))
        was_playing = self.is_playing() or self._play_event.is_set()
        # 播放结束后读/渲染线程都会退出（EOS 即结束）。这种状态下轻量 seek
        # 等不来任何新帧 —— 改走"重开到目标位置"，顺带把整条管线重新拉起。
        if self._pipeline_dead():
            self._restart_at(target)
            return
        self._start_token += 1           # 作废进行中的起播流程
        self._play_event.clear()
        self.clock.pause()

        for q in (self.video_queue, self.audio_queue):
            if q is not None:
                q.clear()
        if self.a_render is not None:
            self.a_render.flush()

        try:
            self._backend.seek(target)
        except NotImplementedError:
            log.info("后端不支持 seek，改为重新打开到 %.2fs", target)
            self._restart_at(target)
            return
        except Exception as exc:
            self.error_occurred.emit("跳转失败: %s" % exc)
            return

        # 重新打开 EOS 标志，让读线程继续填新位置的帧
        for q in (self.video_queue, self.audio_queue):
            if q is not None:
                q.reopen()
        self._av_waiting = False
        self._video_done = not (self.info and self.info.has_video)
        self._audio_done = not (self.info and self.info.has_audio and self._audio_enabled)
        self.clock.set_time(target)
        log.debug("seek(%.2f)", target)

        if was_playing:
            self._set_state(PlayerState.BUFFERING)
            self.buffering_changed.emit(True)
            self._start_async()

    def seek_relative(self, delta: float) -> None:
        self.seek(self.position() + delta)

    def _pipeline_dead(self) -> bool:
        """EOS 之后各线程会陆续退出（read_xxx 返回 None / 队列 eos 即结束）。

        只要有一个该在跑的线程不在了，轻量 seek 就等不来新帧，
        调用方应改走 `_restart_at`（整条管线重开）。
        """
        threads = [self.v_reader, self.a_reader, self.v_render, self.a_render]
        if self.info and self.info.has_video:
            if not (self.v_reader and self.v_reader.isRunning()):
                return True
        if self.info and self.info.has_audio and self._audio_enabled:
            if not (self.a_reader and self.a_reader.isRunning()):
                return True
        for th in threads:
            if th is not None and not th.isRunning():
                return True
        return False

    def _restart_at(self, t: float) -> None:
        """后端不支持内建 seek 时，用"重开"实现。"""
        path = self.info.path if self.info else ""
        prefer = self._backend.name if self._backend else None
        self.stop()
        if not self.open(path, autoplay=False, prefer_backend=prefer):
            return
        # 已经回到 0 点，直接把时钟推到目标位置（ffmpeg 后端其实是支持 seek 的，
        # 这条路径只给极端后端兜底）
        self._start_token += 1
        self._play_event.clear()
        for q in (self.video_queue, self.audio_queue):
            if q is not None:
                q.clear()
        if self._backend is not None:
            try:
                self._backend.seek(t)
            except Exception:
                pass
        for q in (self.video_queue, self.audio_queue):
            if q is not None:
                q.reopen()
        self.clock.set_time(t)
        self.play()

    # =================================================================== 变速
    def set_speed(self, speed: float) -> None:
        """变速播放。

        画面：只调整媒体时钟的斜率（README 有图），无需重启解码。
        声音：优先让后端用 ffmpeg 的 atempo 重新以新倍速解码；
              做不到时切换成 CPU 侧的 OLA 时域拉伸。
        """
        speed = max(config.MIN_SPEED, min(config.MAX_SPEED, float(speed)))
        if abs(speed - self._speed) < 1e-6:
            return
        self._speed = speed
        self.clock.set_speed(speed)     # 立即生效：此后时间流速 = speed 倍

        tool = "无音频"
        if self._audio_enabled and self.a_render is not None and self._backend is not None:
            policy = config.AUDIO_SPEED_POLICY
            want_native = (self._backend.supports_native_tempo and policy in ("auto", "native"))
            normalized = abs(speed - 1.0) < 1e-3

            if policy == "mute" and not normalized:
                self._audio_stretching = False
                self._restart_audio_speed(speed)
                tool = "静音(策略)"
            elif normalized:
                self._audio_stretching = False
                if want_native:
                    self._restart_audio_speed(1.0)
                    tool = "atempo(恢复1x)"
                else:
                    tool = "直通"
            elif want_native:
                if self._restart_audio_speed(speed):
                    self._audio_stretching = False
                    tool = "atempo(filter)"
                else:
                    self._audio_stretching = True
                    tool = "OLA(CPU)"
            else:
                self._audio_stretching = True
                tool = "OLA(CPU)"

            self.a_render.set_speed(speed)
            self.a_render.set_stretching(self._audio_stretching)
        self._last_reported_speed_tool = tool
        self.speed_changed.emit(speed)
        log.info("变速 %.2fx (%s)", speed, tool)

    def _restart_audio_speed(self, speed: float) -> bool:
        """让后端从"当前听到"的位置以新倍速重建音频流。"""
        if self._backend is None or self.audio_queue is None:
            return False
        resume_at = self._last_audio_pos or self.clock.get_time()
        ok = self._backend.apply_audio_speed(speed, resume_at)
        if ok:
            self.audio_queue.clear()      # generation +1：丢弃旧倍速产生的残帧
            self.audio_queue.reopen()
            self._audio_done = False
        return ok

    # =================================================================== 声音
    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, float(volume)))
        if self.a_render is not None:
            self.a_render.set_volume(self._volume)
        self.volume_changed.emit(self._volume, self._muted)

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        if self.a_render is not None:
            self.a_render.set_muted(self._muted)
        self.volume_changed.emit(self._volume, self._muted)

    def set_loop(self, enabled: bool) -> None:
        self._loop = bool(enabled)

    # =================================================================== 回调
    def _on_audio_clock(self, pos: float) -> None:
        """音频是主时钟：用它软校正媒体时钟。

        两种情况的处理方式刻意不同：
        * 小偏差 -> 按比例微调（`MediaClock.correct`，避免抖动）；
        * **音频落后太多**（声卡刚起步 / 设备卡顿）-> 冻结时钟等它追上来，
          而不是把画面倒回去（倒回去会看到明显的一次回退，体验更差）。
          等超过 `_AV_MAX_WAIT` 还追不上，才认命并硬对齐。
        """
        self._last_audio_pos = pos

        if self._av_waiting:
            if pos >= self.clock.get_time() - 0.05:
                self._av_waiting = False
                if self._play_event.is_set():
                    self.clock.resume()
                log.debug("音频已追上，恢复播放 @ %.3fs", pos)
            elif time.monotonic() - self._av_wait_start > _AV_MAX_WAIT:
                log.warning("等待音频超时(%.1fs)，强制对齐", _AV_MAX_WAIT)
                self._av_waiting = False
                self.clock.set_time(pos)
                if self._play_event.is_set():
                    self.clock.resume()
            return

        if self._play_event.is_set() and pos - self.clock.get_time() < -config.AV_SYNC_BIG_DIFF:
            log.debug("音频落后 %.3fs，冻结画面等待追赶", self.clock.get_time() - pos)
            self._av_waiting = True
            self._av_wait_start = time.monotonic()
            self.clock.pause()
            return

        self.clock.correct(pos, config.AV_SYNC_BIG_DIFF, config.AV_SYNC_FACTOR)

    def _on_audio_unavailable(self, reason: str) -> None:
        if not self._audio_enabled:
            return
        log.warning("音频输出不可用，切换为静音+画面主时钟: %s", reason)
        self._audio_enabled = False
        self._audio_done = True
        self._audio_stretching = False
        self._resume_if_waiting()      # 别让画面冻在"等音频"里
        # 顺序很重要：先关队列把阻塞在 put() 上的读线程唤醒，再 join 线程，
        # 否则 stop_and_wait() 会傻等满超时才去 terminate（白白卡住 UI 线程）
        if self.audio_queue is not None:
            self.audio_queue.close()
        if self.a_reader is not None:
            self.a_reader.stop_and_wait(1500)
            self.a_reader = None
        self.a_render = None

    def _check_audio_thread(self) -> None:
        """兜底：某些环境下 start() 不抛异常但线程立刻退出，
        这里统一判断并把系统切到静音模式。"""
        if self.a_render is not None and not self.a_render.isRunning() and self._audio_enabled:
            if self.info is not None and self.info.has_audio:
                self._on_audio_unavailable("音频渲染线程未能启动")

    def _check_audio_draining(self) -> None:
        """声卡"出声期限"检查。

        设备能打开、也允许 write，但底层从来不消耗数据的情况（无音频设备的环境），
        音频时钟永远不动 —— 继续以它为主时钟只会把画面冻住。所以到期没出声，
        就当这套环境没有声卡，切成"静音 + 画面主时钟"，保证画面照常播。
        真机环境下 Qt 几十毫秒内就会开始消耗，不会走到这里。
        """
        if not self._audio_enabled or self.a_render is None:
            return
        if self.a_render.draining.is_set():
            return
        self._on_audio_unavailable(
            "声卡在 %.1f 秒内没有消耗任何数据，判定为不可用" % config.AUDIO_START_TIMEOUT)

    def _resume_if_waiting(self) -> None:
        """解除"等音频追上来"造成的时钟冻结，避免播放卡死在暂停态。"""
        if self._av_waiting:
            self._av_waiting = False
        if self._play_event.is_set() and self.clock.is_paused():
            self.clock.resume()

    def _on_buffering_flag(self, flag: bool) -> None:
        if flag == self._buffering:
            return
        # 视频和音频任一侧缓冲不足都算在缓冲
        self._buffering = flag
        self.buffering_changed.emit(flag)

    def _on_video_done(self) -> None:
        self._video_done = True
        self._maybe_finish()

    def _on_audio_done(self) -> None:
        self._audio_done = True
        self._maybe_finish()

    def _maybe_finish(self) -> None:
        if not self.info or self.is_stopped_hard():
            return
        video_done = self._video_done or self.v_render is None
        audio_done = self._audio_done or self.a_render is None
        if not (video_done and audio_done):
            return
        if self._loop and self.duration > 0:
            log.info("循环播放 -> 回到起点")
            self.seek(0.0)
            return
        self._play_event.clear()
        self.clock.pause()
        self._set_state(PlayerState.ENDED)
        self.playback_finished.emit()
        log.info("播放结束 @ %.2fs", self.position())

    # =================================================================== 内部
    def _set_state(self, state: PlayerState) -> None:
        if self._state == state:
            return
        self._state = state
        self.state_changed.emit(int(state))

    def _teardown_threads(self) -> None:
        self._start_token += 1
        for thread in (self.v_render, self.a_render, self.v_reader, self.a_reader):
            if thread is not None:
                thread.request_stop()
        for q in (self.video_queue, self.audio_queue):
            if q is not None:
                q.close()          # 唤醒阻塞中的 put/get
        for thread in (self.v_render, self.a_render, self.v_reader, self.a_reader):
            if thread is not None:
                thread.stop_and_wait(2000)
        self.v_render = self.a_render = self.v_reader = self.a_reader = None
        for q in (self.video_queue, self.audio_queue):
            if q is not None and not q.is_closed:
                q.clear()

    # =================================================================== 监控
    def collect_stats(self) -> dict:
        """给界面用的运行时快照（线程状态 / 队列水位 / 丢帧 / 时钟偏差）。"""
        threads = {}
        for name, thread in (
            ("read-video", self.v_reader),
            ("read-audio", self.a_reader),
            ("render-video", self.v_render),
            ("render-audio", self.a_render),
        ):
            threads[name] = {
                "alive": bool(thread and thread.isRunning()),
                "name": thread.objectName() if thread else "-",
            }
        vq = self.video_queue.snapshot() if self.video_queue else {}
        aq = self.audio_queue.snapshot() if self.audio_queue else {}
        return {
            "state": self.state_text,
            "position": self.position(),
            "duration": self.duration,
            "speed": self._speed,
            "clock_time": self.clock.get_time(),
            "audio_pos": self._last_audio_pos,
            "av_diff": round(self._last_audio_pos - self.clock.get_time(), 4)
            if self._audio_enabled else None,
            "sync_master": "audio" if self._audio_enabled else "clock",
            "speed_tool": self._last_reported_speed_tool,
            "video_queue": vq,
            "audio_queue": aq,
            "threads": threads,
            "backend": self.info.backend if self.info else "-",
            "buffering": self._buffering,
            "audio_enabled": self._audio_enabled,
            "stretching": self._audio_stretching,
        }

    def available_backends(self) -> list:
        return describe_backends()
