// 程序入口。
//   miniplayer.exe [文件路径]         —— 打开 GUI（也可把文件拖进窗口）
//   miniplayer.exe --selftest [路径]  —— 无头自测，跑完打印结果并退出
#include "core/selftest.h"
#include "ui/main_window.h"

#include <QApplication>
#include <QFileInfo>
#include <QString>

int main(int argc, char *argv[])
{
    bool selftest = false;
    bool hasPath = false;
    QString source;

    for (int i = 1; i < argc; ++i) {
        const QString arg = QString::fromLocal8Bit(argv[i]);
        if (arg == QLatin1String("--selftest") || arg == QLatin1String("-t")) {
            selftest = true;
        } else if (arg == QLatin1String("--help") || arg == QLatin1String("-h")) {
            qInfo("用法: miniplayer [文件路径] [--selftest]");
            return 0;
        } else if (!arg.startsWith(QLatin1Char('-'))) {
            source = arg;
            hasPath = true;
        }
    }

    if (selftest) {
        // 自测不需要真的开窗口：用 offscreen 平台插件，避免在无显示环境里崩
        qputenv("QT_QPA_PLATFORM", "offscreen");
    }

    QApplication app(argc, argv);
    QApplication::setApplicationName(QStringLiteral("MiniPlayerCpp"));
    QApplication::setOrganizationName(QStringLiteral("MiniPlayer"));

    if (selftest)
        return runSelfTest(hasPath ? source : QStringLiteral("synthetic://demo"));

    MainWindow window;
    window.show();
    if (hasPath && QFileInfo::exists(source))
        window.openPath(source);
    else if (hasPath)
        window.openPath(source);          // 让播放器自己给出"无法打开"的原因

    return app.exec();
}
