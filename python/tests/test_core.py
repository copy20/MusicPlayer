"""内核单元测试（不需要 Qt，不需要 ffmpeg）。

    python -m tests.test_core           # 或 python tests/test_core.py

覆盖：
* 媒体时钟：变速积分、暂停/恢复、seek、外部时钟软校正
* 缓存队列：背压阻塞、时长限流、generation 失效、丢弃策略、统计
* 变速：atempo 级联、OLA 拉伸倍率与保真度
* 合成后端：帧尺寸/时序、seek、EOS
* 多线程联跑：读线程 + 有界队列 + 渲染节奏，验证不死锁、顺序正确
"""
from __future__ import annotations

import math
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.backends.synthetic import SyntheticBackend  # noqa: E402
from app.core.clock import MediaClock, format_time  # noqa: E402
from app.core.dsp.time_stretch import OlaStretcher, build_atempo_chain  # noqa: E402
from app.core.frame_queue import FrameQueue  # noqa: E402
from app.core.frames import AudioFrame, VideoFrame  # noqa: E402
from app.core.workers.reader import MediaReadThread  # noqa: E402
from app.qtcompat import QtCore  # noqa: E402


# ==================================================================== 时钟
class TestMediaClock(unittest.TestCase):
    def test_paused_clock_does_not_move(self):
        clock = MediaClock()
        self.assertTrue(clock.is_paused())
        time.sleep(0.05)
        self.assertAlmostEqual(clock.get_time(), 0.0, places=4)

    def test_speed_integrates(self):
        clock = MediaClock(speed=1.0)
        clock.resume()
        time.sleep(0.2)
        t1 = clock.get_time()
        self.assertGreater(t1, 0.15)
        self.assertLess(t1, 0.35)

    def test_speed_change_does_not_jump_position(self):
        clock = MediaClock()
        clock.resume()
        time.sleep(0.1)
        before = clock.get_time()
        clock.set_speed(2.0)
        self.assertAlmostEqual(clock.get_time(), before, places=2)
        time.sleep(0.2)
        after = clock.get_time()
        # 0.2 秒真实时间 @2x，应推进约 0.4 秒媒体时间
        self.assertGreater(after - before, 0.30)
        self.assertLess(after - before, 0.55)

    def test_pause_resume(self):
        clock = MediaClock()
        clock.resume()
        time.sleep(0.1)
        clock.pause()
        frozen = clock.get_time()
        time.sleep(0.1)
        self.assertAlmostEqual(clock.get_time(), frozen, places=3)
        clock.resume()
        time.sleep(0.05)
        self.assertGreater(clock.get_time(), frozen)

    def test_seek_and_nudge(self):
        clock = MediaClock()
        clock.set_time(100.0)
        self.assertAlmostEqual(clock.get_time(), 100.0, places=3)
        clock.nudge(-5.0)
        self.assertAlmostEqual(clock.get_time(), 95.0, places=3)

    def test_correct_soft_and_hard(self):
        clock = MediaClock()
        clock.set_time(10.0)
        # 小偏差：只按比例追
        clock.correct(10.05, big_diff=0.25, factor=0.5)
        self.assertAlmostEqual(clock.get_time(), 10.025, places=3)
        # 大偏差：直接跳
        clock.correct(20.0, big_diff=0.25, factor=0.5)
        self.assertAlmostEqual(clock.get_time(), 20.0, places=3)

    def test_slow_speed(self):
        clock = MediaClock(speed=0.5)
        clock.resume()
        time.sleep(0.2)
        self.assertGreater(clock.get_time(), 0.05)
        self.assertLess(clock.get_time(), 0.18)

    def test_format_time(self):
        self.assertEqual(format_time(75), "01:15")
        self.assertEqual(format_time(3671), "1:01:11")
        self.assertEqual(format_time(None), "00:00")


# ==================================================================== 队列
def _mkframe(pts, duration=0.04):
    return VideoFrame(pts=pts, duration=duration, width=2, height=2, data=b"\x00" * 12)


