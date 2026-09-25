"""End-to-end check of the audio matcher.

Renders 3 minutes of film-like audio (random note segments plus
envelope-modulated noise -- rich temporal structure, like music and
effects), AAC-encodes it, simulates a loopback capture at a known moment
with louder 'commentary' noise mixed over it, and checks that
find_match_audio recovers the timestamp. Also covers:

- a capture recorded at 48 kHz (the loopback rate) against the 16 kHz
  file path;
- the same film remuxed with container timestamps starting at 5 s and at
  600 s (.m2ts-style), which once shifted every match by the start time;
- a quiet scene inside a loud search window, where uncentered scoring once
  pushed the right answer under the trust gates;
- an 18 s capture swept across a chunk boundary of a whole-film search,
  which once hid a 10 s band before every boundary, the retry ladder's
  6/12/18 s looks reusing one decode, and a 25 s capture, which widens
  the overlap to fit.

bench_audio.py measures real-world accuracy; this only guards regressions.
"""

import os
import subprocess
import tempfile
import wave

import numpy as np
import imageio_ffmpeg

import audio_matcher
import controller
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
    # the loopback records at 48 kHz, not the file path's 16 kHz
    x48 = audio_matcher.resample(x + noise.astype(np.float32), audio_matcher.SR, 48000)
    t, score, z = audio_matcher.find_match_audio(
        clip, audio_matcher.prep_capture(x48, 48000), TRUTH - 30, TRUTH + 30)
    print(f"48 kHz capture: matched {t:.3f}s (err {abs(t - TRUTH) * 1000:.0f} ms, "
          f"score {score:.3f}, z {z:.1f})")
    assert abs(t - TRUTH) < 0.12 and score >= audio_matcher.SCORE_OK

    # container timestamps that do not start at zero
    for name, off in (("offset5.mp4", 5), ("offset600.mkv", 600)):
        shifted = os.path.join(tmp, name)
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-y",
                        "-i", clip, "-c", "copy", "-output_ts_offset", str(off),
                        shifted], check=True, capture_output=True)
        t, score, z = audio_matcher.find_match_audio(shifted, feats,
                                                     TRUTH - 30, TRUTH + 30)
        err = abs(t - TRUTH)
        print(f"{name}: start {matcher.probe(shifted)[1]:.2f}s, matched {t:.3f}s "
              f"(err {err * 1000:.0f} ms)")
        # AAC priming puts the container start ~64 ms before the audio
        assert err < 0.12, f"{name}: start time mishandled, err {err:.3f}s"

    quiet_scene(tmp)
    chunk_boundary(clip)
    print("AUDIO MATCHER TEST PASSED")


def quiet_scene(tmp):
    """A capture from a quiet stretch, searched inside a loud window."""
    rng = np.random.default_rng(3)
    n = int(DUR * SR)
    x = np.zeros(n)
    seg = int(0.25 * SR)
    tt = np.arange(seg) / SR
    for s in range(n // seg):
        for f in np.exp(rng.uniform(np.log(120.0), np.log(5500.0), size=2)):
            x[s * seg:(s + 1) * seg] += 0.35 * np.sin(2 * np.pi * f * tt)
    env = rng.uniform(0.0, 1.0, size=int(DUR * 20)) ** 2
    x += 0.4 * rng.standard_normal(n) * np.interp(
        np.arange(n), np.linspace(0, n, env.size), env)
    x[int(60 * SR):int(150 * SR)] *= 0.03          # a -30 dB scene
    x *= 0.7 / np.abs(x).max()
    wav, clip = os.path.join(tmp, "quiet.wav"), os.path.join(tmp, "quiet.m4a")
    with wave.open(wav, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes((x * 32767).astype(np.int16).tobytes())
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-y",
                    "-i", wav, "-c:a", "aac", "-b:a", "128k", clip],
                   check=True, capture_output=True)
    for truth in (80.0, 100.0, 120.0):
        c = audio_matcher.decode_audio(clip, truth, CAPTURE_S)
        noise = np.convolve(np.random.default_rng(int(truth)).standard_normal(c.size),
                            np.hamming(65), "same")
        noise *= 3.0 * np.sqrt((c * c).mean()) / np.sqrt((noise * noise).mean())
        feats = audio_matcher.prep_capture((c + noise).astype(np.float32),
                                           audio_matcher.SR)
        t, score, z = audio_matcher.find_match_audio(clip, feats, None, None)
        print(f"quiet scene @{truth:.0f}s: matched {t:.3f}s "
              f"(score {score:.3f}, z {z:.1f})")
        assert abs(t - truth) < 0.12, f"quiet scene: wrong match {t:.3f}s"
        assert score >= audio_matcher.SCORE_OK and z >= audio_matcher.Z_OK, \
            f"quiet scene: correct match not trusted (score {score:.3f}, z {z:.1f})"


