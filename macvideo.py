"""A video surface libvlc can draw into, inside a Tk window, on macOS.

libvlc 3.x on macOS ships no standalone video-window provider: a player
with no drawable reaches State.Playing and its clock advances, but
has_vout() stays 0 and nothing is ever displayed. It needs an NSView
handed to media_player.set_nsobject().

Tk on macOS has no per-widget NSView - every widget in a toplevel shares
that toplevel's TKContentView - so handing libvlc the widget's view would
let video cover the whole window, chrome included. Instead we create our
own NSView, add it as a subview, and keep its frame glued to a Tk
widget's rectangle. Everything here is ctypes against libraries already
in the process (Tk itself, libobjc); pyobjc is not required.
"""

import ctypes
import ctypes.util
import sys

IS_MAC = sys.platform == "darwin"


class VideoSurfaceError(RuntimeError):
    pass


def _find_tk_library():
    """Path of the Tk dylib this interpreter already has loaded.

    Asking dyld what is in the process beats guessing paths: it works for
    Homebrew, python.org and framework builds, Intel and Apple silicon,
    Tk 8.6 and Tk 9.
    """
    libc = ctypes.CDLL(None)
    libc._dyld_image_count.restype = ctypes.c_uint32
    libc._dyld_get_image_name.restype = ctypes.c_char_p
    libc._dyld_get_image_name.argtypes = [ctypes.c_uint32]
    for i in range(libc._dyld_image_count()):
        name = libc._dyld_get_image_name(i).decode("utf-8", "replace")
        base = name.rsplit("/", 1)[-1].lower()
        if base.startswith(("libtk", "libtcl9tk")) or base == "tk":
            return name
    raise VideoSurfaceError(
        "Tk's library is not loaded - import tkinter and create the window "
        "before building a video surface.")


class _ObjC:
    """The few Objective-C runtime calls this module needs."""

    def __init__(self):
        self.objc = ctypes.CDLL(ctypes.util.find_library("objc"))
        self.objc.objc_getClass.restype = ctypes.c_void_p
        self.objc.objc_getClass.argtypes = [ctypes.c_char_p]
        self.objc.sel_registerName.restype = ctypes.c_void_p
        self.objc.sel_registerName.argtypes = [ctypes.c_char_p]
        self._msg_send = ctypes.cast(self.objc.objc_msgSend, ctypes.c_void_p).value
        self._cache = {}

    def _fn(self, restype, *argtypes):
        key = (restype, argtypes)
        fn = self._cache.get(key)
        if fn is None:
            proto = ctypes.CFUNCTYPE(restype, ctypes.c_void_p,
                                     ctypes.c_void_p, *argtypes)
            fn = proto(self._msg_send)
            self._cache[key] = fn
        return fn

    def sel(self, name):
        return self.objc.sel_registerName(name.encode())

    def cls(self, name):
        return self.objc.objc_getClass(name.encode())

    def send_id(self, obj, name):
        return self._fn(ctypes.c_void_p)(ctypes.c_void_p(obj), self.sel(name))

    def send_void(self, obj, name):
        self._fn(None)(ctypes.c_void_p(obj), self.sel(name))

    def send_bool(self, obj, name):
        return self._fn(ctypes.c_bool)(ctypes.c_void_p(obj), self.sel(name))

    def send_rect(self, obj, name):
        return self._fn(CGRect)(ctypes.c_void_p(obj), self.sel(name))

    def send_id_rect(self, obj, name, r):
        return self._fn(ctypes.c_void_p, CGRect)(
            ctypes.c_void_p(obj), self.sel(name), r)

    def send_void_rect(self, obj, name, r):
        self._fn(None, CGRect)(ctypes.c_void_p(obj), self.sel(name), r)

    def send_void_id(self, obj, name, arg):
        self._fn(None, ctypes.c_void_p)(
            ctypes.c_void_p(obj), self.sel(name), ctypes.c_void_p(arg))

    def send_void_bool(self, obj, name, val):
        self._fn(None, ctypes.c_bool)(
            ctypes.c_void_p(obj), self.sel(name), bool(val))


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


