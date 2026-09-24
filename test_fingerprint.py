"""Fingerprint + session-math tests, no network or audio devices needed.

Covers: hash robustness across re-encodes, rejection of wrong media,
verify_media offset detection, stream-delay measurement from chunked
voice fingerprints, and the listen-mode host's pause bookkeeping.

Delays and offsets are deliberately NOT multiples of the 250 ms
fingerprint hop: the first version of this test used 7.25 s, which the
hop-quantized alignment of the time happened to hit exactly, and so it
never saw errors of up to 125 ms everywhere else.
"""

import os
import subprocess
import tempfile
import wave

import numpy as np
import imageio_ffmpeg

import audio_matcher
import fingerprint
import matcher
import session

SR = 16000


def synth(dur, seed):
    """Film-like audio: random tone segments + enveloped noise."""
    rng = np.random.default_rng(seed)
    n = int(dur * SR)
    x = np.zeros(n)
    seg = SR // 4
    tt = np.arange(seg) / SR
    ramp = np.minimum(1.0, np.minimum(np.arange(seg), np.arange(seg)[::-1]) / (0.01 * SR))
    for s in range(n // seg):
        for f in np.exp(rng.uniform(np.log(150), np.log(3800), 2)):
            x[s * seg:(s + 1) * seg] += 0.3 * np.sin(2 * np.pi * f * tt) * ramp
    env = rng.uniform(0, 1, int(dur * 20)) ** 2
    x += 0.35 * rng.standard_normal(n) * np.interp(
        np.arange(n), np.linspace(0, n, env.size), env)
    return (0.7 * x / np.abs(x).max()).astype(np.float32)


def encode(x, path, bitrate):
    wav = path + ".wav"
    with wave.open(wav, "wb") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(SR)
        f.writeframes((x * 32767).astype(np.int16).tobytes())
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-y",
                    "-i", wav, "-c:a", "aac", "-b:a", bitrate, path],
                   check=True, capture_output=True)
    os.remove(wav)


def main():
    tmp = tempfile.mkdtemp()
    film = synth(240, seed=11)

    print("encoding two 'releases' of the same film audio...")
    rel_a = os.path.join(tmp, "release_a.m4a")   # the host's copy
    rel_b = os.path.join(tmp, "release_b.m4a")   # the viewer's copy:
    encode(film, rel_a, "128k")                  # different bitrate, gain,
    encode((film * 0.82), rel_b, "96k")          # same content

    host_words = fingerprint.fingerprint_file(rel_a)
    print(f"host fingerprint: {len(host_words)} words "
          f"({len(host_words) * 2 // 1024} KB for {240 / 60:.0f} min)")

    # 1. same media verifies, offset ~0
    delta, ber = session.verify_media(host_words, rel_b)
    print(f"same media: verified, delta {delta:+.3f}s, worst ber {ber:.3f}")
    assert abs(delta) < 0.05, delta
    assert ber < fingerprint.VERIFY_BER

    # 1b. a release with 1.37 s of extra lead-in (another studio logo)
    shifted = os.path.join(tmp, "release_c.m4a")
    lead = np.zeros(int(1.37 * SR), np.float32)
    encode(np.concatenate([lead, film * 0.9]), shifted, "96k")
    delta, ber = session.verify_media(host_words, shifted)
    print(f"shifted release: delta {delta:+.3f}s (true +1.370s), ber {ber:.3f}")
    assert abs(delta - 1.37) < 0.04, delta

    # 2. wrong media is rejected
    other = os.path.join(tmp, "other.m4a")
    encode(synth(240, seed=99), other, "128k")
    try:
        session.verify_media(host_words, other)
        raise AssertionError("wrong media was NOT rejected")
    except matcher.MatchError as e:
        print(f"wrong media: correctly rejected ({str(e)[:60]}...)")

    # 3. voice delay measurement: host words cut on the UTC grid, as
    # HostSession sends them
    voice = synth(120, seed=42)
    buf = session.VoiceBuffer()
    base_utc = 1_000_000.0                  # a multiple of the hop
    n = int(round(session.VOICE_CHUNK / fingerprint.FP_HOP))
    hop = int(fingerprint.FP_HOP * SR)
    span = int((session.VOICE_CHUNK + fingerprint.FP_WIN) * SR)
    for i in range(0, len(voice) - span, n * hop):
        words = fingerprint.fingerprint_samples(voice[i:i + span], SR)[:n]
        buf.add(base_utc + i / SR, words)

    # late enough that the 90 s correlation window is inside the buffer
    # (in production the voice buffer runs continuously, so this is the
    # normal situation after ~90 s of session)
    rng = np.random.default_rng(5)
    errs = []
    for true_delay in (7.25, 7.10, 7.375, 7.40, 7.61, 12.93):
        start = 95.0 - (true_delay - 7.25)      # keep the probe in range
        seg = voice[int(start * SR):int((start + session.MEASURE_SECONDS) * SR)]
        degraded = np.clip(seg * 0.9 + 0.15 * np.convolve(
            rng.standard_normal(seg.size), np.hamming(33), "same").astype(np.float32),
            -1, 1)
        probe_t0 = base_utc + start + true_delay       # heard this late
        d = session.measure_delay(buf, degraded, SR, probe_t0)
        assert d is not None, f"delay {true_delay}: inconclusive"
        errs.append(d - true_delay)
        print(f"delay measurement: true {true_delay:.3f}s, measured {d:.3f}s "
              f"(err {1000 * (d - true_delay):+.0f} ms)")
    assert max(abs(e) for e in errs) < 0.04, errs

    # 4. timeline math: delayed rendering delays pauses too
    tl = session.StateTimeline()
    tl.add({"pos": 100.0, "utc": 1000.0, "playing": True, "default_delay": 8})
    tl.add({"pos": 130.0, "utc": 1030.0, "playing": False})
    pos, playing = tl.at(1030.0 - 8.0)   # viewer 8s behind at host pause time
    assert playing and abs(pos - 122.0) < 0.01, (pos, playing)
    pos, playing = tl.at(1039.0)         # 9s later the pause reaches them
    assert not playing and abs(pos - 130.0) < 0.01, (pos, playing)
    print("timeline math: delayed pause lands correctly")

    listen_mode_pause()

    print("FINGERPRINT TEST PASSED")


