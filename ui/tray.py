"""System tray icon — double-click (or the default menu item) opens the
panel, the only other menu item is Exit. The icon color updates live: a
direct `.icon =` / `.title =` set, safe to call from any thread (pystray
queues the shell update internally).
"""
from __future__ import annotations

import threading

import pystray

from .tray_icon_image import DEFAULT_COLOR, build_icon

_WM_LBUTTONDBLCLK = 0x0203
_START_TIMEOUT_SECONDS = 3.0


def _icon_class(on_open):
    """Build a Win32 icon that opens the panel on double-click, falling back
    to stock pystray (menu-only, no double-click) if the Win32 backend isn't
    available."""
    try:
        from pystray._util import win32 as win32util
        from pystray._win32 import Icon as Win32Icon
    except ImportError:
        return pystray.Icon

    class DoubleClickIcon(Win32Icon):
        def _on_notify(self, wparam, lparam):
            if lparam == win32util.WM_LBUTTONUP:
                return  # swallow the single-click-up that precedes a double-click
            if lparam == _WM_LBUTTONDBLCLK:
                on_open()
                return
            return super()._on_notify(wparam, lparam)

    return DoubleClickIcon


class TrayIcon:
    def __init__(self, on_open, on_exit, *, name: str = "pushtolag"):
        self._on_open, self._on_exit = on_open, on_exit
        self._color = DEFAULT_COLOR
        self._icon = _icon_class(on_open)(
            name, build_icon(color=self._color), "PushToLag",
            menu=pystray.Menu(
                pystray.MenuItem("Open", lambda _icon, _item: on_open(), default=True),
                pystray.MenuItem("Exit", lambda _icon, _item: self._quit()),
            ),
        )
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def set_color(self, color: str, tooltip: str | None = None):
        if color == self._color and tooltip is None:
            return
        self._color = color
        self._icon.icon = build_icon(color=color)
        if tooltip is not None:
            self._icon.title = tooltip

    def _quit(self):
        self._icon.stop()
        self._on_exit()

    def _run(self):
        self._icon.run(setup=lambda icon: (setattr(icon, "visible", True), self._ready.set()))

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="Tray-Icon")
        self._thread.start()
        self._ready.wait(_START_TIMEOUT_SECONDS)

    def stop(self):
        try: self._icon.stop()
        except Exception: pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        self._thread = None