class TestFrameQueue(unittest.TestCase):
    def test_basic_put_get(self):
        q = FrameQueue("t", max_frames=4)
        self.assertTrue(q.put(_mkframe(0.0)))
        got = q.get(timeout=0.2)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got.pts, 0.0)

    def test_get_timeout_returns_none_and_counts_starve(self):
        q = FrameQueue("t", max_frames=2)
        self.assertIsNone(q.get(timeout=0.05))
        self.assertGreaterEqual(q.stats.starve_events, 1)

    def test_block_backpressure(self):
        q = FrameQueue("t", max_frames=2)
        for i in range(2):
            self.assertTrue(q.put(_mkframe(i * 0.04)))
        result = {"ok": None}

        def producer():
            result["ok"] = q.put(_mkframe(2 * 0.04), timeout=0.2)

        t = threading.Thread(target=producer)
        t.start()
        time.sleep(0.05)
        self.assertTrue(t.is_alive(), "队列满时生产者应被阻塞")
        self.assertGreaterEqual(q.stats.full_events, 1)
        q.get(timeout=0.2)          # 腾位置
        t.join(1.0)
        self.assertTrue(result["ok"])
        self.assertFalse(t.is_alive())

    def test_drop_oldest_policy(self):
        q = FrameQueue("t", max_frames=2, overflow="drop_oldest")
        for i in range(5):
            q.put(_mkframe(i * 0.04))
        self.assertEqual(q.size, 2)
        self.assertEqual(q.stats.dropped_by_producer, 3)

    def test_max_duration_limit(self):
        q = FrameQueue("t", max_frames=100, max_duration=0.2)
        for i in range(2):
            q.put(_mkframe(i * 0.1, duration=0.1))
        self.assertTrue(q.put(_mkframe(0.2, duration=0.1), timeout=0.05) is False)

    def test_clear_bumps_serial_and_drops_frames(self):
        q = FrameQueue("t", max_frames=8)
        q.put(_mkframe(0.0))
        old_serial = q.serial
        dropped = q.clear()
        self.assertEqual(dropped, 1)
        self.assertEqual(q.serial, old_serial + 1)
        self.assertTrue(q.is_empty())

    def test_stale_serial_frame_is_rejected(self):
        q = FrameQueue("t", max_frames=8)
        serial = q.serial
        q.clear()                                   # generation +1
        self.assertFalse(q.put(_mkframe(9.0), serial=serial))
        self.assertTrue(q.put(_mkframe(9.0), serial=q.serial))

    def test_peek_and_buffered_duration(self):
        q = FrameQueue("t", max_frames=8)
        for i in range(3):
            q.put(_mkframe(i * 0.04, duration=0.04))
        self.assertAlmostEqual(q.peek().pts, 0.0)
        self.assertAlmostEqual(q.peek(1).pts, 0.04)
        self.assertAlmostEqual(q.buffered_duration(), 0.12, places=3)

    def test_wait_ready(self):
        q = FrameQueue("t", max_frames=8)
        self.assertFalse(q.wait_ready(min_frames=2, timeout=0.05))
        threading.Timer(0.1, lambda: [q.put(_mkframe(i * 0.04)) for i in range(3)]).start()
        self.assertTrue(q.wait_ready(min_frames=3, timeout=1.0))

    def test_mark_eos_and_snapshot(self):
        q = FrameQueue("t", max_frames=4)
        q.put(_mkframe(0.0))
        q.mark_eos()
        self.assertTrue(q.is_eos)
        snap = q.snapshot()
        self.assertEqual(snap["size"], 1)
        self.assertTrue(snap["eos"])

    def test_consumer_drop_front(self):
        q = FrameQueue("t", max_frames=4)
        for i in range(3):
            q.put(_mkframe(i * 0.04))
        self.assertTrue(q.drop_front())
        self.assertEqual(q.stats.dropped_by_consumer, 1)
        self.assertAlmostEqual(q.peek().pts, 0.04)

    def test_close_wakes_blocked_get(self):
        q = FrameQueue("t", max_frames=4)
        done = threading.Event()
        threading.Thread(target=lambda: (q.get(timeout=5.0), done.set()), daemon=True).start()
        time.sleep(0.05)
        q.close()
        self.assertTrue(done.wait(1.0), "close() 应唤醒阻塞的消费者")


# ==================================================================== 变速
class TestTempoChain(unittest.TestCase):
    def test_in_range_returns_single_stage(self):
        self.assertEqual(build_atempo_chain(1.5), [1.5])
        self.assertEqual(build_atempo_chain(0.75), [0.75])

    def test_out_of_range_chained(self):
        for target in (4.0, 3.0, 0.25, 0.3):
            chain = build_atempo_chain(target)
            product = 1.0
            for v in chain:
                self.assertGreaterEqual(v, 0.5 - 1e-9)
                self.assertLessEqual(v, 2.0 + 1e-9)
                product *= v
            self.assertAlmostEqual(product, target, places=2)


