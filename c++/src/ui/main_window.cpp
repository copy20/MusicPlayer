#include "ui/main_window.h"

#include "core/decoder_factory.h"
#include "core/player.h"
#include "ui/style.h"
#include "ui/video_widget.h"

#include <QAction>
#include <QActionGroup>
#include <QApplication>
#include <QComboBox>
#include <QDockWidget>
#include <QDragEnterEvent>
#include <QDropEvent>
#include <QFileDialog>
#include <QFileInfo>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QKeyEvent>
#include <QLabel>
#include <QMenuBar>
#include <QMessageBox>
#include <QMimeData>
#include <QPushButton>
#include <QSlider>
#include <QStatusBar>
#include <QTimer>
#include <QTreeWidget>
#include <QTreeWidgetItem>
#include <QVBoxLayout>
#include <QUrl>

#include <algorithm>
#include <cmath>

namespace {

QString formatTime(double seconds)
{
    if (seconds < 0 || !std::isfinite(seconds))
        seconds = 0;
    const int total = int(seconds + 0.5);
    const int h = total / 3600;
    const int m = (total % 3600) / 60;
    const int s = total % 60;
    if (h > 0)
        return QStringLiteral("%1:%2:%3")
                .arg(h)
                .arg(m, 2, 10, QLatin1Char('0'))
                .arg(s, 2, 10, QLatin1Char('0'));
    return QStringLiteral("%1:%2")
            .arg(m, 2, 10, QLatin1Char('0'))
            .arg(s, 2, 10, QLatin1Char('0'));
}

const double kSpeeds[] = { 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0 };
constexpr int kSliderMax = 1000;

} // namespace

MainWindow::MainWindow(QWidget *parent)
    : QMainWindow(parent)
{
    player_ = new Player(this);
    buildUi();
    buildMenus();

    connect(player_, &Player::stateChanged, this, &MainWindow::onStateChanged);
    connect(player_, &Player::positionChanged, this, &MainWindow::onPositionChanged);
    connect(player_, &Player::frameReady, this, &MainWindow::onFrame);
    connect(player_, &Player::mediaOpened, this, &MainWindow::onMediaOpened);
    connect(player_, &Player::errorOccurred, this, &MainWindow::onError);

    statsTimer_ = new QTimer(this);
    statsTimer_->setInterval(250);
    connect(statsTimer_, &QTimer::timeout, this, &MainWindow::refreshStats);
    statsTimer_->start();

    setWindowTitle(QStringLiteral("MiniPlayer C++ · 多线程 + 缓存队列 + 变速"));
    resize(1180, 720);
    setAcceptDrops(true);
    setControlsEnabled(false);
    onStateChanged(int(player_->state()));
}

MainWindow::~MainWindow() = default;

