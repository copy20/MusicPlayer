"""线程基类。

用 QThread 而不是 threading.Thread 的理由：需要 Signal 把结果安全投递到 GUI 线程。
但**不要在里面起事件循环**——工作线程都是纯循环，避免和 Qt 的事件队列纠缠。
"""
from __future__ import annotations

import logging
import threading

from app.qtcompat import QtCore, Signal

log = logging.getLogger("core.workers")


class StoppableThread(QtCore.QThread):
    """可被"软停止"的工作线程。

    停止有两个层次：
    1. ``request_stop()`` 置标志 -> 循环自己退出（正常路径）；
    2. ``QThread.quit()/terminate()`` 兜底（异常路径）。

    线程里的所有等待都通过 ``sleep()``（内部是 Event.wait），
    因此停止请求可以立刻生效，而不是傻等到超时。
    """

    error_occurred = Signal(str)     # 非预期异常
    finished_ok = Signal(str)

    def __init__(self, name: str = "worker", parent=None):
        super().__init__(parent)
        self.setObjectName(name)
        self._stop = threading.Event()

    # ------------------------------------------------------------ 状态
    def request_stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def is_running(self) -> bool:
        return self.isRunning() and not self._stop.is_set()

    def sleep(self, seconds: float) -> bool:
        """可中断睡眠；返回 True 表示被请求停止。"""
        return self._stop.wait(max(0.0, seconds))

    # ------------------------------------------------------------ 生命周期
    def stop_and_wait(self, timeout_ms: int = 3000) -> None:
        self.request_stop()
        self.wait(timeout_ms)
        if self.isRunning():
            log.warning("[%s] 未能优雅退出，强制 terminate", self.objectName())
            self.terminate()
            self.wait(1000)

    def _describe(self) -> str:
        return self.objectName()