class TestOlaStretcher(unittest.TestCase):
    SR = 48000
    CH = 2

    @staticmethod
    def _signal(units: int) -> bytes:
        """带包络的测试信号（避免纯直流导致 Parseval 判据失效）。"""
        import array
        out = array.array("h")
        for i in range(units):
            v = 0.5 * math.sin(2 * math.pi * 440 * i / 48000.0)
            v *= 0.5 + 0.5 * math.sin(2 * math.pi * 3 * i / 48000.0)
            iv = int(max(-1.0, min(1.0, v)) * 30000)
            for _ in range(2):
                out.append(iv)
        return out.tobytes()

    def _stretch(self, data, speed, chunk_units=1024):
        st = OlaStretcher(channels=self.CH, sample_rate=self.SR, speed=speed, frame_ms=20.0)
        unit = self.CH * 2
        pos = 0
        out = bytearray()
        while pos < len(data):
            st.push(data[pos:pos + chunk_units * unit])
            pos += chunk_units * unit
            out.extend(st.pull(chunk_units * unit))
        # 输入喂完后必须排空（每次迭代的输出量可能超过单次请求量）
        while True:
            tail = st.pull(chunk_units * unit)
            if not tail:
                break
            out.extend(tail)
        out.extend(st.flush())
        return bytes(out)

    def test_speed_ratio(self):
        data = self._signal(48000)              # 约 1 秒
        for speed in (0.5, 1.0, 2.0):
            out = self._stretch(data, speed)
            ratio = (len(out) / len(data))
            self.assertAlmostEqual(ratio, 1.0 / speed, delta=0.06,
                                   msg="倍速 %.1f 输出长度比例异常: %.3f" % (speed, ratio))

    def test_unity_gain_and_fidelity_at_1x(self):
        data = self._signal(24000)
        out = self._stretch(data, 1.0)
        # 在若干延迟点上找最佳匹配（OLA 必然引入固定延迟）
        import array
        a = array.array("h"); a.frombytes(data)
        b = array.array("h"); b.frombytes(out)
        best = (float("inf"), -1)
        for delay in range(0, 4000, 8):
            n = min(len(a) - delay, len(b) - delay, 8000)
            if n <= 512:
                continue
            err = 0.0
            energy = 0.0
            for i in range(0, n, 7):          # 抽样比较，控制测试耗时
                d = b[i + delay] - a[i]
                err += d * d
                energy += a[i] * a[i]
            rms_err = math.sqrt(err / (n / 7.0))
            rms_sig = math.sqrt(energy / (n / 7.0)) or 1.0
            score = rms_err / rms_sig
            if score < best[0]:
                best = (score, delay)
        self.assertLess(best[0], 0.15, "1x 时输出与输入差异过大: %.3f" % best[0])

    def test_reset_clears_state(self):
        st = OlaStretcher(channels=2, sample_rate=self.SR, speed=2.0)
        st.push(self._signal(4096))
        st.pull(4096 * 4)
        st.reset()
        self.assertEqual(st.available_units(), 0)


# ==================================================================== 合成后端
class TestSyntheticBackend(unittest.TestCase):
    def test_frame_shape_and_timeline(self):
        b = SyntheticBackend(width=64, height=48, fps=25, duration=1.0)
        info = b.open("synthetic://test")
        self.assertTrue(info.has_video and info.has_audio)
        f0 = b.read_video()
        self.assertIsNotNone(f0)
        self.assertEqual(len(f0.data), 64 * 48 * 3)
        self.assertAlmostEqual(f0.pts, 0.0)
        f1 = b.read_video()
        self.assertAlmostEqual(f1.pts, 0.04)
        b.close()

    def test_audio_frame(self):
        b = SyntheticBackend()
        b.open()
        a = b.read_audio()
        self.assertIsNotNone(a)
        self.assertGreater(a.n_samples, 0)
        self.assertAlmostEqual(a.duration, a.n_samples / 48000.0, places=6)
        b.close()

    def test_seek_and_eos(self):
        b = SyntheticBackend(fps=25, duration=1.0)
        b.open()
        b.seek(0.9)
        got = []
        while True:
            f = b.read_video()
            if f is None:
                break
            got.append(f.pts)
        self.assertTrue(got, "seek 之后应该还能读到帧")
        first = math.floor(0.9 * 25) / 25.0     # seek 会把视频对齐到帧边界
        self.assertAlmostEqual(got[0], first, places=5)
        self.assertLessEqual(got[-1], 1.01)
        b.close()


# ==================================================================== 多线程联跑
class TestPipelineThreads(unittest.TestCase):
    """验证：读线程 + 有界队列 + 渲染节奏 不死锁、顺序正确、背压生效。"""

    def test_read_thread_fills_queue_with_backpressure(self):
        app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
        backend = SyntheticBackend(width=64, height=48, fps=25, duration=5.0)
        backend.open()
        # 故意开很小的队列 -> 必然触发背压
        q = FrameQueue("video", max_frames=4, max_duration=0.5)
        reader = MediaReadThread("video", backend, q)
        consumed = []
        stop = threading.Event()

        def consumer():
            while not stop.is_set():
                frame = q.get(timeout=0.1)
                if frame is not None:
                    consumed.append(frame.pts)
                    time.sleep(0.004)      # 模拟"比解码慢"的渲染端

        c = threading.Thread(target=consumer, daemon=True)
        reader.start()
        c.start()
        time.sleep(1.0)
        stop.set()
        c.join(1.0)
        reader.request_stop()
        q.close()
        reader.wait(2000)

        self.assertGreater(len(consumed), 5, "应该有帧被消费")
        self.assertEqual(consumed, sorted(consumed), "pts 必须单调递增")
        self.assertLessEqual(q.size, 4, "队列容量必须被真正限制住")
        self.assertGreater(reader.stats()["frames"], 0)
        backend.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
