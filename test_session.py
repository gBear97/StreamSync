"""A whole watch party on localhost: relay, host, viewer - end to end.

Real relay process, real HostSession and ViewerSession threads, real
fingerprints; only the edges are simulated. The host's microphone and
the viewer's loopback are fake devices playing the same synthetic voice,
the viewer's a known STREAM_DELAY later, both on the true perf_counter
clock. Internet time is skipped (both ends share this machine's clock).

Checks the things that only show up with everything running:
- the viewer verifies its copy and measures the stream delay from the
  host's voice, to within tens of milliseconds;
- the viewer's player follows the host's timeline, delayed by that delay
  and shifted by the user's nudge;
- a dropped host connection resumes the same room, a dropped viewer
  connection rejoins it, and the session carries on;
- leaving stops everything (no microphone left open).
"""

import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import wave

import numpy as np
import imageio_ffmpeg

import audio_capture
import session
import test_fingerprint

PORT = 8898
URL = f"ws://127.0.0.1:{PORT}"
SR = 16000
STREAM_DELAY = 3.37
FILM_START = 20.0


class Voice:
    """A synthetic voice as a function of absolute perf_counter time."""
    def __init__(self, seconds=400):
        self.t0 = time.perf_counter() - 30.0
        self.x = test_fingerprint.synth(seconds, seed=42)

    def at(self, t_first, n):
        i = int(round((t_first - self.t0) * SR))
        return self.x[i:i + n].copy()


class Device:
    """A capture device whose sample n was heard at t_open + n/SR."""
    def __init__(self, voice, delay, opened):
        self.voice, self.delay = voice, delay
        self.n = 0
        self.opened = opened

    def __enter__(self):
        self.t_open = time.perf_counter()
        self.opened.append(self)
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def record(self, numframes):
        ready = self.t_open + (self.n + numframes) / SR
        wait = ready - time.perf_counter()
        if wait > 0:
            time.sleep(wait)
        out = self.voice.at(self.t_open + self.n / SR - self.delay, numframes)
        self.n += numframes
        return out


class Player:
    """Follows sync_seek exactly; records what it was told."""
    def __init__(self):
        self.anchor = None
        self.playing = False
        self.lock = threading.Lock()

    def pos_at(self, perf):
        with self.lock:
            if self.anchor is None:
                return None
            p, t = self.anchor
            return p + (perf - t if self.playing else 0.0)

    def time(self):
        return self.pos_at(time.perf_counter())

    def clock(self):
        now = time.perf_counter()
        return None if not self.playing else (self.pos_at(now), now)

    def is_playing(self):
        return self.playing

    def sync_seek(self, match_t, t0, offset):
        now = time.perf_counter()
        with self.lock:
            self.anchor = (match_t + (now - t0) + offset, now)
            self.playing = True

    def pause(self):
        now = time.perf_counter()
        p = self.pos_at(now)
        with self.lock:
            self.anchor, self.playing = (p, now), False

    def seek(self, t):
        with self.lock:
            self.anchor = (t, time.perf_counter())

    def set_mute(self, m):
        pass


class LocalClock(session.SharedClock):
    """Both ends on this machine already share a clock - no NTP."""
    def sync(self, samples=4):
        self.synced = True
        return self.offset


def make_film(path):
    x = test_fingerprint.synth(90, seed=11)
    wav = path + ".wav"
    with wave.open(wav, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes((x * 32767).astype(np.int16).tobytes())
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-y",
                    "-i", wav, "-c:a", "aac", "-b:a", "96k", path],
                   check=True, capture_output=True)


def wait(cond, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def main():
    tmp = tempfile.mkdtemp()
    film = os.path.join(tmp, "film.m4a")
    make_film(film)

    voice = Voice()
    opened = []
    real_monitor = audio_capture.AudioMonitor

    def monitor(kind, name=None, sr=None, keep=90.0, **kw):
        delay = 0.0 if kind == "mic" else STREAM_DELAY
        return real_monitor(
            kind, name, sr=SR, keep=keep,
            source=lambda: (Device(voice, delay, opened), None))

    session.audio_capture.AudioMonitor = monitor
    session.SharedClock = LocalClock
    session.MEASURE_LOOKBACK = 20.0
    session.MEASURE_SECONDS = 6.0
    session.MEASURE_INTERVAL = 7.0
    session.VERIFY_WINDOW = 20.0

    relay = subprocess.Popen([sys.executable, "relay_server.py",
                              "--port", str(PORT), "--host-grace", "20"],
                             stdout=subprocess.DEVNULL)
    host = viewer = None
    try:
        time.sleep(1.5)
        # the host plays its film from FILM_START, in its own player
        t_start = time.perf_counter()

        def host_position():
            now = time.perf_counter()
            return FILM_START + (now - t_start), True, now

        hq, vq = queue.Queue(), queue.Queue()
        host = session.HostSession(URL, film, hq, position_source=host_position,
                                   mic_name="fake", default_delay=8.0)
        host.start()
        wait(lambda: host.code is not None, 60, "the room")
        print(f"host: room {host.code}")

        player = Player()
        viewer = session.ViewerSession(URL, host.code, film, player, vq)
        viewer.start()
        wait(lambda: viewer.verified, 60, "verification")
        print(f"viewer: verified, file offset {viewer.delta:+.3f}s")
        assert abs(viewer.delta) < 0.05, viewer.delta

        wait(lambda: viewer.delay is not None, 60, "a delay measurement")
        print(f"viewer: stream delay {viewer.delay:.3f}s (true {STREAM_DELAY}s)")
        assert abs(viewer.delay - STREAM_DELAY) < 0.05, viewer.delay

        # the player follows the host, STREAM_DELAY behind, plus a nudge
        viewer.offset = 0.4
        time.sleep(2.5)
        now = time.perf_counter()
        want = FILM_START + (now - t_start) - viewer.effective_delay() + 0.4
        got = player.pos_at(now)
        print(f"viewer: player at {got:.3f}, host-delayed target {want:.3f}")
        assert abs(got - want) < session.DRIFT_TOLERANCE + 0.05, (got, want)

        # connections drop; both ends come back to the same room
        code = host.code
        host.link.ws.close()
        wait(lambda: any("reconnected" in m for m in drain(hq)), 30,
             "the host to resume")
        viewer.link.ws.close()
        wait(lambda: any("Reconnected" in m for m in drain(vq)), 30,
             "the viewer to rejoin")
        assert host.code == code and not viewer.stop_flag.is_set()
        n_before = len(viewer.voice.blocks)
        wait(lambda: len(viewer.voice.blocks) > n_before, 15,
             "voice to flow again after reconnecting")
        print("reconnect: host resumed and viewer rejoined the same room")

        # leaving closes every device and ends the viewer's session
        host.stop()
        wait(lambda: viewer.stop_flag.is_set(), 10, "the viewer to hear it ended")
        viewer.stop()
        wait(lambda: all(getattr(d, "closed", False) for d in opened), 10,
             "every capture device to close")
        print(f"teardown: session ended, all {len(opened)} devices closed")
        print("SESSION TEST PASSED")
    finally:
        for s in (host, viewer):
            if s is not None:
                s.stop()
        relay.terminate()


_seen = {}


def drain(q):
    """All session messages so far on q (kept, so repeated waits see them)."""
    seen = _seen.setdefault(id(q), [])
    while not q.empty():
        kind, *payload = q.get()
        if kind == "session":
            seen.append(payload[0])
    return seen


if __name__ == "__main__":
    main()
