"""Builds every screen of the shared shell (ui/) under real Tk and checks
what each one shows and does, against the approved boards.

First, with no display needed (so it runs on the ubuntu CI runner too):
every ui module imports without making a Tk root or loading AppKit, both
platform adapters fill in the whole PlatformAdapter contract (the Windows
one is read with ast, since it cannot be imported off Windows), and the
cross-mixin contract in ui/contract.py holds for the tree.

Then, where there is a display, the real App - Windows' adapter on
Windows, the Mac's everywhere else - over a stand-in player (no VLC, no
film): FirstRun, Main synced and playing, Settings, Watch party idle and
running, the Update dialog's views, the film menu, the fullscreen bar's
controls, the keys and what they must leave alone, config migration, a
film that cannot be reopened, and the event loop surviving a bad event.
Prints "SKIPPED: no display" when Tk cannot open one.

With STREAMSYNC_SHOTS (or STREAMSYNC_SCREENSHOTS) set to a folder, a PNG
of each screen is saved there: 01-first-run-win.png and so on, -mac on a
Mac and -linux under X11. Best effort: a capture that fails prints
SKIPPED and fails nothing. On a Mac each window is read from the window
server as one of the test's own, which needs no Screen Recording
permission (reading the screen, as screencapture does, would).

    python test_ui.py

The settings and the log go to a temporary folder: nothing here reads or
writes ~/.streamsync.json.
"""

import ast
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import zlib
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
fails = []

# The board's sample film is a real title; the test's stand-in is not.
FILM_NAME = "Example Film (2014).mkv"
FILM_LENGTH = 10143.0        # 2:49:03, as on the boards
FILM_AT = 4360.0             # 1:12:40
KEYS = ("<space>", "<Left>", "<Right>", "<KeyPress-f>", "<KeyPress-F>", "<Escape>")


def check(label, got, want=True):
    if got != want:
        fails.append(f"{label}: got {got!r}, wanted {want!r}")


def check_in(label, needle, haystack):
    """`needle` is one of the texts, or part of one."""
    if not any(needle in t for t in haystack):
        fails.append(f"{label}: {needle!r} not among {haystack!r}")


def phase(name, fn, *args):
    """Run one group of checks; an exception fails it without hiding the
    groups after it."""
    print(f"--- {name}", flush=True)
    try:
        return fn(*args)
    except Exception:
        fails.append(f"{name}: raised\n{traceback.format_exc()}")
        return None


# ------------------------------------------------------ no display needed

def imports():
    import tkinter
    import ui.adapter
    import ui.contract
    import ui.dialogs
    import ui.icons
    import ui.mainwin
    import ui.platform_mac
    import ui.shell
    import ui.theme
    import ui.update
    import ui.video
    import ui.widgets                                  # noqa: F401
    check("importing ui made no Tk root", getattr(tkinter, "_default_root", None), None)
    check("importing ui loaded no AppKit",
          [m for m in ("AppKit", "objc") if m in sys.modules], [])
    if sys.platform != "win32":
        # it calls windll as it imports: only Windows (or a stub) may load it
        check("ui.platform_win stays unimported off Windows",
              "ui.platform_win" in sys.modules, False)


def _class(rel, name):
    tree = ast.parse((HERE / rel).read_text(encoding="utf-8"))
    found = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name]
    if len(found) != 1:
        raise AssertionError(f"{rel}: expected one class {name}, found {len(found)}")
    return found[0]


def _raises_not_implemented(fn):
    body = [s for s in fn.body
            if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
    return (len(body) == 1 and isinstance(body[0], ast.Raise)
            and "NotImplementedError" in ast.unparse(body[0]))


def adapters_complete():
    """Each adapter defines every PlatformAdapter method that has no
    default, and every constant the base leaves empty."""
    base = _class("ui/adapter.py", "PlatformAdapter")
    needed = [f.name for f in base.body
              if isinstance(f, ast.FunctionDef) and _raises_not_implemented(f)]
    needed += [t.id for a in base.body if isinstance(a, ast.Assign)
               and isinstance(a.value, ast.Constant) and a.value.value == ""
               for t in a.targets if isinstance(t, ast.Name)]
    check("PlatformAdapter has methods without a default", len(needed) > 10)
    for rel in ("ui/platform_win.py", "ui/platform_mac.py"):
        cls = _class(rel, "Adapter")
        have = set()
        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                have.add(node.name)
            elif isinstance(node, ast.Assign):
                have.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                have.add(node.target.id)
        check(f"{rel} defines the whole adapter contract",
              [n for n in needed if n not in have], [])


def contract_holds():
    from ui import contract
    found = contract.problems()
    check("ui/contract.py: every name defined once, in its owner", found, [])
    check("ui/contract.py lists names", len(contract.CONTRACT) >= 5)


def selftest_config():
    """item 13: an unattended run's config goes to a temporary folder."""
    import controller
    from ui import shell
    before = controller.CONFIG_PATH
    try:
        shell._selftest_config()
        moved = Path(controller.CONFIG_PATH)
        temp = Path(tempfile.gettempdir()).resolve()
        check("selftest config moved", moved != Path(before))
        check("selftest config is streamsync.json", moved.name, "streamsync.json")
        check("selftest config folder is a fresh temporary one",
              moved.parent.name.startswith("streamsync-selftest-")
              and temp in moved.resolve().parents)
        check("selftest config not written yet", moved.exists(), False)
        shutil.rmtree(moved.parent, ignore_errors=True)
    finally:
        controller.CONFIG_PATH = before


# ------------------------------------------------------------ screenshots

SHOTS = os.environ.get("STREAMSYNC_SHOTS") or os.environ.get("STREAMSYNC_SCREENSHOTS")
SUFFIX = {"win32": "win", "darwin": "mac"}.get(sys.platform, "linux")
_mac_capture_ok = None


def _png(path, w, h, rgb):
    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))
    row = w * 3
    raw = b"".join(b"\x00" + rgb[i * row:(i + 1) * row] for i in range(h))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(raw, 6)))
        f.write(chunk(b"IEND", b""))


def _rgb_from_32bit(data, w, h, stride, red_first):
    """Rows of 4-byte pixels (B,G,R,x, or R,G,B,x) as packed RGB."""
    return _rgb_at(data, w, h, stride, (0, 1, 2) if red_first else (2, 1, 0))


def _rgb_at(data, w, h, stride, offsets):
    """Rows of 4-byte pixels as packed RGB; `offsets` are where red, green
    and blue sit in each pixel's 4 bytes."""
    r, g, b = offsets
    out = bytearray(w * h * 3)
    for y in range(h):
        line = data[y * stride:y * stride + w * 4]
        o = y * w * 3
        out[o:o + w * 3:3] = line[r::4]
        out[o + 1:o + w * 3:3] = line[g::4]
        out[o + 2:o + w * 3:3] = line[b::4]
    return bytes(out)


