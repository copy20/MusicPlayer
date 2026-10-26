"""画面渲染控件。

职责极简：把来自渲染线程的 QImage 画到 QWidget 上（保持宽高比的黑边)。

几个容易被忽略的点：
* 只在 paintEvent 里画，任何地方都不直接碰 QPainter；
* 控件尺寸变化时重新计算 letterbox 矩形，而不是拉伸变形；
* 收到 None 表示"清空画面"（stop / 打开新片前）。
"""
from __future__ import annotations

from app.qtcompat import Qt, QtCore, QtGui, QtWidgets, Signal


class VideoWidget(QtWidgets.QWidget):
    """轻量视频画布：QImage + QPainter，不依赖 OpenGL，兼容性最好。"""

    double_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.StrongFocus)

        self._image = None
        self._target = QtCore.QRect()
        self._show_hint = True
        self._hint_text = "拖入视频文件，或点击「文件 -> 打开合成演示源」"

    # ------------------------------------------------------------ 对外
    def set_frame(self, image) -> None:
        """主线程调用（由跨线程 Signal 投递）。image 为 None 表示清空。"""
        if isinstance(image, QtGui.QImage) and image.isNull():
            image = None
        self._image = image
        self._show_hint = image is None
        self.update()

    def clear_frame(self) -> None:
        self._image = None
        self.update()

    def set_hint(self, text: str) -> None:
        self._hint_text = text
        self.update()

    # ------------------------------------------------------------ 绘制
    def _recalc_target(self) -> None:
        if self._image is None:
            return
        iw, ih = self._image.width(), self._image.height()
        if iw <= 0 or ih <= 0:
            return
        scaled = self._image.size()
        scaled.scale(self.size(), Qt.KeepAspectRatio)
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        self._target = QtCore.QRect(QtCore.QPoint(x, y), scaled)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QtGui.QPainter(self)
        painter.fillRect(event.rect(), QtGui.QColor(12, 12, 16))
        if self._image is None or self._image.isNull():
            if self._show_hint:
                painter.setPen(QtGui.QColor(150, 150, 165))
                font = painter.font()
                font.setPointSize(10)
                painter.setFont(font)
                painter.drawText(self.rect(), Qt.AlignCenter, self._hint_text)
            return
        if self._target.isEmpty() or self._target.size() != self._image.size().scaled(
                self.size(), Qt.KeepAspectRatio):
            self._recalc_target()
        painter.drawImage(self._target, self._image)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._target = QtCore.QRect()
        self.update()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)
