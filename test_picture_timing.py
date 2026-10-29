"""picture_timing on made-up pictures, and on a film ffmpeg makes.

The stream test plays the part of a Twitch window: the film's cuts shown a
known time behind its sound, on a 60 fps stream grabbed at about 140 Hz,
with false cuts mixed in. The window test puts a video, a ticker inside it
and a busy chat column beside it on one page. The film-window test shows
film frames a known time after a clock, on a 144 Hz screen. Each must come
back within a few ms of what was put in.
"""

import os
import shutil
import subprocess
import tempfile

import numpy as np

import picture_timing as pt

RNG = np.random.default_rng(7)


def check(name, ok, detail=""):
    if not ok:
        raise AssertionError(f"{name}: {detail}")
    print(f"ok   {name}" + (f"  ({detail})" if detail else ""))


def channel_keys():
    for title, want in (("(45) willneff - Twitch - Brave", "willneff - Twitch"),
                        ("willneff - Twitch - Google Chrome", "willneff - Twitch"),
                        ("(3+) xQc - Kick — Mozilla Firefox", "xQc - Kick"),
                        ("Movie night - Twitch - Microsoft​ Edge", "Movie night - Twitch"),
                        ("VLC media player", "VLC media player"), ("", "")):
        check(f"channel key of {title!r}", pt.channel_key(title) == want, pt.channel_key(title))