def listen_mode_pause():
    """A listen-mode host pauses at utc 1012 (film 112) after a good match
    at utc 1000 (film 100); checks run every 4 s.

    The old code relabelled the 1000/100 anchor "paused", so viewers were
    sent back to 100 and paused ~12 s early. Now: one miss is not a pause,
    two are, the pause lands where the audio stopped, and the resume is
    dated to when playback restarted.
    """
    tr = session.ListenTracker()
    tl = session.StateTimeline()

    def publish(state):
        if state is not None:
            pos, utc, playing = state
            tl.add({"pos": pos, "utc": utc, "playing": playing,
                    "default_delay": 10})

    publish(tr.hit(100.0, 1000.0))
    publish(tr.hit(104.0, 1004.0))
    publish(tr.hit(108.0, 1008.0))
    # the 1012 recording is silent from its start; so is the 1016 one
    publish(tr.miss(1012.0, stopped_utc=1012.0))
    assert tr.anchor[2], "one miss must not count as a pause"
    publish(tr.miss(1016.0, stopped_utc=1012.0))
    pos, at, playing = tr.anchor
    assert not playing and abs(pos - 112.0) < 1e-6 and at == 1012.0, tr.anchor
    for wall, want_pos, want_play in ((1018.0, 108.0, True), (1021.9, 111.9, True),
                                      (1024.0, 112.0, False)):
        p, pl = tl.at(wall - 10.0)
        assert pl == want_play and abs(p - want_pos) < 1e-6, (wall, p, pl)
    print("listen-mode pause: viewers pause at 112 when it reaches them, "
          "not back at 100")

    # host resumes at utc 1030; the 1032 recording finds film 114
    publish(tr.hit(114.0, 1032.0))
    pos, at, playing = tr.anchor
    assert playing and abs(pos - 112.0) < 1e-6 and abs(at - 1030.0) < 1e-6, tr.anchor
    p, pl = tl.at(1031.0)
    assert pl and abs(p - 113.0) < 1e-6, (p, pl)
    print("listen-mode resume: dated to when playback restarted (1030), "
          "not when it was heard (1032)")

    # a host who jumps elsewhere while paused is not back-dated
    tr2 = session.ListenTracker()
    tr2.hit(100.0, 1000.0)
    tr2.miss(1004.0, 1004.0)
    tr2.miss(1008.0, 1004.0)
    assert tr2.hit(900.0, 1020.0) == (900.0, 1020.0, True)


if __name__ == "__main__":
    main()
