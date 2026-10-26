// 帧数据结构：解码线程产出、渲染线程消费
// 视频帧用 QByteArray 装 RGB24，隐式共享 => peek()/拷贝代价极低
#pragma once

#include <QByteArray>
#include <QMetaType>
#include <QString>

struct VideoFrame {
    double pts = 0.0;            // 显示时间戳（媒体时间，秒）
    double duration = 0.0;       // 帧持续时长（秒）
    int width = 0;
    int height = 0;
    QByteArray data;             // RGB24，width*height*3

    bool isValid() const { return width > 0 && height > 0 && !data.isEmpty(); }
};

struct AudioFrame {
    double pts = 0.0;
    double duration = 0.0;
    QByteArray data;             // 交错 S16LE
    int sampleRate = 48000;
    int channels = 2;

    int units() const { return data.size() / (sizeof(qint16) * (channels > 0 ? channels : 1)); }
    bool isValid() const { return !data.isEmpty(); }
};

// 后端探测出来的媒体信息
struct MediaInfo {
    QString path;
    QString backend;             // 实际使用的后端名
    QString title;
    QString notes;               // 降级 / 能力受限提示
    double duration = 0.0;
    double fps = 25.0;
    int width = 0;
    int height = 0;
    int sampleRate = 48000;
    int channels = 2;
    bool hasVideo = false;
    bool hasAudio = false;

    bool isValid() const { return hasVideo || hasAudio; }
};

Q_DECLARE_METATYPE(MediaInfo)
