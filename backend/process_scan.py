"""Running-process discovery."""
from __future__ import annotations

import threading
from typing import Callable

try:
    import psutil
except ImportError:
    psutil = None


def enumerate_running_apps():
    """Running processes with a resolvable exe path -> {exe_path: display_name}.
    Pure psutil, no COM involved — callers should run this on its own throwaway
    thread rather than the FirewallService queue, so a slow scan can never
    delay a pending block_batch behind it."""
    result, name_counts, seen = {}, {}, set()
    if psutil is None: return result
    for p in psutil.process_iter(["name", "exe"]):
        try:
            exe, name = p.info.get("exe"), p.info.get("name")
            if not exe or not name or exe in seen: continue
            seen.add(exe)
            name_counts[name] = name_counts.get(name, 0) + 1
            result[exe] = f"[{name_counts[name]}] {name}" if name_counts[name] > 1 else name
        except Exception:
            pass
    return dict(sorted(result.items(), key=lambda kv: kv[1].lower()))


class ProcessScanner:
    """Long-lived worker for running-process discovery: one persistent thread
    woken by an Event, rather than a fresh threading.Thread spawned on every
    refresh. Repeated requests (e.g. app-add, search refresh, manual refresh
    firing close together) coalesce into a single scan instead of piling up
    redundant psutil sweeps concurrently."""

    def __init__(self, dispatch: Callable, on_result: Callable):
        self._dispatch = dispatch; self._on_result = on_result
        self._wake = threading.Event(); self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def request_scan(self):
        self._wake.set()

    def stop(self):
        self._stop.set(); self._wake.set()

    def _run(self):
        while True:
            self._wake.wait(); self._wake.clear()
            if self._stop.is_set(): return
            app_map = enumerate_running_apps()
            if not self._stop.is_set(): self._dispatch(self._on_result, app_map)
