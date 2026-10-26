"""界面层：只负责画和人机交互，不参与任何解码 / 时序逻辑。"""
from app.ui.main_window import MainWindow
from app.ui.video_widget import VideoWidget

__all__ = ["MainWindow", "VideoWidget"]