def texture(h, w):
    """A smooth random picture: blobs, not pixel noise, so that a pan of a
    pixel keeps it nearly the same, as in a film."""
    coarse = RNG.uniform(20, 235, (h // 6 + 3, w // 6 + 3))
    big = np.kron(coarse, np.ones((6, 6)))
    k = np.ones(5) / 5
    big = np.apply_along_axis(lambda r: np.convolve(r, k, mode="same"), 1, big)
    big = np.apply_along_axis(lambda c: np.convolve(c, k, mode="same"), 0, big)
    return big[:h, :w]


def film_shots(length=40.0, fps=24000 / 1001, size=(48, 27)):
    """A film: shots of 1-4 s, each a smooth picture panning a pixel a
    frame, with a little grain. Returns (pts, frames, cut times)."""
    w, h = size
    n = int(length * fps)
    pts = np.arange(n) / fps
    frames = np.empty((n, h, w), np.uint8)
    cuts, shot, nxt, start = [], None, 0.0, 0
    for i, p in enumerate(pts):
        if p >= nxt:
            shot = texture(h, w + 110)
            if i:
                cuts.append(p)
            nxt = p + RNG.uniform(1.0, 4.0)
            start = i
        d = min(i - start, 109)
        frames[i] = np.clip(shot[:, d:d + w] + RNG.normal(0, 3, (h, w)), 0, 255)
    return pts, frames, np.array(cuts)


def stream_cuts():
    pts, frames, film_cuts = film_shots()
    lag = 0.142
    base = 1000.0                               # perf_counter at film time 0's sound
    sound_at = lambda t: t - base
    # The stream: 60 fps, each stream frame the film frame its picture is at
    # (the sound's film time less the lag), grabbed every ~7 ms with jitter.
    grabs = base + np.cumsum(RNG.uniform(0.0065, 0.0075, 4300))
    grabs = grabs[grabs < base + pts[-1] - 0.5]
    stream_frame = np.floor((grabs - base) * 60) / 60          # what the stream shows
    film_t = stream_frame - lag
    idx = np.clip(np.searchsorted(pts, film_t, side="right") - 1, 0, len(pts) - 1)
    shown = frames[idx].astype(np.int16)
    shown = np.clip(shown * 0.8 + 30 + RNG.normal(0, 3, shown.shape), 0, 255).astype(np.uint8)
    seen = pt.cuts(grabs[1:], pt.change(pt.normalize(shown)))
    # two false cuts: a flash the film has not, and a cut the stream lost
    seen = np.sort(np.concatenate([seen, [base + 7.31, base + 22.9]]))
    ref = pt.cuts(pts[1:], pt.change(pt.normalize(frames)))
    check("film cuts found", abs(len(ref) - len(film_cuts)) <= 1, f"{len(ref)} of {len(film_cuts)}")
    res = pt.stream_lag(seen, sound_at, ref)
    check("stream lag found", res is not None)
    # a cut shows at the stream's next frame: half a 60 fps frame later on
    # average, which is what a viewer sees too (and these grabs, unlike a
    # real screen's, are not tied to its refresh: up to one more grab late)
    err = (res["lag"] - (lag + 0.5 / 60)) * 1e3
    check("stream lag within 8 ms", abs(err) < 8,
          f"{res['lag'] * 1e3:.1f} ms from {res['cuts']} of {res['seen']} cuts, error {err:+.1f} ms")
    check("false cuts left out", res["cuts"] <= res["seen"] - 2, f"{res['cuts']} of {res['seen']}")
    check("no cut fits: None", pt.stream_lag(seen, lambda t: t - base + 100.0, ref) is None)
    check("two cuts are not enough", pt.stream_lag(seen[:2], sound_at, ref) is None)


def window_rect():
    """A page 160x90: video at x 20-120, y 10-66 (cuts every ~1.5 s, a
    ticker along its top), a chat column at x 130-156 scrolling now and
    then, the rest still."""
    n, h, w = 600, 90, 160
    page = np.full((n, h, w), 30, np.uint8)
    shot = RNG.integers(0, 255, (56, 100))
    for i in range(n):
        if i % 210 == 0:
            shot = RNG.integers(0, 255, (56, 100))
        page[i, 10:66, 20:120] = np.roll(shot, i // 3, axis=1)     # a slow pan
        page[i, 10:14, 20:120] = np.roll(np.arange(100) * 7 % 255, i * 2)   # the ticker
        if i % 40 == 0:                                              # a chat message
            page[i:, 5:85, 130:156] = RNG.integers(0, 255, (80, 26))
    rect = pt.video_rect(page)
    check("video found beside a chat", rect is not None and abs(rect[0] - 20) <= 3
          and abs(rect[2] - 120) <= 3 and abs(rect[1] - 10) <= 3 and abs(rect[3] - 66) <= 3,
          f"{rect}")
    still = np.repeat(page[:1], 50, axis=0)
    check("a still page has no video", pt.video_rect(still) is None)


def bands():
    """What a timing grabs: a band through the picture's middle, cut to
    BAND_MOST, and where it is as fractions of the picture, for the film's
    frames to be cut alike."""
    rect, crop = pt.band((100, 50, 1000, 500))
    check("a band through the middle rows", rect == (100, 200, 1000, 150)
          and np.allclose(crop, (0.0, 0.3, 1.0, 0.6)), f"{rect} {crop}")
    rect, crop = pt.band((-2560, 0, 2560, 1440))
    x, y, w, h = rect
    check("a big picture's band is cut to its middle", (w, h) == pt.BAND_MOST
          and x == -1920 and abs(y + h / 2 - 0.45 * 1440) <= 1, f"{rect}")
    check("...and its crop says where", np.allclose(
        crop, ((x + 2560) / 2560, y / 1440, (x + w + 2560) / 2560, (y + h) / 1440)), f"{crop}")
    t = 7.0 + np.arange(1440) / 144.0
    check("grabs a second", abs(pt.grab_rate(t) - 144) < 0.01
          and abs(pt.grab_rate(t[::2]) - 72) < 0.01 and pt.grab_rate(t[:1]) == 0.0)


def film_window():
    pts, frames, _ = film_shots(length=8.0, size=(64, 36))
    lead = 0.0123
    t0 = 500.0                                   # perf_counter when the clock is at 0
    clock = lambda t: t - t0
    # 144 Hz: a frame is on screen from the first vsync after its time + lead
    vsync = t0 + np.arange(int(7.5 * 144)) / 144.0
    shown = np.clip(np.searchsorted(pts + lead, vsync - t0, side="right") - 1, 0, len(pts) - 1)
    grabs = vsync + 0.0004                       # the copy returns just after the vsync
    keep = grabs - t0 > 0.3
    res = pt.film_lead(grabs[keep], pt.normalize(frames[shown[keep]]),
                       [clock(t) for t in grabs[keep]], pts, pt.normalize(frames))
    check("film lead found", res is not None)
    # a frame waits for the next refresh: seen half a refresh after its
    # time + lead on average, and that is what the eye gets
    want = lead + 0.5 / 144
    err = (res["lead"] - want) * 1e3
    check("film lead within 2 ms", abs(err) < 2.0,
          f"{res['lead'] * 1e3:.1f} ms from {res['frames']} frames, error {err:+.2f} ms")
    still = pt.film_lead(grabs[:200], pt.normalize(frames[np.zeros(200, int)]),
                         [clock(t) for t in grabs[:200]], pts, pt.normalize(frames))
    check("a still picture gives no lead", still is None)


def decoded_film():
    """decode_frames on a film whose frame k is brightness 8k: each frame
    must carry its own time - no repeated first frame (ffmpeg's habit),
    which put every lag one frame out."""
    import imageio_ffmpeg
    d = tempfile.mkdtemp()
    try:
        path = os.path.join(d, "ramp.mkv")
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
                        "-y", "-f", "lavfi", "-i", "color=c=black:s=64x36:r=24000/1001:d=6",
                        "-vf", "geq=lum='mod(N*8,256)':cb=128:cr=128",
                        "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv444p", path],
                       check=True, creationflags=pt._creationflags())
        # from the start nothing is skipped or padded: the frames as they are
        all_pts, all_frames = pt.decode_frames(path, 0.0, 6.0, (32, 18))
        check("the whole film decodes", len(all_pts) >= 140, f"{len(all_pts)} frames")
        for t0 in (1.0, 2.37, 3.001):
            pts, frames = pt.decode_frames(path, t0, 2.0, (32, 18))
            same = [np.array_equal(f, all_frames[np.argmin(np.abs(all_pts - p))])
                    for p, f in zip(pts, frames)]
            check(f"frames from {t0} s carry their own times",
                  all(same) and t0 - 0.001 <= pts[0] < t0 + 0.05,
                  f"{len(pts)} frames from {pts[0]:.3f}, {sum(same)} matching")
        pts, frames = pt.decode_frames(path, 1.0, 1.0, (32, 18), crop=(0.25, 0.25, 0.75, 0.75))
        check("crop and size", frames.shape[1:] == (18, 32))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    channel_keys()
    stream_cuts()
    window_rect()
    bands()
    film_window()
    decoded_film()
    print("PICTURE TIMING TEST PASSED")


if __name__ == "__main__":
    main()
