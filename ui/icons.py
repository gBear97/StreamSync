"""The design's icons, drawn with Canvas primitives (no image files).

Each icon is the boards' SVG path on its 24-unit grid, with the stroke
width the boards give it. Curves become short polylines, strokes have
round caps and joins, and the stroke scales with the icon. Two icons are
not on the boards and are drawn in the same style: `play` (a triangle,
the pause button's other face) and `speaker_on` (the muted speaker with
two arcs instead of the cross). `chevron` is the drop-down's arrow.

draw() works in the canvas's own pixels: (x, y) is the top-left corner of
the size x size box the icon fills. Callers (the controls in widgets.py)
scale their design sizes before they call it.
"""

import math

NAMES = ("film", "resync", "fullscreen", "speaker_muted", "speaker_on", "subtitles",
         "settings", "people", "pause", "play", "stop", "frame_back", "frame_fwd",
         "download", "chevron")


# --- path pieces on the 24-unit grid -------------------------------------

def _arc(cx, cy, r, start, end, steps=None):
    """Points along a circle from angle `start` to `end` (degrees, SVG
    sense: 0 is right, 90 is down)."""
    steps = steps or max(4, int(abs(end - start) / 12))
    return [(cx + r * math.cos(math.radians(start + (end - start) * i / steps)),
             cy + r * math.sin(math.radians(start + (end - start) * i / steps)))
            for i in range(steps + 1)]


def _cubic(p0, p1, p2, p3, steps=10):
    pts = []
    for i in range(steps + 1):
        t = i / steps
        a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3
        pts.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                    a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return pts


def _rounded(x0, y0, x1, y1, r):
    """A closed rounded rectangle (SVG rect with rx)."""
    return (_arc(x1 - r, y0 + r, r, -90, 0, 4) + _arc(x1 - r, y1 - r, r, 0, 90, 4)
            + _arc(x0 + r, y1 - r, r, 90, 180, 4) + _arc(x0 + r, y0 + r, r, 180, 270, 4))


# Each icon: (stroke width in grid units, [shapes]). A shape is
#   ("line", points)           an open stroked path
#   ("shape", points, filled)  a closed path, stroked; filled when True
#   ("circle", cx, cy, r)      a stroked circle
_FILM = (1.8, [
    ("shape", _rounded(3, 4, 21, 20, 2), False),
    ("line", [(7, 4), (7, 20)]), ("line", [(17, 4), (17, 20)]),
    ("line", [(3, 9), (7, 9)]), ("line", [(3, 15), (7, 15)]),
    ("line", [(17, 9), (21, 9)]), ("line", [(17, 15), (21, 15)]),
])
# M20 12a8 8 0 1 1-2.3-5.7: from the right, clockwise round to the top right
_RESYNC = (1.9, [
    ("line", _arc(12, 12, 8, 0, 315)),
    ("line", [(20, 4), (20, 9), (15, 9)]),
])
_FULLSCREEN = (1.9, [
    ("line", [(4, 9), (4, 4), (9, 4)]), ("line", [(20, 9), (20, 4), (15, 4)]),
    ("line", [(4, 15), (4, 20), (9, 20)]), ("line", [(20, 15), (20, 20), (15, 20)]),
])
_SPEAKER = ("shape", [(4, 10), (4, 14), (8, 14), (13, 18), (13, 6), (8, 10)], False)
_SPEAKER_MUTED = (1.9, [
    _SPEAKER,
    ("line", [(17, 9), (21, 15)]), ("line", [(21, 9), (17, 15)]),
])
_SPEAKER_ON = (1.9, [
    _SPEAKER,
    ("line", _arc(13, 12, 4, -45, 45, 6)),
    ("line", _arc(13, 12, 8, -48, 48, 10)),
])
_SUBTITLES = (1.9, [
    ("shape", _rounded(3, 5, 21, 19, 2), False),
    ("line", [(7, 15), (11, 15)]), ("line", [(13, 15), (17, 15)]),
    ("line", [(7, 11), (17, 11)]),
])
_SETTINGS = (1.9, [
    ("line", [(4, 7), (14, 7)]), ("line", [(18, 7), (20, 7)]),
    ("line", [(4, 17), (8, 17)]), ("line", [(12, 17), (20, 17)]),
    ("circle", 16, 7, 2), ("circle", 10, 17, 2),
])
# M3 19c0-3 3-5 6-5s6 2 6 5 and M16 14c3 0 5 2 5 5
_PEOPLE = (1.9, [
    ("circle", 9, 8, 3),
    ("line", _cubic((3, 19), (3, 16), (6, 14), (9, 14))
     + _cubic((9, 14), (12, 14), (15, 16), (15, 19))[1:]),
    ("circle", 17, 9, 2.5),
    ("line", _cubic((16, 14), (19, 14), (21, 16), (21, 19))),
])
_PAUSE = (2.6, [("line", [(9, 6), (9, 18)]), ("line", [(15, 6), (15, 18)])])
# the pause button's other face: its weight sits a little right of centre,
# as a triangle's should to look centred
_PLAY = (2.0, [("shape", [(8, 5.5), (19, 12), (8, 18.5)], True)])
_STOP = (0.0, [("shape", _rounded(6, 6, 18, 18, 1), True)])
_FRAME_BACK = (2.2, [("line", [(6, 5), (6, 19)]),
                     ("shape", [(18, 6), (10, 12), (18, 18)], False)])