void MainWindow::buildUi()
{
    setStyleSheet(darkStyleSheet());

    auto *central = new QWidget(this);
    auto *root = new QVBoxLayout(central);
    root->setContentsMargins(0, 0, 0, 0);
    root->setSpacing(0);

    video_ = new VideoWidget(central);
    root->addWidget(video_, 1);

    auto *controlBar = new QWidget(central);
    controlBar->setObjectName(QStringLiteral("controlBar"));
    auto *bar = new QHBoxLayout(controlBar);
    bar->setContentsMargins(12, 8, 12, 8);
    bar->setSpacing(10);

    playButton_ = new QPushButton(QStringLiteral("▶ 播放"), controlBar);
    stopButton_ = new QPushButton(QStringLiteral("■ 停止"), controlBar);
    muteButton_ = new QPushButton(QStringLiteral("🔊"), controlBar);
    muteButton_->setFixedWidth(44);

    positionSlider_ = new QSlider(Qt::Horizontal, controlBar);
    positionSlider_->setRange(0, kSliderMax);
    positionSlider_->setSingleStep(1);

    timeLabel_ = new QLabel(QStringLiteral("00:00 / 00:00"), controlBar);
    timeLabel_->setObjectName(QStringLiteral("timeLabel"));
    timeLabel_->setMinimumWidth(104);

    speedBox_ = new QComboBox(controlBar);
    for (double s : kSpeeds)
        speedBox_->addItem(QStringLiteral("%1x").arg(s), s);
    speedBox_->setCurrentIndex(2);           // 1.0x

    volumeSlider_ = new QSlider(Qt::Horizontal, controlBar);
    volumeSlider_->setRange(0, 100);
    volumeSlider_->setValue(80);
    volumeSlider_->setFixedWidth(110);

    stateLabel_ = new QLabel(controlBar);
    stateLabel_->setObjectName(QStringLiteral("clockLabel"));
    stateLabel_->setMinimumWidth(150);

    bar->addWidget(playButton_);
    bar->addWidget(stopButton_);
    bar->addSpacing(6);
    bar->addWidget(positionSlider_, 1);
    bar->addWidget(timeLabel_);
    bar->addSpacing(6);
    bar->addWidget(new QLabel(QStringLiteral("速度"), controlBar));
    bar->addWidget(speedBox_);
    bar->addWidget(volumeSlider_);
    bar->addWidget(muteButton_);
    bar->addWidget(stateLabel_);
    root->addWidget(controlBar);

    setCentralWidget(central);

    // ---- 右侧监控面板（Ctrl+M 开关）
    auto *dock = new QDockWidget(QStringLiteral("运行时监控"), this);
    dock->setObjectName(QStringLiteral("monitorDock"));
    dock->setAllowedAreas(Qt::LeftDockWidgetArea | Qt::RightDockWidgetArea);
    monitor_ = new QTreeWidget(dock);
    monitor_->setColumnCount(2);
    monitor_->setHeaderLabels({ QStringLiteral("项目"), QStringLiteral("值") });
    monitor_->setRootIsDecorated(false);
    monitor_->setAlternatingRowColors(true);
    monitor_->setUniformRowHeights(true);
    monitor_->header()->setStretchLastSection(true);
    dock->setWidget(monitor_);
    dock->setMinimumWidth(330);
    addDockWidget(Qt::RightDockWidgetArea, dock);

    // ---- 状态栏
    backendLabel_ = new QLabel(QStringLiteral("后端：-"), this);
    statusBar()->addPermanentWidget(backendLabel_);
    statusBar()->showMessage(QStringLiteral("就绪"));

    connect(playButton_, &QPushButton::clicked, this, [this]() { player_->togglePause(); });
    connect(stopButton_, &QPushButton::clicked, this, [this]() { player_->stop(); });
    connect(muteButton_, &QPushButton::clicked, this, [this]() {
        player_->setMuted(!player_->isMuted());
        muteButton_->setText(player_->isMuted() ? QStringLiteral("🔇") : QStringLiteral("🔊"));
    });
    connect(positionSlider_, &QSlider::sliderPressed, this, [this]() { dragging_ = true; });
    connect(positionSlider_, &QSlider::sliderReleased, this, &MainWindow::onSliderReleased);
    connect(positionSlider_, &QSlider::valueChanged, this, &MainWindow::onSliderMoved);
    connect(speedBox_, QOverload<int>::of(&QComboBox::currentIndexChanged),
            this, &MainWindow::onSpeedIndexChanged);
    connect(volumeSlider_, &QSlider::valueChanged, this, &MainWindow::onVolumeChanged);
}