def _rect(x, y, w, h):
    return CGRect(CGPoint(float(x), float(y)), CGSize(float(w), float(h)))


_objc = None
_tk_get_root_control = None


def _load():
    global _objc, _tk_get_root_control
    if _tk_get_root_control is not None:
        return
    if not IS_MAC:
        raise VideoSurfaceError("macvideo is macOS-only.")
    tk_lib = ctypes.CDLL(_find_tk_library(), mode=ctypes.RTLD_GLOBAL)
    try:
        # Tk 9 renamed this in the headers only: tkPlatDecls.h defines
        # Tk_MacOSXGetNSViewForDrawable as an alias for the symbol that has
        # always been exported, TkMacOSXGetRootControl. Tk 8.6 exports the
        # same name, so one lookup covers both.
        fn = tk_lib.TkMacOSXGetRootControl
    except AttributeError as e:
        raise VideoSurfaceError(
            "This Tk build exposes no way to reach its NSView "
            f"(TkMacOSXGetRootControl missing): {e}")
    fn.restype = ctypes.c_void_p
    fn.argtypes = [ctypes.c_void_p]
    _tk_get_root_control = fn
    _objc = _ObjC()


def content_view(widget):
    """The TKContentView backing `widget`'s toplevel."""
    _load()
    view = _tk_get_root_control(ctypes.c_void_p(widget.winfo_id()))
    if not view:
        raise VideoSurfaceError(
            "Tk returned no NSView for that widget - is the window mapped?")
    return view


class VideoSurface:
    """An NSView pinned to a Tk widget's rectangle, for libvlc to render into.

    Create it only after the widget is mapped (`update_idletasks()` first).
    Pass `.view` to media_player.set_nsobject().
    """

    def __init__(self, widget):
        _load()
        self.widget = widget
        self.toplevel = widget.winfo_toplevel()
        self.content = content_view(widget)
        # TKContentView is not flipped, so Tk's top-left origin has to be
        # converted to Cocoa's bottom-left. Ask rather than assume.
        self.flipped = _objc.send_bool(self.content, "isFlipped")

        view = _objc.send_id_rect(
            _objc.send_id(_objc.cls("NSView"), "alloc"),
            "initWithFrame:", _rect(0, 0, 16, 16))
        if not view:
            raise VideoSurfaceError("Could not create an NSView for video.")
        self.view = view
        _objc.send_void_bool(view, "setWantsLayer:", True)
        _objc.send_void_id(self.content, "addSubview:", view)

        self._binds = [
            (widget, "<Configure>", widget.bind("<Configure>",
                                                lambda e: self.sync(), add="+")),
            (widget, "<Map>", widget.bind("<Map>", lambda e: self.sync(), add="+")),
            (widget, "<Unmap>", widget.bind("<Unmap>", lambda e: self.sync(), add="+")),
            (self.toplevel, "<Configure>", self.toplevel.bind(
                "<Configure>", lambda e: self.sync(), add="+")),
        ]
        self.sync()

    def sync(self):
        """Match the NSView to the Tk widget's current position and size."""
        if not self.view:
            return
        try:
            mapped = bool(self.widget.winfo_ismapped())
            dx = self.widget.winfo_rootx() - self.toplevel.winfo_rootx()
            dy = self.widget.winfo_rooty() - self.toplevel.winfo_rooty()
            w, h = self.widget.winfo_width(), self.widget.winfo_height()
        except Exception:
            return  # widget is being destroyed
        # A hidden Tk widget must not leave video floating over the layout.
        _objc.send_void_bool(self.view, "setHidden:", not mapped)
        if not mapped:
            return
        if self.flipped:
            r = _rect(dx, dy, w, h)
        else:
            bounds = _objc.send_rect(self.content, "bounds")
            r = _rect(dx, bounds.size.height - dy - h, w, h)
        _objc.send_void_rect(self.view, "setFrame:", r)

    def destroy(self):
        for widget, seq, ident in getattr(self, "_binds", []):
            try:
                widget.unbind(seq, ident)
            except Exception:
                pass
        self._binds = []
        if self.view:
            _objc.send_void(self.view, "removeFromSuperview")
            _objc.send_void(self.view, "release")
            self.view = None
