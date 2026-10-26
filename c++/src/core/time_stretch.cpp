#include "core/time_stretch.h"

#include <QtGlobal>
#include <algorithm>
#include <cmath>
#include <cstring>

// MSVC 默认不定义 M_PI，这里自己给一个，省得跨编译器踩坑
static constexpr double kPi = 3.14159265358979323846;

OlaStretcher::OlaStretcher(int channels, int sampleRate, double speed, double frameMs)
    : channels_(channels > 0 ? channels : 2)
    , sampleRate_(sampleRate > 0 ? sampleRate : 48000)
    , speed_(speed > 0.05 ? speed : 1.0)
    , frameMs_(frameMs > 1.0 ? frameMs : 20.0)
{
    setFormat(channels_, sampleRate_);
    setSpeed(speed_);
}

void OlaStretcher::setFormat(int channels, int sampleRate)
{
    channels_ = channels > 0 ? channels : 2;
    sampleRate_ = sampleRate > 0 ? sampleRate : 48000;
    frameLen_ = std::max(64, int(sampleRate_ * frameMs_ / 1000.0));
    // 保证是偶数，方便 50% 重叠
    if (frameLen_ % 2)
        ++frameLen_;
    setSpeed(speed_);
    reset();
}

void OlaStretcher::setSpeed(double speed)
{
    // 下限锁在 0.5：合成跳距一旦超过窗长（speed < 0.5），一个分析窗填不满输出区间，
    // 多出来的部分只能是静音，听起来就是"一半响一半没声"。GUI 的最慢档也正是 0.5。
    speed_ = std::clamp(speed, 0.5, 4.0);
    outHop_ = std::max(32, int(std::lround(analysisHop() / speed_)));
    ensureBuffers();
}

bool OlaStretcher::active() const
{
    return std::fabs(speed_ - 1.0) > 1e-3;
}

void OlaStretcher::reset()
{
    in_.clear();
    in_.shrink_to_fit();
    outQueue_.clear();
    inRemainder_.clear();
    ensureBuffers();
}

void OlaStretcher::ensureBuffers()
{
    const int need = (frameLen_ + outHop_) * channels_;
    acc_.assign(need, 0.0f);
    winAcc_.assign(need, 0.0f);
}

void OlaStretcher::push(const char *data, int bytes)
{
    if (!data || bytes <= 0)
        return;

    inRemainder_.append(data, bytes);
    const int unit = int(sizeof(qint16)) * channels_;      // 一个采样帧的字节数
    const int usable = (inRemainder_.size() / unit) * unit;
    if (usable <= 0)
        return;

    const qint16 *src = reinterpret_cast<const qint16 *>(inRemainder_.constData());
    const int samples = usable / int(sizeof(qint16));
    const size_t base = in_.size();
    in_.resize(base + size_t(samples));
    for (int i = 0; i < samples; ++i)
        in_[base + size_t(i)] = float(src[i]) / 32768.0f;

    if (usable < inRemainder_.size())
        inRemainder_.remove(0, usable);
    else
        inRemainder_.clear();

    process();
}

int OlaStretcher::synthesisHop() const
{
    return outHop_;
}