def _grab_windows(hwnd, path):
    """The window's screen pixels (its visible frame), with GDI: no Pillow."""
    import ctypes
    from ctypes import wintypes
    # Private handles to the DLLs: argtypes set here must not change the
    # shared ctypes.windll functions windowctl calls.
    u32, g32 = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
    rect = wintypes.RECT()
    try:
        # the frame as drawn, without Windows 10's invisible resize borders
        dwm = ctypes.WinDLL("dwmapi")
        got = dwm.DwmGetWindowAttribute(wintypes.HWND(hwnd), 9, ctypes.byref(rect),
                                        ctypes.sizeof(rect)) == 0
    except OSError:
        got = False
    if not got:
        u32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    x, y = rect.left, rect.top
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise OSError(f"empty window rectangle {w}x{h}")
    u32.GetDC.restype = wintypes.HDC
    u32.GetDC.argtypes = [wintypes.HWND]
    u32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    g32.CreateCompatibleDC.restype = wintypes.HDC
    g32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    g32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    g32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    g32.SelectObject.restype = wintypes.HGDIOBJ
    g32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    g32.BitBlt.argtypes = [wintypes.HDC] + [ctypes.c_int] * 4 + [wintypes.HDC] \
        + [ctypes.c_int] * 2 + [wintypes.DWORD]
    g32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    g32.DeleteDC.argtypes = [wintypes.HDC]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    g32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                              ctypes.c_void_p, ctypes.POINTER(BITMAPINFOHEADER),
                              wintypes.UINT]
    screen = u32.GetDC(None)
    mem = g32.CreateCompatibleDC(screen)
    bmp = g32.CreateCompatibleBitmap(screen, w, h)
    old = g32.SelectObject(mem, bmp)
    try:
        # SRCCOPY | CAPTUREBLT: layered windows (the bar's alpha) included
        if not g32.BitBlt(mem, 0, 0, w, h, screen, x, y, 0x00CC0020 | 0x40000000):
            raise OSError("BitBlt failed")
        info = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0,
                                0, 0, 0, 0, 0)          # -h: top row first
        buf = ctypes.create_string_buffer(w * h * 4)
        if g32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(info), 0) != h:
            raise OSError("GetDIBits failed")
    finally:
        g32.SelectObject(mem, old)
        g32.DeleteObject(bmp)
        g32.DeleteDC(mem)
        u32.ReleaseDC(None, screen)
    _png(path, w, h, _rgb_from_32bit(buf.raw, w, h, w * 4, red_first=False))


def _grab_x11(x, y, w, h, path):
    """A screen rectangle from the X server, with libX11 through ctypes."""
    import ctypes
    import ctypes.util
    name = ctypes.util.find_library("X11")
    if not name:
        raise OSError("no libX11")
    x11 = ctypes.CDLL(name)

    class XImage(ctypes.Structure):       # the leading fields of Xlib's XImage
        _fields_ = [("width", ctypes.c_int), ("height", ctypes.c_int),
                    ("xoffset", ctypes.c_int), ("format", ctypes.c_int),
                    ("data", ctypes.c_void_p), ("byte_order", ctypes.c_int),
                    ("bitmap_unit", ctypes.c_int), ("bitmap_bit_order", ctypes.c_int),
                    ("bitmap_pad", ctypes.c_int), ("depth", ctypes.c_int),
                    ("bytes_per_line", ctypes.c_int), ("bits_per_pixel", ctypes.c_int),
                    ("red_mask", ctypes.c_ulong), ("green_mask", ctypes.c_ulong),
                    ("blue_mask", ctypes.c_ulong)]

    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XDefaultRootWindow.restype = ctypes.c_ulong
    x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x11.XGetImage.restype = ctypes.POINTER(XImage)
    x11.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                              ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int]
    x11.XSetErrorHandler.restype = ctypes.c_void_p
    x11.XSetErrorHandler.argtypes = [ctypes.c_void_p]
    x11.XFree.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    dpy = x11.XOpenDisplay(None)
    if not dpy:
        raise OSError("cannot open the X display")
    # Xlib's default error handler ends the process. Ours only for the
    # length of the grab; Tk's is put back before Tk runs again.
    quiet = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(lambda d, e: 0)
    previous = x11.XSetErrorHandler(ctypes.cast(quiet, ctypes.c_void_p))
    try:
        img = x11.XGetImage(dpy, x11.XDefaultRootWindow(dpy), x, y, w, h,
                            ctypes.c_ulong(-1).value, 2)         # AllPlanes, ZPixmap
        if not img:
            raise OSError("XGetImage failed")
        im = img.contents
        try:
            if im.bits_per_pixel != 32 or im.byte_order != 0:
                raise OSError(f"unsupported pixels: {im.bits_per_pixel} bpp, "
                              f"byte order {im.byte_order}")
            stride = im.bytes_per_line
            data = ctypes.string_at(im.data, stride * h)
            red_first = im.red_mask == 0xFF
        finally:
            x11.XFree(im.data)                # what XDestroyImage does
            x11.XFree(img)
    finally:
        x11.XSetErrorHandler(previous)
        x11.XCloseDisplay(dpy)
    _png(path, w, h, _rgb_from_32bit(data, w, h, stride, red_first))


def _grab_mac(win, path):
    """`win`'s pixels from the window server, with CoreGraphics through
    ctypes: no Pillow, and no Screen Recording permission, which macOS only
    asks for before a process reads other apps' windows, not its own."""
    import ctypes
    cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    vp = ctypes.c_void_p

    class CGRect(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double),
                    ("w", ctypes.c_double), ("h", ctypes.c_double)]

    for fn, res, args in (
            (cf.CFArrayGetCount, ctypes.c_long, [vp]),
            (cf.CFArrayGetValueAtIndex, vp, [vp, ctypes.c_long]),
            (cf.CFDictionaryGetValue, vp, [vp, vp]),
            (cf.CFStringCreateWithCString, vp, [vp, ctypes.c_char_p, ctypes.c_uint32]),
            (cf.CFStringGetCString, ctypes.c_bool, [vp, ctypes.c_char_p, ctypes.c_long,
                                                    ctypes.c_uint32]),
            (cf.CFNumberGetValue, ctypes.c_bool, [vp, ctypes.c_int, vp]),
            (cf.CFRelease, None, [vp]),
            (cg.CGWindowListCopyWindowInfo, vp, [ctypes.c_uint32, ctypes.c_uint32]),
            (cg.CGWindowListCreateImage, vp, [CGRect, ctypes.c_uint32, ctypes.c_uint32,
                                              ctypes.c_uint32]),
            (cg.CGImageGetWidth, ctypes.c_size_t, [vp]),
            (cg.CGImageGetHeight, ctypes.c_size_t, [vp]),
            (cg.CGColorSpaceCreateWithName, vp, [vp]),
            (cg.CGBitmapContextCreate, vp, [vp, ctypes.c_size_t, ctypes.c_size_t,
                                            ctypes.c_size_t, ctypes.c_size_t, vp,
                                            ctypes.c_uint32]),
            (cg.CGContextDrawImage, None, [vp, CGRect, vp]),
            (cg.CGBitmapContextGetData, vp, [vp]),
            (cg.CGContextRelease, None, [vp]),
            (cg.CGColorSpaceRelease, None, [vp]),
            (cg.CGImageRelease, None, [vp])):
        fn.restype, fn.argtypes = res, args
    utf8 = 0x08000100
    keys = {k: cf.CFStringCreateWithCString(None, k.encode(), utf8)
            for k in ("kCGWindowOwnerPID", "kCGWindowNumber", "kCGWindowName")}

    def number(d, key):
        v = cf.CFDictionaryGetValue(d, keys[key])
        out = ctypes.c_int64()
        return out.value if v and cf.CFNumberGetValue(v, 4, ctypes.byref(out)) else None

    def string(d, key):
        v = cf.CFDictionaryGetValue(d, keys[key])
        buf = ctypes.create_string_buffer(1024)
        return buf.value.decode() if v and cf.CFStringGetCString(v, buf, 1024, utf8) else None

    # this process's window with the Tk window's title, as the window
    # server lists it (on screen only: 1)
    found = None
    windows = cg.CGWindowListCopyWindowInfo(1, 0)
    if not windows:
        # no window server to ask: CoreFoundation would crash on the NULL
        for k in keys.values():
            cf.CFRelease(k)
        raise OSError("the window server lists no windows")
    try:
        for i in range(cf.CFArrayGetCount(windows)):
            d = cf.CFArrayGetValueAtIndex(windows, i)
            if number(d, "kCGWindowOwnerPID") == os.getpid() \
                    and string(d, "kCGWindowName") == win.title():
                found = number(d, "kCGWindowNumber")
                break
    finally:
        cf.CFRelease(windows)
        for k in keys.values():
            cf.CFRelease(k)
    if found is None:
        raise OSError(f"no window titled {win.title()!r} on screen")
    # CGRectNull: the window's own bounds. Including that window only (8);
    # without its shadow (1), at the screen's own resolution (8).
    inf = float("inf")
    img = cg.CGWindowListCreateImage(CGRect(inf, inf, 0, 0), 8, found, 1 | 8)
    if not img:
        raise OSError("the window server gave no image")
    # Drawn again into an sRGB bitmap, so the PNG holds the colours the app
    # asked for: the window server's image is in the display's own colour
    # space (Display P3 on recent Macs), which reads washed out as sRGB.
    w, h = cg.CGImageGetWidth(img), cg.CGImageGetHeight(img)
    srgb = cg.CGColorSpaceCreateWithName(vp.in_dll(cg, "kCGColorSpaceSRGB"))
    # 8 bits a channel in the order R, G, B, A: alpha premultiplied and last
    # (1), the 32 bits big-endian (0x4000)
    ctx = cg.CGBitmapContextCreate(None, w, h, 8, w * 4, srgb, 0x4000 | 1) if srgb else None
    try:
        if not ctx:
            raise OSError("no sRGB bitmap to draw the window into")
        cg.CGContextDrawImage(ctx, CGRect(0, 0, w, h), img)
        raw = ctypes.string_at(cg.CGBitmapContextGetData(ctx), w * 4 * h)
    finally:
        if ctx:
            cg.CGContextRelease(ctx)
        if srgb:
            cg.CGColorSpaceRelease(srgb)
        cg.CGImageRelease(img)
    _png(path, w, h, _rgb_at(raw, w, h, w * 4, (0, 1, 2)))


