"""How late a picture is, measured off the screen.

StreamSync lines the film up by sound: the stream's sound is matched to the
film's soundtrack, and the film is put where that sound is. Two pictures do
not sit exactly on their sound, and neither can be heard:

- The stream's. A streamer's capture can show the film's picture well behind
  the film's sound - 142 ms on a Twitch stream measured 2026-10-28, the same
  on every computer watching it. Side by side, StreamSync's film then runs
  that far ahead of the stream's window, though it is right on the sound.
- The film window's own. libvlc hands a frame to Windows when its clock gets
  there, and the compositor and the monitor show it later - 12 ms on a
  144 Hz screen. A film placed on the sound by its clock is that late to
  the eye.

Both are measured the same way: the window is grabbed many times a second
(screencap.py) and what it shows is compared with the film itself, decoded
small and grey by ffmpeg.

- stream_lag() uses scene cuts. The stream's picture is cropped, scaled,
  re-encoded and overlaid (a facecam, chat), but a cut is a cut, and each
  one says how far behind the sound the picture showed it. The cuts that
  agree give the lag; one-offs (a flash, a whip pan) do not.
- film_lead() uses the frames themselves. The film window shows the film's
  own frames, so each grab names the frame on screen, and the first grab of
  each frame says when it appeared against the player's clock.

numpy only, no Tk and no Windows: the grabbing is the platform's.
"""

import re
import subprocess
import sys

import numpy as np

CUT = 0.25            # 1 - correlation between two images that counts as a cut
CLEAR_CUT = 0.5       # ...and one no fast move or dissolve passes for
CUT_GAP = 0.3         # s: cuts closer together than this are one
LAG_RANGE = (-0.3, 1.5)   # s: how far a stream's picture may plausibly trail its sound
AGREE = 0.03          # s: cuts within this of each other back the same lag
MIN_CUTS = 3          # cuts that must agree on a stream's lag
MIN_FRAMES = 15       # frames that must be seen for the film window's lead
FRAME_MATCH = 0.9     # correlation a grab needs with a film frame to name it
FRAME_MARGIN = 0.004  # ...and how much better than the frames either side
LEAD_SPREAD = 0.03    # s: a frame further than this from the median is an outlier
BAND = (0.30, 0.60)   # a picture's rows grabbed to time it: its middle, clear of subtitles
BAND_MOST = (1280, 240)   # px: a band wider or taller than this is cut to its middle

# What a browser appends to a page's title, and the unread count a site
# puts in front of it ("(45) willneff - Twitch - Brave"): neither names the
# stream, and the count changes by the minute.
_COUNT = re.compile(r"^\(\d+\+?\)\s*")
_BROWSER = re.compile(r"\s[-–—]\s(Brave|Google Chrome|Chromium|Microsoft​? ?Edge|"
                      r"(Mozilla )?Firefox|Opera|Vivaldi|Arc|LibreWolf|Waterfox|Floorp|"
                      r"Zen Browser)$")


def channel_key(title):
    """The stream a window title names, for remembering a lag per stream:
    '(45) willneff - Twitch - Brave' -> 'willneff - Twitch'."""
    t = _COUNT.sub("", (title or "").strip())
    return _BROWSER.sub("", t).strip()


def _creationflags():
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def band(rect, rows=BAND, most=BAND_MOST):
    """The part of a picture that is grabbed to time it: (band, crop).

    band = (x, y, w, h) on screen: rows `rows` (fractions of its height) of
    the picture at rect = (x, y, w, h), cut to its middle `most` pixels
    wide and tall; crop = the same as fractions of the picture, for
    decode_frames to cut the film's frames alike.

    A grab waits for the screen's next refresh and then copies, and a big
    copy is slow: the whole of a 1278 x 1341 window could only be grabbed
    every other refresh of a 144 Hz screen, so every cut was seen a refresh
    late and the copy returned later still - a stand-in stream's picture
    read 119 ms behind its sound where a band read 109. A band is copied in
    time for every refresh, and two bands of the same size - the stream's
    and the film window's - are seen equally late, which cancels when the
    film is lined up with the stream's picture.
    """
    x, y, w, h = rect
    top, bottom = y + h * rows[0], y + h * rows[1]
    bh = max(1, int(round(min(bottom - top, most[1]))))
    by = int(round((top + bottom - bh) / 2))
    bw = max(1, int(min(w, most[0])))
    bx = int(round(x + (w - bw) / 2))
    crop = ((bx - x) / w, (by - y) / h, (bx + bw - x) / w, (by + bh - y) / h)
    return (bx, by, bw, bh), crop


