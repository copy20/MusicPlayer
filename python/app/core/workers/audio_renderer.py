"""音频渲染线程 —— 声卡写入 + 主时钟来源。

为什么让音频当"主时钟"
----------------------
声卡播放是最连续、最能反映真实进度的量；画面跟着它走才不会跑偏。
所以本线程除了写 PCM，还要不断地报告"此刻听到的位置"，供上层校正媒体时钟：

    audible_pos = expected_pts(已写入的最后一个采样对应的媒体时间)
                  - 设备缓冲秒数 * speed

（注意乘 speed：缓冲的时间是墙上时间，换算回媒体时间要按倍速缩放。）

变速在这里有两种落地方式，由上层通过 set_stretching() 决定：

* 后端支持 atempo（ffmpeg 子进程）：音频本身就是变速后的数据，
  直接透传即可，音质最好；
* 否则：本线程用 dsp.OlaStretcher 在 CPU 侧做时域拉伸后写入。

线程安全：QAudioSink/QAudioOutput 必须在创建它的线程里使用，因此设备的
创建、写入、音量调整全部在 run() 这一个线程里完成；外部想改音量/倍速，
通过线程安全的命令队列 _commands 投递，而不是直接跨线程调用。
"""
from __future__ import annotations

import logging
import queue
import threading
import time

from app.core import config
from app.core.dsp.time_stretch import OlaStretcher
from app.core.frames import AUDIO_BYTES_PER_SAMPLE, AUDIO_CHANNELS, AUDIO_SAMPLE_RATE
from app.core.workers.base import StoppableThread
from app.qtcompat import AudioOutput, QAudioFormat, Signal, QtMultimedia

log = logging.getLogger("core.audio")

_BYTES_PER_SEC = AUDIO_SAMPLE_RATE * AUDIO_CHANNELS * AUDIO_BYTES_PER_SAMPLE