def _mac_may_capture():
    """True when screencapture will not make macOS ask for Screen
    Recording permission (or when running in CI, where nobody is asked)."""
    global _mac_capture_ok
    if _mac_capture_ok is None:
        try:
            import ctypes
            cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
            _mac_capture_ok = bool(cg.CGPreflightScreenCaptureAccess())
        except Exception:
            _mac_capture_ok = False
        if not _mac_capture_ok and os.environ.get("CI"):
            _mac_capture_ok = True
    return _mac_capture_ok


def shot(root, win, name, on_top=True):
    """Save `win` as SHOTS/<name>-<os>.png, if SHOTS is set."""
    if not SHOTS:
        return
    path = os.path.join(SHOTS, f"{name}-{SUFFIX}.png")
    try:
        os.makedirs(SHOTS, exist_ok=True)
        if on_top:
            win.attributes("-topmost", True)
        win.lift()
        pump(root, 0.6)
        if sys.platform == "win32":
            _grab_windows(int(win.wm_frame(), 16), path)
        else:
            x, y = win.winfo_rootx(), win.winfo_rooty()
            w, h = win.winfo_width(), win.winfo_height()
            # a rectangle off the screen is an error to both tools
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            x, y = max(0, x), max(0, y)
            w, h = min(w, sw - x), min(h, sh - y)
            if w <= 1 or h <= 1:
                raise OSError(f"window not on screen ({w}x{h})")
            if sys.platform == "darwin":
                try:
                    _grab_mac(win, path)
                except Exception as e:
                    # the screen instead, where that asks nobody
                    if not _mac_may_capture():
                        raise OSError(f"{e}; and no Screen Recording permission to read "
                                      f"the screen (not asked for outside CI)")
                    r = subprocess.run(["screencapture", "-x", "-R", f"{x},{y},{w},{h}", path],
                                       capture_output=True, timeout=20)
                    if r.returncode or not os.path.exists(path):
                        raise OSError(r.stderr.decode(errors="replace").strip() or "no file")
            else:
                _grab_x11(x, y, w, h, path)
        print(f"screenshot: {path}")
    except Exception as e:
        print(f"SKIPPED screenshot {name}: {e}")
    finally:
        if on_top:
            try:
                win.attributes("-topmost", False)
            except Exception:
                pass


# -------------------------------------------------------------- the app

def pump(root, seconds=0.1):
    end = time.monotonic() + seconds
    while True:
        root.update()
        if time.monotonic() >= end:
            break
        time.sleep(0.01)


def drop_afters(root):
    """Cancel what a closed app left scheduled. Its interpreter outlives
    root.destroy(), and the next app's event loop would run those timers
    against commands that are gone ("invalid command name" on stderr).
    The real app never sees this: main() ends the process after its loop."""
    try:
        for after_id in root.tk.splitlist(root.tk.call("after", "info")):
            root.tk.call("after", "cancel", after_id)
    except Exception:
        pass


def descendants(widget):
    """Every widget under `widget` in its own window (other toplevels and
    menus are windows of their own)."""
    import tkinter as tk
    for child in widget.winfo_children():
        if isinstance(child, (tk.Toplevel, tk.Menu)):
            continue
        yield child
        yield from descendants(child)


def texts(widget):
    """What `widget`'s window shows, as a list of strings: labels' and
    buttons' text, drawn text on canvases, entries' and text boxes'
    contents. Only laid-out widgets count: a hidden view's text is not
    on screen."""
    import tkinter as tk
    out = []

    def walk(w):
        try:
            t = w.cget("text")
            if isinstance(t, str) and t:
                out.append(t)
        except (tk.TclError, AttributeError, ValueError):
            pass
        if isinstance(w, tk.Canvas):
            for item in w.find_all():
                if w.type(item) == "text":
                    t = w.itemcget(item, "text")
                    if t:
                        out.append(t)
        elif isinstance(w, tk.Entry):
            if w.get():
                out.append(w.get())
        elif isinstance(w, tk.Text):
            out.append(w.get("1.0", "end-1c"))
        for child in w.winfo_children():
            if child.winfo_manager() in ("pack", "grid", "place", "canvas"):
                walk(child)

    walk(widget)
    return out


def find(widget, test):
    for w in descendants(widget):
        try:
            if test(w):
                return w
        except Exception:
            pass
    return None


def text_is(text):
    def test(w):
        return w.cget("text") == text
    return test


def ev(widget):
    return SimpleNamespace(widget=widget)


def switch_is_on(sw):
    """Read the Switch's drawing, not its variable: the knob sits right
    when on."""
    ovals = [i for i in sw.find_all() if sw.type(i) == "oval"]
    x0, _, x1, _ = sw.coords(ovals[-1])
    w = sw.winfo_width() if sw.winfo_width() > 1 else sw.winfo_reqwidth()
    return (x0 + x1) / 2 > w / 2


def radio_is_on(radio):
    """The drawn dot, not the variable: only a chosen Radio has one."""
    from ui import theme
    return any(radio.type(i) == "oval" and radio.itemcget(i, "fill") == theme.ON_ACCENT
               for i in radio.find_all())


