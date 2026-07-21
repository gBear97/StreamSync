"""End-to-end check of the audio matcher.

Renders 3 minutes of film-like audio (random note segments plus
envelope-modulated noise -- rich temporal structure, like music and
effects), AAC-encodes it, simulates a loopback capture at a known moment
with louder 'commentary' noise mixed over it, and checks that
find_match_audio recovers the timestamp.
"""

import os
import subprocess
import tempfile
import wave

import numpy as np
import imageio_ffmpeg

import audio_matcher
import matcher

TRUTH = 97.4
CAPTURE_S = 6.0
SR = 16000
DUR = 180.0


def render_clip(path):
    rng = np.random.default_rng(3)
    n = int(DUR * SR)
    x = np.zeros(n, dtype=np.float64)

    # "music": two random tones per quarter-second segment
    seg = int(0.25 * SR)
    tt = np.arange(seg) / SR
    ramp = np.minimum(1.0, np.minimum(np.arange(seg), np.arange(seg)[::-1]) / (0.01 * SR))
    for s in range(n // seg):
        freqs = np.exp(rng.uniform(np.log(120.0), np.log(5500.0), size=2))
        tone = sum(np.sin(2 * np.pi * f * tt) for f in freqs)
        x[s * seg:(s + 1) * seg] += 0.35 * tone * ramp

    # "effects": noise with a randomly varying loudness envelope
    env_pts = rng.uniform(0.0, 1.0, size=int(DUR * 20)) ** 2
    env = np.interp(np.arange(n), np.linspace(0, n, env_pts.size), env_pts)
    x += 0.4 * rng.standard_normal(n) * env

    x *= 0.7 / np.abs(x).max()
    wav = path + ".wav"
    with wave.open(wav, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes((x * 32767).astype(np.int16).tobytes())
    subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-y",
        "-i", wav, "-c:a", "aac", "-b:a", "128k", path,
    ], check=True, capture_output=True)
    os.remove(wav)


def main():
    tmp = tempfile.mkdtemp()
    clip = os.path.join(tmp, "clip.m4a")
    print("rendering synthetic film-audio clip...")
    render_clip(clip)
    _, start = matcher.probe(clip)

    # simulate the loopback capture: film audio + louder 'commentary' noise
    x = audio_matcher.decode_audio(clip, start + TRUTH, CAPTURE_S)
    rng = np.random.default_rng(7)
    noise = np.convolve(rng.standard_normal(x.size), np.hamming(65), "same")
    noise *= 1.2 * np.sqrt((x * x).mean()) / (np.sqrt((noise * noise).mean()) + 1e-12)
    feats = audio_matcher.prep_capture(x + noise.astype(np.float32), audio_matcher.SR)

    cases = {
        "narrow window": (TRUTH - 30, TRUTH + 30),
        "whole file": (None, None),
    }
    for label, (a, b) in cases.items():
        t, score, z = audio_matcher.find_match_audio(
            clip, feats, a, b, progress=lambda m: print("   ", m))
        err = abs(t - TRUTH)
        print(f"{label}: matched {t:.3f}s (truth {TRUTH}s, "
              f"err {err * 1000:.0f} ms, score {score:.3f}, z {z:.1f})")
        assert err < 0.12, f"match error too large: {err:.3f}s"
        assert score >= audio_matcher.SCORE_OK, f"score too low: {score:.3f}"
        assert z >= audio_matcher.Z_OK, f"peak z too low: {z:.1f}"
    print("AUDIO MATCHER TEST PASSED")


if __name__ == "__main__":
    main()
