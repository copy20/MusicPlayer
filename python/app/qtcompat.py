"""Qt 绑定兼容层。

项目的重点在多线程、缓存队列与变速播放，UI 不应该和某个具体绑定绑死，
因此这里统一导出一份"够用"的 API：PyQt5 -> PySide6 -> PyQt6 依次降级。

可用命令行 `QT_API=PySide6 python main.py` 强制指定。
"""
from __future__ import annotations

import os

__all__ = [
    "QtCore", "QtGui", "QtWidgets", "QtMultimedia", "Qt",
    "Signal", "Slot", "QT_BINDING", "QT_VERSION",
    "exec_app", "audio_output_class", "AudioOutput", "QAudioFormat",
]


def _load_pyqt5():
    from PyQt5 import QtCore, QtGui, QtWidgets
    try:
        from PyQt5 import QtMultimedia
    except ImportError:  # 极少数裁剪版 wheel 不带 QtMultimedia
        QtMultimedia = None
    return dict(
        QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets, QtMultimedia=QtMultimedia,
        Signal=QtCore.pyqtSignal, Slot=QtCore.pyqtSlot,
        binding="PyQt5", version=QtCore.QT_VERSION_STR,
    )


def _load_pyside6():
    from PySide6 import QtCore, QtGui, QtWidgets
    try:
        from PySide6 import QtMultimedia
    except ImportError:
        QtMultimedia = None
    return dict(
        QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets, QtMultimedia=QtMultimedia,
        Signal=QtCore.Signal, Slot=QtCore.Slot,
        binding="PySide6", version=QtCore.qVersion(),
    )


def _load_pyqt6():
    from PyQt6 import QtCore, QtGui, QtWidgets
    try:
        from PyQt6 import QtMultimedia
    except ImportError:
        QtMultimedia = None
    return dict(
        QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets, QtMultimedia=QtMultimedia,
        Signal=QtCore.pyqtSignal, Slot=QtCore.pyqtSlot,
        binding="PyQt6", version=QtCore.QT_VERSION_STR,
    )


_LOADERS = {"pyqt5": _load_pyqt5, "pyside6": _load_pyside6, "pyqt6": _load_pyqt6}

_ORDER_BY_PREF = {
    "pyqt5": ["pyqt5", "pyside6", "pyqt6"],
    "pyside6": ["pyside6", "pyqt5", "pyqt6"],
    "pyqt6": ["pyqt6", "pyqt5", "pyside6"],
}


def _load():
    pref = (os.environ.get("QT_API") or "").lower()
    if pref in ("5", "pyqt5"):
        pref = "pyqt5"
    elif pref in ("6", "pyside6"):
        pref = "pyside6"
    errors = []
    for name in _ORDER_BY_PREF.get(pref, ["pyqt5", "pyside6", "pyqt6"]):
        try:
            return _LOADERS[name]()
        except ImportError as exc:  # 换下一个
            errors.append("%s: %s" % (name, exc))
    raise ImportError(
        "未找到可用的 Qt 绑定（已尝试 %s）。请执行：python -m pip install PyQt5\n%s"
        % (", ".join(_LOADERS), "\n".join(errors))
    )


_loaded = _load()

QtCore = _loaded["QtCore"]
QtGui = _loaded["QtGui"]
QtWidgets = _loaded["QtWidgets"]
QtMultimedia = _loaded["QtMultimedia"]
Signal = _loaded["Signal"]
Slot = _loaded["Slot"]
QT_BINDING = _loaded["binding"]
QT_VERSION = _loaded["version"]
Qt = QtCore.Qt


def exec_app(app):
    """兼容 Qt5 的 exec_ / Qt6 的 exec。"""
    exec_fn = getattr(app, "exec", None)
    if callable(exec_fn):
        return exec_fn()
    return app.exec_()  # Qt5


def audio_output_class():
    """返回 Qt 的"推模式"音频输出类：Qt6 叫 QAudioSink，Qt5 叫 QAudioOutput。"""
    if QtMultimedia is None:
        raise ImportError(
            "当前 Qt 绑定(%s) 不含 QtMultimedia 模块，无法播放声音。"
            "界面仍可正常使用（静音模式）。" % QT_BINDING
        )
    for name in ("QAudioSink", "QAudioOutput"):
        cls = getattr(QtMultimedia, name, None)
        if cls is not None:
            return cls
    raise ImportError("QtMultimedia 中未找到 QAudioSink / QAudioOutput")


try:
    AudioOutput = audio_output_class()
except ImportError:  # 允许无音频设备环境（如 offscreen 自测）下继续运行界面
    AudioOutput = None

QAudioFormat = QtMultimedia.QAudioFormat if QtMultimedia is not None else None