class FakePlayer:
    """EmbeddedPlayer as the shell, the controller and the film window use
    it, with no VLC: it plays nothing and records what it was asked."""

    fail_load = None             # a message: load() raises VLCError with it

    def __init__(self, hwnd=None):
        self.hwnd = None if hwnd is None else int(hwnd)
        self.nsview = None
        self.embedded = hwnd is not None      # Windows: attached as it is made
        self.has_media = False
        self.path = None
        self.t = None
        self.n = None
        self.playing = False
        self.muted = None
        self.vol = 100
        self.sub = -1
        self.pauses = 0
        self.seeks = []
        self.nudges = []
        self.fs = []

    def attach_tk(self, win_id):
        self.nsview = int(win_id)
        self.embedded = True
        return True

    def load(self, path):
        if FakePlayer.fail_load:
            from players import VLCError
            raise VLCError(FakePlayer.fail_load)
        self.path = path
        self.has_media = True

    def ensure_playing(self, timeout=6.0):
        self.playing = True

    def sync_seek(self, match_t, t0_perf, offset, waiting=None):
        self.seeks.append(match_t + offset)

    def goto(self, target, at):
        self.seeks.append(target)

    def nudge(self, delta):
        self.nudges.append(delta)

    def toggle_pause(self):
        self.pauses += 1
        self.playing = not self.playing

    def pause(self):
        self.playing = False

    def seek(self, seconds):
        self.seeks.append(seconds)
        self.t = seconds

    def time(self):
        return self.t

    def clock(self):
        return (self.t, time.perf_counter()) if self.playing and self.t is not None else None

    def clock_state(self):
        return self.t, self.playing, time.perf_counter()

    def length(self):
        return self.n

    def fps(self):
        return 25.0

    def set_mute(self, mute):
        self.muted = bool(mute)

    def volume(self):
        return self.vol

    def set_volume(self, v):
        self.vol = max(0, min(125, int(v)))

    def set_fullscreen(self, flag):
        self.fs.append(flag)
        return False

    def is_playing(self):
        return self.playing

    def stop(self):
        self.playing = False

    def subtitle_tracks(self):
        return []

    def set_subtitle(self, track_id):
        self.sub = int(track_id)

    def current_subtitle(self):
        return self.sub

    def add_subtitle_file(self, path):
        pass


class Env:
    """What every app in this run shares: the temporary folder, the
    stand-ins, the error traps and what they caught."""

    def __init__(self):
        import tkinter as tk
        from tkinter import messagebox
        import webbrowser

        import audio_capture
        import controller
        import diagnostics
        from ui import shell
        self.tk, self.shell, self.controller = tk, shell, controller
        if sys.platform == "win32":
            from ui import platform_win as platform
        else:
            # The Mac's adapter imports anywhere (its helpers are
            # subprocess-only); the Windows one needs windll. Linux has
            # no adapter of its own, so the Mac's stands in under X11.
            from ui import platform_mac as platform
        self.platform = platform
        self.mac_adapter = platform.__name__.endswith("platform_mac")
        self.work = Path(tempfile.mkdtemp(prefix="streamsync-ui-test-"))
        self.config = self.work / "streamsync.json"
        controller.CONFIG_PATH = self.config
        diagnostics.LOG_DIR = str(self.work)
        diagnostics.LOG_FILE = str(self.work / "streamsync.log")
        self.film = self.work / FILM_NAME
        self.film.write_bytes(b"")

        platform.EmbeddedPlayer = FakePlayer
        shell.matcher.probe = lambda path: (FILM_LENGTH, 0.0)
        # The machine's own devices would make the screens differ by
        # runner (or fail to list on one with no sound card).
        audio_capture.list_speakers = lambda: ["Speakers", "Headphones"]
        audio_capture.default_speaker_name = lambda: "Speakers"
        audio_capture.list_microphones = lambda: ["Built-in Microphone"]
        # Nothing here may open a browser or sit on a native modal.
        self.opened = []
        webbrowser.open = lambda url, *a, **k: self.opened.append(url)
        self.boxes = []
        for kind in ("showerror", "showinfo", "showwarning"):
            setattr(messagebox, kind,
                    lambda title=None, message=None, _k=kind, **kw:
                    self.boxes.append((_k, title, message)) or "ok")
        self.errors = []
        threading.excepthook = lambda a: self.errors.append(
            f"thread: {a.exc_type.__name__}: {a.exc_value}")
        self.titles = {}

    def write_config(self, cfg):
        self.config.write_text(json.dumps(cfg))

    def make(self):
        """A root and an App on it, as main() makes them."""
        root = self.tk.Tk()
        root.report_callback_exception = lambda exc, val, tb: self.errors.append(
            f"Tk callback: {exc.__name__}: {val}\n"
            + "".join(traceback.format_tb(tb))[-1200:])
        # The screen's maximum as the app reads it while it builds the
        # main window, where it caps the window's minimum. On a Mac Tk's
        # maximum is the screen less the menu bar and the Dock, and the Dock
        # resizes as apps start and quit, so a read made later can differ.
        self.screen_max = []
        read_max = root.wm_maxsize

        def wm_maxsize(width=None, height=None):
            got = read_max(width, height)
            if width is None and height is None:
                self.screen_max.append(tuple(got))
            return got
        root.wm_maxsize = root.maxsize = wm_maxsize
        try:
            app = self.shell.create_app(root, self.platform)
        finally:
            del root.wm_maxsize, root.maxsize
        ctl = app.ctl
        real_set_auto = ctl.set_auto

        # Auto mode must never start listening here: a real capture would
        # need a sound card, and on a Mac the microphone permission.
        def set_auto(enabled, follow, interval):
            self.auto_calls.append((enabled, follow, interval))
            real_set_auto(False, follow, interval)
        self.auto_calls = []
        ctl.set_auto = set_auto
        # The stream-pause swap would raise or hide real windows and apps.
        self.swaps = []
        app._stream_swap = lambda show, why="": self.swaps.append(show)
        app._present_root()
        root.geometry("+40+40")
        pump(root, 0.4)
        return root, app

    def title(self, win):
        self.titles[str(win)] = win.title()


def first_run(env, root, app):
    from ui import widgets
    from version import __version__
    check("FirstRun shows without a film", app.first_frame.winfo_ismapped(), 1)
    from ui import theme
    # its full size, or as much as the screen allows: a minimum past the
    # screen's maximum (CI's macOS runner, at 1024x768) left the window at
    # Tk's 200x200 default, and pack unmapped what did not fit.
    # As much as the screen allowed when the app built the window, not
    # now: the Dock shrinks a point or two once this app's icon joins it,
    # or grows once the last test's leaves, up to a second later. CI's
    # runner had 647 there and 649 here, failing a window at its full 647.
    check("the app read the screen's maximum to build the main window",
          bool(env.screen_max), True)
    maxw, maxh = env.screen_max[-1] if env.screen_max else root.wm_maxsize()
    full = (root.winfo_width() >= min(theme.px(560), maxw),
            root.winfo_height() >= min(theme.px(664), maxh))
    check("the main window is its full size", full, (True, True))
    if full != (True, True):
        # what Tk thought, for a runner whose window came out small
        print("main window:", {
            "geometry": root.geometry(), "req": (root.winfo_reqwidth(), root.winfo_reqheight()),
            "outer req": [(str(c), c.winfo_reqwidth(), c.winfo_reqheight(), c.winfo_manager())
                          for c in root.winfo_children()][:4],
            "minsize": root.wm_minsize(), "maxsize": root.wm_maxsize(),
            "maxsize when built": env.screen_max,
            "state": root.state(), "screen": (root.winfo_screenwidth(), root.winfo_screenheight()),
            "resizable": root.resizable(), "tk": root.tk.call("info", "patchlevel")})
    check("Main is hidden without a film", app.main_frame.winfo_ismapped(), 0)
    shown = texts(app.root)
    for s in ("Open the film you're watching along to", "Your own copy, on this computer.",
              "Open film…", "Open your copy of the film.",
              "Play the stream in your browser, with its sound on.",
              "Press Sync to stream. StreamSync listens and jumps the film to the same moment.",
              "Sync to stream", "change", "Settings", "Watch party",
              f"StreamSync {__version__}"):
        check_in("FirstRun text", s, shown)
    check_in("FirstRun listening line", "Listening to: Speakers", shown)
    sync = find(app.first_frame, lambda w: isinstance(w, widgets.FlatButton)
                and w.cget("text") == "Sync to stream")
    check("FirstRun has its Sync button", sync is not None)
    if sync is not None:
        check("FirstRun Sync is disabled", sync.cget("state"), "disabled")
        cycle, w = [], app.root
        for _ in range(80):
            w = w.tk_focusNext()
            if w is None or str(w) in cycle:
                break
            cycle.append(str(w))
        opener = find(app.first_frame, text_is("Open film…"))
        check("Tab reaches Open film…", str(opener) in cycle)
        check("Tab skips the disabled Sync", str(sync) in cycle, False)
    # F with no film: nothing to fullscreen
    check("F with no film is swallowed", app._on_key("fullscreen", ev(app.root)), "break")
    pump(root, 0.1)
    check("F with no film leaves the film window withdrawn", app.video_win.state(), "withdrawn")
    check("F with no film leaves fullscreen off", app.fullscreen, False)
    shot(root, root, "01-first-run")