class AudioRenderThread(StoppableThread):
    """把音频队列里的 PCM 推给声卡，并回报音频时钟。"""

    audio_clock = Signal(float)       # 当前"听到"的媒体位置（秒）
    buffering = Signal(bool)
    unavailable = Signal(str)
    stats_updated = Signal(dict)
    stream_done = Signal()

    def __init__(self, frame_queue, play_event: threading.Event,
                 sample_rate: int = AUDIO_SAMPLE_RATE, channels: int = AUDIO_CHANNELS,
                 volume: float = 1.0, parent=None):
        super().__init__("render-audio", parent)
        self.queue = frame_queue
        self._play = play_event
        self.sample_rate = sample_rate
        self.channels = channels
        self.unit_bytes = channels * AUDIO_BYTES_PER_SAMPLE

        self._sink = None
        self._dev = None
        self._commands: "queue.Queue" = queue.Queue()
        self._volume = max(0.0, min(1.0, volume))
        self._muted = False
        self._speed = 1.0
        self._stretching = False
        self._stretcher: OlaStretcher | None = None
        self._expected = None      # 下一个待写入采样对应的媒体时间
        self._buffering = True
        self._written_bytes = 0
        self._last_report = 0.0
        self.started = threading.Event()     # 声卡"真的开始吃数据"后才置位
        self.draining = threading.Event()    # 设备确实开始消耗了（能听到东西了）
        self._last_processed = None

    # ------------------------------------------------------------ 外部调用线程安全入口
    def set_volume(self, volume: float) -> None:
        self._commands.put(("volume", max(0.0, min(1.0, volume))))

    def set_muted(self, muted: bool) -> None:
        self._commands.put(("mute", bool(muted)))

    def set_speed(self, speed: float) -> None:
        self._commands.put(("speed", float(speed)))

    def set_stretching(self, enabled: bool) -> None:
        self._commands.put(("stretch", bool(enabled)))

    def wait_started(self, timeout: float) -> bool:
        """等到第一次真正写入成功（声卡设备初始化有时比视频慢一秒多）。"""
        return self.started.wait(timeout)

    def wait_draining(self, timeout: float) -> bool:
        """等到设备确实消耗过数据（`processedUSecs` 变大了）。"""
        return self.draining.wait(timeout)

    def flush(self) -> None:
        """seek 之后调用：清掉拉伸器残留，重新锁定 pts。"""
        self._commands.put(("flush", None))

    # ------------------------------------------------------------ 设备
    def _open_device(self):
        if AudioOutput is None or QtMultimedia is None:
            raise RuntimeError("QtMultimedia 不可用")
        fmt = QAudioFormat()
        fmt.setSampleRate(self.sample_rate)
        fmt.setChannelCount(self.channels)
        fmt.setSampleSize(16)
        fmt.setCodec("audio/pcm")
        fmt.setByteOrder(QAudioFormat.LittleEndian)
        fmt.setSampleType(QAudioFormat.SignedInt)

        info = None
        preferred = getattr(QtMultimedia, "QAudioDevice", None)
        # Qt5.15+/Qt6 支持查询默认设备并校验格式，Qt5.12 只能直接构造
        try:
            devices_cls = getattr(QtMultimedia, "QMediaDevices", None)
            if devices_cls is not None:
                info = devices_cls.defaultAudioOutput()
        except Exception:
            info = None

        try:
            if info is not None and preferred is not None:
                if getattr(info, "isNull", lambda: True)():
                    info = None
                elif not info.isFormatSupported(fmt):
                    log.warning("默认设备不支持 48k/16bit 立体声，使用最接近格式")
                    fmt = info.preferredFormat()
                sink = AudioOutput(info, fmt)
            else:
                sink = AudioOutput(fmt)
        except Exception as exc:
            log.debug("构造带设备参数的 AudioOutput 失败(%s)，回退默认设备", exc)
            sink = AudioOutput(fmt)

        sink.setVolume(self._volume)
        dev = sink.start()
        if dev is None:
            raise RuntimeError("声卡 start() 未返回可写设备")
        return sink, dev

    # ------------------------------------------------------------ 主循环
    def run(self) -> None:  # noqa: D102
        try:
            self._sink, self._dev = self._open_device()
        except Exception as exc:
            log.warning("音频输出不可用，转为静音模式: %s", exc)
            self.unavailable.emit(str(exc))
            return

        log.debug("[%s] 音频渲染线程启动，设备缓冲区 %d 字节",
                  self.objectName(), self._sink.bufferSize())
        self._buffering = True
        self.buffering.emit(True)

        while not self.stopping and not self.queue.is_closed:
            self._drain_commands()

            if not self._play.is_set():
                self._suspend()
                self._play.wait(0.02)
                self._resume()
                continue

            frame = self.queue.get(timeout=0.02)
            if frame is None:
                if self.queue.is_eos or self.queue.is_closed:
                    break
                if not self._buffering:
                    self._buffering = True
                    self.buffering.emit(True)
                continue
            if self._buffering:
                self._buffering = False
                self.buffering.emit(False)

            data = frame.data
            if self._stretching and self._stretcher is not None:
                self._stretcher.push(data)
                data = self._stretcher.pull(len(frame.data))
                if not data:
                    continue

            if self._expected is None or abs(frame.pts - self._expected) > 0.3:
                # 首帧 / seek / 变速后重启流：重新对齐
                self._expected = frame.pts

            written = self._write_all(data)
            if written > 0:
                self.started.set()
            if written <= 0 and self.stopping:
                break
            if written == len(data):
                # 媒体时间的推进始终等于这一块素材覆盖的时长，与是否拉伸无关
                self._expected = (self._expected or frame.pts) + frame.duration
            self._written_bytes += written

            now = time.monotonic()
            if now - self._last_report >= 0.05:
                self._last_report = now
                self._track_device_progress()
                pos = (self._expected or 0.0) - self._buffered_seconds() * self._speed
                self.audio_clock.emit(pos)
                self.stats_updated.emit(self.stats())

        try:
            if self._sink is not None:
                self._sink.stop()
        except Exception:
            pass
        log.debug("[%s] 音频渲染线程结束，共写 %d 字节", self.objectName(), self._written_bytes)
        self.stream_done.emit()

    # ------------------------------------------------------------ 写
    def _write_all(self, data: bytes) -> int:
        total = 0
        n = len(data)
        while total < n and not self.stopping and self._dev is not None:
            try:
                free = self._sink.bytesFree()
            except Exception:
                return total
            if free <= 0:                      # 设备缓冲满 -> 等待（天然的限速器）
                if self.sleep(0.005):
                    break
                continue
            chunk = data[total: total + min(free, n - total)]
            try:
                w = self._dev.write(chunk)
            except Exception as exc:
                log.warning("写声卡失败: %s", exc)
                return total
            if w <= 0:
                if self.sleep(0.005):
                    break
                continue
            total += w
        return total

    def _track_device_progress(self) -> None:
        """用 processedUSecs 判断声卡是否真的在出声。

        有些环境（无音频设备的服务器 / offscreen / 虚拟声卡）里 Qt 会把设备
        打开成功、也让你 write，但底层从来不消耗——这时音频时钟是"死"的，
        上层据此校正画面只会把播放拖住，所以要把这个事实暴露出去。
        """
        try:
            usec = self._sink.processedUSecs()
        except Exception:
            self.draining.set()          # 拿不到指标就别冤枉它
            return
        if self._last_processed is None:
            self._last_processed = usec
        elif usec > self._last_processed:
            self._last_processed = usec
            self.draining.set()

    def _buffered_seconds(self) -> float:
        try:
            used = self._sink.bufferSize() - self._sink.bytesFree()
            return max(0.0, used / float(_BYTES_PER_SEC))
        except Exception:
            return 0.0

    def _suspend(self) -> None:
        """暂停：让声卡停住（保留缓冲），避免旧数据继续被播出去。"""
        try:
            if self._sink is not None:
                self._sink.suspend()
        except Exception:
            pass

    def _resume(self) -> None:
        try:
            if self._sink is not None:
                self._sink.resume()
        except Exception:
            pass

    # ------------------------------------------------------------ 命令
    def _drain_commands(self) -> None:
        while True:
            try:
                cmd, value = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                self._apply_command(cmd, value)
            except Exception as exc:
                log.debug("处理音频命令 %s 失败: %s", cmd, exc)

    def _apply_command(self, cmd: str, value) -> None:
        if cmd == "volume":
            self._volume = value
            if self._sink is not None:
                self._sink.setVolume(0.0 if self._muted else self._volume)
        elif cmd == "mute":
            self._muted = value
            if self._sink is not None:
                self._sink.setVolume(0.0 if self._muted else self._volume)
        elif cmd == "speed":
            self._speed = value
            if self._stretcher is not None:
                self._stretcher.set_speed(value)
        elif cmd == "stretch":
            self._stretching = value
            if value and self._stretcher is None:
                self._stretcher = OlaStretcher(
                    channels=self.channels, sample_rate=self.sample_rate,
                    speed=self._speed, frame_ms=config.OLA_FRAME_MS)
            elif not value and self._stretcher is not None:
                self._stretcher.reset()
                self._stretcher = None
        elif cmd == "flush":
            if self._stretcher is not None:
                self._stretcher.reset()
            self._expected = None

    # ------------------------------------------------------------ 统计
    def stats(self) -> dict:
        return {
            "written_kb": round(self._written_bytes / 1024.0, 1),
            "device_buffered_ms": round(self._buffered_seconds() * 1000, 1),
            "stretching": self._stretching,
            "speed": round(self._speed, 3),
        }
