"""PyAV 后端（可选：`python -m pip install av`）。

不想单独下载 ffmpeg.exe 时可以用它做替代：PyAV 自己带了 FFmpeg 的动态库。

线程模型（这是本后端最需要注意的地方）
--------------------------------------
一个 AVFormatContext **不允许**被多线程同时 demux，所以这里显式实现了
"串行化解封装"的三层流水线，而不是让两个读线程各调各的 demux：

    demux 线程 ──► video packet 队列 ──► read_video()(解码, 在视频读线程)
              └──► audio packet 队列 ──► read_audio()(解码, 在音频读线程)

队列里放的不是 Packet 对象，而是 demux 线程内拍好的**字节快照**（data + 时间戳）：
PyAV 的 Packet 没有为跨线程持有设计，直接传对象的话，时间戳会读到过期值
（seek 落点漂移）、偶发访问已回收缓冲（进程 0xC0000005 崩溃）。解码线程
用快照重建 Packet，再交给对应流的 codec_context 解码 —— 这样 demux 线程
继续推进文件时，读线程手里没有任何指向容器内部缓冲的东西。

packet 队列有界，因此当某个解码线程慢的时候，demux 会自然阻塞，
形成和 ffmpeg 子进程后端一样的两级背压。

变速：PyAV 也能接 avfilter 的 atempo，但要在图中处理 flush/时基，
这里保守起见标记为不支持内建变速，交给 CPU 侧的 OLA 兜底
（app/core/dsp/time_stretch.py）。
"""
from __future__ import annotations

import ctypes
import logging
import os
import queue
import threading
import time
from typing import Optional

from app.core.backends.base import DecodeBackend, DecodeError
from app.core.frames import (
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE,
    VIDEO_BYTES_PER_PIXEL,
    AudioFrame,
    MediaInfo,
    VideoFrame,
)

log = logging.getLogger("backend.pyav")

try:  # 延迟到类属性里 import，模块导入不应失败
    import av  # type: ignore
    import fractions  # for time base conversions

    _AV_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover
    av = None
    _AV_IMPORT_ERROR = exc


