"""Arm/disarm/reconnect/countdown state machine + settings persistence.

No dialogs or UI calls live here. UI concerns become either a return value
(set_app -> bool) or a call on `listener` (an AppControllerListener) — e.g. a
confirmation dialog is the caller's job *before* calling clear_all_rules(),
not this module's job.

AppController never imports tkinter; it depends only on the `dispatch`
callable (marshal a callback onto whatever UI thread exists) and on
AppControllerListener, an abstract observer the UI subclasses.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional

from .app_state import AppEntry, AppState, STATUS_CONNECTED, STATUS_DISCONNECTED, STATUS_INACTIVE
from .firewall import FirewallService, boost_current_thread
from .keybind import KeybindManager
from .lifecycle import RESTART_ENV_VAR, restart_self, was_restarted
from .process_scan import ProcessScanner
from .reconnect import ReconnectScheduler
from .settings import DEFAULT_OVERLAY_COLORS, DEFAULT_OVERLAY_OFFSET, DEFAULT_OVERLAY_POSITION, \
    DEFAULT_OVERLAY_SIZE, Settings

AUTO_REFRESH_MS = 5000  # how often to re-scan running processes, to keep status/overlay honest
COUNTDOWN_TICK_MS = 25


class AppControllerListener:
    """No-op base class — a UI subclasses this and overrides what it needs."""

    def on_entry_added(self, entry: AppEntry): pass
    def on_entry_removed(self, entry: AppEntry): pass
    def on_entries_reordered(self): pass
    def on_entry_status(self, entry: AppEntry): pass
    def on_app_conflict(self, entry: AppEntry): pass  # UI should warn: path already configured elsewhere
    def on_apps_refreshed(self): pass  # live/known app maps changed; UI should re-pull filtered_apps()
    def on_disconnect_timing(self, text: str): pass
    def on_reconnect_timing(self, text: str): pass
    def on_keybinds_changed(self, keybinds: list[str]): pass
    def on_capture_state(self, active: bool): pass
    def on_overlay_settings_changed(self): pass  # position/offset/size/colors/show-flags changed
    def on_overlay_visibility(self, visible: bool): pass
    def on_overlay_state(self, state: str): pass  # "connected" / "disconnected"
    def on_overlay_countdown(self, text: str, blink_on: bool): pass
    def on_fatal_error(self, message: str): pass  # firewall COM init failed even after restart
    def on_rules_cleared(self, removed: int, failed: int): pass


class AppController:
    def __init__(self, dispatch: Callable, listener: Optional[AppControllerListener] = None):
        self._dispatch = dispatch
        self._listener = listener or AppControllerListener()
        self._restoring = True
        self._settings = Settings()
        self._apps: list[AppEntry] = []
        self._known_apps = self._settings.known_apps  # path -> display name, incl. not-currently-running
        self._g_delay = self._settings.delay_ms
        self._last_app_map: dict[str, str] = {}
        self._search_query = self._settings.search_query
        self._disconnected = False  # true from the moment apps are blocked until reconnect fires
        self._desired_blocked = False
        self.launch_minimized = self._settings.launch_minimized

        self.overlay_enabled = self._settings.overlay_enabled
        self.overlay_position = self._settings.overlay_position
        self.overlay_offset_x = self._settings.overlay_offset_x
        self.overlay_offset_y = self._settings.overlay_offset_y
        self.overlay_size = self._settings.overlay_size
        self.overlay_colors = {
            "connected": self._settings.overlay_color("connected"),
            "disconnected": self._settings.overlay_color("disconnected"),
            "reconnecting": self._settings.overlay_color("reconnecting"),
        }
        self.overlay_show_connected = self._settings.overlay_option("connected")
        self.overlay_show_disconnected = self._settings.overlay_option("disconnected")
        self.overlay_blink_enabled = self._settings.overlay_option("blink")
        self.overlay_countdown_enabled = self._settings.overlay_option("countdown")
        self._overlay_state = "connected"

        self._reconnect_samples: list[float] = []
        self._countdown_deadline = None
        self._countdown_duration = None
        self._countdown_timer: Optional[threading.Timer] = None
        self._pending_reconnect_uids: tuple = ()
        self._pending_reconnect_generation = 0
        self._blocked_uids: set[str] = set()
        self._transition_generation = 0

        self._fw = FirewallService(dispatch, on_error=self._on_backend_error)
        self._scanner = ProcessScanner(dispatch, self._apply_app_map)
        self._reconnect = ReconnectScheduler(lambda: self._g_delay, self._on_reconnect_fire)
        self._keys = KeybindManager(dispatch, self._settings.keybinds,
                                     on_arm=self._on_arm, on_disarm=self._on_disarm,
                                     on_capture_done=self._on_capture_done)
        self._auto_refresh_timer: Optional[threading.Timer] = None

    # ── lifecycle ────────────────────────────────────────────────────────
    @property
    def restarted(self) -> bool:
        return was_restarted()

    def start(self):
        self._fw.start()
        self._scanner.start()
        self._restore()
        self._restoring = False
        self._refresh_overlay_visibility()
        self._keys.start()
        self.request_scan()
        self._fw.send("sweep", [e.state.app_path for e in self._apps])
        self._schedule_auto_refresh()

    def quit(self):
        self._cancel_auto_refresh()
        self._stop_countdown()
        self._save(); self._reconnect.cancel(); self._keys.stop()
        self._scanner.stop()
        self._fw.send("quit")

    def _on_backend_error(self, message):
        # try exactly one auto-restart (the env var makes it a one-shot, not a
        # crash loop); if we already restarted once and it failed again, give
        # up and tell the UI instead of retrying forever
        if not was_restarted():
            try:
                restart_self(); return
            except Exception:
                pass
        self._listener.on_fatal_error(
            f"Couldn't initialize the Windows Firewall COM API (even after an automatic "
            f"restart): {message}. Make sure the Windows Defender Firewall service is running.")

    # ── apps ─────────────────────────────────────────────────────────────
    @property
    def apps(self) -> list[AppEntry]:
        return list(self._apps)

    def filtered_apps(self) -> tuple[dict, dict]:
        """Returns (live_map, filtered_map): live_map is path->name for apps
        actually running right now; filtered_map is every app worth offering
        in a picker (known + live), narrowed by the current search query."""
        q = self._search_query.strip().lower()
        combined = dict(self._known_apps); combined.update(self._last_app_map)
        filtered = combined if not q else \
            {path: name for path, name in combined.items() if q in name.lower()}
        return self._last_app_map, filtered

    @property
    def search_query(self) -> str:
        return self._search_query

    def set_search_query(self, query: str):
        self._search_query = query
        self._listener.on_apps_refreshed()
        self._save()

    def request_scan(self):
        # Runs on its own persistent scanner thread so a slow process scan can
        # never delay a pending hotkey toggle.
        self._scanner.request_scan()

    def add_app(self) -> AppEntry:
        state = AppState()
        entry = AppEntry(state, len(self._apps))
        self._apps.append(entry)
        self._listener.on_entry_added(entry)
        self._save(); self.request_scan()
        self._refresh_overlay_visibility()
        return entry

    @staticmethod
    def _normalized_app_path(app_path):
        return os.path.normcase(os.path.normpath(app_path)) if app_path else ""

    def _app_path_in_use(self, entry, app_path):
        wanted = self._normalized_app_path(app_path)
        return bool(wanted) and any(
            other is not entry and self._normalized_app_path(other.state.app_path) == wanted
            for other in self._apps
        )

    def set_app(self, entry: AppEntry, app_path: str) -> bool:
        """Returns False (and notifies on_app_conflict) if app_path is already
        configured on another row — caller is responsible for reverting any
        picker widget to its previous selection in that case."""
        if self._app_path_in_use(entry, app_path):
            self._listener.on_app_conflict(entry)
            return False
        if entry.state.uid in self._blocked_uids:
            self._blocked_uids.discard(entry.state.uid)
            self._pending_reconnect_uids = tuple(
                uid for uid in self._pending_reconnect_uids if uid != entry.state.uid)
        entry.state.app_path = app_path
        self._attach(entry); self._save()
        if self._disconnected and not self._blocked_uids:
            self._transition_generation += 1
            self._reconnect.cancel()
            self._desired_blocked = False
            self._disconnected = False
            self._stop_countdown()
            self._set_overlay_state("connected")
        self._refresh_overlay_visibility()
        return True

    def _attach(self, entry: AppEntry):
        def cb(ok):
            entry.attached = ok
            self._refresh_entry_status(entry)
        self._fw.send("attach", (entry.state.uid, entry.state.app_path, cb))

    def remove_app(self, entry: AppEntry):
        idx = self._apps.index(entry)
        self._apps.pop(idx)
        self._blocked_uids.discard(entry.state.uid)
        self._pending_reconnect_uids = tuple(
            uid for uid in self._pending_reconnect_uids if uid != entry.state.uid)
        self._fw.send("remove", entry.state.uid)
        for i, e in enumerate(self._apps): e.index = i
        if self._disconnected and not self._blocked_uids:
            self._transition_generation += 1
            self._reconnect.cancel()
            self._disconnected = False
            self._desired_blocked = False
            self._stop_countdown()
            self._set_overlay_state("connected")
        self._listener.on_entry_removed(entry)
        self._refresh_overlay_visibility()
        self._save(); self.request_scan(); self._listener.on_entries_reordered()

    def move_app(self, entry: AppEntry, direction: int):
        idx = self._apps.index(entry); new_idx = idx + direction
        if new_idx < 0 or new_idx >= len(self._apps): return
        self._apps[idx], self._apps[new_idx] = self._apps[new_idx], self._apps[idx]
        for i, e in enumerate(self._apps): e.index = i
        self._listener.on_entries_reordered()
        self._save()

    def _restore(self):
        seen_paths = set()
        for i in range(self._settings.app_count):
            state = AppState.from_dict(self._settings.app_dict(i))
            normalized = self._normalized_app_path(state.app_path)
            if normalized and normalized in seen_paths:
                # Older preferences could contain duplicates. Keep the first
                # row and leave later rows unselected so they cannot share and
                # delete one another's firewall rules.
                state.app_path = ""
            elif normalized:
                seen_paths.add(normalized)
            if state.app_path and state.app_path not in self._known_apps:
                # Seen in a past session but not yet re-observed running this
                # session — best-effort name until the next scan finds it.
                self._known_apps[state.app_path] = os.path.basename(state.app_path)
            entry = AppEntry(state, len(self._apps))
            self._apps.append(entry)
            self._listener.on_entry_added(entry)
            self._attach(entry)

    def _apply_app_map(self, app_map):
        self._last_app_map = app_map
        # Remember every app we've ever seen running, so it stays pickable in
        # every row's picker even after it's closed (e.g. a game process that
        # only exists while the game is open).
        known_changed = False
        for path, name in app_map.items():
            if self._known_apps.get(path) != name:
                self._known_apps[path] = name; known_changed = True
        self._listener.on_apps_refreshed()
        for e in self._apps: self._refresh_entry_status(e)
        self._refresh_overlay_visibility()
        if known_changed: self._save()

    def _refresh_entry_status(self, entry: AppEntry):
        """Single source of truth for the status:
        INACTIVE — no app picked, rule failed, or the app just isn't running right now
        DISCONNECTED — app is running and hooked, but currently blocked by the hotkey
        CONNECTED — app is running and hooked, not currently blocked"""
        if entry.attached is None or not entry.attached:
            entry.set_status(STATUS_INACTIVE)
        elif entry.state.app_path not in self._last_app_map:
            entry.set_status(STATUS_INACTIVE)
        elif entry.state.uid in self._blocked_uids:
            entry.set_status(STATUS_DISCONNECTED)
        else:
            entry.set_status(STATUS_CONNECTED)
        self._listener.on_entry_status(entry)

    def _schedule_auto_refresh(self):
        t = threading.Timer(AUTO_REFRESH_MS / 1000.0, self._auto_refresh_tick)
        t.daemon = True; self._auto_refresh_timer = t; t.start()

    def _cancel_auto_refresh(self):
        if self._auto_refresh_timer: self._auto_refresh_timer.cancel(); self._auto_refresh_timer = None

    def _auto_refresh_tick(self):
        # Keeps status/overlay honest as apps launch or close on their own,
        # without waiting on a manual refresh. Purely cosmetic upkeep — a scan
        # started here never re-touches firewall attach state, only what
        # _apply_app_map already does for the manual path.
        self.request_scan()
        self._schedule_auto_refresh()

    # ── keybinds ─────────────────────────────────────────────────────────
    @property
    def keybinds(self) -> list[str]:
        return self._keys.keybinds

    def begin_add_key(self):
        if not self._keys.begin_capture(): return
        self._listener.on_capture_state(True)

    def _on_capture_done(self):
        self._save(); self._listener.on_keybinds_changed(self._keys.keybinds)
        self._listener.on_capture_state(False)

    def remove_key(self, label: str):
        now_empty = self._keys.remove(label)
        self._save()
        if now_empty:
            self._reconnect.cancel(); self._on_disarm()
        self._listener.on_keybinds_changed(self._keys.keybinds)

    # ── arm / disarm / reconnect state machine ──────────────────────────
    def _active_uids(self):
        """Rules whose configured process is attached and running right now."""
        return tuple(
            e.state.uid for e in self._apps
            if e.attached and e.state.app_path in self._last_app_map
        )

    def _on_arm(self):
        self._reconnect.cancel()
        self._stop_countdown()
        uids = self._active_uids()
        if not uids: return
        self._transition_generation += 1
        generation = self._transition_generation
        self._blocked_uids = set(uids)
        self._desired_blocked = True
        self._disconnected = True
        self._set_overlay_state("disconnected")
        self._emit_countdown("--", False)
        callback = lambda cb_uids, blocked, elapsed, results, gen=generation: \
            self._on_block_timing(gen, cb_uids, blocked, elapsed, results)
        self._fw.send("block_batch", (uids, True, callback))
        for e in self._apps: self._refresh_entry_status(e)

    def _on_disarm(self):
        self._transition_generation += 1
        self._desired_blocked = False
        if not self._disconnected:
            return
        # Re-enable exactly the rules this press blocked. A process may exit
        # during the hold/delay, but its rule still must be disabled so the
        # saved app is not unexpectedly blocked when it launches again.
        self._pending_reconnect_uids = tuple(self._blocked_uids)
        self._pending_reconnect_generation = self._transition_generation
        estimate_ms = (sum(self._reconnect_samples) / len(self._reconnect_samples)) \
            if self._reconnect_samples else 50.0
        self._countdown_duration = max(0.001, (self._g_delay + estimate_ms) / 1000.0)
        self._countdown_deadline = time.perf_counter() + self._countdown_duration
        self._update_countdown()
        self._reconnect.schedule()

    def _on_reconnect_fire(self):
        # Runs on the scheduler thread. Queue the reconnect directly so a busy
        # UI event loop cannot add latency between timer expiry and COM. The
        # immutable uid snapshot was captured at release.
        if self._desired_blocked or not self._pending_reconnect_uids:
            return
        generation = self._pending_reconnect_generation
        callback = lambda cb_uids, blocked, elapsed, results, gen=generation: \
            self._on_block_timing(gen, cb_uids, blocked, elapsed, results)
        self._fw.send("block_batch", (self._pending_reconnect_uids, False, callback))

    def _on_block_timing(self, generation, uids, blocked, elapsed_ms, results):
        if generation != self._transition_generation:
            return
        action = "disconnect" if blocked else "reconnect"
        failed = [uid for uid, (ok, _actual) in results.items() if not ok]
        for uid, (_ok, actual_blocked) in results.items():
            if actual_blocked: self._blocked_uids.add(uid)
            else: self._blocked_uids.discard(uid)
        if blocked:
            self._pending_reconnect_uids = tuple(self._blocked_uids)
        if failed:
            # A failed firewall write already leaves the app in whatever state
            # it was in before — don't report its duration as if it were a
            # successful disconnect/reconnect measurement.
            text = f"Last {action}: {len(failed)} of {len(uids)} failed"
        else:
            text = f"Last {action} ({len(uids)} app{'s' if len(uids) != 1 else ''}): {elapsed_ms:.2f} ms"
            if not blocked:
                self._reconnect_samples.append(elapsed_ms)
                self._reconnect_samples = self._reconnect_samples[-5:]
        if not blocked:
            self._stop_countdown()
            if self._blocked_uids:
                self._disconnected = True
                self._set_overlay_state("disconnected")
                self._emit_countdown("--", False)
            elif not self._desired_blocked:
                self._disconnected = False
                self._set_overlay_state("connected")
        elif not self._blocked_uids:
            self._disconnected = False
            self._set_overlay_state("connected")
        for entry in self._apps: self._refresh_entry_status(entry)
        if blocked: self._listener.on_disconnect_timing(text)
        else: self._listener.on_reconnect_timing(text)

    def _update_countdown(self):
        if self._countdown_deadline is None or not self._disconnected:
            return
        remaining = max(0.0, self._countdown_deadline - time.perf_counter())
        text = f"{remaining:.1f}s"
        half_duration = self._countdown_duration / 2.0
        blink_on = False
        if remaining <= half_duration:
            # Linear frequency sweep from 2 Hz at 50% remaining to 10 Hz at
            # zero. Integrating the changing frequency produces a smooth
            # accelerating blink without resetting phase on each tick.
            blink_elapsed = half_duration - remaining
            frequency_ramp = (10.0 - 2.0) / max(half_duration, 0.001)
            phase_cycles = 2.0 * blink_elapsed + 0.5 * frequency_ramp * blink_elapsed ** 2
            blink_on = int(phase_cycles * 2.0) % 2 == 0
        self._emit_countdown(text, blink_on)
        t = threading.Timer(COUNTDOWN_TICK_MS / 1000.0, self._update_countdown)
        t.daemon = True; self._countdown_timer = t; t.start()

    def _stop_countdown(self):
        if self._countdown_timer is not None:
            self._countdown_timer.cancel()
            self._countdown_timer = None
        self._countdown_deadline = None
        self._countdown_duration = None

    def _emit_countdown(self, text, blink_on):
        self._dispatch(self._listener.on_overlay_countdown, text, blink_on)

    def _set_overlay_state(self, state):
        self._overlay_state = state
        self._dispatch(self._listener.on_overlay_state, state)

    # ── reconnect delay ──────────────────────────────────────────────────
    @property
    def delay_ms(self) -> int:
        return self._g_delay

    def set_delay(self, value: int):
        self._g_delay = value

    def commit_delay(self):
        self._save()

    def set_launch_minimized(self, enabled: bool):
        self.launch_minimized = enabled
        self._save()

    # ── firewall rule cleanup ───────────────────────────────────────────
    def clear_all_rules(self):
        """Deletes every firewall rule PushToLag has created, including ones
        from previous sessions, and reconnects anything currently blocked.
        Caller must confirm with the user before calling this — no dialog
        happens here."""
        self._transition_generation += 1
        self._reconnect.cancel()
        self._desired_blocked = False
        self._disconnected = False
        self._blocked_uids.clear()
        self._pending_reconnect_uids = ()
        self._stop_countdown()
        self._set_overlay_state("connected")
        for entry in self._apps:
            entry.attached = None
            self._refresh_entry_status(entry)
        self._fw.send("reset", self._on_rules_reset)

    def _on_rules_reset(self, removed, results):
        for entry in self._apps:
            if entry.state.uid in results:
                entry.attached = results[entry.state.uid]
                self._refresh_entry_status(entry)
        self._refresh_overlay_visibility()
        failures = sum(result is False for result in results.values())
        self._listener.on_rules_cleared(removed, failures)

    # ── overlay settings ─────────────────────────────────────────────────
    def _has_active_apps(self):
        return any(e.attached and e.state.app_path in self._last_app_map for e in self._apps)

    def _refresh_overlay_visibility(self):
        visible = self.overlay_enabled and self._has_active_apps()
        self._listener.on_overlay_visibility(visible)

    def set_overlay_enabled(self, enabled: bool):
        self.overlay_enabled = enabled
        self._refresh_overlay_visibility()
        self._save()

    def set_overlay_option(self, name: str, enabled: bool):
        if name == "connected": self.overlay_show_connected = enabled
        elif name == "disconnected": self.overlay_show_disconnected = enabled
        elif name == "blink": self.overlay_blink_enabled = enabled
        elif name == "countdown": self.overlay_countdown_enabled = enabled
        else: return
        self._listener.on_overlay_settings_changed()
        self._save()

    def set_overlay_position(self, position: str):
        self.overlay_position = position
        self.overlay_offset_x = DEFAULT_OVERLAY_OFFSET
        self.overlay_offset_y = DEFAULT_OVERLAY_OFFSET
        self._listener.on_overlay_settings_changed()
        self._save()

    def set_overlay_offset(self, x: int, y: int):
        self.overlay_offset_x, self.overlay_offset_y = x, y
        self._listener.on_overlay_settings_changed()
        self._save()

    def set_overlay_size(self, size: int):
        self.overlay_size = size
        self._listener.on_overlay_settings_changed()
        self._save()

    def set_overlay_color(self, state: str, hex_color: str):
        """Caller picks the color itself (e.g. its own color-picker dialog);
        this just records and broadcasts the result."""
        self.overlay_colors[state] = hex_color
        self._listener.on_overlay_settings_changed()
        self._save()

    # ── persistence ──────────────────────────────────────────────────────
    def _save(self):
        if self._restoring: return
        overlay = {
            "overlay_enabled": self.overlay_enabled, "overlay_position": self.overlay_position,
            "overlay_offset_x": self.overlay_offset_x, "overlay_offset_y": self.overlay_offset_y,
            "overlay_size": self.overlay_size,
            "overlay_color_connected": self.overlay_colors["connected"],
            "overlay_color_disconnected": self.overlay_colors["disconnected"],
            "overlay_color_reconnecting": self.overlay_colors["reconnecting"],
            "overlay_show_connected": self.overlay_show_connected,
            "overlay_show_disconnected": self.overlay_show_disconnected,
            "overlay_show_blink": self.overlay_blink_enabled,
            "overlay_show_countdown": self.overlay_countdown_enabled,
        }
        self._settings.save(len(self._apps), self._keys.keybinds, self._g_delay,
                             [e.state for e in self._apps], self._search_query, overlay, self._known_apps,
                             self.launch_minimized)
