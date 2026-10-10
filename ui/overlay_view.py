"""On-screen network-state overlay.

The overlay's colors are a user-configurable feature (pick what "connected"
vs "disconnected" looks like), not app chrome skinning, so they're kept —
everything else in this UI layer uses theme defaults.
"""
from __future__ import annotations

import ctypes
import tkinter as tk

from backend.settings import DEFAULT_OVERLAY_COLORS, DEFAULT_OVERLAY_OFFSET, DEFAULT_OVERLAY_POSITION, \
    DEFAULT_OVERLAY_SIZE, OVERLAY_OFFSET_MIN, OVERLAY_OFFSET_MAX, OVERLAY_POSITIONS, OVERLAY_SIZE_MIN, \
    OVERLAY_SIZE_MAX

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WDA_EXCLUDEFROMCAPTURE = 0x00000011
LWA_COLORKEY = 0x00000001

try:
    _user32 = ctypes.windll.user32
    _user32.GetWindowLongPtrW.restype = ctypes.c_void_p
    _user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.SetWindowLongPtrW.restype = ctypes.c_void_p
    _user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    _user32.SetWindowDisplayAffinity.restype = ctypes.c_bool
    _user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _user32.SetLayeredWindowAttributes.restype = ctypes.c_bool
    _user32.SetLayeredWindowAttributes.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_ubyte, ctypes.c_uint32
    ]
    HAVE_WIN32 = True
except AttributeError:
    _user32 = None
    HAVE_WIN32 = False


