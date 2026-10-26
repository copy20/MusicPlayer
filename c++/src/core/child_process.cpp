#include "core/child_process.h"

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

#include <algorithm>
#include <chrono>

namespace {

// Windows 命令行参数转义：含空格/制表/引号时整体加引号，
// 反斜杠只在与引号相邻时翻倍，其余原样保留。
QString quoteArg(const QString &arg)
{
    const bool needQuote = arg.isEmpty() || arg.contains(QLatin1Char(' '))
            || arg.contains(QLatin1Char('\t')) || arg.contains(QLatin1Char('"'));
    if (!needQuote)
        return arg;

    QString out;
    out.reserve(arg.size() + 4);
    out += QLatin1Char('"');
    int backslashes = 0;
    for (const QChar ch : arg) {
        if (ch == QLatin1Char('\\')) {
            ++backslashes;
        } else if (ch == QLatin1Char('"')) {
            out += QString(backslashes * 2 + 1, QLatin1Char('\\'));
            out += QLatin1Char('"');
            backslashes = 0;
        } else {
            out += QString(backslashes, QLatin1Char('\\'));
            out += ch;
            backslashes = 0;
        }
    }
    out += QString(backslashes * 2, QLatin1Char('\\'));
    out += QLatin1Char('"');
    return out;
}

QString win32Error(DWORD code)
{
    wchar_t *buffer = nullptr;
    const DWORD len = FormatMessageW(FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM
                                             | FORMAT_MESSAGE_IGNORE_INSERTS,
                                     nullptr, code, 0,
                                     reinterpret_cast<wchar_t *>(&buffer), 0, nullptr);
    QString text = len > 0 ? QString::fromWCharArray(buffer, int(len)).trimmed()
                           : QStringLiteral("错误码 %1").arg(code);
    if (buffer != nullptr)
        LocalFree(buffer);
    return text;
}

HANDLE handleOf(void *p)
{
    return static_cast<HANDLE>(p);
}

} // namespace

ChildProcess::~ChildProcess()
{
    kill();
}

bool ChildProcess::start(const QString &program, const QStringList &args)
{
    kill();

    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = TRUE;

    // 管道缓冲必须给足：rawvideo 一帧 640x360 就是 691KB，用默认 4KB 的话
    // 读一帧要往返一百多次，中间还夹着 Sleep，实测帧率会掉到 0.4x。
    // 1MB 让 ffmpeg 能一次把整帧写进来，读端一把拿走。
    constexpr DWORD kPipeBytes = 1u << 20;      // 1MB

    HANDLE outR = nullptr;
    HANDLE outW = nullptr;
    HANDLE errR = nullptr;
    HANDLE errW = nullptr;
    if (!CreatePipe(&outR, &outW, &sa, kPipeBytes) || !CreatePipe(&errR, &errW, &sa, 1u << 16)) {
        lastError_ = QStringLiteral("创建管道失败：%1").arg(win32Error(GetLastError()));
        if (outR != nullptr)
            CloseHandle(outR);
        if (outW != nullptr)
            CloseHandle(outW);
        if (errR != nullptr)
            CloseHandle(errR);
        if (errW != nullptr)
            CloseHandle(errW);
        return false;
    }
    // 读端不能被子进程继承，否则关不掉 EOF
    SetHandleInformation(outR, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(errR, HANDLE_FLAG_INHERIT, 0);

    // ffmpeg 不需要输入，给它一个 NUL，避免等待控制台输入
    HANDLE nul = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &sa,
                             OPEN_EXISTING, 0, nullptr);

    STARTUPINFOW si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdOutput = outW;
    si.hStdError = errW;
    si.hStdInput = (nul == INVALID_HANDLE_VALUE) ? nullptr : nul;

    QString cmdline = quoteArg(program);
    for (const QString &arg : args)
        cmdline += QLatin1Char(' ') + quoteArg(arg);
    std::wstring mutableCmd = cmdline.toStdWString();

    PROCESS_INFORMATION pi{};
    const BOOL ok = CreateProcessW(nullptr, &mutableCmd[0], nullptr, nullptr, TRUE,
                                   CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi);

    // 父进程手里的写端必须关掉，否则读端永远等不到 EOF
    CloseHandle(outW);
    CloseHandle(errW);
    if (nul != INVALID_HANDLE_VALUE)
        CloseHandle(nul);

    if (!ok) {
        lastError_ = QStringLiteral("启动 %1 失败：%2")
                             .arg(program, win32Error(GetLastError()));
        CloseHandle(outR);
        CloseHandle(errR);
        return false;
    }

    CloseHandle(pi.hThread);
    process_ = pi.hProcess;
    outRead_ = outR;
    errRead_ = errR;
    started_ = true;
    lastError_.clear();
    stderrTail_.clear();
    return true;
}

