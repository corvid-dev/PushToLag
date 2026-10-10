"""One app row: reorder arrows, app picker, status, remove — vanilla ttk only.

Status color is kept — it's informational state, not app-chrome skinning.
"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk

from backend.app_state import AppEntry, STATUS_CONNECTED, STATUS_DISCONNECTED, STATUS_INACTIVE, STATUS_LABELS

NONE_OPTION = "NONE"

STATUS_COLORS = {
    STATUS_INACTIVE: "#888888",
    STATUS_CONNECTED: "#00b894",
    STATUS_DISCONNECTED: "#e17055",
}


def _status_text(status: str) -> str:
    return STATUS_LABELS[status]


class AppRowView:
    def __init__(self, entry: AppEntry, on_select, on_move, on_remove):
        self._entry, self._on_select = entry, on_select
        self._on_move, self._on_remove = on_move, on_remove
        self._display_to_path: dict[str, str] = {}
        self.frame = None
        self._idxlbl = self._svar = self._combo = self._slbl = None
        self._btn_u = self._btn_d = None

    def build(self, parent) -> ttk.Frame:
        self.frame = ttk.Frame(parent)
        self.frame.pack(fill="x", pady=3)
        self._idxlbl = ttk.Label(self.frame, text=self._idx_text(), width=3, anchor="e")
        self._idxlbl.pack(side="left", padx=(6, 2))
        mv = ttk.Frame(self.frame)
        mv.pack(side="left", padx=(0, 6))
        self._btn_u = ttk.Button(mv, text="▲", width=2, command=lambda: self._on_move(self._entry, -1))
        self._btn_u.pack()
        self._btn_d = ttk.Button(mv, text="▼", width=2, command=lambda: self._on_move(self._entry, +1))
        self._btn_d.pack()
        self._combo = ttk.Combobox(self.frame, state="readonly", height=50)
        self._combo["values"] = [NONE_OPTION]; self._combo.current(0)
        self._combo.pack(side="left", fill="x", expand=True, padx=4)
        self._combo.bind("<<ComboboxSelected>>", self._on_app_selected)
        self._svar = tk.StringVar(value=_status_text(self._entry.status))
        self._slbl = tk.Label(self.frame, textvariable=self._svar, width=12, anchor="center",
                               bg=STATUS_COLORS[self._entry.status], fg="white",
                               font=("Segoe UI", 9, "bold"), padx=8, pady=2)
        self._slbl.pack(side="left", padx=4)
        ttk.Button(self.frame, text="✕", width=3,
                   command=lambda: self._on_remove(self._entry)).pack(side="left", padx=(2, 6))
        self._entry.on_status_change = self.set_status
        return self.frame

    def update_arrows(self, is_first, is_last):
        if self._btn_u: self._btn_u.state(["disabled" if is_first else "!disabled"])
        if self._btn_d: self._btn_d.state(["disabled" if is_last else "!disabled"])

    def _idx_text(self): return f"{self._entry.index + 1}."

    def set_index(self, i):
        self._entry.index = i
        if self._idxlbl: self._idxlbl.config(text=self._idx_text())

    def set_status(self, status: str):
        if self._svar: self._svar.set(_status_text(status))
        if self._slbl: self._slbl.config(bg=STATUS_COLORS[status])

    def refresh_apps(self, live_map: dict, filtered_map: dict):
        """filtered_map is path -> display name for every app worth offering
        (currently running or previously configured), already narrowed by the
        header search box. live_map (apps actually running right now) isn't
        needed for display text anymore — the status column already says
        INACTIVE/CONNECTED/DISCONNECTED, so the picker just shows names."""
        self._display_to_path = {}
        values = [NONE_OPTION]
        for p, name in filtered_map.items():
            values.append(name); self._display_to_path[name] = p
        path = self._entry.state.app_path
        if path and path in filtered_map:
            current = filtered_map[path]
        elif path:
            # configured but filtered out by the search box — keep it
            # visible/selectable rather than pretending it's gone. Show just
            # the filename, not the full path: a narrow combobox truncates
            # from the right, which would otherwise hide the only useful part.
            tag = os.path.basename(path) or path
            values.append(tag); self._display_to_path[tag] = path
            current = tag
        else:
            current = NONE_OPTION
        self._combo["values"] = values
        self._combo.set(current)

    def _on_app_selected(self, _=None):
        display = self._combo.get()
        path = "" if display == NONE_OPTION else self._display_to_path.get(display)
        if path is not None: self._on_select(self._entry, path)

    def revert_selection(self, live_map: dict, filtered_map: dict):
        """Re-apply the entry's actual app_path to the combobox — used after
        on_app_conflict rejects a selection, so the picker doesn't keep
        showing the rejected value."""
        self.refresh_apps(live_map, filtered_map)

    def destroy(self):
        self._entry.on_status_change = None
        if self.frame: self.frame.destroy(); self.frame = None
