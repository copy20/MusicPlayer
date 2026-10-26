"""后端自动选择。

优先级：**FFmpeg 子进程 > PyAV > 内置合成源**。
前两者负责"能不能解码真实文件"，合成源保证"任何时候界面都能演示"。
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional, Type

from app.core.backends.base import DecodeBackend, DecodeError
from app.core.backends.ffmpeg_cli import FFmpegCLIBackend
from app.core.backends.pyav_backend import PyAVBackend
from app.core.backends.synthetic import SYNTHETIC_SCHEME, SyntheticBackend

log = logging.getLogger("backend.factory")

CANDIDATES: List[Type[DecodeBackend]] = [FFmpegCLIBackend, PyAVBackend, SyntheticBackend]
# 真实文件只在这两个里挑；合成源只服务 synthetic:// 这种伪协议，
# 不会悄悄替你看门——否则"解不出来"会被伪装成"正在播放"。
REAL_CANDIDATES: List[Type[DecodeBackend]] = [FFmpegCLIBackend, PyAVBackend]


def capability_score(backend: DecodeBackend) -> float:
    """给后端"真实能不能放"打分。

    存在 ≠ 能用：极简编译出来的 ffmpeg.exe 常常既没有音频解码器、
    也没有 atempo 滤镜。每个后端在 open() 时都会实测自己的解码能力
    （见 FFmpegCLIBackend._verify_decode_capability），这里按结果挑最好的。
    """
    info = getattr(backend, "info", None)
    if info is None:
        return -1.0
    score = float(bool(info.has_video)) + float(bool(info.has_audio))
    if getattr(backend, "supports_native_tempo", False):
        score += 0.5          # 能保音变速者优先（同分情况下）
    return score


def describe_backends() -> List[dict]:
    """给界面用的后端清单（含可用性与原因）。"""
    out = []
    for cls in CANDIDATES:
        available = False
        reason = ""
        try:
            available = cls.is_available()
            if available:
                reason = "可用"
            elif cls is FFmpegCLIBackend:
                reason = "未检测到 ffmpeg.exe（把它放到 PATH 或 tools/ffmpeg/bin/ 即可启用）"
            elif cls is PyAVBackend:
                reason = "未安装 PyAV（python -m pip install av）"
            else:
                reason = "兜底源，始终可用"
        except Exception as exc:  # 探测本身不应崩
            reason = "探测异常: %s" % exc
        out.append({
            "name": cls.name,
            "display_name": cls.display_name,
            "available": available,
            "reason": reason,
            "native_tempo": cls.supports_native_tempo,
        })
    return out


def pick_backend_class(path: str = "", prefer: Optional[str] = None) -> Type[DecodeBackend]:
    """挑一个能打开 path 的后端类。

    :param prefer: 指定后端名（"ffmpeg-cli" / "pyav" / "synthetic"），失败则报错而不是兜底。
    """
    if prefer:
        for cls in CANDIDATES:
            if cls.name == prefer or cls.display_name == prefer:
                if cls.is_available():
                    return cls
                raise DecodeError("指定的后端不可用: %s" % prefer)
        raise DecodeError("未知后端: %s" % prefer)

    if (not path) or path.startswith(SYNTHETIC_SCHEME) or path == "demo":
        return SyntheticBackend

    for cls in CANDIDATES:
        try:
            if cls.can_open(path):
                return cls
        except Exception as exc:
            log.debug("后端 %s 探测失败: %s", cls.name, exc)

    raise DecodeError(
        "没有可用的解码后端，无法播放该文件。\n"
        "解决方案（任选其一）：\n"
        "  1) 把 ffmpeg.exe + ffprobe.exe 放到项目 tools/ffmpeg/bin/ 或加入 PATH；\n"
        "  2) 安装 PyAV：python -m pip install av；\n"
        "  3) 用菜单「文件 -> 打开合成演示源」体验纯 Python 合成源的完整管线。"
    )


def auto_backend(path: str = "", prefer: Optional[str] = None) -> DecodeBackend:
    """创建（但还没 open）的后端实例，按 path 自动选择。"""
    cls = pick_backend_class(path, prefer)
    log.info("选择后端: %s (%s)", cls.display_name, cls.name)
    instance = cls()
    return instance


def open_backend(path: str = "", prefer: Optional[str] = None) -> DecodeBackend:
    """选择后端并立即 open。

    真实文件走的流程：**逐个候选后端真的打开一次**，按 capability_score 挑最强的。
    这样即便 ffmpeg.exe 存在但是个缺斤少两的构建，也会自动让位给能干活的那个。
    """
    if (not path) or path.startswith(SYNTHETIC_SCHEME) or path == "demo":
        backend = SyntheticBackend()
        backend.open(path or (SYNTHETIC_SCHEME + "demo"))
        return backend

    if not os.path.isfile(path):
        raise DecodeError("文件不存在: %s" % path)

    if prefer:                       # 用户指名了后端，就老实照做
        backend = auto_backend(path, prefer)
        backend.open(path)
        return backend

    best: Optional[DecodeBackend] = None
    best_score = -1.0
    failures = []
    for cls in REAL_CANDIDATES:
        try:
            if not cls.can_open(path):
                continue
        except Exception as exc:
            log.debug("后端 %s 探测失败: %s", cls.name, exc)
            continue
        candidate = cls()
        try:
            candidate.open(path)
        except Exception as exc:
            failures.append("- %s：%s" % (cls.display_name, exc))
            candidate.close()
            continue
        score = capability_score(candidate)
        note = getattr(candidate.info, "notes", "")
        log.info("后端 %s 打开成功，能力评分 %.1f%s",
                 cls.display_name, score, ("（%s）" % note) if note else "")
        if score > best_score:
            if best is not None:
                best.close()
            best, best_score = candidate, score
        else:
            candidate.close()

    if best is None:
        detail = "\n".join(failures) if failures else "（没有候选后端被尝试）"
        raise DecodeError(
            "没有后端能解码该文件：%s\n%s\n解决办法：\n"
            "  1) 换一个完整的 ffmpeg.exe 放到 tools/ffmpeg/bin/；\n"
            "  2) 安装 PyAV：python -m pip install av（自带完整 FFmpeg 解码库）；\n"
            "  3) 菜单「文件 -> 打开合成演示源」可以体验纯 Python 合成源版本的完整管线。"
            % (path, detail))
    return best
