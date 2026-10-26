#include "ui/style.h"

QString darkStyleSheet()
{
    return QStringLiteral(R"(
QWidget {
    background: #12141a;
    color: #dfe4ec;
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 13px;
}
QMenuBar {
    background: #171a22;
    border-bottom: 1px solid #23272f;
}
QMenuBar::item { padding: 6px 12px; background: transparent; }
QMenuBar::item:selected { background: #262b36; border-radius: 4px; }
QMenu {
    background: #171a22;
    border: 1px solid #2b303a;
    padding: 4px;
}
QMenu::item { padding: 6px 22px; border-radius: 4px; }
QMenu::item:selected { background: #2f6fd0; }
QMenu::separator { height: 1px; background: #2b303a; margin: 4px 8px; }

QPushButton {
    background: #232833;
    border: 1px solid #2e3542;
    border-radius: 6px;
    padding: 6px 14px;
    min-width: 64px;
}
QPushButton:hover { background: #2b3240; }
QPushButton:pressed { background: #1d222b; }
QPushButton:disabled { color: #6b7280; background: #1b1f27; }

QSlider::groove:horizontal {
    height: 5px;
    background: #2a303c;
    border-radius: 3px;
}
QSlider::sub-page:horizontal { background: #4b8bf4; border-radius: 3px; }
QSlider::handle:horizontal {
    width: 13px; height: 13px; margin: -5px 0;
    background: #e8eefc; border-radius: 7px;
}
QSlider::handle:horizontal:hover { background: #ffffff; }

QComboBox {
    background: #232833;
    border: 1px solid #2e3542;
    border-radius: 6px;
    padding: 4px 10px;
    min-width: 72px;
}
QComboBox QAbstractItemView {
    background: #171a22;
    border: 1px solid #2b303a;
    selection-background-color: #2f6fd0;
}

QStatusBar { background: #171a22; border-top: 1px solid #23272f; }
QStatusBar::item { border: none; }
QSplitter::handle { background: #23272f; }
QTreeWidget {
    background: #10131a;
    border: 1px solid #23272f;
    border-radius: 6px;
    alternate-background-color: #131720;
}
QTreeWidget::item { padding: 3px 2px; }
QHeaderView::section {
    background: #1a1e27;
    color: #a9b2c2;
    border: none;
    padding: 5px;
}
QLabel#timeLabel { color: #aab3c3; font-size: 12px; }
QLabel#clockLabel { color: #7f8b9e; font-size: 11px; }
QWidget#controlBar {
    background: #171a22;
    border-top: 1px solid #23272f;
}
)");
}
