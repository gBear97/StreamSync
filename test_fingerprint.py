"""Fingerprint + session-math tests, no network or audio devices needed.

Covers: hash robustness across re-encodes, rejection of wrong media,
verify_media offset detection, and stream-delay measurement from chunked
voice fingerprints.
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
    assert abs(delta) < 0.6, delta
    assert ber < fingerprint.VERIFY_BER

    # 2. wrong media is rejected
    other = os.path.join(tmp, "other.m4a")
    encode(synth(240, seed=99), other, "128k")
    try:
        session.verify_media(host_words, other)
        raise AssertionError("wrong media was NOT rejected")
    except matcher.MatchError as e:
        print(f"wrong media: correctly rejected ({str(e)[:60]}...)")

    # 3. voice delay measurement from chunked fingerprints
    voice = synth(120, seed=42)
    buf = session.VoiceBuffer()
    base_utc = 1_000_000.0
    chunk = int(session.VOICE_CHUNK * SR)
    for i in range(0, len(voice) - chunk, chunk):
        words = fingerprint.fingerprint_samples(voice[i:i + chunk], SR)
        buf.add(base_utc + i / SR, words)

    rng = np.random.default_rng(5)

    def probe_at(media_t, delay):
        """Fingerprint the host's voice as a viewer hears it `delay` late."""
        seg = voice[int(media_t * SR):
                    int((media_t + session.MEASURE_SECONDS) * SR)]
        degraded = np.clip(seg * 0.9 + 0.15 * np.convolve(
            rng.standard_normal(seg.size), np.hamming(33),
            "same").astype(np.float32), -1, 1)
        return (fingerprint.fingerprint_samples(degraded, SR),
                base_utc + media_t + delay)

    true_delay = 7.25
    probe, probe_t0 = probe_at(95.0, true_delay)
    d = session.measure_delay(buf, probe, probe_t0)
    assert d is not None, "delay measurement was inconclusive"
    print(f"delay measurement: true {true_delay}s, measured {d:.2f}s")
    assert abs(d - true_delay) < 0.3, d

    # 3a. short delays must be measurable too. The reference window has to
    # run past the *end* of the probe: a window stopping 5 s after the
    # probe starts can only ever align streams >= MEASURE_SECONDS - 5 s
    # behind, so every viewer nearer than that silently fell back to the
    # host's guess.
    for short in (0.0, 0.75, 2.0, 4.0):
        probe, probe_t0 = probe_at(95.0, short)
        d = session.measure_delay(buf, probe, probe_t0)
        assert d is not None, f"delay of {short}s was unmeasurable"
        assert abs(d - short) < 0.3, (short, d)
    print("delay measurement: 0.0-4.0s delays measure correctly too")

    # 3b. coverage gate. Fingerprinting a VOICE_CHUNK block yields only
    # VOICE_CHUNK - FP_WIN seconds of words, so a gap-free host tops out at
    # VOICE_DUTY; the gate has to clear real per-block overhead while still
    # catching an actual dropout.
    assert session.MIN_VOICE_COVERAGE < session.VOICE_DUTY, "gate unreachable"

    def build_buffer(period, upto):
        b = session.VoiceBuffer()
        t = 0.0
        while t + session.VOICE_CHUNK < upto:
            seg = voice[int(t * SR):int((t + session.VOICE_CHUNK) * SR)]
            b.add(base_utc + t, fingerprint.fingerprint_samples(seg, SR))
            t += period
        return b

    def coverage(b, probe_t0):
        t_from = max(probe_t0 - session.MAX_STREAM_DELAY, b.first_utc())
        return b.timeline(t_from,
                          probe_t0 + session.MEASURE_SECONDS + 1.0)[2]

    for overhead in (0.0, 0.5, 1.0):     # device open + fingerprint + send
        cov = coverage(build_buffer(session.VOICE_CHUNK + overhead, 118),
                       base_utc + 105.0)
        assert cov >= session.MIN_VOICE_COVERAGE, (overhead, cov)
    holed = build_buffer(session.VOICE_CHUNK, 118)
    with holed.lock:                     # host mic died for 40 s
        holed.blocks = [b for b in holed.blocks
                        if not (base_utc + 55 <= b[0] < base_utc + 95)]
    cov = coverage(holed, base_utc + 105.0)
    assert cov < session.MIN_VOICE_COVERAGE, cov
    probe, probe_t0 = probe_at(95.0, true_delay)
    assert session.measure_delay(holed, probe, probe_t0) is None
    print("coverage gate: passes with loop overhead, rejects a real dropout")

    # 3c. a viewer joining mid-session can measure without waiting for the
    # full correlation window to fill with history that does not exist yet
    young = build_buffer(session.VOICE_CHUNK, 30.0)
    probe, probe_t0 = probe_at(16.0, 2.0)
    d = session.measure_delay(young, probe, probe_t0)
    assert d is not None and abs(d - 2.0) < 0.3, d
    print(f"early session: measured {d:.2f}s from only 30 s of host voice")

    # 4. timeline math: delayed rendering delays pauses too
    tl = session.StateTimeline()
    tl.add({"pos": 100.0, "utc": 1000.0, "playing": True, "default_delay": 8})
    tl.add({"pos": 130.0, "utc": 1030.0, "playing": False})
    pos, playing = tl.at(1030.0 - 8.0)   # viewer 8s behind at host pause time
    assert playing and abs(pos - 122.0) < 0.01, (pos, playing)
    pos, playing = tl.at(1039.0)         # 9s later the pause reaches them
    assert not playing and abs(pos - 130.0) < 0.01, (pos, playing)
    print("timeline math: delayed pause lands correctly")

    print("FINGERPRINT TEST PASSED")


if __name__ == "__main__":
    main()
