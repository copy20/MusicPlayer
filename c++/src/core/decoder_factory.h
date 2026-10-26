// 后端选择：不是"谁存在就用谁"，而是**逐个真打开一次，按实测能力打分**。
//
// 现实教训：用户丢进来的 ffmpeg.exe 常常是 --disable-everything 的极简构建，
// 能解析流头、却既没有音频解码器也没有 rawvideo 输出。只看"文件存在"就选它，
// 用户看到的就是黑屏且毫无提示。所以候选后端必须在 open() 里自证能力。
#pragma once

#include "core/decoder.h"

#include <QString>
#include <QStringList>
#include <memory>

class DecoderFactory {
public:
    struct Attempt {
        QString backend;
        double score = -1.0;
        QString error;
    };

    /// 打开媒体（真实文件或 synthetic://）。失败抛 DecodeError。
    static std::unique_ptr<IDecoder> open(const QString &path);

    /// 上一次 open() 的候选尝试记录（给"后端信息"对话框用）
    static QList<Attempt> lastAttempts();
    static QString availabilitySummary();
};