_FRAME_FWD = (2.2, [("line", [(18, 5), (18, 19)]),
                    ("shape", [(6, 6), (14, 12), (6, 18)], False)])
_DOWNLOAD = (1.9, [("line", [(12, 4), (12, 15)]), ("line", [(7, 10), (12, 15), (17, 10)]),
                   ("line", [(5, 20), (19, 20)])])
_CHEVRON = (2.0, [("line", [(6, 9), (12, 15), (18, 9)])])

_ICONS = {
    "film": _FILM, "resync": _RESYNC, "fullscreen": _FULLSCREEN,
    "speaker_muted": _SPEAKER_MUTED, "speaker_on": _SPEAKER_ON, "subtitles": _SUBTITLES,
    "settings": _SETTINGS, "people": _PEOPLE, "pause": _PAUSE, "play": _PLAY, "stop": _STOP,
    "frame_back": _FRAME_BACK, "frame_fwd": _FRAME_FWD, "download": _DOWNLOAD,
    "chevron": _CHEVRON,
}


def draw(canvas, name, x, y, size, color, width=None, fill=False):
    """Draw icon `name` on `canvas` in `color`; returns the item ids. The
    stroke `width` (canvas px) defaults to the board's width for that icon,
    scaled to `size`. `fill` also fills the icon's closed outlines."""
    if name not in _ICONS:
        raise ValueError(f"no icon named {name!r}")
    sw, shapes = _ICONS[name]
    u = size / 24
    if width is None:
        width = max(1.0, round(sw * u, 2))

    def at(points):              # 24-unit grid -> flat canvas coordinates
        return [v for px_, py_ in points for v in (x + px_ * u, y + py_ * u)]

    style = {"capstyle": "round", "joinstyle": "round"}
    items = []
    for shape in shapes:
        kind = shape[0]
        if kind == "line":
            items.append(canvas.create_line(at(shape[1]), fill=color, width=width, **style))
        elif kind == "shape":
            filled = shape[2] or fill
            items.append(canvas.create_polygon(
                at(shape[1]), fill=color if filled else "", outline=color,
                width=width, joinstyle="round"))
        else:                    # circle
            _, cx, cy, r = shape
            items.append(canvas.create_oval(
                x + (cx - r) * u, y + (cy - r) * u, x + (cx + r) * u, y + (cy + r) * u,
                outline=color, width=width, fill=color if fill else ""))
    return items