def grab_rate(times):
    """Grabs a second over `times`: a screen's refresh rate when the grabs
    kept up with it, less when they missed some."""
    times = np.asarray(times, float)
    if len(times) < 2 or times[-1] <= times[0]:
        return 0.0
    return (len(times) - 1) / float(times[-1] - times[0])


def decode_frames(path, t0, dur, size, crop=None):
    """The film's frames from t0 for dur seconds, small and grey.

    Returns (pts, frames): pts in seconds on the player timeline, frames as
    uint8 [n, h, w] for size = (w, h), after cropping to crop = (x0, y0, x1,
    y1), fractions of the picture. ffmpeg's input -ss counts from the
    container's start, as the player does, and its output starts at 0 there,
    so a frame's player time is t0 plus its pts. Frames pass through as
    decoded (-fps_mode passthrough): left to itself, ffmpeg repeats the
    first frame to fill the moment before its time, and every frame after
    it was labelled one frame early - 42 ms off every lag measured.
    """
    import imageio_ffmpeg
    w, h = size
    vf = ["showinfo"]
    if crop:
        x0, y0, x1, y1 = crop
        vf.append(f"crop=iw*{x1 - x0:.6f}:ih*{y1 - y0:.6f}:iw*{x0:.6f}:ih*{y0:.6f}")
    vf += [f"scale={w}:{h}:flags=area", "format=gray"]
    t0 = max(0.0, t0)
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostats",
           "-ss", f"{t0:.3f}", "-i", path, "-t", f"{dur:.3f}",
           "-map", "0:v:0", "-an", "-sn", "-vf", ",".join(vf),
           "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    out = subprocess.run(cmd, capture_output=True, creationflags=_creationflags())
    data = np.frombuffer(out.stdout, np.uint8)
    n = data.size // (w * h)
    frames = data[:n * w * h].reshape(n, h, w)
    pts = np.array([float(x) for x in
                    re.findall(rb"pts_time:\s*(-?[0-9.]+)", out.stderr)][:n])
    if pts.size != n or n < 2:
        raise RuntimeError("Could not read the film's pictures there.")
    keep = pts > -0.0005
    return t0 + pts[keep], frames[keep]


def normalize(frames):
    """Each image as a zero-mean, unit-length float32 row: the dot product
    of two rows is the images' correlation, so brightness, contrast and a
    re-encode's colour shift all drop out."""
    x = np.asarray(frames, np.float32).reshape(len(frames), -1)
    x = x - x.mean(axis=1, keepdims=True)
    return x / (np.linalg.norm(x, axis=1, keepdims=True) + 1e-6)


def change(norm):
    """1 - correlation of each image with the one before: len(norm) - 1."""
    return 1.0 - np.sum(norm[1:] * norm[:-1], axis=1)


def cuts(times, signal, threshold=CUT, gap=CUT_GAP):
    """Where `signal` (one value per time) peaks above threshold: the cuts.
    Peaks within `gap` of a kept one are the same cut (a stream may show a
    cut over two frames); the first is kept."""
    times = np.asarray(times, float)
    signal = np.asarray(signal, float)
    out = []
    for i in np.where(signal > threshold)[0]:
        if out and times[i] - out[-1] < gap:
            continue
        out.append(float(times[i]))
    return np.array(out)


def _run(density, frac):
    """The contiguous stretch around density's peak that stays above frac
    of the peak: (lo, hi), hi exclusive."""
    peak = int(np.argmax(density))
    floor = frac * density[peak]
    lo, hi = peak, peak + 1
    while lo > 0 and density[lo - 1] > floor:
        lo -= 1
    while hi < len(density) and density[hi] > floor:
        hi += 1
    return lo, hi


