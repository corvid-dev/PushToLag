"""Per-row app state.

Status is a semantic key, not a color — the UI layer owns the color mapping;
the backend only reports which of the three states applies.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

STATUS_INACTIVE = "inactive"
STATUS_CONNECTED = "connected"
STATUS_DISCONNECTED = "disconnected"

STATUS_LABELS = {
    STATUS_INACTIVE: "INACTIVE",
    STATUS_CONNECTED: "CONNECTED",
    STATUS_DISCONNECTED: "DISCONNECTED",
}


@dataclass
class AppState:
    uid: str = field(default_factory=lambda: str(uuid.uuid4()))  # runtime only, not persisted
    app_path: str = ""

    @staticmethod
    def from_dict(d):
        return AppState(app_path=d.get("app_path", ""))

    def to_dict(self):
        return {"app_path": self.app_path}


class AppEntry:
    """Plain state/status holder — AppController drives all transitions."""

    def __init__(self, state: AppState, index: int):
        self.state = state
        self.index = index
        self.status = STATUS_INACTIVE
        self.attached = None  # None: no app selected, True: rule attached ok, False: rule error
        self.on_status_change = None  # Callable[[str status], None]

    def set_status(self, status: str):
        self.status = status
        if self.on_status_change:
            self.on_status_change(status)