void MainWindow::buildMenus()
{
    auto *fileMenu = menuBar()->addMenu(QStringLiteral("文件(&F)"));
    auto *openAct = fileMenu->addAction(QStringLiteral("打开视频文件…"));
    openAct->setShortcut(QKeySequence::Open);
    connect(openAct, &QAction::triggered, this, &MainWindow::openFileDialog);

    auto *demoAct = fileMenu->addAction(QStringLiteral("打开合成演示源"));
    demoAct->setShortcut(QKeySequence(QStringLiteral("Ctrl+D")));
    connect(demoAct, &QAction::triggered, this, &MainWindow::openSynthetic);

    fileMenu->addSeparator();
    auto *quitAct = fileMenu->addAction(QStringLiteral("退出"));
    quitAct->setShortcut(QKeySequence::Quit);
    connect(quitAct, &QAction::triggered, this, &QWidget::close);

    auto *playMenu = menuBar()->addMenu(QStringLiteral("播放(&P)"));
    auto *toggleAct = playMenu->addAction(QStringLiteral("播放 / 暂停"));
    toggleAct->setShortcut(QKeySequence(Qt::Key_Space));
    connect(toggleAct, &QAction::triggered, this, [this]() { player_->togglePause(); });

    auto *stopAct = playMenu->addAction(QStringLiteral("停止"));
    stopAct->setShortcut(QKeySequence(Qt::Key_S));
    connect(stopAct, &QAction::triggered, this, [this]() { player_->stop(); });

    playMenu->addSeparator();
    auto *backAct = playMenu->addAction(QStringLiteral("后退 5 秒"));
    backAct->setShortcut(QKeySequence(Qt::Key_Left));
    connect(backAct, &QAction::triggered, this, [this]() { seekBy(-5.0); });

    auto *fwdAct = playMenu->addAction(QStringLiteral("前进 5 秒"));
    fwdAct->setShortcut(QKeySequence(Qt::Key_Right));
    connect(fwdAct, &QAction::triggered, this, [this]() { seekBy(5.0); });

    playMenu->addSeparator();
    auto *speedGroup = new QActionGroup(this);
    speedGroup->setExclusive(true);
    for (double s : kSpeeds) {
        auto *act = playMenu->addAction(QStringLiteral("%1x").arg(s));
        act->setCheckable(true);
        act->setChecked(qFuzzyCompare(s, 1.0));
        speedGroup->addAction(act);
        connect(act, &QAction::triggered, this, [this, s]() {
            player_->setSpeed(s);
            const int idx = speedBox_->findData(s);
            if (idx >= 0) {
                speedBox_->blockSignals(true);
                speedBox_->setCurrentIndex(idx);
                speedBox_->blockSignals(false);
            }
        });
    }

    auto *viewMenu = menuBar()->addMenu(QStringLiteral("视图(&V)"));
    auto *monitorAct = viewMenu->addAction(QStringLiteral("运行时监控面板"));
    monitorAct->setCheckable(true);
    monitorAct->setChecked(true);
    monitorAct->setShortcut(QKeySequence(QStringLiteral("Ctrl+M")));
    connect(monitorAct, &QAction::toggled, this, [this](bool on) {
        if (auto *dock = findChild<QDockWidget *>(QStringLiteral("monitorDock")))
            dock->setVisible(on);
    });

    auto *fsAct = viewMenu->addAction(QStringLiteral("全屏"));
    fsAct->setCheckable(true);
    fsAct->setShortcut(QKeySequence(Qt::Key_F));
    connect(fsAct, &QAction::toggled, this, [this](bool on) {
        fullscreen_ = on;
        if (on)
            showFullScreen();
        else
            showNormal();
    });

    auto *helpMenu = menuBar()->addMenu(QStringLiteral("帮助(&H)"));
    auto *backendAct = helpMenu->addAction(QStringLiteral("后端信息"));
    backendAct->setShortcut(QKeySequence(QStringLiteral("Ctrl+I")));
    connect(backendAct, &QAction::triggered, this, &MainWindow::showBackendInfo);

    auto *aboutAct = helpMenu->addAction(QStringLiteral("关于"));
    connect(aboutAct, &QAction::triggered, this, &MainWindow::showAbout);
}

void MainWindow::setControlsEnabled(bool enabled)
{
    playButton_->setEnabled(enabled);
    stopButton_->setEnabled(enabled);
    positionSlider_->setEnabled(enabled);
}

void MainWindow::openPath(const QString &path)
{
    try {
        if (!player_->open(path, true))
            return;
    } catch (const std::exception &ex) {
        onError(QString::fromUtf8(ex.what()));
        return;
    }
    setControlsEnabled(true);
    setWindowTitle(QStringLiteral("MiniPlayer C++ · %1").arg(QFileInfo(path).fileName()));
    statusBar()->showMessage(QStringLiteral("已打开：%1").arg(path), 5000);
}

void MainWindow::openFileDialog()
{
    const QString path = QFileDialog::getOpenFileName(
        this, QStringLiteral("打开视频文件"), QString(),
        QStringLiteral("视频文件 (*.mp4 *.mkv *.avi *.mov *.flv *.ts *.webm);;所有文件 (*.*)"));
    if (!path.isEmpty())
        openPath(path);
}

void MainWindow::openSynthetic()
{
    openPath(QStringLiteral("synthetic://demo"));
}

void MainWindow::onStateChanged(int state)
{
    const auto s = Player::State(state);
    playButton_->setText(s == Player::State::Playing || s == Player::State::Buffering
                                 ? QStringLiteral("⏸ 暂停")
                                 : QStringLiteral("▶ 播放"));
    stateLabel_->setText(QStringLiteral("%1 · 主时钟 %2")
                             .arg(player_->stateText(), player_->clockMaster()));
    video_->setOverlayText(s == Player::State::Buffering ? QStringLiteral("缓冲中…") : QString());
}