class PyAVBackend(DecodeBackend):
    name = "pyav"
    display_name = "PyAV (libav)"
    supports_native_tempo = False   # 退化到 CPU 侧 OLA 变速
    supports_seek = True

    PACKET_QUEUE_SIZE = 256

    # seek 后最多花这么久去"丢掉早于目标点的帧"；超时就放弃过滤先出画面
    SEEK_FILTER_TIMEOUT = 1.5

    def __init__(self):
        super().__init__()
        self._container = None
        self._v_stream = None
        self._a_stream = None
        # codec_context 在 open()（主线程）里创建一次，之后只被各自的读线程使用，
        # 不存在并发访问；decode 时传快照重建的 Packet，不依赖 packet.stream 路由
        self._v_codec = None
        self._a_codec = None
        self._resampler = None
        self._v_packets: "queue.Queue" = queue.Queue(self.PACKET_QUEUE_SIZE)
        self._a_packets: "queue.Queue" = queue.Queue(self.PACKET_QUEUE_SIZE)
        self._demux: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._seek_request: "queue.Queue[float]" = queue.Queue()
        self._v_eos = threading.Event()
        self._a_eos = threading.Event()
        # seek 后"丢弃早于目标点的帧"是有期限的：万一目标点就是等不来
        # （pts 换算与 seek 落点不一致、或素材本身时间戳异常），到期就放弃过滤，
        # 否则会表现为"跳转之后永久不出帧"。
        self._seek_deadline = 0.0
        self._pending_video = []
        self._pending_audio = []
        # seek 目标点**按流分开存**：视频和音频是两个独立的读线程，
        # 共用一份会被先到达的那条轨道清掉，另一条轨道就会放行 pts 早于目标的帧
        #（现象：跳转后画面停在 7.3s 而不是 12s）
        self._seek_target_v: Optional[float] = None
        self._seek_target_a: Optional[float] = None
        self._lock = threading.Lock()
        self._demux_lock = threading.Lock()

    # ------------------------------------------------------------ 能力
    @classmethod
    def is_available(cls) -> bool:
        return av is not None

    @classmethod
    def can_open(cls, path: str) -> bool:
        return cls.is_available() and os.path.isfile(path)

    # ------------------------------------------------------------ 打开
    def open(self, path: str) -> MediaInfo:
        if av is None:
            raise DecodeError("PyAV 未安装(%s)" % _AV_IMPORT_ERROR)
        try:
            self._container = av.open(path)
        except Exception as exc:
            raise DecodeError("PyAV 打开失败: %s" % exc)

        info = MediaInfo(path=os.path.abspath(path), backend=self.name)
        info.title = os.path.basename(path)
        try:
            if self._container.duration:
                info.duration = float(self._container.duration) / av.time_base
        except Exception:
            pass

        v_streams = self._container.streams.video
        if v_streams:
            self._v_stream = v_streams[0]
            self._v_codec = self._v_stream.codec_context
            s = self._v_stream
            info.has_video = True
            info.width = s.codec_context.width
            info.height = s.codec_context.height
            try:
                info.fps = float(s.average_rate) or 25.0
            except Exception:
                info.fps = 25.0
        else:
            self._v_eos.set()

        a_streams = self._container.streams.audio
        if a_streams:
            self._a_stream = a_streams[0]
            self._a_codec = self._a_stream.codec_context
            info.has_audio = True
            info.sample_rate = int(self._a_stream.codec_context.sample_rate or AUDIO_SAMPLE_RATE)
            info.channels = int(self._a_stream.codec_context.channels or AUDIO_CHANNELS)
            self._resampler = av.AudioResampler(
                format="s16", layout="stereo", rate=AUDIO_SAMPLE_RATE)
        else:
            self._a_eos.set()

        if not (info.has_video or info.has_audio):
            raise DecodeError("没有可用的音视频流")

        self.info = info
        self._stop.clear()
        self._demux = threading.Thread(target=self._demux_loop, name="pyav-demux", daemon=True)
        self._demux.start()
        return info

    # ------------------------------------------------------------ demux 线程
    # 关键：**容器的所有操作都必须在这个线程里做**。
    # AVFormatContext 不能被多线程并发访问 —— 如果外部线程一边 seek、demux 线程
    # 一边 demux()，轻则乱序，重则 av_read_frame 报错导致 demux 循环直接挂掉
    #（现象就是 seek 之后永远不再出帧）。所以 seek 只投递请求，由这里执行。
    def _demux_loop(self) -> None:
        streams = [s for s in (self._v_stream, self._a_stream) if s is not None]

        while not self._stop.is_set():
            seeked = False
            try:
                for packet in self._container.demux(*streams):
                    if self._stop.is_set():
                        return
                    target = self._take_seek_request()      # seek 请求优先处理
                    if target is not None:
                        self._apply_seek(target)
                        seeked = True
                        break                               # 重新起一轮 demux
                    if packet.stream is None:
                        continue
                    snapshot = self._snapshot(packet)
                    dest = self._v_packets if packet.stream is self._v_stream else self._a_packets
                    if not self._safe_put(dest, snapshot):
                        break            # 有 seek 请求：回外层处理
            except Exception as exc:
                log.debug("demux 异常: %s", exc)

            if seeked:
                continue

            # 到这里说明本轮 demux 迭代结束：要么真到文件尾，要么出错。
            # 先发 EOS 哨兵让读线程收尾，但本线程不能就此退出 ——
            # 播完之后"拖回前面再播"的 seek 只能由这里执行
            # （AVFormatContext 不允许从别的线程碰）。切片轮询新请求直到 close()。
            self._v_packets.put(None)
            self._a_packets.put(None)
            while not self._stop.is_set():
                target = self._take_seek_request(timeout=0.5)
                if target is not None:
                    self._apply_seek(target)
                    break
            else:
                return                      # close()：唯一合法的退出点
            continue                        # 从 seek 后的新位置继续 demux

    def _take_seek_request(self, timeout: float = 0.0):
        try:
            if timeout <= 0:
                return self._seek_request.get_nowait()
            return self._seek_request.get(timeout=timeout)
        except queue.Empty:
            return None

    def _apply_seek(self, target: float) -> None:
        try:
            stream = self._v_stream or self._a_stream
            # PyAV 的 seek 单位约定（这里踩过坑，写清楚）：
            #   * 传了 stream -> offset 单位是**该流的时间刻度**（如 1/12800），
            #     所以 target 秒要除以 stream.time_base，而不是乘 av.time_base；
            #   * 只接受 int，传 float / Fraction 会被直接拒绝
            #     ("Container.seek only accepts integer offset")；
            #   * 新版 PyAV 已经没有 whence 关键字了。
            offset = int(target / float(stream.time_base))
            self._container.seek(offset, stream=stream, backward=True, any_frame=False)
            log.debug("demux 线程完成 seek -> %.3fs (offset=%d @ 1/%d)",
                      target, offset, int(1.0 / float(stream.time_base)))
        except Exception as exc:
            log.warning("seek(%.3f) 失败: %s", target, exc)
            # 宁可继续播，也不要卡住等不来的帧
            self._seek_target_v = None
            self._seek_target_a = None

    def _safe_put(self, dest: "queue.Queue", packet) -> bool:
        """把 packet 放进有界队列（背压在这里生效）。

        返回 False 表示"有 seek 请求，赶紧回外层处理" —— 这一步很关键：
        队列满时若死等，demux 线程就永远看不到 seek 请求，跳转会被无限推迟，
        表现为"点了进度条之后原地空转一会儿才有反应"。
        """
        while not self._stop.is_set():
            try:
                dest.put(packet, timeout=0.1)
                return True
            except queue.Full:
                if self._seek_request.qsize():
                    return False
                continue
        return True

    @staticmethod
    def _snapshot(packet):
        """在 demux 线程内把 packet 拍成不可变快照 ``(data, pts, dts, duration)``。

        PyAV 的 Packet 对象直接跨线程持有是不安全的：demux 线程继续读文件时，
        读线程再访问 packet 的时间戳/缓冲会拿到过期值（seek 落点漂移），
        偶发地还会碰到已回收的内存（进程直接 0xC0000005 崩溃）。
        av 19 没有 to_bytes()，用 ctypes 按底层指针拷一份最稳。
        """
        size = packet.size or 0
        data = ctypes.string_at(packet.buffer_ptr, size) if size > 0 else b""
        return (data, packet.pts, packet.dts, packet.duration)

    # ------------------------------------------------------------ 读帧
    def _pump(self, packets: "queue.Queue", stream, codec, eos_event, pending: list,
              kind: str):
        """把下一个 packet 解出的帧放进 pending，返回是否已到 EOS。"""
        while not pending:
            # 统一在这里判停止：下面每个 continue 分支都可能是"长时间等不到帧"，
            # 逐个补检查容易漏（漏一个就会出现"停止时读线程退不出、被强杀"）。
            if self._stop.is_set():
                return True
            try:
                item = packets.get(timeout=0.1)
            except queue.Empty:
                # 关键：停止时 packet 队列可能长时间为空。这里不检查停止标志的话，
                # read_video() 会一直卡在这个循环里返回不了 —— 读线程退不出，
                # 上层 2 秒后就会 terminate() 强杀它，进程直接 abort（退出码 3）。
                if self._stop.is_set():
                    return True
                continue
            if item is None:
                # seek 进行中时，这个 EOS 哨兵可能是"跳转前那一代的遗留物"
                # （demux 线程刚投进去、还没轮到处理 seek 请求）。
                # 把它当文件结束会让读线程直接退出 —— 表现为"跳转后永远不再出帧"。
                if self._seek_request.qsize() or self._seek_target_v is not None \
                        or self._seek_target_a is not None:
                    log.debug("[%s] 忽略 seek 期间的过期 EOS 哨兵", kind)
                    self._v_eos.clear()
                    self._a_eos.clear()
                    continue
                eos_event.set()
                return True
            data, pts, dts, duration = item
            # seek 后要先"追"到目标点。关键帧稀疏的素材里，这一步可能要跨过
            # 几百个 packet —— 如果每个都解码一遍再丢，Python 侧要一两秒，
            # 表现就是"拖完进度条画面卡住"。快照的 pts 在 demux 线程就拍好了，
            # 直接在这里跳过，连解码都不做；精确落点由 read_xxx() 的帧级过滤兜底。
            target = self._seek_target_v if kind == "video" else self._seek_target_a
            if target is not None and pts is not None:
                try:
                    t = float(pts * stream.time_base)
                except Exception:
                    t = None
                if t is not None and t < target - 0.01:
                    continue
            if not data:
                continue                    # 空 packet（部分封装格式会有）
            # 从快照重建 Packet，交给本流的 codec_context 解码（不依赖
            # packet.stream 路由，也就不碰容器的任何共享状态）
            try:
                pkt = av.Packet(data)
                pkt.pts = pts
                pkt.dts = dts
                pkt.duration = duration
                pkt.time_base = stream.time_base
                for frame in codec.decode(pkt):
                    if frame is None:
                        continue
                    pending.append(frame)
            except Exception as exc:
                log.debug("解码失败(%s): %s", kind, exc)
                continue
        return False

    def read_video(self) -> Optional[VideoFrame]:
        """取下一帧；只有真正到文件尾（或已停止）才返回 None。

        外层这个循环是必须的：seek 之后会有一批 pts 早于目标点的帧要被丢掉，
        如果这批帧刚好把 pending 丢空就直接返回 None，调用方会当成 EOS ——
        表现为"往中间跳转之后立刻就停止播放"。
        """
        if self._v_stream is None or self._stop.is_set():
            return None
        while not self._stop.is_set():
            if self._pump(self._v_packets, self._v_stream, self._v_codec, self._v_eos,
                          self._pending_video, "video"):
                return None
            while self._pending_video:
                raw = self._pending_video.pop(0)
                pts = self._frame_pts(raw, self._v_stream)
                if self._seek_target_v is not None and pts is not None \
                        and pts < self._seek_target_v - 0.01:
                    if time.monotonic() > self._seek_deadline:
                        # 目标点等不来（pts 换算与 seek 落点不一致时会出现）。
                        # 再丢下去就是"跳转后永久不出帧"，所以放弃过滤、本帧照常出。
                        log.warning("[video] seek 过滤超时，放弃等待 %.2fs 之后的帧",
                                    self._seek_target_v)
                        self._seek_target_v = None
                    else:
                        continue
                self._seek_target_v = None
                data = self._to_rgb24(raw)
                if data is None:
                    continue
                duration = 0.0
                if raw.time_base and raw.pts is not None:
                    try:
                        duration = float(raw.duration or 1) * float(raw.time_base)
                    except Exception:
                        duration = 1.0 / max(1e-3, self.info.fps)
                return VideoFrame(pts=pts or 0.0, duration=duration,
                                  width=raw.width, height=raw.height, data=data)
        return None

    def read_audio(self) -> Optional[AudioFrame]:
        # 外层循环的意义同 read_video：被 seek 过滤掉一批帧时不要误报 EOS
        if self._a_stream is None or self._stop.is_set():
            return None
        while not self._stop.is_set():
            if self._pump(self._a_packets, self._a_stream, self._a_codec, self._a_eos,
                          self._pending_audio, "audio"):
                return None
            while self._pending_audio:
                raw = self._pending_audio.pop(0)
                pts = self._frame_pts(raw, self._a_stream)
                if self._seek_target_a is not None and pts is not None \
                        and pts < self._seek_target_a - 0.01:
                    if time.monotonic() > self._seek_deadline:
                        log.warning("[audio] seek 过滤超时，放弃等待 %.2fs 之后的帧",
                                    self._seek_target_a)
                        self._seek_target_a = None
                    else:
                        continue
                try:
                    frames = self._resampler.resample(raw) if self._resampler else [raw]
                except Exception as exc:
                    log.debug("重采样失败: %s", exc)
                    continue
                if not frames:
                    continue
                data = self._plane_bytes(frames[0])
                if not data:
                    continue
                units = len(data) // (AUDIO_CHANNELS * 2)
                return AudioFrame(pts=pts or 0.0,
                                  duration=units / float(AUDIO_SAMPLE_RATE),
                                  data=data,
                                  sample_rate=AUDIO_SAMPLE_RATE,
                                  channels=AUDIO_CHANNELS)
        return None

    # ------------------------------------------------------------ 工具
    @staticmethod
    def _frame_pts(frame, stream) -> Optional[float]:
        if frame.pts is None or stream.time_base is None:
            return None
        try:
            return float(frame.pts * stream.time_base)
        except Exception:
            return None

    @staticmethod
    def _plane_bytes(frame) -> bytes:
        """读出首平面的内容（不需要 numpy）。

        坑：`plane.update(buf)` 是**写入**语义（把 buf 拷进 plane），不是读取。
        之前用它"读"画面，等于先把整帧清零再返回那个零缓冲 —— 解码、队列、
        出帧计数全都正常，唯独画面上什么都没有（全黑）。用 buffer 协议读才对。
        """
        try:
            return bytes(frame.planes[0])
        except Exception:
            return b""

    def _to_rgb24(self, frame) -> Optional[bytes]:
        try:
            out = frame.reformat(format="rgb24")
        except Exception as exc:
            log.debug("reformat 失败: %s", exc)
            return None
        data = self._plane_bytes(out)
        need = frame.width * frame.height * VIDEO_BYTES_PER_PIXEL
        if len(data) < need:
            return None
        return data[:need]

    # ------------------------------------------------------------ seek / close
    def seek(self, t: float) -> None:
        if self._container is None:
            return
        t = max(0.0, float(t))
        with self._demux_lock:
            self._seek_target_v = t       # 视频/音频各自的过滤目标，互不影响
            self._seek_target_a = t
            self._seek_deadline = time.monotonic() + self.SEEK_FILTER_TIMEOUT
            # 清掉 packet 队列，避免 seek 前积压的 packet 继续被解码
            for q in (self._v_packets, self._a_packets):
                try:
                    while True:
                        q.get_nowait()
                except queue.Empty:
                    pass
            # 就地清空而不是重新绑定：读线程可能正持有这两个 list 的引用
            del self._pending_video[:]
            del self._pending_audio[:]
            self._v_eos.clear()
            self._a_eos.clear()
        # seek 本身交给 demux 线程执行（AVFormatContext 不能并发操作）
        self._seek_request.put(t)

    def request_stop(self) -> None:
        """只置位停止标志并唤醒阻塞中的读循环，资源留到 close() 再释放。"""
        self._stop.set()
        for q in (self._v_packets, self._a_packets):
            try:
                q.put_nowait(None)
            except queue.Full:
                pass

    def close(self) -> None:
        self.request_stop()
        if self._demux is not None and self._demux.is_alive():
            self._demux.join(timeout=1.0)
        try:
            if self._container is not None:
                self._container.close()
        except Exception:
            pass
        self._container = None
        log.debug("PyAVBackend closed")
