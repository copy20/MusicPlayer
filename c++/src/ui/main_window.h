#pragma once

#include "core/frame.h"

#include <QMainWindow>

class Player;
class VideoWidget;
class QComboBox;
class QLabel;
class QPushButton;
class QSlider;
class QTreeWidget;
class QTimer;

class MainWindow : public QMainWindow {
    Q_OBJECT
public:
    explicit MainWindow(QWidget *parent = nullptr);
    ~MainWindow() override;

    void openPath(const QString &path);

protected:
    void dragEnterEvent(QDragEnterEvent *event) override;
    void dropEvent(QDropEvent *event) override;
    void keyPressEvent(QKeyEvent *event) override;

private slots:
    void openFileDialog();
    void openSynthetic();
    void onStateChanged(int state);
    void onPositionChanged(double seconds);
    void onFrame(const QImage &image);
    void onMediaOpened(const MediaInfo &info);
    void onError(const QString &message);
    void onSliderMoved(int value);
    void onSliderReleased();
    void onSpeedIndexChanged(int index);
    void onVolumeChanged(int value);
    void refreshStats();
    void showBackendInfo();
    void showAbout();
    void toggleFullscreen();

private:
    void buildUi();
    void buildMenus();
    void setControlsEnabled(bool enabled);
    void seekBy(double delta);

    Player *player_ = nullptr;
    VideoWidget *video_ = nullptr;
    QPushButton *playButton_ = nullptr;
    QPushButton *stopButton_ = nullptr;
    QPushButton *muteButton_ = nullptr;
    QSlider *positionSlider_ = nullptr;
    QSlider *volumeSlider_ = nullptr;
    QLabel *timeLabel_ = nullptr;
    QLabel *stateLabel_ = nullptr;
    QLabel *backendLabel_ = nullptr;
    QComboBox *speedBox_ = nullptr;
    QTreeWidget *monitor_ = nullptr;
    QTimer *statsTimer_ = nullptr;

    bool dragging_ = false;
    bool fullscreen_ = false;
};