def config_read(env, root, app):
    """item 11, first half: a 1.x pick under the platform's old key."""
    check("1.x stream pick read from its old key", app.stream_window, "Old pick")
    check("1.x stream pick shown", app.stream_var.get(), "Old pick")
    check("check_updates read", app.check_updates_var.get(), False)


def main_synced(env, root, app):
    from ui import widgets
    p = app.embedded
    check("the built-in player is the stand-in", isinstance(p, FakePlayer))
    app._show_video_window()                  # as _choose_file does, before the load
    app.ctl.load_file(str(env.film))
    app._film_loaded(str(env.film))
    app._present_root()
    pump(root, 0.3)                           # the probe thread's film_info
    app._nudge(0.2)
    app.auto_var.set(True)
    p.t, p.n, p.playing = FILM_AT, FILM_LENGTH, True
    app.q.put(("synced", {"match": FILM_AT, "at": time.perf_counter(), "trusted": True,
                          "applied": True, "source": "sync"}))
    pump(root, 0.9)                           # the queue, and one 700-ms tick
    check("Main shows with a film", app.main_frame.winfo_ismapped(), 1)
    check("FirstRun hides with a film", app.first_frame.winfo_ismapped(), 0)
    shown = texts(app.root)
    for s in ("Synced", " - stream at 1:12:40", "1:12:40", "2:49:03", "Film muted",
              "+0.20 s", "Keep in sync automatically", "Fullscreen",
              "Resync", "Sync to stream", "Fine-tune", "Reset", "Open film…"):
        check_in("Main text", s, shown)
    # The controls row cuts the subtitle label to fit the card. Linux has
    # neither platform's font, so there it may be cut; the boards' fonts
    # fit it whole.
    subs = app.subs_btn.cget("text")
    check("Main subtitles label", subs == "Subtitles: Off"
          or (SUFFIX == "linux" and subs.startswith("Subtitles: ")))
    check_in("Main header", "2:49:03 · built-in player", shown)
    check_in("Main header film name", "Example Film", shown)
    check("bar status gives the headline", app._fs_status.cget("text"),
          "Synced - stream at 1:12:40")
    check("a trusted manual sync clears the raw status", app.status_lbl.cget("text"), "")
    check("the nudge reached the player", p.nudges, [0.2])
    sw = find(app.main_frame, lambda w: isinstance(w, widgets.Switch))
    check("Main has the auto Switch", sw is not None)
    if sw is not None:
        check("auto_var set by code draws the Switch on", switch_is_on(sw))
    shot(root, root, "02-main-synced")


def keys_and_film_window(env, root, app):
    # item 9: the six keys on the three windows that take them
    for name in ("root", "video_win", "fs_bar"):
        w = getattr(app, name)
        check(f"keys bound on {name}", [s for s in KEYS if not w.bind(s)], [])
    # item 8: the film window's menu and the fullscreen bar
    m = app.video_menu
    end = m.index("end")
    check("film menu layout", [m.type(i) for i in range(end + 1)],
          ["command", "command", "separator", "checkbutton", "command", "command",
           "separator", "cascade", "separator", "command"])
    check("film menu labels",
          [m.entrycget(i, "label") for i in range(end + 1) if m.type(i) != "separator"],
          ["Play / Pause", "Fullscreen", "Mute film sound", "Volume up", "Volume down",
           "Subtitles", "Hide video window"])
    check("Play / Pause shows its key", m.entrycget(0, "accelerator"), "Space")
    check("Fullscreen shows its key", m.entrycget(1, "accelerator"), "F")
    check("bar play button", app._fs_play.cget("text") in ("Pause", "Play"))
    check("bar buttons",
          [b.cget("text") for b in (app._fs_stop, app._fs_frame_back, app._fs_frame_fwd,
                                    app._fs_resync, app._fs_full)],
          ["Stop", "−1 frame", "+1 frame", "Resync here", "Resync (full)"])
    for name in ("root", "video_win", "fs_bar"):
        env.title(getattr(app, name))
    # item 10: Command-Q goes through _on_close
    if env.mac_adapter:
        check("tk::mac::Quit is ours",
              bool(root.tk.call("info", "commands", "::tk::mac::Quit")))
        check("tk::mac::ShowPreferences is ours",
              bool(root.tk.call("info", "commands", "::tk::mac::ShowPreferences")))


def typing_guard(env, root, app):
    """item 4: a text field in root keeps its keys."""
    p = app.embedded
    entry = env.tk.Entry(app.root)
    entry.place(x=0, y=0, width=60)
    pump(root, 0.1)
    pauses, seeks = p.pauses, len(p.seeks)
    check("Space in a text field is left to it", app._on_key("pause", ev(entry)), None)
    check("Left in a text field is left to it", app._on_key("back", ev(entry)), None)
    check("no pause from a text field", p.pauses, pauses)
    check("no seek from a text field", len(p.seeks), seeks)
    # the same keys outside a field do act, so the guard above is not vacuous
    check("Space elsewhere pauses", app._on_key("pause", ev(app.root)), "break")
    check("one pause", p.pauses, pauses + 1)
    check("Left elsewhere seeks back 5 s", (app._on_key("back", ev(app.root)), p.seeks[-1:]),
          ("break", [FILM_AT - 5]))
    p.t = FILM_AT
    p.playing = True
    entry.focus_force()
    pump(root, 0.2)
    if root.focus_get() is entry:
        pauses = p.pauses
        entry.event_generate("<KeyPress>", keysym="space")
        pump(root, 0.1)
        check("a real Space in the field types and does not pause",
              (p.pauses, entry.get()), (pauses, " "))
    else:
        print("SKIPPED real key events: the test window has no focus")
    entry.destroy()
    app._present_root()
    pump(root, 0.1)


def auto_off(env, root, app):
    """item 5, first half: a write from code redraws the Switch."""
    from ui import widgets
    sw = find(app.main_frame, lambda w: isinstance(w, widgets.Switch))
    app.q.put(("auto_off", "Auto re-sync stopped (test)."))
    pump(root, 0.3)
    check("auto_off turns auto_var off", app.auto_var.get(), False)
    check("auto_off draws the Switch off", sw is not None and not switch_is_on(sw))
    check("auto_off says why", app.status_lbl.cget("text"), "Auto re-sync stopped (test).")


