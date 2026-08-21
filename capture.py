"""Screen-region selection overlay and screen grabbing."""

import sys
import time
import tkinter as tk

import numpy as np
import mss

if sys.platform == "darwin":
    OVERLAY_FONT = ("Helvetica Neue", 14)
elif sys.platform == "win32":
    OVERLAY_FONT = ("Segoe UI", 14)
else:
    OVERLAY_FONT = ("DejaVu Sans", 14)


def _to_gray(shot):
    a = np.frombuffer(shot.bgra, dtype=np.uint8)
    a = a.reshape(shot.height, shot.width, 4)
    gray = (a[:, :, 0] * 0.114 + a[:, :, 1] * 0.587 + a[:, :, 2] * 0.299)
    return gray.astype(np.float32) / 255.0


def grab_burst(region, n=4, spacing=0.35):
    """Capture n grayscale frames of `region`, `spacing` seconds apart.

    Returns ([(gray_float_image, seconds_after_first)], perf_counter_at_first).
    """
    left, top, w, h = (int(v) for v in region)
    box = {"left": left, "top": top, "width": w, "height": h}
    frames = []
    with mss.mss() as sct:
        t0 = time.perf_counter()
        for i in range(n):
            target = t0 + i * spacing
            while True:
                d = target - time.perf_counter()
                if d <= 0:
                    break
                time.sleep(min(d, 0.05))
            shot = sct.grab(box)
            frames.append((_to_gray(shot), time.perf_counter() - t0))
    return frames, t0


class RegionSelector:
    """Fullscreen translucent overlay; drag a rectangle to pick a region.

    Coordinates returned are absolute virtual-screen pixels (mss-compatible),
    spanning all monitors.
    """

    def __init__(self, root):
        self.root = root

    def select(self):
        result = {}
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)

        if sys.platform == "darwin":
            # Overlay covers the primary display only. macOS reports
            # display geometry in points through *both* Tk and mss (measured
            # 1440x900 from each on a 1440x900 MBP, mss 10.2), so this ratio
            # is 1.0 and the region passes through in points - which is the
            # unit sct.grab() wants back. Computed rather than hardcoded so a
            # pixel-reporting mss would still line up. A Retina panel may hand
            # back a larger image than the box asked for; harmless, since
            # _to_gray reads each frame's own dimensions.
            with mss.mss() as sct:
                mon = sct.monitors[1]
            over_w, over_h = top.winfo_screenwidth(), top.winfo_screenheight()
            off_x = off_y = 0
            scale_x = mon["width"] / over_w
            scale_y = mon["height"] / over_h
            top.geometry(f"{over_w}x{over_h}+0+0")
        else:
            # Windows (DPI-aware): Tk pixels == mss pixels across all monitors
            with mss.mss() as sct:
                mon = sct.monitors[0]
            over_w, over_h = mon["width"], mon["height"]
            off_x, off_y = mon["left"], mon["top"]
            scale_x = scale_y = 1.0
            top.geometry(f"{over_w}x{over_h}+{off_x}+{off_y}")

        top.attributes("-topmost", True)
        top.attributes("-alpha", 0.30)
        canvas = tk.Canvas(top, bg="black", cursor="crosshair",
                           highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.create_text(
            over_w // 2, 40,
            text="Drag to select the stream's video area  -  Esc cancels",
            fill="white", font=OVERLAY_FONT)

        state = {}

        def press(e):
            state["x0"], state["y0"] = e.x, e.y
            state["rect"] = canvas.create_rectangle(
                e.x, e.y, e.x, e.y, outline="red", width=2)

        def drag(e):
            if "rect" in state:
                canvas.coords(state["rect"], state["x0"], state["y0"], e.x, e.y)

        def release(e):
            if "x0" not in state:
                return
            l, r = sorted((state["x0"], e.x))
            t, b = sorted((state["y0"], e.y))
            if (r - l) >= 24 and (b - t) >= 24:
                result["region"] = (round(off_x + l * scale_x),
                                    round(off_y + t * scale_y),
                                    round((r - l) * scale_x),
                                    round((b - t) * scale_y))
            top.destroy()

        canvas.bind("<ButtonPress-1>", press)
        canvas.bind("<B1-Motion>", drag)
        canvas.bind("<ButtonRelease-1>", release)
        top.bind("<Escape>", lambda e: top.destroy())
        top.grab_set()
        top.focus_force()
        self.root.wait_window(top)
        return result.get("region")
