"""Shared reconnect-delay timer."""
from __future__ import annotations

import threading
from typing import Callable, Optional

from .firewall import boost_current_thread


class ReconnectScheduler:
    """One shared timer for the whole app (not one per configured app). schedule()
    replaces any pending reconnect; cancel() drops one outright."""

    def __init__(self, get_delay_ms: Callable[[], int], on_fire: Callable[[], None]):
        self._get_delay = get_delay_ms; self._on_fire = on_fire
        self._lock = threading.Lock(); self._timer: Optional[threading.Timer] = None

    def schedule(self):
        with self._lock:
            if self._timer: self._timer.cancel()
            delay = self._get_delay()
            if delay <= 0:
                self._timer = None; immediate = True
            else:
                t = threading.Timer(delay / 1000.0, self._fire); t.daemon = True
                self._timer = t; immediate = False
        if immediate: self._on_fire()
        else: t.start()

    def _fire(self):
        with self._lock:
            if self._timer is None: return  # cancelled between firing and lock acquisition
            self._timer = None
        boost_current_thread()
        self._on_fire()

    def cancel(self):
        with self._lock:
            if self._timer: self._timer.cancel(); self._timer = None