def video_rect(frames, step=8, top=12, min_area=0.03):
    """Where the video is in grabs of a whole window, as (x0, y0, x1, y1)
    in the grabs' pixels, or None if nothing changed enough.

    The biggest changes between grabs are the video's: a cut or a fast
    move changes nearly the whole picture at once, while a page's chat or
    buttons change a strip at a time, and not then. So the pixels that
    took part in most of the `top` biggest changes are the video - a
    ticker or a facecam inside it included, a chat column beside it not.
    (The pixels that change most often would not do: a scrolling ticker
    outdoes a talking head.)
    """
    f = np.asarray(frames, np.int16)
    if len(f) < 3:
        return None
    changed = np.abs(np.diff(f, axis=0)) > step
    sizes = changed.reshape(len(changed), -1).sum(axis=1)
    biggest = np.argsort(sizes)[-min(top, len(sizes)):]
    if sizes[biggest].min() < min_area * changed[0].size:
        return None
    mask = changed[biggest].mean(axis=0) >= 0.5
    k = max(1, min(mask.shape) // 40)               # bridge a still patch in the picture
    kern = np.ones(k) / k
    smooth = lambda v: np.convolve(v, kern, mode="same")
    x0, x1 = _run(smooth(mask.mean(axis=0)), 0.3)
    y0, y1 = _run(smooth(mask[:, x0:x1].mean(axis=1)), 0.3)
    x0, x1 = _run(smooth(mask[y0:y1].mean(axis=0)), 0.3)
    if (x1 - x0) * (y1 - y0) < min_area * mask.size:
        return None
    return x0, y0, x1, y1


def stream_lag(cap_cuts, sound_at, ref_cuts, lo=LAG_RANGE[0], hi=LAG_RANGE[1],
               agree=AGREE, min_cuts=MIN_CUTS):
    """How far a stream's picture runs behind its sound, from scene cuts.

    cap_cuts: when (perf_counter) the stream's window showed each cut - the
    first grab showing it, which comes just after the refresh that did, as
    in film_lead; sound_at(t): the film time the stream's sound was at
    then (as heard: audio_capture dates sound by when it plays); ref_cuts:
    where the film's own cuts are (film time). Each pair of a cut seen and
    a film cut within [lo, hi] of where the sound was is a candidate lag;
    the lag that the most cuts back, within `agree`, wins, and its value is
    the median of their lags (one per cut seen, the nearest).

    Returns {"lag", "cuts", "seen", "spread"}, or None when fewer than
    min_cuts cuts agree: a cut or two can pair with the film's by chance.
    """
    idx, lags = [], []
    for i, c in enumerate(cap_cuts):
        s = sound_at(c)
        for p in ref_cuts:
            if lo <= s - p <= hi:
                idx.append(i)
                lags.append(s - p)
    if not lags:
        return None
    idx, lags = np.array(idx), np.array(lags)
    best = None
    for v in lags:
        near = np.abs(lags - v) <= agree
        backers = len(set(idx[near].tolist()))
        spread = float(np.std(lags[near]))
        if best is None or (backers, -spread) > (best[0], -best[1]):
            best = (backers, spread, v)
    if best[0] < min_cuts:
        return None
    centre = best[2]
    per_cut = {}
    for i, v in zip(idx, lags):
        if abs(v - centre) <= agree and (i not in per_cut or abs(v - centre) < abs(per_cut[i] - centre)):
            per_cut[i] = v
    vals = np.array(list(per_cut.values()))
    return {"lag": float(np.median(vals)), "cuts": int(vals.size), "seen": int(len(cap_cuts)),
            "spread": float(np.std(vals))}


def film_lead(times, norm, positions, ref_pts, ref_norm):
    """How long after the player's clock reaches a frame the film window
    shows it.

    times: when each grab returned (perf_counter); norm: the grabs,
    normalized; positions: the player's clock at each of those times;
    ref_pts, ref_norm: the film's frames over the same stretch, cropped and
    shrunk as the grabs were. Each grab is named by the film frame it
    matches clearly. A grab waits for the screen's next refresh, so the
    first grab of a frame comes just after the refresh that showed it - its
    copy taking about as long as the monitor takes to light up that part
    of the screen - and the clock then, less the frame's own time, is how
    late it was seen.

    Returns {"lead", "frames", "spread"} or None if too few frames were
    seen clearly (a still scene, the window covered).
    """
    positions = np.asarray(positions, float)
    ref_pts = np.asarray(ref_pts, float)
    s = norm @ ref_norm.T
    s = np.where(np.abs(ref_pts[None, :] - positions[:, None]) < 1.0, s, -2.0)
    best = s.argmax(axis=1)
    top = s.max(axis=1)
    rows = np.arange(len(best))
    either = np.maximum(s[rows, np.clip(best - 1, 0, None)],
                        s[rows, np.clip(best + 1, None, len(ref_pts) - 1)])
    clear = (top > FRAME_MATCH) & (top - either > FRAME_MARGIN)
    first = {}
    for k in np.where(clear)[0]:
        first.setdefault(int(best[k]), int(k))
    if len(first) < MIN_FRAMES:
        return None
    late = np.array([positions[k] - ref_pts[j] for j, k in first.items()])
    keep = late[np.abs(late - np.median(late)) < LEAD_SPREAD]
    if keep.size < MIN_FRAMES:
        return None
    return {"lead": float(np.median(keep)), "frames": int(keep.size),
            "spread": float(np.std(keep))}
