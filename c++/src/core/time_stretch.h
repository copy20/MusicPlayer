// OLA（Overlap-Add）时域音频拉伸：变速播放时保持音调不变。
//
// 为什么需要它：视频侧变速只是改时钟斜率，音频侧如果直接把采样按倍速重采样，
// 2 倍速就会变成"花栗鼠音"。OLA 通过"分析跳距固定、合成跳距变化 + 重叠相加"
// 改变时长而不改变频率，是最省 CPU 的保音调方案（不需要相位声码器）。
//
// 实现要点：
//   * 分析窗用 Hann 窗，50% 重叠；
//   * 因为合成跳距 ≠ 分析跳距，窗函数叠加不再恒为 1，所以额外累加窗权重并做归一化，
//     否则会出现周期性音量起伏（"颤音"）。
#pragma once

#include <QByteArray>
#include <vector>

class OlaStretcher {
public:
    OlaStretcher(int channels = 2, int sampleRate = 48000, double speed = 1.0,
                 double frameMs = 20.0);

    void setFormat(int channels, int sampleRate);
    void setSpeed(double speed);          // 0.5 ~ 4.0（再慢会出现半段静音）；1.0 时建议直接旁路
    double speed() const { return speed_; }
    bool active() const;                  // 是否需要拉伸（≈1.0 时返回 false）

    void reset();

    /// 喂入交错 S16LE 数据（内部转 float 处理）
    void push(const char *data, int bytes);

    /// 取出最多 maxBytes 字节（保证按采样帧对齐）；可能少于请求量
    QByteArray pull(int maxBytes);

    /// 把内部残留都挤出来（用于收尾）
    QByteArray flush();

private:
    void process();
    int frameLength() const { return frameLen_; }
    int analysisHop() const { return frameLen_ / 2; }
    int synthesisHop() const;
    void ensureBuffers();

    int channels_ = 2;
    int sampleRate_ = 48000;
    double speed_ = 1.0;
    double frameMs_ = 20.0;
    int frameLen_ = 960;
    int outHop_ = 480;

    std::vector<float> in_;        // 输入环形缓冲（交错 float）
    std::vector<float> acc_;       // 信号累加器
    std::vector<float> winAcc_;    // 窗权重累加器
    QByteArray outQueue_;          // 待输出的 S16LE
    QByteArray inRemainder_;       // 不足一个采样点的残余字节
};
