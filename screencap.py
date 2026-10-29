"""Grabs of an area of the screen, many a second (Windows).

For picture_timing: the stream's window and the film's, as the eye sees
them. GDI's BitBlt from the screen waits for the compositor's next frame,
so each grab is one whole composed frame - every window in it from the same
moment - and grabs come at the monitor's refresh rate (7 ms apart at
144 Hz). Each is dated by perf_counter as its copy returns.

Only grey is kept, shrunk by a whole factor: cut detection and frame
matching need no more, and 20 s of grabs then fit in a few tens of MB.
"""

import ctypes
import time
from ctypes import wintypes

import numpy as np

SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000         # layered windows too, as the eye sees them
DIB_RGB_COLORS = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _proto(name, restype, *argtypes, dll):
    # typed privately, as windowctl does: windll.user32 is one object for
    # the whole process
    return ctypes.WINFUNCTYPE(restype, *argtypes)((name, dll))


_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32
_GetDC = _proto("GetDC", wintypes.HDC, wintypes.HWND, dll=_user32)
_ReleaseDC = _proto("ReleaseDC", ctypes.c_int, wintypes.HWND, wintypes.HDC, dll=_user32)
_CreateCompatibleDC = _proto("CreateCompatibleDC", wintypes.HDC, wintypes.HDC, dll=_gdi32)
_DeleteDC = _proto("DeleteDC", wintypes.BOOL, wintypes.HDC, dll=_gdi32)
_CreateDIBSection = _proto("CreateDIBSection", wintypes.HBITMAP, wintypes.HDC,
                           ctypes.POINTER(BITMAPINFO), wintypes.UINT,
                           ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE,
                           wintypes.DWORD, dll=_gdi32)
_SelectObject = _proto("SelectObject", wintypes.HGDIOBJ, wintypes.HDC, wintypes.HGDIOBJ,
                       dll=_gdi32)
_DeleteObject = _proto("DeleteObject", wintypes.BOOL, wintypes.HGDIOBJ, dll=_gdi32)
_BitBlt = _proto("BitBlt", wintypes.BOOL, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                 ctypes.c_int, ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                 wintypes.DWORD, dll=_gdi32)
_GdiFlush = _proto("GdiFlush", wintypes.BOOL, dll=_gdi32)


def grab(rect, seconds, width=160, stop=None):
    """Grab rect = (x, y, w, h), screen pixels, for `seconds` or until
    stop() is true: (times, images).

    times are perf_counter as each copy returned; images are uint8 [n, h',
    w'], the green channel shrunk by the whole factor that brings w nearest
    `width` (green carries most of the brightness, and matching needs no
    more)."""
    x, y, w, h = (int(v) for v in rect)
    f = max(1, int(round(w / float(width))))
    cw, ch = (w // f) * f, (h // f) * f
    if cw < f or ch < f:
        raise ValueError("There is nothing to grab there.")
    screen = _GetDC(None)
    mem = _CreateCompatibleDC(screen)
    bmi = BITMAPINFO()
    head = bmi.bmiHeader
    head.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    head.biWidth, head.biHeight = cw, -ch       # top-down rows
    head.biPlanes, head.biBitCount = 1, 32
    bits = ctypes.c_void_p()
    dib = _CreateDIBSection(mem, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits),
                            None, 0)
    if not dib or not bits.value:
        _DeleteDC(mem)
        _ReleaseDC(None, screen)
        raise OSError("Could not make room to grab the screen.")
    old = _SelectObject(mem, dib)
    try:
        view = np.ctypeslib.as_array(ctypes.cast(bits, ctypes.POINTER(ctypes.c_uint8)),
                                     shape=(ch, cw, 4))
        green = view[:, :, 1].reshape(ch // f, f, cw // f, f)
        times, images = [], []
        end = time.perf_counter() + seconds
        while time.perf_counter() < end and not (stop and stop()):
            if not _BitBlt(mem, 0, 0, cw, ch, screen, x, y, SRCCOPY | CAPTUREBLT):
                raise OSError("The screen could not be read.")
            _GdiFlush()
            times.append(time.perf_counter())
            images.append((green.sum(axis=(1, 3), dtype=np.uint32) // (f * f))
                          .astype(np.uint8))
        if not images:
            raise OSError("The screen could not be read.")
        return np.array(times), np.array(images)
    finally:
        _SelectObject(mem, old)
        _DeleteObject(dib)
        _DeleteDC(mem)
        _ReleaseDC(None, screen)
