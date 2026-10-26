"""MiniPlayer 入口。

用法：
    python main.py                 # 打开界面，可再拖入文件 / 打开合成演示源
    python main.py 视频文件.mp4      # 直接播放指定文件
    python main.py synthetic://demo # 使用内置合成演示源
"""
from __future__ import annotations

import logging
import os
import sys


def _ensure_importable() -> None:
    root = os.path.dirname(os.path.abspath(__file__))
    if root not in sys.path:
        sys.path.insert(0, root)


def _setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)-16s | %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _ensure_importable()
    _setup_logging(logging.DEBUG if "--debug" in argv else logging.INFO)
    if "--debug" in argv:
        argv.remove("--debug")

    from app.qtcompat import QtCore, QtWidgets, exec_app
    from app.ui.main_window import MainWindow

    # Qt6 默认开启高分屏缩放；Qt5 需要下面这行（Qt5 上无效也不会报错）
    if hasattr(QtCore.Qt, "AA_EnableHighDpiScaling"):
        QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)

    app = QtWidgets.QApplication([sys.argv[0]] + argv)
    app.setApplicationName("MiniPlayer")
    app.setOrganizationName("MiniPlayer")

    window = MainWindow()
    window.show()

    if argv:
        target = argv[0]
        QtCore.QTimer.singleShot(200, lambda: window.open_path(target))

    return exec_app(app)


if __name__ == "__main__":
    sys.exit(main())
