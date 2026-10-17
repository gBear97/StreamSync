"""The design's colours, sizes and fonts.

Pure data and functions: nothing here touches Tk, so this imports anywhere,
headless included, and a test can read or swap any of it.

Every size in the design is in design px. px() turns one into screen px at
the scale the platform reports, and font() builds a font at that scale, so
the windows keep their proportions at 150% and 200% on Windows. The Mac
reports 1.0: Tk measures in points there, and macOS scales points itself.
init() sets both from the platform adapter before the first widget is
built (Shell.__init__); until then the scale is 1.0.
"""

# --- the palette ---------------------------------------------------------

GROUND = "#16171a"           # window background
CARD = "#1f2125"
CARD_LINE = "#2c2f34"        # a card's 1-px border, and dividers
RAISED = "#2a2d32"           # secondary buttons, chips, key caps
RAISED_HOVER = "#33363c"
LINE = "#3a3d43"             # field borders, an off switch, the timeline's rest
FIELD = "#24272b"
TILE = "#24272b"             # icon tiles
TEXT = "#ececec"
MUTED = "#a3a7ae"
FAINT = "#8b9098"
BODY_TEXT = "#c9ccd1"        # the Update dialog's paragraph
PLAY_BG = "#ececec"          # the round play button
KNOB = "#f2f2f2"

ACCENT = "#ff8a1f"
ACCENT_HOVER = "#ff9d45"
ACCENT_PRESSED = "#e67812"
ON_ACCENT = "#17181a"        # text and knobs drawn on ACCENT
LINK = "#ffb066"
LINK_HOVER = "#ffd0a3"

SYNCED = "#4cc38a"
# Not on the boards, which only show a good sync: a weak match and a
# failure need a state dot of their own.
WARN = "#e8b04a"
ERROR = "#e5484d"

DISABLED_BG = "#2a2d32"
DISABLED_FG = "#8b9098"
DISABLED_LINE = "#34373d"

# The fullscreen bar and the volume OSD float over the film, so they keep
# the darker, neutral look of a player's controls rather than the cards'.
BAR_BG = "#1b1b1b"
BAR_LINE = "#3a3a3a"
BAR_ALPHA = 0.92
BAR_BTN = "#2c2c2c"
BAR_BTN_HOVER = "#444444"
BAR_FG = "#e6e6e6"
BAR_MUTED = "#a3a3a3"
BAR_TRACK = "#4a4a4a"
BAR_SEP = "#444444"
OSD_BG = "#111111"
OSD_FG = "#e6e6e6"

# --- scale and font ------------------------------------------------------

SCALE = 1.0                  # screen px per design px
FAMILY = "TkDefaultFont"     # replaced by the platform's family in init()


def init(scale, family):
    """Set the scale and font family for every size and font made after
    this. A scale Tk could not report (None, zero) counts as 1.0."""
    global SCALE, FAMILY
    try:
        scale = float(scale)
    except (TypeError, ValueError):
        scale = 1.0
    SCALE = scale if scale > 0 else 1.0
    FAMILY = family or "TkDefaultFont"


def px(n):
    """Design px -> whole screen px at the current scale."""
    return round(n * SCALE)


def font(size, bold=False):
    """A font tuple for a size in design px. The size is negative, which
    Tk reads as pixels, so text keeps its size against the layout on
    every platform; the design's weight 600 is Tk's bold."""
    return (FAMILY, -px(size), "bold" if bold else "normal")