def settings(env, root, app):
    """items 3 and 5."""
    from ui import widgets
    app._open_settings()
    pump(root, 0.4)
    d = app._settings
    check("Settings registered", d is not None)
    if d is None:
        return
    env.title(d.win)
    check("Settings title", d.win.title(), "StreamSync - Settings")
    shown = texts(d.win)
    for s in ("LISTENING", "PLAYER", "SYNCING", "UPDATES", "KEYS", "Listen to",
              "Built-in player", "The most precise sync, with fullscreen controls and subtitles.",
              "VLC app", "Plays in your installed VLC's own window instead.",
              "Start searching near", "Optional. Blank searches the whole film.",
              "Re-check every", "seconds", 'While "Keep in sync automatically" is on.',
              "Follow the stream's pauses", "Show the stream while the film is paused",
              app.STREAM_LABEL, "Refresh", "Check for updates when StreamSync starts",
              "Check now", "Space", "Play / pause", "Back / forward 5 s", "Fullscreen",
              "Esc", "Leave fullscreen", "Done", app.LISTEN_HELP):
        check_in("Settings text", s, shown)
    check_in("Settings device", "Speakers (", shown)
    check("Settings interval field shows the interval", d.interval_field.get(), "30")
    check("dialogs carry no app keys", str(app.root) in d.win.bindtags(), False)
    check("dialog fields carry no app keys", str(app.root) in d.hint_field.entry.bindtags(),
          False)
    check("no Space binding on the dialog", d.win.bind("<space>"), "")
    shot(root, d.win, "03-settings")

    # item 5, second half: a failed switch to the VLC app puts the Radio back
    radios = {r.cget("text"): r for r in descendants(d.win) if isinstance(r, widgets.Radio)}
    builtin, vlc = radios.get("Built-in player"), radios.get("VLC app")
    check("Settings has both player Radios", builtin is not None and vlc is not None)
    if builtin is not None and vlc is not None:
        check("Built-in player drawn chosen", radio_is_on(builtin))
        app.ctl.use_external = lambda mute: "VLC could not start (test)."
        boxes = len(env.boxes)
        vlc.invoke()
        pump(root, 0.3)
        check("a failed switch says why", env.boxes[boxes:],
              [("showerror", "StreamSync", "VLC could not start (test).")])
        check("a failed switch keeps the built-in player", app.player is app.embedded)
        check("player_var reset", app.player_var.get(), "embedded")
        check("the Radio redraws on Built-in player", (radio_is_on(builtin), radio_is_on(vlc)),
              (True, False))

    # a bad interval: the message shows and the dialog stays
    d.interval_field.set("abc")
    d.done_btn.invoke()
    pump(root, 0.3)
    check("a bad interval keeps Settings open", app._settings is d)
    check("a bad interval shows why", bool(d.interval_err.cget("text"))
          and bool(d.interval_err.winfo_ismapped()))
    check("a bad interval never reaches interval_var", app.interval_var.get(), 30)
    # a good one: applied, handed to the engine, and the dialog closes
    d.interval_field.set("45")
    d.done_btn.invoke()
    pump(root, 0.3)
    check("Done applies the interval", app.ctl.auto_interval, 45)
    check("Done applies it to interval_var", app.interval_var.get(), 45)
    check("Done closes Settings", app._settings, None)
    check("Settings window gone", bool(d.win.winfo_exists()), False)
    check("Done handed the interval to the engine", env.auto_calls[-1:], [(False, True, 45)])
    check("the engine's auto mode stayed off in this test", app.ctl.auto_enabled, False)


