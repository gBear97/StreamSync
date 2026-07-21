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

    true_delay = 7.25
    # late enough that the 90 s correlation window is inside the buffer
    # (in production the voice buffer runs continuously, so this is the
    # normal situation after ~90 s of session)
    probe_start_media = 95.0
    seg = voice[int(probe_start_media * SR):
                int((probe_start_media + session.MEASURE_SECONDS) * SR)]
    rng = np.random.default_rng(5)
    degraded = np.clip(seg * 0.9 + 0.15 * np.convolve(
        rng.standard_normal(seg.size), np.hamming(33), "same").astype(np.float32),
        -1, 1)
    probe = fingerprint.fingerprint_samples(degraded, SR)
    probe_t0 = base_utc + probe_start_media + true_delay  # heard this late
    d = session.measure_delay(buf, probe, probe_t0)
    assert d is not None, "delay measurement was inconclusive"
    print(f"delay measurement: true {true_delay}s, measured {d:.2f}s")
    assert abs(d - true_delay) < 0.3, d

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
