"""Find where a captured stream frame occurs inside a local video file.

Decoding is done with the ffmpeg binary bundled by imageio-ffmpeg: frames
come out as small grayscale thumbnails which are matched against the
capture using zero-normalized cross-correlation. A two-stage search
(keyframes first, then a dense scan around the best candidates) keeps
scans of long windows fast.
"""

import re
import subprocess
import sys
import threading

import numpy as np
from PIL import Image
import imageio_ffmpeg

THUMB_W, THUMB_H = 64, 36      # size used for correlation
DECODE_W, DECODE_H = 128, 72   # size ffmpeg decodes to (autocropped down later)
FINE_FPS = 12.0                # dense-scan sampling rate -> ~83 ms resolution
KEYFRAME_SPAN = 720.0          # windows beyond this use the keyframes-only prepass
CAND_MARGIN = 0.08             # refine all prepass candidates within this of the best

_PTS_RE = re.compile(r"pts_time:\s*(-?\d+(?:\.\d+)?)")
_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_START_RE = re.compile(r"start:\s*(-?\d+(?:\.\d+)?)")


class MatchError(RuntimeError):
    pass


def _creationflags():
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def probe(path):
    """Return (duration_s, container_start_time_s)."""
    out = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", path],
        capture_output=True, text=True, errors="replace",
        creationflags=_creationflags(),
    ).stderr
    m = _DUR_RE.search(out)
    if not m:
        raise MatchError("ffmpeg could not read this file as a video.")
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = _START_RE.search(out)
    start = float(m.group(1)) if m else 0.0
    return duration, start


def _decode(path, seek_abs, end_abs, keyframes_only=False, fps=None,
            progress=None, label=""):
    """Decode grayscale thumbnails between two absolute source timestamps.

    Returns (frames, pts): frames as float32 (N, H, W) in 0..1, pts as each
    frame's absolute source timestamp (-copyts keeps original pts, showinfo
    reports them on stderr).
    """
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostats"]
    if keyframes_only:
        cmd += ["-skip_frame", "nokey"]
    cmd += ["-ss", f"{max(seek_abs, 0.0):.3f}", "-copyts", "-i", path,
            "-to", f"{end_abs:.3f}", "-an", "-sn"]
    vf = ([f"fps={fps}"] if fps else []) + [f"scale={DECODE_W}:{DECODE_H}", "showinfo"]
    cmd += ["-vf", ",".join(vf), "-fps_mode", "passthrough",
            "-f", "rawvideo", "-pix_fmt", "gray", "-"]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=_creationflags())
    pts, errtail = [], []

    def read_stderr():
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace")
            if "pts_time:" in line:
                m = _PTS_RE.search(line)
                pts.append(float(m.group(1)) if m else float("nan"))
            elif line.strip():
                errtail.append(line.strip())
                del errtail[:-15]

    reader = threading.Thread(target=read_stderr, daemon=True)
    reader.start()

    frame_bytes = DECODE_W * DECODE_H
    chunks = []
    while True:
        buf = proc.stdout.read(frame_bytes)
        if len(buf) < frame_bytes:
            break
        chunks.append(buf)
        if progress and len(chunks) % 200 == 0:
            progress(f"{label}: {len(chunks)} frames scanned...")
    proc.stdout.close()
    proc.wait()
    reader.join(timeout=5)

    if not chunks:
        raise MatchError(
            "No frames decoded in the search window."
            + (" ffmpeg said: " + " | ".join(errtail[-3:]) if errtail else ""))
    n = min(len(chunks), len(pts))
    frames = np.frombuffer(b"".join(chunks[:n]), dtype=np.uint8)
    frames = frames.reshape(n, DECODE_H, DECODE_W)  # uint8 keeps memory low
    times = np.array(pts[:n], dtype=np.float64)
    good = ~np.isnan(times)
    return frames[good], times[good]


def _autocrop(img, thresh=0.07):
    """Trim black letterbox/pillarbox bars from a grayscale float image."""
    mask = img > thresh
    rows, cols = mask.any(axis=1), mask.any(axis=0)
    if not rows.any() or not cols.any():
        return img
    r0, r1 = int(np.argmax(rows)), len(rows) - int(np.argmax(rows[::-1]))
    c0, c1 = int(np.argmax(cols)), len(cols) - int(np.argmax(cols[::-1]))
    # never crop away most of the image (keeps dark scenes intact)
    if (r1 - r0) * (c1 - c0) < 0.25 * img.size:
        return img
    return img[r0:r1, c0:c1]


def prep_gray(img, mask=None):
    """Grayscale float image (any size) -> normalized thumbnail vector.

    Two vectors produced with the same `mask` score their similarity as a
    plain dot product (zero-normalized cross-correlation, -1..1). `mask` is
    a (THUMB_H, THUMB_W) bool array; False pixels (e.g. a facecam zone) are
    excluded from the comparison.
    """
    img = _autocrop(np.asarray(img, dtype=np.float32))
    pil = Image.fromarray((img * 255.0).clip(0, 255).astype(np.uint8))
    a = np.asarray(pil.resize((THUMB_W, THUMB_H), Image.BILINEAR), dtype=np.float32)
    if mask is not None:
        a = a[mask]
    a = a - a.mean()
    norm = float(np.sqrt((a * a).sum()))
    if norm < 1e-6:
        return np.zeros(a.size, dtype=np.float32)
    return (a / norm).ravel()