class OverlayView:
    """Persistent colored square showing network state, plus an optional
    reconnect countdown label. Uses Windows click-through and capture-
    exclusion when available."""

    def __init__(self, root):
        self._root = root
        self._win = None
        self._square = None
        self._timer = None
        self._position = DEFAULT_OVERLAY_POSITION
        self._offset_x = DEFAULT_OVERLAY_OFFSET
        self._offset_y = DEFAULT_OVERLAY_OFFSET
        self._size = DEFAULT_OVERLAY_SIZE
        self._colors = dict(DEFAULT_OVERLAY_COLORS)
        self._state = "connected"
        self._countdown_text = ""
        self._blink_on = False
        self._show_connected = True
        self._show_disconnected = True
        self._blink_enabled = True
        self._countdown_enabled = True

    def set_position(self, position):
        self._position = position if position in OVERLAY_POSITIONS else DEFAULT_OVERLAY_POSITION
        self._reposition()

    def set_size(self, size):
        self._size = max(OVERLAY_SIZE_MIN, min(OVERLAY_SIZE_MAX, int(size)))
        if self._win is not None:
            # The Frame's width/height are fixed at creation time — rebuild
            # it in place rather than trying to resize the existing widget.
            self.hide()
            self.show()

    def set_offset(self, offset_x, offset_y):
        self._offset_x = max(OVERLAY_OFFSET_MIN, min(OVERLAY_OFFSET_MAX, int(offset_x)))
        self._offset_y = max(OVERLAY_OFFSET_MIN, min(OVERLAY_OFFSET_MAX, int(offset_y)))
        self._reposition()

    def set_state(self, state):
        self._state = state
        if state == "connected":
            self._countdown_text = ""
            self._blink_on = False
        self._refresh_widgets()

    def set_countdown(self, text, blink_on=False):
        self._countdown_text = text
        self._blink_on = bool(blink_on)
        self._refresh_widgets()

    def set_color(self, state, color):
        self._colors[state] = color
        self._refresh_widgets()

    def set_option(self, name, enabled):
        attr = {
            "connected": "_show_connected",
            "disconnected": "_show_disconnected",
            "blink": "_blink_enabled",
            "countdown": "_countdown_enabled",
        }.get(name)
        if attr is None:
            return
        setattr(self, attr, bool(enabled))
        self._refresh_widgets()

    def show(self):
        if self._win is not None:
            return
        win = tk.Toplevel(self._root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg="black")
        try:
            win.attributes("-transparentcolor", "black")  # Windows-only Tk feature
        except tk.TclError:
            pass
        self._square = tk.Frame(
            win, bg=self._colors.get(self._state, DEFAULT_OVERLAY_COLORS["connected"]),
            width=self._size, height=self._size,
        )
        self._square.pack_propagate(False)
        self._square.pack(side="left")
        self._timer = tk.Label(
            win, text="", bg="black", fg=self._colors["disconnected"],
            font=("Consolas", max(9, self._size - 2), "bold"), padx=4, pady=0,
            width=5, anchor="w",
        )
        # Always packed, even with empty text: its fixed-width slot is reserved
        # whether or not a countdown is showing, so the window's total size —
        # and therefore the square's position — never changes when the
        # countdown appears/disappears. Otherwise every anchor except
        # top-left (the one position whose formula doesn't depend on content
        # size) visibly shifts the square when the label is added or removed.
        # Default (center) vertical packing is safe here specifically because
        # the row's height is now constant too (blank vs. filled text is still
        # one line of the same font) — centered alignment lines the countdown
        # up with the square's middle instead of its top edge.
        self._timer.pack(side="left")
        self._win = win
        self._refresh_widgets()
        self._reposition()
        self._apply_click_through()
        self._apply_capture_exclusion()

    def hide(self):
        if self._win is not None:
            self._win.destroy()
            self._win = None
            self._square = None
            self._timer = None

    def _refresh_widgets(self):
        if self._square is None:
            return
        effective_blink = self._blink_enabled and self._blink_on
        color_state = "reconnecting" if effective_blink else self._state
        self._square.config(bg=self._colors.get(color_state, DEFAULT_OVERLAY_COLORS["connected"]))
        square_visible = self._show_connected if self._state == "connected" else self._show_disconnected
        if square_visible and not self._square.winfo_manager():
            self._square.pack(side="left", before=self._timer)
        elif not square_visible and self._square.winfo_manager():
            self._square.pack_forget()
        # The label itself is never pack_forget'ed — only its text changes —
        # so its reserved width stays constant (see the comment in show()).
        show_countdown = self._countdown_enabled and bool(self._countdown_text)
        self._timer.config(
            text=self._countdown_text if show_countdown else "",
            fg=self._colors.get(color_state, DEFAULT_OVERLAY_COLORS["disconnected"]),
        )
        if not square_visible and not show_countdown:
            self._win.withdraw()
            return
        self._win.deiconify()
        self._reposition()

    def _reposition(self):
        if self._win is None:
            return
        self._win.update_idletasks()
        w = self._win.winfo_reqwidth()
        h = self._win.winfo_reqheight()
        sw = self._win.winfo_screenwidth()
        sh = self._win.winfo_screenheight()
        mx, my = self._offset_x, self._offset_y
        x, y = {
            "Top-left": (mx, -my),
            "Top-middle": ((sw - w) // 2 + mx, -my),
            "Top-right": (sw - w + mx, -my),
            "Left-middle": (mx, (sh - h) // 2 - my),
            "Center": ((sw - w) // 2 + mx, (sh - h) // 2 - my),
            "Right-middle": (sw - w + mx, (sh - h) // 2 - my),
            "Bottom-left": (mx, sh - h - my),
            "Bottom-middle": ((sw - w) // 2 + mx, sh - h - my),
            "Bottom-right": (sw - w + mx, sh - h - my),
        }.get(self._position, (sw - w + mx, -my))
        self._win.geometry(f"{w}x{h}{x:+d}{y:+d}")

    def _apply_click_through(self):
        if not HAVE_WIN32 or self._win is None:
            return
        hwnd = self._win.winfo_id()
        style = int(_user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) or 0)
        style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
        _user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
        _user32.SetLayeredWindowAttributes(hwnd, 0x000000, 0, LWA_COLORKEY)

    def _apply_capture_exclusion(self):
        if not HAVE_WIN32 or self._win is None:
            return
        _user32.SetWindowDisplayAffinity(self._win.winfo_id(), WDA_EXCLUDEFROMCAPTURE)
