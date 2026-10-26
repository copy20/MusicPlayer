"""主窗口：菜单 / 控制条 / 状态栏 / 运行时监控面板。

界面与内核之间只有三条通道：

    UI ──调用──► PlayerController 的方法（play / seek / set_speed …）
    UI ──监听──► PlayerController 的信号（state / position / frame_ready …）
    UI ──定时──► player.collect_stats()（监控面板用，200ms 一次）

UI 线程里没有任何阻塞式 IO，也没有任何 sleep；画面数据通过
跨线程信号（自动 QueuedConnection）投递过来。
"""
from __future__ import annotations

import logging
import os

from app.core import config
from app.core.clock import format_time
from app.core.player import STATE_TEXT, PlayerController, PlayerState
from app.qtcompat import Qt, QtCore, QtGui, QtWidgets, Signal
from app.ui.styles import DARK_QSS
from app.ui.video_widget import VideoWidget

log = logging.getLogger("ui.main")

SLIDER_MAX = 1000
SEEK_STEP = 5.0


class MainWindow(QtWidgets.QMainWindow):
    """播放器主窗口。"""

    request_stats = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MiniPlayer - 多线程视频播放器")
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setAcceptDrops(True)

        self.player = PlayerController(self)
        self.video_widget = VideoWidget()
        self.video_widget.double_clicked.connect(self.toggle_fullscreen)
        self.video_widget.installEventFilter(self)

        self._slider_pressed = False
        self._preferred_backend = None
        self._last_stats: dict = {}
        self._settings = QtCore.QSettings("MiniPlayer", "MiniPlayer")

        container = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.video_widget, 1)
        layout.addWidget(self._build_control_bar(), 0)
        self.setCentralWidget(container)

        self._build_actions()
        self._build_menu()
        self._build_shortcuts()
        self._build_monitor()
        self._build_statusbar()
        self._connect_player()

        self.setStyleSheet(DARK_QSS)
        self.resize(1060, 680)

        self._ui_timer = QtCore.QTimer(self)
        self._ui_timer.setInterval(200)
        self._ui_timer.timeout.connect(self._refresh_ui)
        self._ui_timer.start()

        self._restore_settings()
        self._refresh_backend_hint()

    # ==================================================================== 构建
    def _build_control_bar(self) -> QtWidgets.QWidget:
        bar = QtWidgets.QWidget()
        bar.setMaximumHeight(96)
        v = QtWidgets.QVBoxLayout(bar)
        v.setContentsMargins(12, 6, 12, 8)
        v.setSpacing(6)

        # ---------- 进度
        seek_row = QtWidgets.QHBoxLayout()
        self.time_label = QtWidgets.QLabel("00:00 / 00:00")
        self.time_label.setMinimumWidth(110)
        self.time_label.setAlignment(Qt.AlignCenter)
        self.slider = QtWidgets.QSlider(Qt.Horizontal)
        self.slider.setRange(0, SLIDER_MAX)
        self.slider.sliderPressed.connect(self._on_slider_pressed)
        self.slider.sliderReleased.connect(self._on_slider_released)
        seek_row.addWidget(self.time_label)
        seek_row.addWidget(self.slider, 1)
        v.addLayout(seek_row)

        # ---------- 按钮
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setSpacing(8)

        self.btn_backward = QtWidgets.QPushButton("⏪ 5s")
        self.btn_backward.clicked.connect(lambda: self.player.seek_relative(-SEEK_STEP))
        self.btn_play = QtWidgets.QPushButton("▶ 播放")
        self.btn_play.clicked.connect(self.player.toggle_play)
        self.btn_stop = QtWidgets.QPushButton("■ 停止")
        self.btn_stop.clicked.connect(self.player.stop)
        self.btn_forward = QtWidgets.QPushButton("⏩ 5s")
        self.btn_forward.clicked.connect(lambda: self.player.seek_relative(SEEK_STEP))

        for b in (self.btn_backward, self.btn_play, self.btn_stop, self.btn_forward):
            b.setMinimumWidth(78)
            btn_row.addWidget(b)

        btn_row.addSpacing(16)
        btn_row.addWidget(QtWidgets.QLabel("倍速"))
        self.speed_combo = QtWidgets.QComboBox()
        for s in config.SPEED_PRESETS:
            self.speed_combo.addItem("%.2fx" % s, s)
        self.speed_combo.setCurrentIndex(config.SPEED_PRESETS.index(1.0)
                                         if 1.0 in config.SPEED_PRESETS else 0)
        self.speed_combo.currentIndexChanged.connect(self._on_speed_combo)
        btn_row.addWidget(self.speed_combo)

        self.btn_speed_down = QtWidgets.QPushButton("−")
        self.btn_speed_down.setMaximumWidth(30)
        self.btn_speed_down.clicked.connect(lambda: self._step_speed(-0.25))
        self.btn_speed_up = QtWidgets.QPushButton("+")
        self.btn_speed_up.setMaximumWidth(30)
        self.btn_speed_up.clicked.connect(lambda: self._step_speed(0.25))
        btn_row.addWidget(self.btn_speed_down)
        btn_row.addWidget(self.btn_speed_up)

        self.chk_loop = QtWidgets.QCheckBox("循环")
        self.chk_loop.stateChanged.connect(lambda s: self.player.set_loop(bool(s)))
        btn_row.addWidget(self.chk_loop)

        btn_row.addStretch(1)

        self.btn_mute = QtWidgets.QPushButton("🔊")
        self.btn_mute.setCheckable(True)
        self.btn_mute.setMaximumWidth(40)
        self.btn_mute.toggled.connect(lambda m: self.player.set_muted(m))
        self.volume = QtWidgets.QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setMaximumWidth(110)
        self.volume.valueChanged.connect(lambda v: self.player.set_volume(v / 100.0))
        btn_row.addWidget(self.btn_mute)
        btn_row.addWidget(self.volume)

        self.btn_fullscreen = QtWidgets.QPushButton("全屏")
        self.btn_fullscreen.setMaximumWidth(60)
        self.btn_fullscreen.clicked.connect(self.toggle_fullscreen)
        btn_row.addWidget(self.btn_fullscreen)

        v.addLayout(btn_row)
        return bar

    # ---------------------------------------------------------------- 动作
    def _mk_action(self, text, shortcut, callback, checkable=False, checked=False):
        act = QtWidgets.QAction(text, self)
        if shortcut:
            act.setShortcut(shortcut)
        if checkable:
            act.setCheckable(True)
            act.setChecked(checked)
        act.triggered.connect(callback) if callback is not None else None
        return act

    def _build_actions(self) -> None:
        self.act_open = self._mk_action("打开…(&O)", QtGui.QKeySequence.Open, self.open_file)
        self.act_open_demo = self._mk_action("打开合成演示源(&D)", "Ctrl+Shift+D", self.open_demo)
        self.act_quit = self._mk_action("退出(&Q)", "Ctrl+Q", self.close)

        self.act_play = self._mk_action("播放/暂停", "Space", self.player.toggle_play)
        self.act_stop = self._mk_action("停止", "S", self.player.stop)
        self.act_backward = self._mk_action("后退 5 秒", "Left", lambda: self.player.seek_relative(-SEEK_STEP))
        self.act_forward = self._mk_action("前进 5 秒", "Right", lambda: self.player.seek_relative(SEEK_STEP))

        self.act_slower = self._mk_action("减速 0.25x", "Ctrl+Down", lambda: self._step_speed(-0.25))
        self.act_faster = self._mk_action("加速 0.25x", "Ctrl+Up", lambda: self._step_speed(0.25))

        self.act_loop = self._mk_action("循环播放", "Ctrl+L", lambda c: self.player.set_loop(c),
                                        checkable=True)
        self.act_fullscreen = self._mk_action("全屏", "F", self.toggle_fullscreen)
        self.act_monitor = self._mk_action("运行时监控面板", "Ctrl+I", None, checkable=True, checked=True)
        self.act_about = self._mk_action("关于 / 后端信息", "F1", self.show_about)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("文件(&F)")
        file_menu.addAction(self.act_open)
        file_menu.addAction(self.act_open_demo)
        file_menu.addSeparator()
        file_menu.addAction(self.act_quit)

        play_menu = self.menuBar().addMenu("播放(&P)")
        play_menu.addAction(self.act_play)
        play_menu.addAction(self.act_stop)
        play_menu.addSeparator()
        play_menu.addAction(self.act_backward)
        play_menu.addAction(self.act_forward)
        play_menu.addSeparator()
        play_menu.addAction(self.act_slower)
        play_menu.addAction(self.act_faster)
        play_menu.addAction(self.act_loop)

        # ---- 倍速快捷菜单
        speed_menu = play_menu.addMenu("倍速")
        self.speed_actions = {}
        self.speed_group = QtWidgets.QActionGroup(self)
        for s in config.SPEED_PRESETS:
            act = self._mk_action("%.2fx" % s, "", lambda c=False, v=s: self.player.set_speed(v),
                                  checkable=True)
            act.setChecked(abs(s - 1.0) < 1e-6)
            self.speed_group.addAction(act)
            speed_menu.addAction(act)
            self.speed_actions[s] = act

        backend_menu = play_menu.addMenu("解码后端")
        self.backend_actions = {}
        self.backend_group = QtWidgets.QActionGroup(self)
        self._add_backend_action(backend_menu, "自动选择", None, True)
        for info in self.player.available_backends():
            self._add_backend_action(backend_menu, info["display_name"], info["name"], False)

        view_menu = self.menuBar().addMenu("视图(&V)")
        view_menu.addAction(self.act_fullscreen)
        view_menu.addAction(self.act_monitor)

        help_menu = self.menuBar().addMenu("帮助(&H)")
        help_menu.addAction(self.act_about)

    def _add_backend_action(self, menu, text, name, checked):
        act = self._mk_action(text, "", lambda c=False, n=name: self._set_preferred_backend(n),
                              checkable=True, checked=checked)
        menu.addAction(act)
        self.backend_group.addAction(act)
        self.backend_actions[name] = act

    def _build_shortcuts(self) -> None:
        # 全局快捷键放在 QShortcut 上，避免被子控件吞掉
        for key, callback in (
            ("Space", self.player.toggle_play),
            ("Left", lambda: self.player.seek_relative(-SEEK_STEP)),
            ("Right", lambda: self.player.seek_relative(SEEK_STEP)),
            ("Up", lambda: self._step_volume(5)),
            ("Down", lambda: self._step_volume(-5)),
            ("M", lambda: self.btn_mute.toggle()),
            ("F", self.toggle_fullscreen),
            ("Ctrl+Up", lambda: self._step_speed(0.25)),
            ("Ctrl+Down", lambda: self._step_speed(-0.25)),
            ("Escape", self._exit_fullscreen),
        ):
            QtWidgets.QShortcut(QtGui.QKeySequence(key), self).activated.connect(callback)

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        self.status_info = QtWidgets.QLabel("就绪")
        self.status_backend = QtWidgets.QLabel("-")
        self.status_queue = QtWidgets.QLabel("-")
        self.status_sync = QtWidgets.QLabel("-")
        sb.addWidget(self.status_info, 1)
        sb.addPermanentWidget(self.status_backend)
        sb.addPermanentWidget(self.status_queue)
        sb.addPermanentWidget(self.status_sync)
        self.act_monitor.triggered.connect(self.monitor_dock.setVisible)
        self.monitor_dock.visibilityChanged.connect(self.act_monitor.setChecked)

    def _build_monitor(self) -> None:
        self.monitor = QtWidgets.QTreeWidget()
        self.monitor.setHeaderLabels(["监控项", "当前值"])
        self.monitor.setRootIsDecorated(True)
        self.monitor.setAlternatingRowColors(True)
        self.monitor.setColumnWidth(0, 180)
        self.monitor_dock = QtWidgets.QDockWidget("运行时监控", self)
        self.monitor_dock.setWidget(self.monitor)
        self.monitor_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.addDockWidget(Qt.RightDockWidgetArea, self.monitor_dock)
        self.resizeDocks([self.monitor_dock], [260], Qt.Horizontal)

    # ==================================================================== 信号连接
    def _connect_player(self) -> None:
        p = self.player
        p.state_changed.connect(self._on_state)
        p.media_changed.connect(self._on_media_changed)
        p.frame_ready.connect(self.video_widget.set_frame)
        p.buffering_changed.connect(self._on_buffering)
        p.speed_changed.connect(self._on_speed_changed)
        p.error_occurred.connect(self._on_error)
        p.playback_finished.connect(self._on_finished)

    # ==================================================================== 槽
    def _on_state(self, _state: int) -> None:
        self._refresh_ui()

    def _on_media_changed(self, info) -> None:
        title = getattr(info, "title", "") or "-"
        self.setWindowTitle("%s - MiniPlayer" % title)
        self.status_backend.setText("%s | %dx%d@%.2f" % (
            info.backend, info.width, info.height, info.fps))
        self.time_label.setText("00:00 / %s" % format_time(info.duration))
        notes = getattr(info, "notes", "")
        self.status_backend.setToolTip(notes or self.status_backend.text())
        if notes:                       # 例如"极简 ffmpeg 缺少音频解码器"这类降级提示
            self.status_info.setText(notes)
            QtCore.QTimer.singleShot(6000, lambda: self.status_info.setText(self.player.state_text))

    def _on_buffering(self, flag: bool) -> None:
        if flag:
            self.status_info.setText("缓冲中…")

    def _on_speed_changed(self, speed: float) -> None:
        for value, act in self.speed_actions.items():
            act.setChecked(abs(value - speed) < 1e-6)
        idx = self.speed_combo.findData(round(speed, 2))
        if idx < 0:
            self.speed_combo.addItem("%.2fx" % speed, speed)
            idx = self.speed_combo.count() - 1
        self.speed_combo.blockSignals(True)
        self.speed_combo.setCurrentIndex(idx)
        self.speed_combo.blockSignals(False)

    def _on_error(self, message: str) -> None:
        log.error("播放器错误: %s", message)
        QtWidgets.QMessageBox.warning(self, "无法播放", message)

    def _on_finished(self) -> None:
        self.status_info.setText("播放结束")

    # ---------------------------------------------------------------- 控件回调
    def _on_slider_pressed(self) -> None:
        self._slider_pressed = True

    def _on_slider_released(self) -> None:
        self._slider_pressed = False
        duration = self.player.duration
        if duration > 0:
            self.player.seek(self.slider.value() / float(SLIDER_MAX) * duration)

    def _on_speed_combo(self) -> None:
        data = self.speed_combo.currentData()
        if data is not None:
            self.player.set_speed(float(data))

    # ==================================================================== 动作实现
    def open_file(self) -> None:
        last_dir = self._settings.value("last_dir", "", type=str)
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择媒体文件", last_dir,
            "媒体文件 (*.mp4 *.mkv *.avi *.mov *.flv *.webm *.mpg *.ts *.mp3 *.wav *.m4a *.aac);;"
            "所有文件 (*.*)")
        if not path:
            return
        self._settings.setValue("last_dir", os.path.dirname(path))
        self.open_path(path)

    def open_demo(self) -> None:
        self.open_path("synthetic://demo")

    def open_path(self, path: str) -> None:
        self.video_widget.set_frame(None)
        if not self.player.open(path, autoplay=True, prefer_backend=self._preferred_backend):
            self.status_info.setText("打开失败: %s" % os.path.basename(path))

    def _step_speed(self, delta: float) -> None:
        self.player.set_speed(round(self.player.speed + delta, 2))

    def _step_volume(self, delta: int) -> None:
        self.volume.setValue(max(0, min(100, self.volume.value() + delta)))

    def _set_preferred_backend(self, name) -> None:
        self._preferred_backend = name

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self._exit_fullscreen()
        else:
            self.ad_monitor_was_visible = self.monitor_dock.isVisible()
            self.monitor_dock.hide()
            self.showFullScreen()

    def _exit_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            if getattr(self, "ad_monitor_was_visible", False):
                self.monitor_dock.show()

    def show_about(self) -> None:
        lines = ["<b>MiniPlayer</b> 多线程视频播放器",
                 "<br>Qt 绑定: PyQt5/6 或 PySide6 均可（运行时自动选择）",
                 "<br><br><b>解码后端</b>"]
        for info in self.player.available_backends():
            flag = "可用" if info["available"] else "不可用"
            lines.append("&nbsp;• %s — <i>%s</i>（变速=%s）<br>&nbsp;&nbsp;%s" % (
                info["display_name"], flag,
                "atempo 真变速" if info["native_tempo"] else "CPU 侧 OLA",
                info["reason"]))
        lines.append("<br>把 ffmpeg.exe / ffprobe.exe 放到项目 <code>tools/ffmpeg/bin/</code> "
                     "即可启用真实文件解码。")
        QtWidgets.QMessageBox.information(self, "关于 MiniPlayer", "<br>".join(lines))

    # ==================================================================== 刷新
    def _refresh_ui(self) -> None:
        pos = self.player.position()
        duration = self.player.duration
        stats = self.player.collect_stats()
        if not self._slider_pressed:
            ratio = (pos / duration) if duration > 0 else 0.0
            self.slider.blockSignals(True)
            self.slider.setValue(int(ratio * SLIDER_MAX))
            self.slider.blockSignals(False)
        self.time_label.setText("%s / %s" % (format_time(pos), format_time(duration)))

        state = self.player.state
        playing = state == PlayerState.PLAYING
        self.btn_play.setText("⏸ 暂停" if playing else "▶ 播放")
        self.btn_play.setEnabled(self.player.has_media)

        self.btn_stop.setEnabled(self.player.has_media)
        self.slider.setEnabled(self.player.has_media and duration > 0)

        label = STATE_TEXT.get(state, "")
        if stats.get("buffering"):
            label = "缓冲中…"
        elif self.player.speed != 1.0 and playing:
            label += " (%.2fx)" % self.player.speed
        self.status_info.setText(label)

        vq = stats.get("video_queue") or {}
        aq = stats.get("audio_queue") or {}
        self.status_queue.setText(
            "队列 V %s/%s(%ss) A %s/%s(%ss)" % (
                vq.get("size", 0), vq.get("max_size", 0), round(vq.get("buffered_sec", 0.0), 2),
                aq.get("size", 0), aq.get("max_size", 0), round(aq.get("buffered_sec", 0.0), 2)))
        diff = stats.get("av_diff")
        self.status_sync.setText("%s同步 %s" % (
            stats.get("sync_master", "-"),
            ("偏差 %+dms" % int(diff * 1000)) if diff is not None else "—"))

        if self.monitor_dock.isVisible():
            self._update_monitor(stats)

    def _update_monitor(self, stats: dict) -> None:
        self.monitor.clear()

        def add(parent, key, value):
            item = QtWidgets.QTreeWidgetItem(parent, [str(key), str(value)])
            parent.addChild(item)

        root_play = QtWidgets.QTreeWidgetItem(self.monitor, ["播放", ""])
        add(root_play, "状态", stats.get("state", "-"))
        add(root_play, "位置", "%.2f / %.2f s" % (stats.get("position", 0), stats.get("duration", 0)))
        add(root_play, "媒体时钟", "%.3f s" % stats.get("clock_time", 0))
        add(root_play, "倍速", "%.2fx" % stats.get("speed", 1))
        add(root_play, "变速实现", stats.get("speed_tool", "-"))
        add(root_play, "同步主时钟", stats.get("sync_master", "-"))
        diff = stats.get("av_diff")
        add(root_play, "音画偏差", ("%+d ms" % int(diff * 1000)) if diff is not None else "—")

        self.monitor.addTopLevelItem(root_play)

        for key, title in (("video_queue", "视频缓存队列"), ("audio_queue", "音频缓存队列")):
            q = stats.get(key) or {}
            node = QtWidgets.QTreeWidgetItem(self.monitor, [title, ""])
            add(node, "帧数", "%d / %d" % (q.get("size", 0), q.get("max_size", 0)))
            add(node, "缓存时长", "%.3f s" % q.get("buffered_sec", 0.0))
            add(node, "队首/队尾 pts", "%s / %s" % (
                _fmt(q.get("head_pts")), _fmt(q.get("tail_pts"))))
            add(node, "写入 / 取出", "%d / %d" % (q.get("put", 0), q.get("get", 0)))
            add(node, "生产者丢帧", q.get("dropped_producer", 0))
            add(node, "迟到丢帧", q.get("dropped_consumer", 0))
            add(node, "空队列次数", q.get("starve", 0))
            add(node, "峰值帧数", q.get("peak", 0))
            add(node, "generation", q.get("serial", 0))
            add(node, "EOS", "是" if q.get("eos") else "否")
            self.monitor.addTopLevelItem(node)
            node.setExpanded(True)

        node = QtWidgets.QTreeWidgetItem(self.monitor, ["线程", "运行中"])
        for name, info in (stats.get("threads") or {}).items():
            add(node, name, "是" if info.get("alive") else "否")
        self.monitor.addTopLevelItem(node)
        node.setExpanded(True)

        node = QtWidgets.QTreeWidgetItem(self.monitor, ["环境", ""])
        add(node, "解码后端", stats.get("backend", "-"))
        add(node, "音频输出", "启用" if stats.get("audio_enabled") else "静音")
        add(node, "CPU 时域拉伸", "开启" if stats.get("stretching") else "关闭")
        self.monitor.addTopLevelItem(node)
        self.monitor.expandItem(root_play)

    # ==================================================================== 杂项
    def _refresh_backend_hint(self) -> None:
        backends = self.player.available_backends()
        if not any(b["available"] for b in backends[:2]):
            self.video_widget.set_hint(
                "未检测到 ffmpeg / PyAV\n\n拖入视频文件将无法解码。\n"
                "可点击「文件 -> 打开合成演示源」体验合成源版本的完整管线，\n"
                "或把 ffmpeg.exe 放到 tools/ffmpeg/bin/ 后重启程序。")

    def _restore_settings(self) -> None:
        vol = self._settings.value("volume", 80, type=int)
        self.volume.setValue(vol)
        self.player.set_volume(vol / 100.0)
        self.chk_loop.setChecked(self._settings.value("loop", False, type=bool))

    def closeEvent(self, event) -> None:  # noqa: N802
        self._settings.setValue("volume", self.volume.value())
        self._settings.setValue("loop", self.chk_loop.isChecked())
        self.player.stop()
        super().closeEvent(event)

    # ---------------------------------------------------------------- 拖放
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        urls = event.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if path:
                self.open_path(path)


def _fmt(value) -> str:
    return "—" if value is None else "%.3f" % value
