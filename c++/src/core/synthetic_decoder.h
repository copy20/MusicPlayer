// 内置合成演示源：程序化生成画面 + 音频。
//
// 存在的意义：没有 ffmpeg 时也能**完整跑通整条管线**（两读线程、有界队列、
// 时钟节拍、丢帧、背压、变速、seek），而不是让程序变成一个空壳界面。
#pragma once

#include "core/decoder.h"

class SyntheticDecoder : public IDecoder {
public:
    static bool canOpen(const QString &path);      // synthetic://xxx 或 demo

    QString displayName() const override { return QStringLiteral("内置合成演示源"); }

    MediaInfo open(const QString &path) override;
    void close() override;
    VideoFrame readVideo() override;
    AudioFrame readAudio() override;
    void seek(double seconds) override;
    qint64 decodedBytes() const override { return bytes_; }

private:
    void generateVideo(double pts, VideoFrame &out);
    void generateAudio(double pts, int units, int sampleRate, int channels, AudioFrame &out);

    bool closed_ = true;
    double duration_ = 180.0;
    double fps_ = 25.0;
    int width_ = 960;
    int height_ = 540;
    int sampleRate_ = 48000;
    int channels_ = 2;
    double videoPts_ = 0.0;
    double audioPts_ = 0.0;
    qint64 bytes_ = 0;
    quint32 frameIndex_ = 0;
};
