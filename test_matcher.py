"""End-to-end check of the frame matcher against a synthetic video.

Renders a 3-minute test clip with ffmpeg, pretends a screenshot burst was
taken at a known moment (rescaled and brightness-shifted to imitate a
stream capture), and checks that find_match recovers the timestamp.
"""

import os
import subprocess
import tempfile

import numpy as np
import imageio_ffmpeg

import matcher

TRUTH = 137.3
BURST_SPACING = 1 / 3


def render_clip(path):
    subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-y",
        "-f", "lavfi", "-i", "testsrc2=duration=180:size=640x360:rate=24",
        "-c:v", "libx264", "-preset", "veryfast", "-g", "48",
        "-pix_fmt", "yuv420p", path,
    ], check=True, capture_output=True)


def fake_stream_capture(path, t):
    """One frame at time t, downscaled and brightness-shifted."""
    out = subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner",
        "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
        "-vf", "scale=320:180", "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ], check=True, capture_output=True).stdout
    img = np.frombuffer(out[:320 * 180], dtype=np.uint8).reshape(180, 320)
    img = img.astype(np.float32) / 255.0
    return np.clip(img * 0.9 + 0.05, 0.0, 1.0)


def main():
    tmp = tempfile.mkdtemp()
    clip = os.path.join(tmp, "clip.mp4")
    print("rendering synthetic 3-minute clip...")
    render_clip(clip)

    burst = []
    for i in range(3):
        dt = i * BURST_SPACING
        burst.append((matcher.prep_gray(fake_stream_capture(clip, TRUTH + dt)), dt))

    cases = {
        "narrow window (direct scan)": (TRUTH - 20, TRUTH + 20),
        "wide window (coarse + fine)": (0, 180),
    }
    for label, (a, b) in cases.items():
        t, score = matcher.find_match(clip, burst, a, b,
                                      progress=lambda m: print("   ", m))
        err = abs(t - TRUTH)
        print(f"{label}: matched {t:.3f}s "
              f"(truth {TRUTH}s, err {err * 1000:.0f} ms, confidence {score:.3f})")
        assert err < 0.35, f"match error too large: {err:.3f}s"
        assert score > 0.6, f"confidence too low: {score:.3f}"

    # mechanical check of the keyframes-only decode used for whole-file scans
    # (clip has -g 48 at 24 fps -> a keyframe every <= 2 s)
    frames, pts = matcher._decode(clip, 0, 180, keyframes_only=True)
    print(f"keyframe decode: {len(frames)} keyframes, "
          f"pts {pts[0]:.2f}s .. {pts[-1]:.2f}s")
    assert 60 <= len(frames) <= 120, f"unexpected keyframe count {len(frames)}"
    assert all(b > a for a, b in zip(pts, pts[1:])), "keyframe pts not increasing"
    print("MATCHER TEST PASSED")


if __name__ == "__main__":
    main()