int ChildProcess::readSome(char *buf, int maxBytes, int timeoutMs,
                           const std::atomic<bool> *abort)
{
    if (!started_ || outRead_ == nullptr || buf == nullptr || maxBytes <= 0)
        return -1;

    HANDLE out = handleOf(outRead_);
    const auto begin = std::chrono::steady_clock::now();

    for (;;) {
        if (abort != nullptr && abort->load())
            return -1;

        DWORD avail = 0;
        if (!PeekNamedPipe(out, nullptr, 0, nullptr, &avail, nullptr)) {
            // 写端已关：把剩余 stderr 收下来，方便报错
            takeStderr();
            if (!stderrTail_.isEmpty())
                lastError_ = QString::fromUtf8(stderrTail_).trimmed();
            return -1;
        }

        if (avail > 0) {
            const DWORD want = DWORD(std::min<int>(maxBytes, int(avail)));
            DWORD got = 0;
            if (!ReadFile(out, buf, want, &got, nullptr) || got == 0) {
                takeStderr();
                return -1;
            }
            takeStderr();
            return int(got);
        }

        takeStderr();

        // 没数据且进程已退出 => 正常 EOF
        if (!isRunning()) {
            if (!stderrTail_.isEmpty())
                lastError_ = QString::fromUtf8(stderrTail_).trimmed();
            return -1;
        }

        const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                                     std::chrono::steady_clock::now() - begin)
                                     .count();
        if (elapsed >= timeoutMs)
            return 0;
        Sleep(1);
    }
}

bool ChildProcess::readExact(char *buf, int bytes, int timeoutMs,
                             const std::atomic<bool> *abort)
{
    int got = 0;
    const auto begin = std::chrono::steady_clock::now();
    while (got < bytes) {
        const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                                     std::chrono::steady_clock::now() - begin)
                                     .count();
        const int left = timeoutMs - int(elapsed);
        if (left <= 0)
            return false;
        const int r = readSome(buf + got, bytes - got, left, abort);
        if (r <= 0)
            return false;
        got += r;
    }
    return true;
}

void ChildProcess::takeStderr()
{
    if (errRead_ == nullptr)
        return;
    HANDLE err = handleOf(errRead_);
    char tmp[1024];
    for (;;) {
        DWORD avail = 0;
        if (!PeekNamedPipe(err, nullptr, 0, nullptr, &avail, nullptr) || avail == 0)
            return;
        DWORD got = 0;
        const DWORD want = std::min<DWORD>(avail, sizeof(tmp));
        if (!ReadFile(err, tmp, want, &got, nullptr) || got == 0)
            return;
        stderrTail_.append(tmp, int(got));
        if (stderrTail_.size() > 4096)
            stderrTail_ = stderrTail_.right(2048);
    }
}

bool ChildProcess::isRunning() const
{
    if (process_ == nullptr)
        return false;
    DWORD code = 0;
    if (!GetExitCodeProcess(handleOf(process_), &code))
        return false;
    return code == STILL_ACTIVE;
}

void ChildProcess::kill()
{
    if (process_ != nullptr) {
        HANDLE h = handleOf(process_);
        DWORD code = 0;
        if (GetExitCodeProcess(h, &code) && code == STILL_ACTIVE) {
            TerminateProcess(h, 1);
            WaitForSingleObject(h, 1500);
        }
        CloseHandle(h);
        process_ = nullptr;
    }
    if (outRead_ != nullptr) {
        CloseHandle(handleOf(outRead_));
        outRead_ = nullptr;
    }
    if (errRead_ != nullptr) {
        CloseHandle(handleOf(errRead_));
        errRead_ = nullptr;
    }
    started_ = false;
}
