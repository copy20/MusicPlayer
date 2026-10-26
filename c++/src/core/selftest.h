#pragma once

#include <QString>

/// 无头自测：跑一遍完整管线并检查关键指标，返回 0 表示全通过。
/// 用法： miniplayer_console.exe --selftest [文件路径]
int runSelfTest(const QString &source);