void OlaStretcher::process()
{
    const int n = frameLen_;
    const int ch = channels_;
    const int ha = analysisHop();
    const int hs = outHop_;
    const int accLen = n + hs;

    // Hann 窗（周期性，50% 重叠时两两相加 ≈1）
    static thread_local std::vector<float> win;
    if (int(win.size()) != n) {
        win.resize(size_t(n));
        for (int i = 0; i < n; ++i)
            win[size_t(i)] = float(0.5 - 0.5 * std::cos(2.0 * kPi * i / double(n)));
    }

    // WSOLA 的"相似度搜索"：纯粹按固定跳距叠加，加速时相邻两帧会因相位差互相抵消，
    // 叠加后的波形被削平 —— 实测 1.5x 音量只剩约 25%、2x 约 65%，听感是又闷又抖。
    // 所以在叠加前先在这 ±tol 范围内找一段与"已叠加但还没输出"的部分最同相的输入，
    // 让它接上去，能量就不会自相残杀。
    // tol 取半个分析跳距（且不超过 1/4 窗长），足够覆盖最低频的一个周期；
    // 减速（hs >= n）时一帧独立输出，不存在叠加抵消，不必搜索。
    const int tol = (hs >= n) ? 0 : std::max(8, std::min(ha / 2, n / 4));
    const int overlap = std::max(0, n - hs);   // acc_ 里"已叠加但还没输出"的长度

    while (int(in_.size()) / ch >= n + 2 * tol) {
        // 默认起点放在正中间，前后各留 tol 作搜索余量
        int start = tol;
        if (tol > 0 && overlap > 32 && winAcc_[0] > 1e-4f) {
            double bestScore = -1e300;
            for (int d = -tol; d <= tol; d += 2) {
                const int base = tol + d;
                double dot = 0.0;
                double energy = 0.0;
                // 只比第一声道、每 4 帧取样：相位对齐对降采样不敏感，够用且省 CPU
                for (int i = 0; i < overlap; i += 4) {
                    const float w = win[size_t(i)];
                    const float a = acc_[size_t(i * ch)] * w;
                    const float b = in_[size_t((base + i) * ch)] * w;
                    dot += double(a) * double(b);
                    energy += double(b) * double(b);
                }
                if (energy <= 1e-9)
                    continue;
                const double score = dot / std::sqrt(energy);
                if (score > bestScore) {
                    bestScore = score;
                    start = base;
                }
            }
        }

        // 1) 加窗累加：整帧叠加到 acc_ 的 [0, n)
        for (int i = 0; i < n; ++i) {
            const float w = win[size_t(i)];
            const int src = (start + i) * ch;
            const int dst = i * ch;
            for (int c = 0; c < ch; ++c) {
                acc_[size_t(dst + c)] += in_[size_t(src + c)] * w;
                winAcc_[size_t(dst + c)] += w;
            }
        }

        // 2) 输出 hs 个采样帧（做窗权重归一化，消除音量起伏）
        const int outBytes = hs * ch * int(sizeof(qint16));
        const int offset = outQueue_.size();
        outQueue_.resize(offset + outBytes);
        qint16 *dst = reinterpret_cast<qint16 *>(outQueue_.data() + offset);
        for (int i = 0; i < hs; ++i) {
            const int base = i * ch;
            for (int c = 0; c < ch; ++c) {
                const float wsum = winAcc_[size_t(base + c)];
                float v = wsum > 1e-4f ? acc_[size_t(base + c)] / wsum : 0.0f;
                v = std::clamp(v, -1.0f, 1.0f);
                dst[i * ch + c] = qint16(std::lround(v * 32767.0f));
            }
        }

        // 3) 累加器左移 hs
        const int keep = accLen - hs;
        std::memmove(acc_.data(), acc_.data() + size_t(hs) * ch, size_t(keep) * ch * sizeof(float));
        std::memmove(winAcc_.data(), winAcc_.data() + size_t(hs) * ch,
                     size_t(keep) * ch * sizeof(float));
        std::fill(acc_.begin() + size_t(keep) * ch, acc_.end(), 0.0f);
        std::fill(winAcc_.begin() + size_t(keep) * ch, winAcc_.end(), 0.0f);

        // 4) 输入恒定前移一个分析跳距（与 start 无关！）
        //    变速比例只由 ha/hs 决定；搜索偏移只改变"取哪一段"，不能参与推进量，
        //    否则平均速率会被搜索带偏，时长就不准了。
        in_.erase(in_.begin(), in_.begin() + size_t(ha) * ch);
    }
}

QByteArray OlaStretcher::pull(int maxBytes)
{
    const int unit = int(sizeof(qint16)) * channels_;
    const int want = (maxBytes / unit) * unit;
    if (want <= 0)
        return QByteArray();

    QByteArray out;
    out.reserve(want);
    while (out.size() < want && !outQueue_.isEmpty()) {
        const int take = std::min(want - out.size(), outQueue_.size());
        out.append(outQueue_.constData(), take);
        outQueue_.remove(0, take);
    }
    return out;
}

QByteArray OlaStretcher::flush()
{
    QByteArray rest = outQueue_;
    outQueue_.clear();

    // 尾部还不足一整帧的输入（最多约一帧，拉伸后几十毫秒）：
    // 按同样倍速直接抽样拼上去。不做重叠相加是因为凑不出完整窗口了，
    // 而"结尾被硬切掉"比"最后 20ms 音质粗一点"更明显。
    const int ch = channels_;
    const int frames = int(in_.size()) / ch;
    if (frames > 0 && speed_ > 0.0) {
        const int outFrames = std::max(1, int(std::lround(double(frames) / speed_)));
        const int offset = rest.size();
        const int addBytes = outFrames * ch * int(sizeof(qint16));
        rest.resize(offset + addBytes);
        qint16 *dst = reinterpret_cast<qint16 *>(rest.data() + offset);
        for (int i = 0; i < outFrames; ++i) {
            const int src = std::min(frames - 1, int(double(i) * speed_));
            for (int c = 0; c < ch; ++c) {
                const float v = std::clamp(in_[size_t(src * ch + c)], -1.0f, 1.0f);
                dst[i * ch + c] = qint16(std::lround(v * 32767.0f));
            }
        }
    }

    in_.clear();
    inRemainder_.clear();
    return rest;
}