def chunk_boundary(clip):
    """Long listens across the chunk boundaries of a whole-film search.

    A search longer than CHUNK_S is cut into chunks, and a capture is only
    found where it fits entirely inside one. With an 8 s overlap, the 18 s
    retry listen could not be found in the 10 s before every boundary - it
    was answered from somewhere else. A capture too long for the overlap
    widens it, or the same would happen to it. The chunks are shrunk to
    60 s here so the 3-minute clip has boundaries to sweep across.
    """
    ladder = (controller.AUDIO_SYNC_SECONDS,) + controller.AUDIO_RETRY_SECONDS
    saved = audio_matcher.CHUNK_S
    audio_matcher.CHUNK_S = 60.0
    try:
        def look(truth, seconds):
            c = audio_matcher.decode_audio(clip, truth, seconds)
            noise = np.convolve(np.random.default_rng(int(truth * 10)).standard_normal(c.size),
                                np.hamming(65), "same")
            noise *= 1.2 * np.sqrt((c * c).mean()) / np.sqrt((noise * noise).mean())
            feats = audio_matcher.prep_capture((c + noise).astype(np.float32),
                                               audio_matcher.SR)
            return audio_matcher.find_match_audio(clip, feats, None, None)

        # the controller's weak-match ladder listens longer over the same
        # window; every look must cut the same chunks, so the retries reuse
        # the first look's decode instead of decoding the film again
        audio_matcher._cache.clear()
        for seconds in ladder:
            look(100.3, seconds)
            if seconds == ladder[0]:
                chunks = set(audio_matcher._cache)
            assert set(audio_matcher._cache) == chunks, \
                f"a {seconds:.0f} s look re-decoded the film in new chunks"
        print(f"chunk cache: {len(chunks)} chunks decoded once for "
              f"{'/'.join(f'{s:.0f}' for s in ladder)} s looks")

        def sweep(seconds, truths):
            missed = []
            for truth in truths:
                t, score, z = look(truth, seconds)
                if not (abs(t - truth) < 0.12 and score >= audio_matcher.SCORE_OK
                        and z >= audio_matcher.Z_OK):
                    missed.append(f"{truth:.1f}s -> {t:.1f}s (score {score:.1f}, "
                                  f"z {z:.1f})")
            print(f"{seconds:.0f} s capture swept across a chunk boundary: "
                  f"{len(missed)} missed")
            assert not missed, (f"blind band at a chunk boundary ({seconds:.0f} s "
                                "capture): " + ", ".join(missed))

        sweep(max(ladder), np.arange(38.3, 57.0, 1.0))   # across the one at 60 s
        # a capture too long for OVERLAP_S widens the overlap to its own
        # length plus a second; at a fixed 20 s, a 25 s one could not be
        # found starting 35-40 s, before the boundary at 60 s
        sweep(25.0, np.arange(34.3, 41.0, 1.0))
    finally:
        audio_matcher.CHUNK_S = saved


if __name__ == "__main__":
    main()
