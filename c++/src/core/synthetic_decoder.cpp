#include "core/synthetic_decoder.h"

#include <QImage>
#include <QPainter>
#include <QLinearGradient>
#include <QColor>
#include <QtMath>

#include <cmath>
#include <cstring>

static constexpr double kPi = 3.14159265358979323846;

bool SyntheticDecoder::canOpen(const QString &path)
{
    return path.isEmpty() || path == QLatin1String("demo")
        || path.startsWith(QLatin1String("synthetic://"), Qt::CaseInsensitive);
}

MediaInfo SyntheticDecoder::open(const QString &path)
{
    closed_ = false;
    videoPts_ = 0.0;
    audioPts_ = 0.0;
    bytes_ = 0;
    frameIndex_ = 0;

    MediaInfo info;
    info.path = path.isEmpty() ? QStringLiteral("synthetic://demo") : path;
    info.backend = displayName();
    info.title = QStringLiteral("合成演示源（程序化生成）");
    info.duration = duration_;
    info.fps = fps_;
    info.width = width_;
    info.height = height_;
    info.sampleRate = sampleRate_;
    info.channels = channels_;
    info.hasVideo = true;
    info.hasAudio = true;
    info.notes = QStringLiteral("当前没有可用的 ffmpeg，正在播放程序化生成的演示源");
    info_ = info;
    return info;
}

void SyntheticDecoder::close()
{
    closed_ = true;
}

void SyntheticDecoder::seek(double seconds)
{
    videoPts_ = qBound(0.0, seconds, duration_);
    audioPts_ = videoPts_;
}

VideoFrame SyntheticDecoder::readVideo()
{
    VideoFrame frame;
    if (closed_ || videoPts_ >= duration_)
        return frame;

    generateVideo(videoPts_, frame);
    videoPts_ += 1.0 / fps_;
    ++frameIndex_;
    bytes_ += frame.data.size();
    return frame;
}

AudioFrame SyntheticDecoder::readAudio()
{
    AudioFrame frame;
    if (closed_ || audioPts_ >= duration_)
        return frame;

    const int units = 1024;                       // 每块 1024 个采样帧
    generateAudio(audioPts_, units, sampleRate_, channels_, frame);
    audioPts_ += double(units) / double(sampleRate_);
    bytes_ += frame.data.size();
    return frame;
}

void SyntheticDecoder::generateVideo(double pts, VideoFrame &out)
{
    QImage img(width_, height_, QImage::Format_RGB888);
    QPainter p(&img);
    p.setRenderHint(QPainter::Antialiasing, true);

    // 背景：随媒体时间缓慢流动的渐变（色相随时间推移）
    const int hue = int(std::fmod(pts * 12.0, 360.0));
    QLinearGradient bg(0, 0, width_, height_);
    bg.setColorAt(0.0, QColor::fromHsv(hue, 160, 60));
    bg.setColorAt(1.0, QColor::fromHsv((hue + 60) % 360, 180, 26));
    p.fillRect(img.rect(), bg);

    // 一根随秒针摆动的扫描线，肉眼可判断"是否在动"
    const double sweep = std::fmod(pts, 1.0);
    const int lineX = int(sweep * width_);
    p.fillRect(QRect(lineX - 2, 0, 4, height_), QColor(255, 255, 255, 40));

    // 五根跳动的竖条：振幅与相位都跟媒体时间相关
    const int bars = 5;
    const int barW = width_ / (bars * 3);
    for (int i = 0; i < bars; ++i) {
        const double phase = pts * 2.0 + i * 0.7;
        const double amp = 0.30 + 0.22 * std::sin(phase * 1.7);
        const int h = int(height_ * amp);
        const int x = width_ / (bars + 1) * (i + 1) - barW / 2;
        const int y = (height_ - h) / 2;
        QColor c = QColor::fromHsv((hue + i * 45) % 360, 200, 235);
        c.setAlpha(220);
        p.fillRect(QRect(x, y, barW, h), c);
    }

    // 旋转圆环：秒针，转一圈正好 1 秒
    const int cx = width_ / 2;
    const int cy = height_ / 2;
    const int r = int(qMin(width_, height_) * 0.13);
    p.setPen(QPen(QColor(255, 255, 255, 150), 3));
    p.drawEllipse(QPoint(cx, cy), r, r);
    const double angle = (pts - std::floor(pts)) * 2.0 * kPi;
    p.setPen(QPen(QColor(255, 255, 255, 230), 5));
    p.drawLine(QPoint(cx, cy),
               QPoint(cx + int(std::cos(angle) * r), cy + int(std::sin(angle) * r)));

    // 底部进度条
    const int pad = 24;
    const int barH = 10;
    const int barY = height_ - pad - barH;
    p.setPen(Qt::NoPen);
    p.setBrush(QColor(0, 0, 0, 120));
    p.drawRoundedRect(QRect(pad, barY, width_ - pad * 2, barH), 5, 5);
    const double ratio = qBound(0.0, pts / duration_, 1.0);
    p.setBrush(QColor(90, 200, 255));
    p.drawRoundedRect(QRect(pad, barY, int((width_ - pad * 2) * ratio), barH), 5, 5);
    p.end();

    // 打包成紧凑 RGB24（QImage 每行可能有对齐填充，必须逐行拷）
    out.pts = pts;
    out.duration = 1.0 / fps_;
    out.width = width_;
    out.height = height_;
    out.data.resize(width_ * height_ * 3);
    char *dst = out.data.data();
    const int rowBytes = width_ * 3;
    for (int y = 0; y < height_; ++y) {
        std::memcpy(dst + y * rowBytes, img.constScanLine(y), size_t(rowBytes));
    }
}

void SyntheticDecoder::generateAudio(double pts, int units, int sampleRate, int channels,
                                     AudioFrame &out)
{
    const int bytes = units * channels * int(sizeof(qint16));
    out.pts = pts;
    out.duration = double(units) / double(sampleRate);
    out.sampleRate = sampleRate;
    out.channels = channels;
    out.data.resize(bytes);
    qint16 *dst = reinterpret_cast<qint16 *>(out.data.data());

    for (int i = 0; i < units; ++i) {
        const double t = pts + double(i) / double(sampleRate);
        // 基础音：随时间升高的正弦（方便听出"变速"）
        const double freq = 220.0 + 60.0 * std::sin(t * 0.6);
        double v = 0.22 * std::sin(2.0 * kPi * freq * t);

        // 每秒一声"嘀"：1 秒周期的短包络
        const double beat = std::fmod(t, 1.0);
        if (beat < 0.06) {
            const double env = std::sin(kPi * beat / 0.06);
            v += 0.35 * env * std::sin(2.0 * kPi * 1320.0 * t);
        }

        // 轻微立体声差异，能听出左右
        const double pan = 0.18 * std::sin(t * 0.9);
        const qint16 l = qint16(qBound(-1.0, v * (1.0 + pan), 1.0) * 32767.0);
        const qint16 r = qint16(qBound(-1.0, v * (1.0 - pan), 1.0) * 32767.0);
        if (channels >= 2) {
            dst[i * channels + 0] = l;
            dst[i * channels + 1] = r;
        } else {
            dst[i] = qint16((l + r) / 2);
        }
    }
}
