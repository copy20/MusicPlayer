"""生成一段测试视频（不需要任何外部素材）。

    python tools/make_sample_clip.py                       # 默认 assets/sample.mp4
    python tools/make_sample_clip.py out.mkv 30 1280 720   # 自定义路径/时长/分辨率

用 PyAV 自带的 FFmpeg 编码：H.264 + AAC / MP4，画面是移动色块 + 进度条 + 秒数，
声音是每秒一个"嘀"的正弦音，方便肉眼/耳朵判断播放是否在推进、是否在变速。
"""
from __future__ import annotations

import array
import math
import os
import sys
from fractions import Fraction

import av


def render_rgb(t: float, w: int, h: int) -> bytes:
    rows = []
    phase = t * 0.9
    for y in range(h):
        a = y / max(1.0, h - 1)
        r = 128 + 96 * math.sin(phase + 2.0 * math.pi * a)
        g = 128 + 96 * math.sin(phase + 2.0 * math.pi * a + 2.094)
        b = 128 + 96 * math.sin(phase + 2.0 * math.pi * a + 4.188)
        rows.append(bytes((_c(r), _c(g), _c(b))) * w)

    bw, bh = max(8, w // 8), max(8, h // 8)
    bx = int((0.5 + 0.5 * math.sin(t * 1.7)) * (w - bw))
    by = int((0.5 + 0.5 * math.cos(t * 1.1)) * (h - bh))
    box = bytes((250, 236, 90))
    # 注意：rows[y] 是字节串，下标要用"字节偏移"= 像素位置 × 3
    head, tail = bx * 3, (bx + bw) * 3
    for y in range(by, by + bh):
        rows[y] = rows[y][:head] + box * bw + rows[y][tail:]

    pw = int(w * (t % 10.0) / 10.0)
    for y in range(h - 8, h):
        rows[y] = bytes((48, 200, 130)) * pw + bytes((26, 28, 34)) * (w - pw)
    return b"".join(rows)


def render_pcm(start_units: int, units: int, sr: int) -> bytes:
    out = array.array("h")
    for i in range(units):
        tt = (start_units + i) / float(sr)
        env = 0.5 * math.exp(-6.0 * (tt % 1.0))          # 每秒一个"嘀"
        v = 0.3 * env * math.sin(2 * math.pi * 440.0 * tt)
        iv = int(max(-1.0, min(1.0, v)) * 32000)
        out.append(iv)
        out.append(iv)
    return out.tobytes()


def _c(v: float) -> int:
    iv = int(v)
    return 0 if iv < 0 else (255 if iv > 255 else iv)


def make(path: str = "assets/sample.mp4", duration: int = 20,
         width: int = 640, height: int = 360, fps: int = 25, sr: int = 48000) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    total_frames = duration * fps
    samples_per_block = 1024

    out = av.open(path, "w", format="mp4")
    v = out.add_stream("libx264", rate=fps)
    v.width, v.height, v.pix_fmt = width, height, "yuv420p"
    v.options = {"crf": "26", "preset": "veryfast"}
    a = out.add_stream("aac", rate=sr, layout="stereo")

    v_tb = Fraction(1, fps)
    a_tb = Fraction(1, sr)
    audio_units = 0
    audio_total = duration * sr

    for i in range(total_frames):
        t = i / float(fps)
        frame = av.VideoFrame(width, height, "rgb24")
        frame.planes[0].update(render_rgb(t, width, height))
        frame.pts = i
        frame.time_base = v_tb
        for pkt in v.encode(frame.reformat(width=width, height=height, format="yuv420p")):
            out.mux(pkt)

        # 穿插写入这一帧时间范围对应的音频
        target_units = min(audio_total, int(round((i + 1) / float(fps) * sr)))
        while target_units - audio_units >= samples_per_block:
            block = av.AudioFrame(format="s16", layout="stereo", samples=samples_per_block)
            block.sample_rate = sr
            block.planes[0].update(render_pcm(audio_units, samples_per_block, sr))
            block.pts = audio_units
            block.time_base = a_tb
            for pkt in a.encode(block):
                out.mux(pkt)
            audio_units += samples_per_block

    while audio_units < audio_total:
        n = min(samples_per_block, audio_total - audio_units)
        block = av.AudioFrame(format="s16", layout="stereo", samples=n)
        block.sample_rate = sr
        block.planes[0].update(render_pcm(audio_units, n, sr))
        block.pts = audio_units
        block.time_base = a_tb
        for pkt in a.encode(block):
            out.mux(pkt)
        audio_units += n

    for pkt in v.encode():
        out.mux(pkt)
    for pkt in a.encode():
        out.mux(pkt)
    out.close()

    size = os.path.getsize(path) / 1024.0
    print("生成完成: %s (%.1f KB, %ds, %dx%d@%d, H.264+AAC)" % (path, size, duration, width, height, fps))
    return path


if __name__ == "__main__":
    args = sys.argv[1:]
    make(
        path=args[0] if len(args) > 0 else "assets/sample.mp4",
        duration=int(args[1]) if len(args) > 1 else 20,
        width=int(args[2]) if len(args) > 2 else 640,
        height=int(args[3]) if len(args) > 3 else 360,
    )
