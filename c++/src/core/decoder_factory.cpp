#include "core/decoder_factory.h"

#include "core/ffmpeg_cli_decoder.h"
#include "core/synthetic_decoder.h"

#include <QFileInfo>
#include <QLoggingCategory>

Q_LOGGING_CATEGORY(lcFactory, "player.factory")

namespace {
QList<DecoderFactory::Attempt> g_lastAttempts;

double scoreOf(const MediaInfo &info, bool nativeTempo)
{
    double score = 0.0;
    if (info.hasVideo)
        score += 1.0;
    if (info.hasAudio)
        score += 1.0;
    if (nativeTempo)
        score += 0.5;          // 能保音调变速者同分优先
    return score;
}
} // namespace

QList<DecoderFactory::Attempt> DecoderFactory::lastAttempts()
{
    return g_lastAttempts;
}

QString DecoderFactory::availabilitySummary()
{
    QStringList lines;
    const QString ffmpeg = FfmpegCliDecoder::locateFfmpeg();
    if (ffmpeg.isEmpty()) {
        lines << QStringLiteral("FFmpeg 子进程后端：不可用（没找到 ffmpeg.exe）");
    } else {
        lines << QStringLiteral("FFmpeg 子进程后端：%1%2")
                     .arg(ffmpeg,
                          FfmpegCliDecoder::hasAtempo()
                              ? QStringLiteral("（支持 atempo 保音变速）")
                              : QStringLiteral("（无 atempo，变速走 CPU 拉伸）"));
    }
    lines << QStringLiteral("内置合成演示源：始终可用（synthetic://demo）");
    return lines.join(QLatin1Char('\n'));
}

std::unique_ptr<IDecoder> DecoderFactory::open(const QString &path)
{
    g_lastAttempts.clear();

    // 伪协议 / 空路径 -> 合成源，别去碰文件系统
    if (SyntheticDecoder::canOpen(path)) {
        auto decoder = std::make_unique<SyntheticDecoder>();
        decoder->open(path.isEmpty() ? QStringLiteral("synthetic://demo") : path);
        g_lastAttempts << Attempt{ decoder->displayName(), 3.0, {} };
        return decoder;
    }

    if (!QFileInfo::exists(path))
        throw DecodeError(QStringLiteral("文件不存在: %1").arg(path));

    std::unique_ptr<IDecoder> best;
    double bestScore = -1.0;

    // 候选 1：FFmpeg 子进程
    try {
        if (FfmpegCliDecoder::canOpen(path)) {
            auto decoder = std::make_unique<FfmpegCliDecoder>();
            const MediaInfo info = decoder->open(path);
            const double score = scoreOf(info, decoder->supportsNativeTempo());
            g_lastAttempts << Attempt{ decoder->displayName(), score, info.notes };
            qCInfo(lcFactory) << "后端" << decoder->displayName() << "打开成功，评分" << score;
            if (score > bestScore) {
                bestScore = score;
                best = std::move(decoder);
            }
        } else {
            g_lastAttempts << Attempt{ QStringLiteral("FFmpeg 子进程"), -1.0,
                                       QStringLiteral("不可用或无法解码该文件（缺 ffmpeg.exe / 缺解码器）") };
        }
    } catch (const std::exception &ex) {
        g_lastAttempts << Attempt{ QStringLiteral("FFmpeg 子进程"), -1.0,
                                   QString::fromUtf8(ex.what()) };
    }

    if (!best) {
        QString detail;
        for (const Attempt &a : g_lastAttempts)
            detail += QStringLiteral("\n  - %1：%2").arg(a.backend, a.error);
        throw DecodeError(QStringLiteral(
                              "没有可用的解码后端能打开该文件：%1%2\n"
                              "解决办法：\n"
                              "  1) 下载完整版 ffmpeg.exe 放到 c++/tools/ffmpeg/bin/ 下"
                              "（或设置环境变量 FFMPEG=完整路径）；\n"
                              "  2) 或者先体验内置合成源：文件 -> 打开合成演示源。")
                              .arg(path, detail));
    }
    return best;
}
