"""FFmpeg 子进程后端（首选）。

思路
----
不引入任何额外 Python 依赖，直接用 ``subprocess`` 起两个 ffmpeg 进程：

    视频: ffmpeg -ss T -i FILE -map 0:V -f rawvideo -pix_fmt rgb24 pipe:1
    音频: ffmpeg -ss T -i FILE -map 0:A -af atempo=R -f s16le -ac 2 -ar 48000 pipe:1

两个进程 = 两条独立的读写管道 = 天然的线程隔离，因此两个读线程可以各自
阻塞读而互不影响；同时**操作系统管道缓冲区 + 我们的帧队列**形成了两级背压，
ffmpeg 会自己降速，不会把内存打爆。

变速在这个后端是"真变速"：改变倍速时用新的 ``atempo`` 参数并带上 ``-ss 当前位置``
重启音频进程，保持音调不变；视频不需要任何处理——画面快慢由媒体时钟决定
（见 app/core/clock.py）。

seek 也是重启进程（``-ss``），简单、准确、无需处理 avcodec flush 的各种坑。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from typing import Deque, List, Optional

from app.core import config
from app.core.backends.base import DecodeBackend, DecodeError
from app.core.dsp.time_stretch import atempo_filter_string
from app.core.frames import (
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE,
    VIDEO_BYTES_PER_PIXEL,
    AudioFrame,
    MediaInfo,
    VideoFrame,
)

log = logging.getLogger("backend.ffmpeg")

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# --------------------------------------------------------------------------- ffmpeg 定位
def locate_executable(name: str) -> Optional[str]:
    """按 环境变量 -> 项目内 tools/ -> PATH 的顺序找 ffmpeg / ffprobe。"""
    exe = name + (".exe" if os.name == "nt" else "")
    candidates: List[str] = []

    env = os.environ.get("FFMPEG" if name == "ffmpeg" else "FFPROBE")
    if env:
        candidates.append(env)
    for d in config.ffmpeg_search_dirs():
        candidates.append(os.path.join(d, exe))
    found = shutil.which(name)
    if found:
        candidates.append(found)

    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


# --------------------------------------------------------------------------- 子进程封装
class _PipeStream:
    """一个"产出裸数据"的 ffmpeg 子进程。"""

    def __init__(self, cmd: List[str], tag: str):
        self.cmd = cmd
        self.tag = tag
        self.proc: Optional[subprocess.Popen] = None
        self.stderr_tail: Deque[str] = deque(maxlen=50)
        self._return_code: Optional[int] = None
        self._start()

    def _start(self) -> None:
        log.debug("[%s] spawn: %s", self.tag, " ".join(self.cmd))
        try:
            self.proc = subprocess.Popen(
                self.cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                bufsize=0,
                creationflags=_CREATE_NO_WINDOW,
            )
        except OSError as exc:
            raise DecodeError("无法启动 %s: %s" % (self.tag, exc))
        # stderr 必须持续排空，否则 ffmpeg 输出日志会写满管道导致卡死
        threading.Thread(target=self._drain_stderr, daemon=True, name=self.tag + "-stderr").start()

    def _drain_stderr(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                try:
                    text = line.decode("utf-8", "replace").rstrip()
                except Exception:
                    continue
                if text:
                    self.stderr_tail.append(text)
                    log.debug("[%s] %s", self.tag, text)
        except Exception:
            pass

    # ---------------------------------------------------------------- IO
    def read(self, n: int) -> Optional[bytes]:
        """精确读 n 字节；EOF 返回 None。"""
        proc = self.proc
        if proc is None or proc.stdout is None:
            return None
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = proc.stdout.read(n - len(buf))
            except (ValueError, OSError):
                break
            if not chunk:
                if not buf:
                    return None
                log.debug("[%s] 管道提前结束(只剩 %d/%d 字节)", self.tag, len(buf), n)
                return None
            buf.extend(chunk)
        return bytes(buf)

    def return_code(self) -> Optional[int]:
        if self.proc is None:
            return self._return_code
        rc = self.proc.poll()
        if rc is not None:
            self._return_code = rc
        return rc

    def last_error(self) -> str:
        return " | ".join(self.stderr_tail) if self.stderr_tail else ""

    def kill(self, timeout: float = 1.5) -> None:
        proc = self.proc
        if proc is None:
            return
        self.proc = None
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=timeout)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- 后端
class FFmpegCLIBackend(DecodeBackend):
    name = "ffmpeg-cli"
    display_name = "FFmpeg (子进程)"
    supports_native_tempo = True
    supports_seek = True

    AUDIO_UNIT_BYTES = 2 * AUDIO_CHANNELS
    AUDIO_CHUNK_UNITS = 1024          # 每次读取的音频采样帧数

    def __init__(self, ffmpeg_path: Optional[str] = None, ffprobe_path: Optional[str] = None):
        super().__init__()
        self.ffmpeg = ffmpeg_path or locate_executable("ffmpeg")
        self.ffprobe = ffprobe_path or locate_executable("ffprobe")
        self._video: Optional[_PipeStream] = None
        self._audio: Optional[_PipeStream] = None

        self._video_pts = 0.0
        self._video_frame_duration = 1.0 / 25.0
        self._audio_pts = 0.0
        self._audio_tempo = 1.0
        self._video_eos = True
        self._audio_eos = True
        self._vlock = threading.Lock()   # 视频/音频各用一把锁，减少互相阻塞
        self._alock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------ 能力
    @classmethod
    def is_available(cls) -> bool:
        return locate_executable("ffmpeg") is not None

    @classmethod
    def can_open(cls, path: str) -> bool:
        return cls.is_available() and os.path.isfile(path)

    # ------------------------------------------------------------ 打开
    def open(self, path: str) -> MediaInfo:
        if not self.ffmpeg:
            raise DecodeError("未找到 ffmpeg 可执行文件")
        path = os.path.abspath(path)
        info = self.probe(path)
        if not (info.has_video or info.has_audio):
            raise DecodeError("文件中没有可用的视频/音频流: %s" % path)
        self.info = info
        info.backend = self.name
        info.title = os.path.basename(path)
        self._closed = False

        # 这个 ffmpeg 是"完整版"还是"裁剪版"？实测，别猜。
        # 很多便携/极简构建是 --disable-everything 编出来的：
        # 只有几个 demuxer/decoder，没有音频解码器，也没有 atempo 滤镜。
        self.supports_native_tempo = self._has_filter("atempo")
        self._verify_decode_capability(info)
        if not (info.has_video or info.has_audio):
            raise DecodeError("当前 ffmpeg 构建无法解码该文件（缺少对应解码器）: %s" % path)

        self._start_video(info)
        self._start_audio(info, 0.0, 1.0)
        return info

    # ------------------------------------------------------------ 能力实测
    def _has_filter(self, name: str) -> bool:
        """查 ffmpeg 有没有某个滤镜（决定能不能用 atempo 做保音变速）。"""
        try:
            out = subprocess.run([self.ffmpeg, "-hide_banner", "-filters"],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 timeout=10, creationflags=_CREATE_NO_WINDOW)
            return name.encode() in (out.stdout or b"")
        except Exception as exc:
            log.debug("查询 ffmpeg 滤镜列表失败: %s", exc)
            return False

    def _verify_decode_capability(self, info: MediaInfo) -> None:
        """对每条轨道各试读一帧/一块，读不到就把它从 MediaInfo 里摘掉。

        不这么做的话，"极简 ffmpeg"会把看起来正常的流报出来，
        真正开始播放时子进程才会立刻挂掉 => 用户看到黑屏或静音却没有任何提示。
        """
        notes = []
        if info.has_video and not self._try_decode("video"):
            info.has_video = False
            notes.append("视频无法解码")
        if info.has_audio and not self._try_decode("audio"):
            info.has_audio = False
            notes.append("音频无法解码（该 ffmpeg 构建很可能没编译音频解码器）")
        if notes:
            log.warning("ffmpeg 能力受限: %s", "; ".join(notes))
            info.notes = "当前 ffmpeg 构建能力受限：" + "；".join(notes)

    def _try_decode(self, kind: str) -> bool:
        try:
            if kind == "video":
                cmd = self._video_cmd(0.0)
                size = self.info.width * self.info.height * VIDEO_BYTES_PER_PIXEL
            else:
                cmd = self._audio_cmd(0.0, 1.0)
                size = self.AUDIO_CHUNK_UNITS * self.AUDIO_UNIT_BYTES
        except Exception:
            return False
        if size <= 0:
            return False
        stream = None
        try:
            stream = _PipeStream(cmd, "probe-" + kind)
            return stream.read(size) is not None
        except DecodeError:
            return False
        finally:
            if stream is not None:
                stream.kill()

    # ------------------------------------------------------------ 元信息
    def probe(self, path: str) -> MediaInfo:
        info = MediaInfo(path=path)
        video_index = audio_index = None

        data = self._ffprobe_json(path)
        if data:
            fmt = data.get("format", {}) or {}
            try:
                info.duration = float(fmt.get("duration") or 0.0)
            except (TypeError, ValueError):
                info.duration = 0.0
            for st in data.get("streams", []) or []:
                codec_type = st.get("codec_type")
                index = st.get("index")
                if codec_type == "video" and video_index is None:
                    video_index = index
                    info.has_video = True
                    info.width = int(st.get("width") or 0)
                    info.height = int(st.get("height") or 0)
                    info.fps = _parse_frame_rate(st.get("avg_frame_rate") or st.get("r_frame_rate"))
                    sr = st.get("sample_rate")
                elif codec_type == "audio" and audio_index is None:
                    audio_index = index
                    info.has_audio = True
                    try:
                        info.sample_rate = int(st.get("sample_rate") or AUDIO_SAMPLE_RATE)
                    except (TypeError, ValueError):
                        info.sample_rate = AUDIO_SAMPLE_RATE
                    fmt_channels = st.get("channels")
                    info.channels = int(fmt_channels or AUDIO_CHANNELS)
        else:  # 没有 ffprobe 就解析 ffmpeg -i 的 stderr
            text = self._ffmpeg_identify(path)
            video_index, audio_index = _parse_ffmpeg_header(text, info)

        if info.has_video and (info.width <= 0 or info.height <= 0):
            info.has_video = False
            log.warning("无法识别视频分辨率，已忽略视频流")
        if not info.fps or info.fps <= 0:
            info.fps = 25.0
        self.info = info
        self._v_index = video_index
        self._a_index = audio_index
        self._video_frame_duration = 1.0 / info.fps
        return info

    def _ffprobe_json(self, path: str) -> Optional[dict]:
        if not self.ffprobe:
            return None
        cmd = [self.ffprobe, "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", path]
        try:
            out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 timeout=10, creationflags=_CREATE_NO_WINDOW)
            if out.returncode != 0 or not out.stdout:
                return None
            return json.loads(out.stdout.decode("utf-8", "replace"))
        except Exception as exc:
            log.debug("ffprobe 失败: %s", exc)
            return None

    def _ffmpeg_identify(self, path: str) -> str:
        try:
            out = subprocess.run([self.ffmpeg, "-hide_banner", "-i", path],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 timeout=10, creationflags=_CREATE_NO_WINDOW)
            return out.stderr.decode("utf-8", "replace")
        except Exception as exc:
            raise DecodeError("探测文件失败: %s" % exc)

    # ------------------------------------------------------------ 进程管理
    def _video_cmd(self, start: float) -> List[str]:
        idx = self._v_index if getattr(self, "_v_index", None) is not None else "v:0"
        return [
            self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-ss", "%.3f" % max(0.0, start), "-i", self.info.path,
            "-map", "0:%s" % idx, "-an", "-dn", "-sn",
            "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
        ]

    def _audio_cmd(self, start: float, tempo: float) -> List[str]:
        idx = self._a_index if getattr(self, "_a_index", None) is not None else "a:0"
        cmd = [
            self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-ss", "%.3f" % max(0.0, start), "-i", self.info.path,
            "-map", "0:%s" % idx, "-vn", "-dn", "-sn",
        ]
        if abs(tempo - 1.0) > 1e-3:
            cmd += ["-af", atempo_filter_string(tempo)]
        cmd += ["-f", "s16le", "-ac", str(AUDIO_CHANNELS), "-ar", str(AUDIO_SAMPLE_RATE), "-"]
        return cmd

    def _start_video(self, info: MediaInfo, start: float = 0.0) -> None:
        if not info.has_video:
            self._video_eos = True
            return
        with self._vlock:
            self._kill_video()
            self._video = _PipeStream(self._video_cmd(start), "ffmpeg-video")
            self._video_pts = start
            self._video_eos = False

    def _start_audio(self, info: MediaInfo, start: float, tempo: float) -> None:
        if not info.has_audio:
            self._audio_eos = True
            return
        with self._alock:
            self._kill_audio()
            self._audio = _PipeStream(self._audio_cmd(start, tempo), "ffmpeg-audio")
            self._audio_pts = start
            self._audio_tempo = tempo
            self._audio_eos = False

    def _kill_video(self) -> None:
        if self._video is not None:
            self._video.kill()
            self._video = None

    def _kill_audio(self) -> None:
        if self._audio is not None:
            self._audio.kill()
            self._audio = None

    # ------------------------------------------------------------ 读帧
    def read_video(self) -> Optional[VideoFrame]:
        info = self.info
        if info is None or not info.has_video or self._closed:
            return None
        with self._vlock:
            stream = self._video
            if stream is None or self._video_eos:
                return None
            w, h = info.width, info.height
            data = stream.read(w * h * VIDEO_BYTES_PER_PIXEL)
            if not data:
                self._video_eos = True
                rc = stream.return_code()
                if rc not in (0, None):
                    log.warning("ffmpeg 视频进程异常退出(%s): %s", rc, stream.last_error())
                return None
            pts = self._video_pts
            self._video_pts += self._video_frame_duration
            return VideoFrame(pts=pts, duration=self._video_frame_duration,
                              width=w, height=h, data=data)

    def read_audio(self) -> Optional[AudioFrame]:
        info = self.info
        if info is None or not info.has_audio or self._closed:
            return None
        with self._alock:
            stream = self._audio
            if stream is None or self._audio_eos:
                return None
            n = self.AUDIO_CHUNK_UNITS * self.AUDIO_UNIT_BYTES
            data = stream.read(n)
            if not data:
                self._audio_eos = True
                rc = stream.return_code()
                if rc not in (0, None):
                    log.warning("ffmpeg 音频进程异常退出(%s): %s", rc, stream.last_error())
                return None
            units = len(data) // self.AUDIO_UNIT_BYTES
            pts = self._audio_pts
            # atempo=R 时：输出 N 个采样帧代表源素材的 N*R/sr 秒
            advance = units * self._audio_tempo / float(AUDIO_SAMPLE_RATE)
            self._audio_pts += advance
            return AudioFrame(pts=pts, duration=advance, data=data,
                              sample_rate=AUDIO_SAMPLE_RATE, channels=AUDIO_CHANNELS)

    # ------------------------------------------------------------ 变速 / 跳转
    def apply_audio_speed(self, speed: float, resume_at: float) -> bool:
        """从 resume_at 起按新倍速重建音频流（atempo 真变速）。"""
        info = self.info
        if info is None or not info.has_audio or self._closed:
            return False
        try:
            self._start_audio(info, max(0.0, resume_at), speed)
            log.info("音频变速 -> %.2fx (atempo)，恢复点 %.2fs", speed, resume_at)
            return True
        except DecodeError as exc:
            log.error("重建音频流失败: %s", exc)
            return False

    @property
    def audio_speed(self) -> float:
        with self._alock:
            return self._audio_tempo

    def seek(self, t: float) -> None:
        info = self.info
        if info is None:
            return
        t = max(0.0, float(t))
        self._start_video(info, t)
        self._start_audio(info, t, self._audio_tempo)
        log.info("seek -> %.2fs (重启两个 ffmpeg 进程)", t)

    def close(self) -> None:
        self._closed = True
        self._kill_video()
        self._kill_audio()
        self._video_eos = True
        self._audio_eos = True
        log.debug("FFmpegCLIBackend closed")


# --------------------------------------------------------------------------- 头部解析
def _parse_frame_rate(rate: object, default: float = 25.0) -> float:
    """把 ffprobe 的 "30000/1001" 这种分数转成 float。"""
    if isinstance(rate, (int, float)):
        return float(rate) or default
    if isinstance(rate, str) and "/" in rate:
        num, _, den = rate.partition("/")
        try:
            return (float(num) / float(den)) or default
        except (ValueError, ZeroDivisionError):
            return default
    return default


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):([\d.]+)")
# ffmpeg 6/7 的控制台输出形如：Stream #0:0[0x1](und): Video: h264 (avc1 / 0x31637661), ...
# 老版本形如：Stream #0:0(und): Video: ...  两种都要能吃下
_STREAM_RE = re.compile(
    r"Stream #(\d+):(\d+)(?:\[[^\]]*\])?(?:\([^)]*\))?:\s*(Video|Audio|Subtitle):\s*(.*)"
)


def _parse_ffmpeg_header(text: str, info: MediaInfo):
    """从 `ffmpeg -i FILE` 的 stderr 里提取信息（没有 ffprobe 时的兜底）。"""
    m = _DURATION_RE.search(text)
    if m:
        info.duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    video_index = audio_index = None
    for match in _STREAM_RE.finditer(text):
        index = match.group(1) + ":" + match.group(2)
        kind = match.group(3)
        body = match.group(4)
        if kind == "Video" and video_index is None:
            res = re.search(r"(\d{2,5})x(\d{2,5})", body)
            if res:
                info.has_video = True
                info.width = int(res.group(1))
                info.height = int(res.group(2))
                video_index = index
                fps = re.search(r"([\d.]+)\s*(?:fps|tbr)", body)
                if fps:
                    info.fps = float(fps.group(1))
        elif kind == "Audio" and audio_index is None:
            info.has_audio = True
            audio_index = index
            sr = re.search(r"(\d{3,6})\s*Hz", body)
            if sr:
                info.sample_rate = int(sr.group(1))
            ch = re.search(r"(stereo|mono|5\.1|(\d+)\s+channels?)", body)
            if ch:
                info.channels = {"stereo": 2, "mono": 1, "5.1": 6}.get(
                    ch.group(1), int(ch.group(2)) if ch.group(2) else 2)
    return video_index, audio_index