def watch_party(env, root, app):
    """item 6."""
    app._open_watch_party()
    pump(root, 0.4)                           # the mics thread
    d = app._party
    check("Watch party registered", d is not None)
    if d is None:
        return
    env.title(d.win)
    check("Watch party title", d.win.title(), "StreamSync - Watch party")
    shown = texts(d.win)
    for s in ("Host", "Viewers sync their own copies to you. Only your voice is sent.",
              "Password", "(optional)", "Film position from", "What this computer hears",
              "This app's player", "Your microphone", "Delay hint for viewers",
              "Start hosting", "Join",
              "Follow a host with your own copy of the film. Open the film first.",
              "Session code", "Relay server", "ws://localhost:8765", "Close",
              "Built-in Microphone"):
        check_in("Watch party text", s, shown)
    check("the delay hint starts at 10", d.delay_field.get(), "10")
    check("the host button is live with a film", d.host_btn.cget("state"), "normal")
    check("hosting goes through a free Cloudflare tunnel at first", d.tunnel_var.get(), True)
    shot(root, d.win, "04-watch-party")

    # Not modal: a host plays and pauses in the main window with it open.
    # With the grab it held, the main window ignored every click until
    # Watch party was closed (a PC host's Play, 2026-10-22).
    check("Watch party takes no grab", root.grab_current(), None)
    d.lift()
    check("nor when it is brought back to the front", root.grab_current(), None)
    # beside the main window, not over the film's controls, where there is room
    from ui import theme as _theme
    root.update_idletasks()
    rx, rw = root.winfo_rootx(), root.winfo_width()
    dx, dw = d.win.winfo_rootx(), d.win.winfo_width()
    gap, sw = _theme.px(d.GAP), d.win.winfo_screenwidth()
    if rx + rw + gap + dw <= sw or rx - gap - dw >= 0:
        check("Watch party opens beside the main window", dx >= rx + rw or dx + dw <= rx,
              True)
    else:
        print(f"  (a {sw}-px screen has no room beside the main window: placement not checked)")
    # a film opened while it is open turns Host and Join on
    film = app.ctl.video_path
    app.ctl.video_path = None
    d.refresh()
    check("no film: Host waits for one", d.host_btn.cget("state"), "disabled")
    app.ctl.video_path = film
    app._film_loaded(film)
    pump(root, 0.1)
    check("a film opened with Watch party open turns Host on",
          (d.host_btn.cget("state"), d.join_btn.cget("state")), ("normal", "normal"))

    # the running view: a host whose code arrives a tick after the start
    session = SimpleNamespace(code=None, stop_flag=threading.Event())
    app.ctl.session = session
    app._session_changed()
    pump(root, 0.1)
    check("running view shows while hosting", d.running.winfo_ismapped(), 1)
    check("idle view hides while hosting", d.idle.winfo_ismapped(), 0)
    check("no code yet reads …", d.code_field.get(), "…")
    session.code = "ABCD"
    pump(root, 0.9)                           # one 700-ms tick
    check("the code arrives on the tick", d.code_field.get(), "ABCD")
    shown = texts(d.win)
    for s in ("Hosting", "ABCD", "Leave session"):
        check_in("Watch party running text", s, shown)
    check("the footer dot says a session runs", app._party_live, True)
    shot(root, d.win, "05-watch-party-live")
    # a session that ends by itself: the tick notices
    session.stop_flag.set()
    pump(root, 0.9)
    check("a session that ended shows the idle view again", d.idle.winfo_ismapped(), 1)
    app.ctl.session = None

    # A relay at a wss:// address - a tunnel's - goes to the session as
    # typed and is the relay saved for next time.
    import session as engine
    joined = []

    class Viewer(SimpleNamespace):
        def __init__(self, relay_url, code, *a, **kw):
            super().__init__(code=code, stop_flag=threading.Event())
            joined.append(relay_url)

        def start(self):
            pass

    real_viewer = engine.ViewerSession
    engine.ViewerSession = Viewer
    try:
        tunnel = "wss://example-words-here.trycloudflare.com"
        d.relay_field.entry.delete(0, "end")
        d.relay_field.entry.insert(0, f" {tunnel} ")
        d.join_code.set("ABCD-EFGH")
        d._join()
        pump(root, 0.1)
        check("Join hands the wss:// relay to the session", joined, [tunnel])
        check("the controller keeps it", app.ctl.relay_url, tunnel)
        check("it is saved as relay_url",
              json.loads(env.config.read_text()).get("relay_url"), tunnel)
        check("the field shows it while the session runs",
              (d.relay_var.get().strip(), d.relay_field.entry.cget("state")),
              (tunnel, "readonly"))
        app.ctl.session.stop_flag.set()
        pump(root, 0.9)
        app.ctl.session = None

        # cloudflared prints an https:// address. Pasted as it is, it is
        # taken for the wss:// one it stands for, and shown and saved so;
        # http:// likewise for ws://. From Host as from Join.
        from ui import theme
        hosted = []

        class Host(SimpleNamespace):
            def __init__(self, relay_url, *a, **kw):
                super().__init__(code=None, stop_flag=threading.Event())
                hosted.append(relay_url)

            def start(self):
                pass

            def stop(self):
                self.stop_flag.set()

        def press(button, relay):
            d.relay_field.entry.delete(0, "end")
            d.relay_field.entry.insert(0, relay)
            button()
            pump(root, 0.1)
            if app.ctl.session is not None:
                app.ctl.session.stop_flag.set()
                pump(root, 0.9)
                app.ctl.session = None

        def saved():
            return json.loads(env.config.read_text()).get("relay_url")

        real_host = engine.HostSession
        engine.HostSession = Host
        d.tunnel_var.set(False)          # these are the relay server field's checks
        try:
            for name, button, started, err in (
                    ("Join", d._join, joined, d.join_err),
                    ("Host", d._host, hosted, d.host_err)):
                started.clear()
                press(button, "https://example-words-here.trycloudflare.com")
                check(f"{name} takes an https:// relay as wss://", started, [tunnel])
                check(f"{name}: the field shows the wss:// address", d.relay_var.get(),
                      tunnel)
                check(f"{name}: the wss:// address is saved", saved(), tunnel)

                # Anything else that is no ws:// or wss:// address is
                # refused at once, under the button pressed - where it
                # used to start a session that said, after its retries,
                # only "could not reach the relay".
                started.clear()
                press(button, "localhost:8765")
                check(f"{name}: no session for an address without ws://", started, [])
                check(f"{name}: says why", (err.cget("text"), bool(err.winfo_ismapped())),
                      ("The relay server needs ws:// or wss://", True))
                check(f"{name}: the relay field is marked",
                      d.relay_field.entry.cget("highlightbackground"), theme.ERROR)
                check(f"{name}: the refused address is not saved", saved(), tunnel)
                check(f"{name}: and is left to correct", d.relay_var.get(), "localhost:8765")
                if name == "Join":
                    shot(root, d.win, "05-watch-party-relay-error")

            press(d._join, " http://203.0.113.7:8765 ")
            check("http:// is taken as ws://", joined, ["ws://203.0.113.7:8765"])
            check("ws:// is saved", saved(), "ws://203.0.113.7:8765")
            check("a good address clears the mark",
                  d.relay_field.entry.cget("highlightbackground") == theme.ERROR, False)

            # Hosting through this computer, the default. A missing
            # cloudflared is fetched only once the user says yes, and
            # otherwise the way to get it is shown. The tunnel opens on a
            # worker thread - here, a stand-in for ctl.open_tunnel - and
            # Host reads Cancel meanwhile.
            from ui import dialogs as dlg
            import hosting as tunnels
            ctl = app.ctl
            real_ask = dlg.messagebox.askokcancel
            public = "wss://fake-able-mind-words.trycloudflare.com"
            opened = []

            def open_tunnel(h, exe, download, say):
                # begin_hosting's real Hosting, never started: nothing opens
                opened.append((exe, download))
                say("Downloading cloudflared... 50%")
                h.public_url = public
                return "ws://127.0.0.1:9", public

            ctl.find_cloudflared = lambda: (None, None, False)
            ctl.open_tunnel = open_tunnel
            d.tunnel_var.set(True)
            try:
                dlg.messagebox.askokcancel = lambda *a, **k: False
                d._host()
                pump(root, 0.2)
                check("declined: nothing opens", (opened, ctl.session), ([], None))
                check("declined: the way to get cloudflared, with Cloudflare's page",
                      (bool(d.host_note.winfo_ismapped()), bool(d.host_link.winfo_ismapped()),
                       d.host_note.cget("text").startswith("A Cloudflare tunnel needs")),
                      (True, True, True))
                shot(root, d.win, "05-watch-party-get-cloudflared")

                dlg.messagebox.askokcancel = lambda *a, **k: True
                hosted.clear()
                d._host()
                pump(root, 0.6)
                check("yes: cloudflared is downloaded first", opened, [(None, True)])
                check("the session uses this computer's relay", hosted, ["ws://127.0.0.1:9"])
                check("the running view shows the tunnel's address",
                      (bool(d.running.winfo_ismapped()), d.addr_field.get(),
                       bool(d.addr_note.winfo_ismapped())), (True, public, True))
                check("so does the relay field", d.relay_var.get(), public)
                check("the saved relay is not the tunnel's", saved(), "ws://203.0.113.7:8765")
                check("the tunnel choice is saved",
                      json.loads(env.config.read_text()).get("host_via"), "tunnel")
                ctl.session.code = "ABCD-EFGH"
                d._copy_invite()
                pump(root, 0.1)
                check("Copy invite copies the address and the code", root.clipboard_get(),
                      f"StreamSync watch party: {public} code ABCD-EFGH")
                check("and says so", d.invite_btn.cget("text"), "Copied")
                d.refresh_code()
                shot(root, d.win, "05-watch-party-hosting-here")
                h = ctl.hosting
                app._leave_session()
                pump(root, 0.3)
                check("Leave closes the tunnel", (h.cancelled, ctl.hosting), (True, None))
                check("and the relay field has the saved relay back", d.relay_var.get(),
                      "ws://203.0.113.7:8765")
                # its progress went with it (a live run left "Waiting for the
                # address to go live..." under Start hosting)
                check("no progress note is left after the party",
                      (d.host_note.winfo_manager(), d.host_btn.cget("text")),
                      ("", "Start hosting"))

                def slow_open(h, exe, download, say):
                    deadline = time.monotonic() + 10
                    while not h.cancelled and time.monotonic() < deadline:
                        time.sleep(0.02)
                    raise tunnels.Cancelled()

                ctl.find_cloudflared = lambda: ("/fake/cloudflared", "2026.10.3", False)
                ctl.open_tunnel = slow_open
                d._host()
                check("at once, Host reads Cancel", d.host_btn.cget("text"), "Cancel")
                h = ctl.hosting
                pump(root, 0.2)
                d.host_btn.invoke()
                pump(root, 0.3)
                check("Cancel stops it", (h.cancelled, ctl.hosting), (True, None))
                check("and Host is back", d.host_btn.cget("text"), "Start hosting")
            finally:
                dlg.messagebox.askokcancel = real_ask
                del ctl.find_cloudflared, ctl.open_tunnel

            # an invite pasted as the code: its address goes to the relay field
            joined.clear()
            d.join_code.set(f"StreamSync watch party: {public} code wxyz-2345")
            d._join()
            pump(root, 0.1)
            check("Join takes an invite's address", joined, [public])
            check("and its code", (d.join_code.get(), ctl.session.code),
                  ("WXYZ-2345", "WXYZ-2345"))
            ctl.session.stop_flag.set()
            pump(root, 0.9)
            ctl.session = None
        finally:
            engine.HostSession = real_host
    finally:
        engine.ViewerSession = real_viewer
        app.ctl.session = None
    d.close()
    pump(root, 0.1)
    check("Close unregisters Watch party", app._party, None)


