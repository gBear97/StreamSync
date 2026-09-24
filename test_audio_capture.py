"""AudioMonitor timing, with a fake device instead of a sound card.

The fake has a true sample clock (sample n exists at t_dev + n/sr) and
hands out blocks late by a random scheduling delay, the way a busy
machine does. The monitor must date a recording by that true clock -
not by whenever its read happened to return - slice exactly the samples
asked for, and refuse a recording that spans a delivery stall (WASAPI
loopback sends nothing at all while nothing plays).
"""

import threading
import time

import numpy as np

from audio_capture import AudioMonitor

SR = 8000


class FakeDevice:
    def __init__(self, jitter=0.03, stall_at=None, stall_s=0.0, seed=1):
        self.rng = np.random.default_rng(seed)
        self.jitter = jitter
        self.stall_at = stall_at      # frame where delivery stops...
        self.stall_s = stall_s        # ...for this long, samples lost
        self.t_dev = None
        self.n = 0
        self.lost = 0.0
        self.lock = threading.Lock()

    def true_time(self, frame):
        """When `frame` was really captured (perf_counter)."""
        extra = self.stall_s if self.stall_at is not None and frame >= self.stall_at else 0.0
        return self.t_dev + frame / SR + extra

    # context-manager recorder protocol, as soundcard provides
    def __enter__(self):
        self.t_dev = time.perf_counter()
        return self

    def __exit__(self, *exc):
        return False

    def record(self, numframes):
        end = self.n + numframes
        ready = self.true_time(end - 1)
        delay = ready - time.perf_counter() + self.rng.uniform(0, self.jitter)
        if delay > 0:
            time.sleep(delay)
        data = (np.arange(self.n, end, dtype=np.float32) % 1000) / 1000.0 + 0.01
        self.n = end
        return data


def monitor_for(dev):
    return AudioMonitor("loopback", sr=SR, keep=30, source=lambda: (dev, "hint"))


def main():
    # 1. dating: error vs the true sample clock, over several recordings
    dev = FakeDevice(jitter=0.03)
    mon = monitor_for(dev)
    errs = []
    try:
        for _ in range(4):
            mark = mon.mark()
            data, sr, t0 = mon.capture(0.6, start=mark)
            assert sr == SR and data.size == int(0.6 * SR)
            # samples are contiguous and in order
            want = (np.arange(data.size) + (mark % 1000)) % 1000 / 1000.0 + 0.01
            assert np.allclose(data, want, atol=1e-4), "slice is not contiguous"
            errs.append(t0 - dev.true_time(mark))
        # instant capture of already-buffered audio
        time.sleep(0.2)
        data, _, t0 = mon.capture(1.0)
        assert data.size == SR
    finally:
        mon.stop()
    errs_ms = [1000 * e for e in errs]
    print("stamp error vs true clock (ms): " + ", ".join(f"{e:+.1f}" for e in errs_ms))
    # the bound can only err late, and by little more than the gentlest
    # delivery delay in the next couple of seconds
    assert all(-1.0 < e < 6.0 for e in errs_ms), errs_ms

    # 2. a delivery stall inside the recording is refused
    dev = FakeDevice(jitter=0.005, stall_at=int(0.5 * SR), stall_s=0.6)
    mon = monitor_for(dev)
    try:
        mark = mon.mark()
        try:
            mon.capture(1.0, start=mark)
            raise AssertionError("a recording across a stall was accepted")
        except RuntimeError as e:
            print(f"stall: refused ({e})")
    finally:
        mon.stop()

    # 3. silence is refused on a loopback, allowed when asked
    class Quiet(FakeDevice):
        def record(self, numframes):
            super().record(numframes)
            return np.zeros(numframes, np.float32)
    dev = Quiet(jitter=0.005)
    mon = monitor_for(dev)
    try:
        try:
            mon.capture(0.3, start=mon.mark())
            raise AssertionError("silence was accepted")
        except RuntimeError as e:
            assert "hint" in str(e)
        data, _, _ = mon.capture(0.3, start=mon.mark(), allow_silence=True)
        assert data.size == int(0.3 * SR)
        print("silence: refused on loopback, allowed on request")
    finally:
        mon.stop()

    # 4. a device that fails is reported, not waited on
    def broken():
        raise OSError("device unplugged")
    mon = AudioMonitor("loopback", sr=SR, source=broken)
    try:
        mon.capture(0.2, timeout=2.0)
        raise AssertionError("a failed device was not reported")
    except RuntimeError as e:
        assert "unplugged" in str(e), e
        print(f"broken device: {e}")
    print("AUDIO CAPTURE TEST PASSED")


if __name__ == "__main__":
    main()