void MainWindow::onPositionChanged(double seconds)
{
    if (!dragging_) {
        const double duration = player_->duration();
        const int value = duration > 0.0
                ? int(std::lround(seconds / duration * kSliderMax))
                : 0;
        positionSlider_->blockSignals(true);
        positionSlider_->setValue(std::clamp(value, 0, kSliderMax));
        positionSlider_->blockSignals(false);
    }
    timeLabel_->setText(QStringLiteral("%1 / %2")
                            .arg(formatTime(seconds), formatTime(player_->duration())));
}

void MainWindow::onFrame(const QImage &image)
{
    video_->setImage(image);
}

void MainWindow::onMediaOpened(const MediaInfo &info)
{
    backendLabel_->setText(QStringLiteral("后端：%1").arg(info.backend));
    backendLabel_->setToolTip(info.notes.isEmpty() ? info.backend : info.notes);
    if (!info.notes.isEmpty())
        statusBar()->showMessage(info.notes, 8000);
    else
        statusBar()->showMessage(QStringLiteral("%1 · %2x%3 @ %4fps · %5")
                                     .arg(info.title)
                                     .arg(info.width)
                                     .arg(info.height)
                                     .arg(info.fps, 0, 'f', 2)
                                     .arg(formatTime(info.duration)),
                                 8000);
    onPositionChanged(0.0);
}

void MainWindow::onError(const QString &message)
{
    statusBar()->showMessage(message, 8000);
    // 起播失败这类严重错误用弹窗；音频降级之类的提示只在状态栏说明
    if (message.contains(QStringLiteral("没有可用的解码后端"))
        || message.contains(QStringLiteral("文件不存在"))) {
        QMessageBox::warning(this, QStringLiteral("播放失败"), message);
    }
}

void MainWindow::onSliderMoved(int value)
{
    if (!dragging_)
        return;
    const double duration = player_->duration();
    if (duration > 0.0)
        timeLabel_->setText(QStringLiteral("%1 / %2")
                                .arg(formatTime(duration * value / kSliderMax),
                                     formatTime(duration)));
}

void MainWindow::onSliderReleased()
{
    dragging_ = false;
    const double duration = player_->duration();
    if (duration > 0.0)
        player_->seek(duration * positionSlider_->value() / kSliderMax);
}

void MainWindow::onSpeedIndexChanged(int index)
{
    const double s = speedBox_->itemData(index).toDouble();
    if (s > 0.0)
        player_->setSpeed(s);
}

void MainWindow::onVolumeChanged(int value)
{
    player_->setVolume(value / 100.0);
    if (player_->isMuted() && value > 0) {
        player_->setMuted(false);
        muteButton_->setText(QStringLiteral("🔊"));
    }
}

void MainWindow::seekBy(double delta)
{
    if (!player_->hasMedia())
        return;
    player_->seek(player_->position() + delta);
}

