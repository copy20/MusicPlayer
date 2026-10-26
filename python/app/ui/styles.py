"""深色主题（QSS）。"""

DARK_QSS = """
QWidget { background: #14151a; color: #d8dbe3; font-family: "Microsoft YaHei", "Segoe UI", sans-serif; }
QMainWindow::separator { background: #26282f; width: 1px; }
QMenuBar { background: #1b1c22; border-bottom: 1px solid #2a2c34; }
QMenuBar::item:selected { background: #2f323b; }
QMenu { background: #1b1c22; border: 1px solid #2f323b; }
QMenu::item:selected { background: #31363f; }
QToolBar { background: #1b1c22; border: none; spacing: 4px; padding: 4px; }
QToolButton { padding: 4px 8px; border-radius: 4px; }
QToolButton:hover { background: #2c2f37; }
QStatusBar { background: #1b1c22; color: #9aa2b1; }
QStatusBar QLabel { padding: 0 6px; }
QSlider::groove:horizontal { height: 4px; background: #33363f; border-radius: 2px; }
QSlider::handle:horizontal { width: 12px; margin: -5px 0; border-radius: 6px; background: #4c9ffe; }
QSlider::sub-page:horizontal { background: #4c9ffe; border-radius: 2px; }
QComboBox { background: #22242b; border: 1px solid #33363f; padding: 3px 8px; border-radius: 4px; }
QComboBox::drop-down { border: none; }
QComboBox QAbstractItemView { background: #22242b; selection-background-color: #31363f; border: 1px solid #33363f; }
QCheckBox { spacing: 6px; }
QPushButton { background: #22242b; border: 1px solid #33363f; border-radius: 4px; padding: 4px 10px; }
QPushButton:hover { background: #2c2f37; }
QPushButton:checked { background: #31465f; border-color: #4c9ffe; }
QGroupBox { border: 1px solid #2a2c34; margin-top: 12px; border-radius: 4px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #9aa2b1; }
QTreeWidget { background: #17181d; alternate-background-color: #1b1c22; border: none; }
QHeaderView::section { background: #1f2128; border: none; padding: 4px; color: #9aa2b1; }
QLabel#hint { color: #7d8493; }
QToolTip { background: #22242b; color: #d8dbe3; border: 1px solid #33363f; }
"""
