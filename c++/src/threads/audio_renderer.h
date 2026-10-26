// 音频渲染线程：把 S16LE 写进 QAudioOutput，并把"现在听到哪了"报给播放器。
//
// 它是**主时钟的来源**（听觉对音画不同步比视觉敏感得多）。音频时钟按下式推算：
//     audio_pos = 已写入最后一个采样的媒体时间 - 设备里还没播完的秒数 × 倍速
// 只上报"已经写进设备"的时间是不够的，那样时钟会永远超前一个设备缓冲。
//
// 变速：优先用后端的 atempo（音调不变、质量最好）；没有 atempo 时用 OLA 拉伸兜底。
#pragma once

#include "core/frame.h"
#include "core/frame_queue.h"
#include "core/media_clock.h"
#include "core/time_stretch.h"
#include "threads/stoppable_thread.h"

#include <QAudioOutput>
#include <QIODevice>
#include <QMutex>
#include <atomic>
#include <memory>

class AudioRenderer : public StoppableThread {
    Q_OBJECT
public:
    AudioRenderer(std::shared_ptr<MediaClock> clock, FrameQueue<AudioFrame> *queue,
                  std::atomic<bool> *playFlag, QObject *parent = nullptr);

    void setVolume(qreal volume);      // 0.0 ~ 1.0
    void setMuted(bool muted);
    void setNativeTempo(bool native);  // 后端自带 atempo 时不需要 OLA

    bool deviceRunning() const { return deviceRunning_; }

signals:
    void audioClock(double seconds);
    void audioUnavailable(const QString &reason);

protected:
    void run() override;

private:
    bool openDevice(int sampleRate, int channels);
    void closeDevice();
    void reportClock(double expected, double bufferedSeconds);
    QByteArray applySpeed(const AudioFrame &frame);

    std::shared_ptr<MediaClock> clock_;
    FrameQueue<AudioFrame> *queue_;
    std::atomic<bool> *playFlag_;

    QAudioOutput *output_ = nullptr;
    QIODevice *io_ = nullptr;
    int sampleRate_ = 48000;
    int channels_ = 2;
    qreal volume_ = 0.8;
    bool muted_ = false;
    bool nativeTempo_ = false;
    bool deviceRunning_ = false;
    bool verified_ = false;         // 设备确实消耗过数据（才有资格当主时钟）

    OlaStretcher stretcher_;
    double expected_ = 0.0;            // 已写入设备的最后一个采样对应的媒体时间
    bool haveExpected_ = false;
};