void MainWindow::refreshStats()
{
    if (!monitor_)
        return;
    const QVariantMap st = player_->stats();
    monitor_->clear();

    auto addRow = [this](const QString &name, const QString &value, bool warn = false) {
        auto *item = new QTreeWidgetItem(monitor_);
        item->setText(0, name);
        item->setText(1, value);
        if (warn)
            item->setForeground(1, QColor(240, 170, 90));
    };

    addRow(QStringLiteral("后端"), st.value(QStringLiteral("backend")).toString());
    addRow(QStringLiteral("状态"), st.value(QStringLiteral("state")).toString());
    addRow(QStringLiteral("同步主时钟"), st.value(QStringLiteral("clock_master")).toString());
    addRow(QStringLiteral("倍速"), QStringLiteral("%1x").arg(st.value(QStringLiteral("speed")).toDouble(), 0, 'f', 2));
    addRow(QStringLiteral("变速实现"), st.value(QStringLiteral("speed_tool")).toString());
    addRow(QStringLiteral("媒体时钟"),
           QStringLiteral("%1 s").arg(st.value(QStringLiteral("clock")).toDouble(), 0, 'f', 2));
    addRow(QStringLiteral("音频时钟"),
           QStringLiteral("%1 s").arg(st.value(QStringLiteral("audio_pos")).toDouble(), 0, 'f', 2));
    addRow(QStringLiteral("已出帧"), QString::number(st.value(QStringLiteral("frames")).toLongLong()));

    const QVariantMap vq = st.value(QStringLiteral("video_queue")).toMap();
    if (!vq.isEmpty()) {
        addRow(QStringLiteral("视频队列"),
               QStringLiteral("%1/%2 帧 · %3/%4 s")
                   .arg(vq.value(QStringLiteral("size")).toInt())
                   .arg(vq.value(QStringLiteral("max")).toInt())
                   .arg(vq.value(QStringLiteral("seconds")).toDouble(), 0, 'f', 2)
                   .arg(vq.value(QStringLiteral("max_seconds")).toDouble(), 0, 'f', 2));
        addRow(QStringLiteral("  视频 写入/取出"),
               QStringLiteral("%1 / %2")
                   .arg(vq.value(QStringLiteral("pushed")).toLongLong())
                   .arg(vq.value(QStringLiteral("popped")).toLongLong()));
        addRow(QStringLiteral("  视频 迟到丢帧"),
               QString::number(vq.value(QStringLiteral("dropped_late")).toLongLong()),
               vq.value(QStringLiteral("dropped_late")).toLongLong() > 0);
        addRow(QStringLiteral("  视频 空队列次数"),
               QString::number(vq.value(QStringLiteral("empty_waits")).toLongLong()));
        addRow(QStringLiteral("  视频 峰值"),
               QString::number(vq.value(QStringLiteral("peak")).toInt()));
    }

    const QVariantMap aq = st.value(QStringLiteral("audio_queue")).toMap();
    if (!aq.isEmpty()) {
        addRow(QStringLiteral("音频队列"),
               QStringLiteral("%1/%2 帧 · %3/%4 s")
                   .arg(aq.value(QStringLiteral("size")).toInt())
                   .arg(aq.value(QStringLiteral("max")).toInt())
                   .arg(aq.value(QStringLiteral("seconds")).toDouble(), 0, 'f', 2)
                   .arg(aq.value(QStringLiteral("max_seconds")).toDouble(), 0, 'f', 2));
        addRow(QStringLiteral("  音频 写入/取出"),
               QStringLiteral("%1 / %2")
                   .arg(aq.value(QStringLiteral("pushed")).toLongLong())
                   .arg(aq.value(QStringLiteral("popped")).toLongLong()));
    }

    const QVariantMap threads = st.value(QStringLiteral("threads")).toMap();
    for (auto it = threads.constBegin(); it != threads.constEnd(); ++it) {
        addRow(QStringLiteral("线程 %1").arg(it.key()),
               it.value().toBool() ? QStringLiteral("运行中") : QStringLiteral("已停止"),
               !it.value().toBool());
    }
}

void MainWindow::showBackendInfo()
{
    QString text = QStringLiteral("当前后端：%1\n\n可用后端：\n%2\n\n最近一次打开时的候选尝试：\n")
                       .arg(player_->mediaInfo().backend, DecoderFactory::availabilitySummary());
    const auto attempts = DecoderFactory::lastAttempts();
    if (attempts.isEmpty()) {
        text += QStringLiteral("  （还没有打开过文件）");
    } else {
        for (const auto &a : attempts) {
            text += QStringLiteral("  - %1：评分 %2%3\n")
                        .arg(a.backend)
                        .arg(a.score, 0, 'f', 1)
                        .arg(a.error.isEmpty() ? QString() : QStringLiteral("（%1）").arg(a.error));
        }
    }
    QMessageBox::information(this, QStringLiteral("后端信息"), text);
}

void MainWindow::showAbout()
{
    QMessageBox::about(
        this, QStringLiteral("关于 MiniPlayer C++"),
        QStringLiteral(
            "<b>MiniPlayer C++</b><br>"
            "Qt5 + 多线程 + 有界缓存队列 + 变速播放。<br><br>"
            "线程模型：两个读线程、两个渲染线程，队列同时承担背压与代际隔离。<br>"
            "变速：画面只改媒体时钟斜率；音频优先 atempo，其次 OLA 时域拉伸。<br><br>"
            "解码后端：FFmpeg 子进程（若可用）→ 内置合成演示源。"));
}

void MainWindow::toggleFullscreen()
{
    fullscreen_ = !fullscreen_;
    if (fullscreen_)
        showFullScreen();
    else
        showNormal();
}

void MainWindow::keyPressEvent(QKeyEvent *event)
{
    if (event->key() == Qt::Key_Escape && fullscreen_) {
        toggleFullscreen();
        return;
    }
    QMainWindow::keyPressEvent(event);
}

void MainWindow::dragEnterEvent(QDragEnterEvent *event)
{
    if (event->mimeData()->hasUrls())
        event->acceptProposedAction();
}

void MainWindow::dropEvent(QDropEvent *event)
{
    const auto urls = event->mimeData()->urls();
    if (urls.isEmpty())
        return;
    const QString path = urls.first().toLocalFile();
    if (!path.isEmpty())
        openPath(path);
}