def corner_mask(corner, w_frac=0.35, h_frac=0.42):
    """Mask that ignores one corner (typical facecam overlay position)."""
    m = np.ones((THUMB_H, THUMB_W), dtype=bool)
    hh = int(round(THUMB_H * h_frac))
    ww = int(round(THUMB_W * w_frac))
    if corner == "tl":
        m[:hh, :ww] = False
    elif corner == "tr":
        m[:hh, -ww:] = False
    elif corner == "bl":
        m[-hh:, :ww] = False
    elif corner == "br":
        m[-hh:, -ww:] = False
    return m


def rect_mask(nx0, ny0, nx1, ny1):
    """Mask ignoring a rectangle given in 0..1 coordinates of the frame."""
    m = np.ones((THUMB_H, THUMB_W), dtype=bool)
    x0 = max(int(nx0 * THUMB_W), 0)
    x1 = min(int(np.ceil(nx1 * THUMB_W)), THUMB_W)
    y0 = max(int(ny0 * THUMB_H), 0)
    y1 = min(int(np.ceil(ny1 * THUMB_H)), THUMB_H)
    m[y0:y1, x0:x1] = False
    return m


def _thumb_matrix(frames, mask=None):
    return np.stack([prep_gray(f.astype(np.float32) / 255.0, mask)
                     for f in frames])


def _fine_scan(path, burst, a_abs, b_abs, progress=None, mask=None):
    """Dense scan of [a_abs, b_abs]; returns (absolute_pts, score)."""
    frames, pts = _decode(path, a_abs, b_abs, fps=FINE_FPS,
                          progress=progress, label="Scanning")
    V = _thumb_matrix(frames, mask)
    total = np.zeros(len(V), np.float32)
    count = np.zeros(len(V), np.float32)
    for vec, dt in burst:
        k = int(round(dt * FINE_FPS))
        s = V @ vec
        if k <= 0:
            total += s
            count += 1
        elif k < len(s):
            total[:len(s) - k] += s[k:]
            count[:len(s) - k] += 1
    scores = total / np.maximum(count, 1)
    i = int(np.argmax(scores))
    return float(pts[i]), float(scores[i])


def _pick_candidates(scores, times, min_sep, max_n):
    """Indices of well-separated score peaks, best first.

    Keeps at least two peaks, then only ones within CAND_MARGIN of the best
    (near-ties must all be refined; the burst sequence decides between them).
    """
    order = np.argsort(scores)[::-1]
    best = scores[order[0]]
    cands = []
    for i in order:
        if len(cands) >= max_n:
            break
        if scores[i] < best - CAND_MARGIN and len(cands) >= 2:
            break
        if all(abs(times[i] - times[j]) > min_sep for j in cands):
            cands.append(int(i))
    return cands


def _keyframe_search(path, burst, a_abs, b_abs, progress, mask=None):
    """Keyframes-only prepass for very wide windows, then refine the peaks."""
    progress("Coarse scan (keyframes only)...")
    frames, kf_pts = _decode(path, a_abs, b_abs, keyframes_only=True,
                             progress=progress, label="Coarse scan")
    V = _thumb_matrix(frames, mask)
    scores = V @ burst[0][0]
    cands = _pick_candidates(scores, kf_pts, min_sep=15.0, max_n=6)

    # dense scan spanning the keyframe gap around each candidate
    best_pts, best_score = None, -2.0
    for rank, i in enumerate(cands, 1):
        prev_gap = kf_pts[i] - kf_pts[i - 1] if i > 0 else 5.0
        next_gap = kf_pts[i + 1] - kf_pts[i] if i < len(kf_pts) - 1 else 5.0
        a = kf_pts[i] - min(prev_gap, 20.0) - 1.5
        b = kf_pts[i] + min(next_gap, 20.0) + 1.5
        progress(f"Refining candidate {rank}/{len(cands)}...")
        try:
            pts, score = _fine_scan(path, burst, a, b, mask=mask)
        except MatchError:
            continue
        if score > best_score:
            best_pts, best_score = pts, score
    if best_pts is None:
        raise MatchError("Could not refine any match candidate.")
    return best_pts, best_score


def find_match(path, burst, t0=None, t1=None, progress=None, mask=None):
    """Locate the captured burst inside `path`.

    burst   -- list of (vector from prep_gray, seconds after first capture);
               the stream is assumed to play at 1x speed; vectors must have
               been prepped with the same `mask` passed here
    t0, t1  -- search window on the player timeline (None = whole file)
    Returns (match_time_s on the player timeline, confidence in -1..1).
    """
    progress = progress or (lambda msg: None)
    duration, start = probe(path)
    t0 = 0.0 if t0 is None else max(0.0, min(t0, duration))
    t1 = duration if t1 is None else max(0.0, min(t1, duration))
    if t1 - t0 < 2.0:
        t0, t1 = max(0.0, t0 - 2.0), min(duration, t1 + 2.0)
    if t1 <= t0:
        raise MatchError("Empty search window.")
    span = t1 - t0

    # Up to KEYFRAME_SPAN: one dense scan of the whole window. Decoding
    # dominates the cost and ffmpeg decodes every frame regardless of the
    # sampling rate, so sampling densely is essentially free -- and it
    # guarantees the true moment is scored, never skipped by a prepass.
    if span <= KEYFRAME_SPAN:
        progress("Scanning window...")
        pts, score = _fine_scan(path, burst, start + t0, start + t1, progress,
                                mask=mask)
        return pts - start, score

    # Very wide windows (whole file): keyframes-only prepass, which can be
    # fooled if the true moment sits between keyframes of a fast scene --
    # fall back to a dense scan when the result looks weak.
    pts, score = _keyframe_search(path, burst, start + t0, start + t1, progress,
                                  mask=mask)
    if score < 0.72 and span <= 1200.0:
        progress("Weak match - rescanning window densely...")
        pts, score = _fine_scan(path, burst, start + t0, start + t1, progress,
                                mask=mask)
    return pts - start, score