def update_views(env, root, app):
    """item 7: through the queue, as the update flow drives them."""
    from version import __version__
    rel = {"tag_name": "v9.9.9", "html_url": "https://github.com/gBear97/StreamSync/releases",
           "assets": []}

    def offer(can_install):
        app._installed_target = (lambda: "target") if can_install else (lambda: None)
        app.q.put(("update", "v9.9.9", rel))
        pump(root, 0.4)
        return app._update_dialog

    d = offer(True)
    check("an offer opens the Update dialog", d is not None)
    if d is None:
        return
    env.title(d.win)
    check("Update title", d.win.title(), "StreamSync - Update")
    shown = texts(d.win)
    for s in ("StreamSync 9.9.9 is ready", f"You have {__version__}. The update downloads",
              "What's new in 9.9.9", "Later", "Install and restart"):
        check_in("Update offer text", s, shown)
    shot(root, d.win, "06-update")
    d.close()
    pump(root, 0.1)

    d = offer(False)
    if d is not None:
        shown = texts(d.win)
        for s in (f"You have {__version__}. " + app.UPDATE_UNAVAILABLE, "Open download page",
                  "Later"):
            check_in("Update cannot-install text", s, shown)
        check("no Install button when it cannot install",
              any("Install and restart" in t for t in shown), False)
        shot(root, d.win, "06-update-cannot-install")
        d.close()
        pump(root, 0.1)

    d = offer(True)
    if d is not None:
        app.q.put(("update_progress", 37 << 20, 92 << 20, "StreamSync-9.9.9.dmg"))
        pump(root, 0.3)
        shown = texts(d.win)
        for s in ("Updating to 9.9.9", "Downloading StreamSync-9.9.9.dmg - 37 MB", "Cancel"):
            check_in("Update progress text", s, shown)
        shot(root, d.win, "07-update-progress")
        app.q.put(("update_failed", "The download did not match its checksum."))
        pump(root, 0.3)
        shown = texts(d.win)
        for s in ("The update didn't install", "The download did not match its checksum.",
                  "Your installed copy has not been changed.", "Close", "Open download page"):
            check_in("Update failed text", s, shown)
        shot(root, d.win, "07-update-failed")
        d.close()
        pump(root, 0.1)
    del app._installed_target
    check("Update unregistered", app._update_dialog, None)
    check("no browser opened", env.opened, [])


def bar_shot(env, root, app):
    """08-bar. fullscreen is set as a stand-in over a windowed film - on
    the Mac's adapter too, which otherwise asks the window itself whether
    it really is fullscreen before the bar may show."""
    vw = app.video_win
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    vw.geometry(f"{min(1280, sw - 80)}x{min(720, sh - 80)}+40+40")
    app._show_video_window()
    pump(root, 0.3)
    app.fullscreen = True
    app._bar_allowed = lambda: True
    app._fs_moved = time.monotonic()         # as a mouse move over the film does
    try:
        app._show_fs_bar()
        pump(root, 0.2)
        check("the bar shows", app._fs_up, True)
        shot(root, vw, "08-bar", on_top=False)
    finally:
        app._hide_fs_bar()
        del app._bar_allowed
        app.fullscreen = False
        pump(root, 0.1)


def config_written(env, root, app):
    """item 11, second half: the next save writes stream_window only."""
    app._save_config()
    cfg = json.loads(env.config.read_text())
    check("saved stream_window", cfg.get("stream_window"), "Old pick")
    check("the old key is gone", app.OLD_STREAM_KEY in cfg, False)
    check("saved check_updates", cfg.get("check_updates"), False)


def titles(env, root, app):
    if getattr(app, "_osd_win", None) is not None:
        env.title(app._osd_win)
    env.title(root)
    bad = {w: t for w, t in env.titles.items() if not t.startswith("StreamSync")}
    check("every window's title starts with StreamSync", bad, {})


def poll_and_close(env, root, app):
    """item 14."""
    from ui import shell
    n = {"boom": 0}

    def boom():
        n["boom"] += 1
        if n["boom"] == 1:
            raise RuntimeError("boom (test)")

    app.HANDLERS = dict(shell.Shell.HANDLERS, boom="_test_boom", closer="_test_closer")
    app._test_boom = boom
    errors = len(env.errors)
    app.q.put(("boom",))
    app.q.put(("status", "after the boom"))
    pump(root, 0.4)
    caught = env.errors[errors:]
    check("the bad event reached Tk's hook once",
          len(caught) == 1 and "boom (test)" in caught[0], True)
    del env.errors[errors:]
    check("the event behind it ran on the next pass", app.status_lbl.cget("text"),
          "after the boom")

    applied = []
    set_status = app._set_status
    app._set_status = lambda text: (applied.append(text), set_status(text))
    app._test_closer = app._on_close
    app.q.put(("closer",))
    app.q.put(("status", "too late"))
    end = time.monotonic() + 0.5
    try:
        while time.monotonic() < end:
            root.update()
            time.sleep(0.01)
    except env.tk.TclError:
        pass                                  # destroyed: what the closer does
    check("a handler that closed the app ends the drain", applied, [])
    check("the status behind it stays queued", list(app.q.queue), [("status", "too late")])
    check("the close finished", (app._closing, app._exit_hard), (True, True))
    drop_afters(root)


def failed_restore(env):
    """item 12, on an app of its own."""
    env.write_config({"video_path": str(env.film), "check_updates": False})
    FakePlayer.fail_load = "this film cannot be opened (test)"
    try:
        root, app = env.make()
    finally:
        FakePlayer.fail_load = None
    try:
        pump(root, 0.3)
        check("a failed restore leaves no film", app.ctl.video_path, None)
        check("a failed restore withdraws the film window", app.video_win.state(), "withdrawn")
        check("a failed restore shows FirstRun", (app.first_frame.winfo_ismapped(),
                                                  app.main_frame.winfo_ismapped()), (1, 0))
        check("a failed restore says why", app.status_lbl.cget("text"),
              f"Could not reopen {FILM_NAME}: this film cannot be opened (test)")
        check("the reason is on screen", bool(app.status_lbl.winfo_ismapped()))
    finally:
        app._on_close()
        drop_afters(root)


def with_display(env):
    import tkinter as tk
    if sys.platform == "win32":
        env.platform.Adapter.prepare_process()       # as main() does, before any window
    try:
        probe = tk.Tk()
    except tk.TclError as e:
        print(f"SKIPPED: no display ({e})")
        return False
    probe.destroy()

    env.write_config({"check_updates": False, env.platform.Adapter.OLD_STREAM_KEY: "Old pick"})
    root, app = env.make()
    try:
        phase("config read", config_read, env, root, app)
        phase("FirstRun", first_run, env, root, app)
        phase("Main, synced and playing", main_synced, env, root, app)
        phase("keys, film menu and bar", keys_and_film_window, env, root, app)
        phase("the text-field guard", typing_guard, env, root, app)
        phase("auto off", auto_off, env, root, app)
        phase("Settings", settings, env, root, app)
        phase("Watch party", watch_party, env, root, app)
        phase("Update", update_views, env, root, app)
        phase("the fullscreen bar", bar_shot, env, root, app)
        phase("config written", config_written, env, root, app)
        phase("window titles", titles, env, root, app)
        check("the stream swap stand-in saw the pause", env.swaps[:1], [True])
    finally:
        if not app._closing:
            phase("the event loop and close", poll_and_close, env, root, app)
    if not app._closing:
        try:
            app._on_close()
        except Exception:
            pass
        drop_afters(root)
    phase("a film that cannot be reopened", failed_restore, env)
    check("no unexpected message boxes",
          [b for b in env.boxes if "VLC could not start" not in str(b[2])], [])
    check("Tk callback and thread errors", env.errors, [])
    return True


def main():
    phase("imports", imports)
    phase("adapters", adapters_complete)
    phase("contract", contract_holds)
    phase("selftest config", selftest_config)
    env = phase("setup", Env)
    ran = False
    if env is not None:
        try:
            ran = phase("screens", with_display, env)
        finally:
            shutil.rmtree(env.work, ignore_errors=True)
    if fails:
        sys.exit("\n".join(["UI TEST FAILED"] + fails))
    if ran:
        print("UI TEST PASSED")
    else:
        print("UI TEST PASSED (no display: the import, adapter and contract checks only)")


if __name__ == "__main__":
    main()
