"""Global keybind capture/arm-disarm.

Dispatched onto the caller's UI thread via `dispatch(fn, *args)`, same
convention as firewall.py and process_scan.py.
"""
from __future__ import annotations

import threading
from functools import lru_cache
from typing import Callable

from pynput import keyboard, mouse

MOUSE_LABELS = {
    mouse.Button.left: "Mouse-Left", mouse.Button.right: "Mouse-Right",
    mouse.Button.middle: "Mouse-Middle", mouse.Button.x1: "Mouse-X1", mouse.Button.x2: "Mouse-X2",
}
DISPLAY_NAMES = {
    "ctrl_l": "L-Ctrl", "ctrl_r": "R-Ctrl", "shift_l": "L-Shift", "shift_r": "R-Shift", "shift": "Shift",
    "alt_l": "L-Alt", "alt_r": "R-Alt", "Mouse-Left": "Mouse L", "Mouse-Right": "Mouse R",
    "Mouse-Middle": "Mouse M", "Mouse-X1": "Mouse 4", "Mouse-X2": "Mouse 5",
}


def key_label(k):
    if isinstance(k, str): return k
    try: return k.char or str(k).replace("Key.", "")
    except Exception: return str(k).replace("Key.", "")


@lru_cache(maxsize=256)
def disp(label):
    return DISPLAY_NAMES.get(label, label.upper() if len(label) == 1 else label.title())


class KeybindManager:
    """Owns the pynput listeners + held-keys set. Keyboard and mouse each run on
    their own pynput thread, so _dispatch() can be entered concurrently from both —
    the lock guards `_active` and the capture flag. on_arm/on_disarm fire on the
    empty<->non-empty transitions of the held-keys set, marshaled onto the UI
    thread via `dispatch`."""

    def __init__(self, dispatch: Callable, keybinds, on_arm, on_disarm, on_capture_done):
        self._dispatch = dispatch; self._keybinds = list(keybinds)
        self._on_arm, self._on_disarm, self._on_capture_done = on_arm, on_disarm, on_capture_done
        self._active = set(); self._capturing = False
        self._lock = threading.Lock(); self._kb = self._ms = None

    @property
    def keybinds(self): return list(self._keybinds)

    def start(self):
        self._kb = keyboard.Listener(
            on_press=lambda k: self._dispatch_key(key_label(k), True),
            on_release=lambda k: self._dispatch_key(key_label(k), False))
        self._kb.start()
        self._ms = mouse.Listener(on_click=lambda x, y, b, p: self._dispatch_key(MOUSE_LABELS.get(b, str(b)), p))
        self._ms.start()

    def stop(self):
        try: self._kb.stop(); self._ms.stop()
        except Exception: pass

    def begin_capture(self):
        with self._lock:
            if self._capturing: return False
            self._capturing = True
        return True

    def remove(self, label):
        with self._lock:
            if label in self._keybinds: self._keybinds.remove(label)
            self._active.discard(label)
            now_empty = not self._active
        return now_empty

    def _dispatch_key(self, label, pressed):
        if pressed and self._capturing:
            with self._lock:
                if not self._capturing: return
                self._capturing = False
                if label and label not in self._keybinds: self._keybinds.append(label)
            self._dispatch(self._on_capture_done)
            return
        if label not in self._keybinds: return
        fire_arm = fire_disarm = False
        with self._lock:
            if pressed:
                fire_arm = not self._active; self._active.add(label)
            else:
                self._active.discard(label); fire_disarm = not self._active
        if fire_arm: self._dispatch(self._on_arm)
        if fire_disarm: self._dispatch(self._on_disarm)
