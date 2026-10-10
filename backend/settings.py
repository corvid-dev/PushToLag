"""Persisted preferences (prefs.json)."""
from __future__ import annotations

import json
import os

PREFS_PATH = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "PushToLag", "prefs.json")

OVERLAY_POSITIONS = (
    "Top-left", "Top-middle", "Top-right",
    "Left-middle", "Center", "Right-middle",
    "Bottom-left", "Bottom-middle", "Bottom-right",
)
DEFAULT_OVERLAY_POSITION = "Top-right"
DEFAULT_OVERLAY_OFFSET = 0
OVERLAY_OFFSET_MIN, OVERLAY_OFFSET_MAX = -10000, 10000
DEFAULT_OVERLAY_COLORS = {
    "connected": "#00b894",
    "disconnected": "#e17055",
    "reconnecting": "#f1c40f",
}
DEFAULT_OVERLAY_SIZE = 12
OVERLAY_SIZE_MIN, OVERLAY_SIZE_MAX = 4, 64


class Settings:
    """Owns prefs.json — nothing else should read or write it directly."""

    def __init__(self):
        try:
            with open(PREFS_PATH, encoding="utf-8") as f:
                self._data = json.load(f)
        except Exception:
            self._data = {}

    def save(self, app_count, keybinds, delay_ms, app_states, search_query, overlay, known_apps,
             launch_minimized):
        data = {
            "app_count": app_count, "keybinds": list(keybinds), "delay_ms": delay_ms,
            "search_query": search_query, "known_apps": known_apps,
            "launch_minimized": launch_minimized,
        }
        data.update({f"ch{i}": s.to_dict() for i, s in enumerate(app_states)})
        data.update(overlay)
        self._data = data
        try:
            os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
            with open(PREFS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    @property
    def app_count(self):
        return self._data.get("app_count", self._data.get("channel_count", 1))

    @property
    def keybinds(self):
        return list(self._data.get("keybinds", []))

    @property
    def delay_ms(self):
        return max(0, min(10000, int(self._data.get("delay_ms", 250))))

    @property
    def search_query(self):
        return self._data.get("search_query", "")

    @property
    def launch_minimized(self):
        return bool(self._data.get("launch_minimized", False))

    @property
    def known_apps(self):
        return dict(self._data.get("known_apps", {}))

    def app_dict(self, i):
        return self._data.get(f"ch{i}", {})

    @property
    def overlay_enabled(self):
        return bool(self._data.get("overlay_enabled", False))

    def overlay_option(self, name):
        return bool(self._data.get(f"overlay_show_{name}", True))

    @property
    def overlay_position(self):
        return self._data.get("overlay_position", DEFAULT_OVERLAY_POSITION)

    @property
    def overlay_offset_x(self):
        return int(self._data.get("overlay_offset_x", DEFAULT_OVERLAY_OFFSET))

    @property
    def overlay_offset_y(self):
        return int(self._data.get("overlay_offset_y", DEFAULT_OVERLAY_OFFSET))

    def overlay_color(self, state):
        return self._data.get(f"overlay_color_{state}", DEFAULT_OVERLAY_COLORS[state])

    @property
    def overlay_size(self):
        return int(self._data.get("overlay_size", DEFAULT_OVERLAY_SIZE))
