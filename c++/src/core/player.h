// 播放器控制器（住在 GUI 线程）：状态机 + 线程编排，本身不做任何重活。
//
// 线程拓扑：
//     GUI 线程 ── 只发指令、只画界面
//       ├── read-video   读线程：解码器 -> 有界视频队列
//       ├── read-audio   读线程：解码器 -> 有界音频队列
//       ├── render-video 渲染线程：按媒体时钟出帧（丢帧/等待/缓冲判断都在这里）
//       └── render-audio 渲染线程：写 QAudioOutput，并回报音频时钟（主时钟来源）
//
// 队列是**双向**通信的：读线程写、渲染线程读；队列满就把读线程顶住（背压）。
#pragma once

#include "core/decoder.h"
#include "core/frame_queue.h"
#include "core/media_clock.h"

#include <QImage>
#include <QObject>
#include <QVariantMap>
#include <atomic>
#include <memory>

class ReaderThread;
class VideoRenderer;
class AudioRenderer;

class Player : public QObject {
    Q_OBJECT
public:
    enum class State { Stopped, Buffering, Playing, Paused };
    Q_ENUM(State)

    explicit Player(QObject *parent = nullptr);
    ~Player() override;

    /// 打开媒体（真实文件或 synthetic://demo），失败抛 DecodeError
    bool open(const QString &path, bool autoplay = true);
    void closeMedia();

    void play();
    void pause();
    void togglePause();
    void stop();
    void seek(double seconds);
    void setSpeed(double speed);
    void setVolume(qreal volume);
    void setMuted(bool muted);

    State state() const { return state_; }
    QString stateText() const;
    bool hasMedia() const { return decoder_ != nullptr; }
    double position() const;
    double duration() const { return info_.duration; }
    double speed() const;
    MediaInfo mediaInfo() const { return info_; }
    bool audioEnabled() const { return audioEnabled_; }
    bool isMuted() const { return muted_; }
    qreal volume() const { return volume_; }
    QString clockMaster() const;
    QVariantMap stats() const;

signals:
    void stateChanged(int state);
    void positionChanged(double seconds);
    void frameReady(const QImage &image);
    void mediaOpened(const MediaInfo &info);
    void errorOccurred(const QString &message);
    void bufferingChanged(bool buffering);
    void mutedChanged(bool muted);

private slots:
    void onVideoFrameReady(const QImage &image);
    void onBufferingChanged(bool buffering);
    void onAudioClock(double seconds);
    void onAudioUnavailable(const QString &reason);
    void onVideoEos();
    void emitPosition();

private:
    void startThreads();
    void stopThreads();
    void startPreroll();
    void setState(State state);
    void resetQueues();

    std::unique_ptr<IDecoder> decoder_;
    MediaInfo info_;
    QString path_;

    std::unique_ptr<FrameQueue<VideoFrame>> videoQueue_;
    std::unique_ptr<FrameQueue<AudioFrame>> audioQueue_;
    std::shared_ptr<MediaClock> clock_;

    ReaderThread *videoReader_ = nullptr;
    ReaderThread *audioReader_ = nullptr;
    VideoRenderer *videoRenderer_ = nullptr;
    AudioRenderer *audioRenderer_ = nullptr;

    std::atomic<bool> playFlag_{ false };
    std::atomic<int> prerollToken_{ 0 };
    State state_ = State::Stopped;

    bool audioEnabled_ = false;
    bool muted_ = false;
    qreal volume_ = 0.8;
    double lastAudioPos_ = 0.0;
    // 音频时钟第一次"证明自己在出声"时记下的偏移：
    // 声卡可能比视频晚启动半秒到两秒，这个差值要一次性吸收掉，
    // 否则就会演成"画面被拽回去"（软校正解决不了这么大的偏差）
    bool audioSynced_ = false;
    bool audioInControl_ = false;   // 音频当前是否真的在当基准（偏差足够小）
    double audioOffset_ = 0.0;
    int audioStale_ = 0;            // 音频时钟连续"不推进"的报告次数

    // 连续这么多份报告（约 0.6 秒）不推进 => 不再拿它校正画面
    static constexpr int kAudioStaleIgnore = 12;
    // 连续这么多份报告（约 2 秒）不推进 => 判定声卡不可用，切静音
    static constexpr int kAudioStaleFatal = 40;
};
