"""Main window chrome — vanilla ttk, no custom colors: status bar, keybind/
delay/overlay settings bar, app search header, app-row container, bottom bar.

Knows nothing about firewall/keybind/process-scan state; callers (ui.app.App)
wire every callback to backend.controller.AppController and push state back
in via the set_*/rebuild_* methods below.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from backend.keybind import disp
from backend.settings import OVERLAY_OFFSET_MAX, OVERLAY_OFFSET_MIN, OVERLAY_POSITIONS, OVERLAY_SIZE_MAX, \
    OVERLAY_SIZE_MIN

WIN_W = 460
MIN_H = 260


class MainWindowView:
    def __init__(self, root, *, initial_delay, initial_keybinds, on_add_app, on_add_key,
                 on_remove_key, on_clear_rules, on_delay_change, on_delay_commit, on_search_change, on_refresh,
                 initial_overlay_enabled, initial_overlay_position, initial_overlay_offset_x,
                 initial_overlay_offset_y, initial_overlay_size, initial_overlay_colors,
                 initial_overlay_show_connected, initial_overlay_show_disconnected,
                 initial_overlay_blink_enabled, initial_overlay_countdown_enabled,
                 on_overlay_toggle, on_overlay_position_change, on_overlay_offset_change,
                 on_overlay_size_change, on_overlay_color_pick, on_overlay_option_change,
                 initial_launch_minimized, on_launch_minimized_change,
                 initial_search="", restarted=False):
        self.root = root
        self._on_remove_key = on_remove_key
        self._was_zoomed = False
        root.bind("<Configure>", self._on_root_configure)

        outer = ttk.Frame(root)
        outer.pack(fill="both", expand=True)
        self._outer = outer

        self._build_status_bar(outer)
        self._build_global_bar(outer, initial_delay, on_add_key, on_delay_change, on_delay_commit, restarted,
                                initial_overlay_enabled, initial_overlay_position, initial_overlay_offset_x,
                                initial_overlay_offset_y, initial_overlay_size, initial_overlay_colors,
                                initial_overlay_show_connected, initial_overlay_show_disconnected,
                                initial_overlay_blink_enabled, initial_overlay_countdown_enabled,
                                on_overlay_toggle, on_overlay_position_change, on_overlay_offset_change,
                                on_overlay_size_change, on_overlay_color_pick, on_overlay_option_change)

        hdr = ttk.Frame(outer)
        hdr.pack(fill="x", padx=12, pady=(10, 4))
        ttk.Label(hdr, text="Apps", font=("Segoe UI", 11, "bold")).pack(side="left")
        ttk.Button(hdr, text="+ Add App", command=on_add_app).pack(side="right")
        ttk.Button(hdr, text="⟳ Refresh", command=on_refresh).pack(side="right", padx=(0, 6))
        self._search_var = tk.StringVar(value=initial_search)
        self._search_var.trace_add("write", lambda *_: on_search_change(self._search_var.get()))
        ttk.Entry(hdr, textvariable=self._search_var).pack(side="left", fill="x", expand=True, padx=10)

        self.rows_frame = ttk.Frame(outer)
        self.rows_frame.pack(fill="both", expand=True, padx=12, pady=(0, 4))

        bottom = ttk.Frame(outer)
        bottom.pack(side="bottom", fill="x")
        ttk.Button(bottom, text="Clear All Firewall Rules", command=on_clear_rules).pack(
            side="left", padx=6, pady=4)
        self._launch_minimized_var = tk.BooleanVar(value=initial_launch_minimized)
        ttk.Checkbutton(bottom, text="Launch Minimized", variable=self._launch_minimized_var,
                         command=lambda: on_launch_minimized_change(self._launch_minimized_var.get())).pack(
            side="left", padx=(10, 0))
        ttk.Sizegrip(bottom).pack(side="right")

        self.rebuild_keybinds(initial_keybinds)

    def _build_status_bar(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=12, pady=6)
        self._disconnect_var = tk.StringVar(value="Last disconnect: —")
        self._reconnect_var = tk.StringVar(value="Last reconnect: —")
        ttk.Label(bar, textvariable=self._disconnect_var).pack(side="left")
        ttk.Label(bar, textvariable=self._reconnect_var).pack(side="right")
        ttk.Separator(parent).pack(fill="x")

    def _build_global_bar(self, parent, initial_delay, on_add_key, on_delay_change, on_delay_commit, restarted,
                           initial_enabled, initial_position, initial_offset_x, initial_offset_y, initial_size,
                           initial_colors, initial_show_connected, initial_show_disconnected,
                           initial_blink_enabled, initial_countdown_enabled,
                           on_toggle, on_position_change, on_offset_change, on_size_change, on_color_pick,
                           on_option_change):
        inner = ttk.Frame(parent)
        inner.pack(fill="x", padx=12, pady=8)
        self._build_keybind_row(inner, on_add_key)
        ttk.Separator(inner).pack(fill="x", pady=8)
        self._build_delay_row(inner, initial_delay, on_delay_change, on_delay_commit, restarted)
        ttk.Separator(inner).pack(fill="x", pady=8)
        self._build_overlay_row(inner, initial_enabled, initial_position, initial_offset_x, initial_offset_y,
                                 initial_size, initial_colors, initial_show_connected, initial_show_disconnected,
                                 initial_blink_enabled, initial_countdown_enabled,
                                 on_toggle, on_position_change, on_offset_change, on_size_change, on_color_pick,
                                 on_option_change)

    def _build_keybind_row(self, inner, on_add_key):
        krow = ttk.Frame(inner)
        krow.pack(fill="x")
        ttk.Label(krow, text="Global Keybind (holds ALL apps)", font=("Segoe UI", 11, "bold")).pack(side="left")
        self._addkeybtn = ttk.Button(krow, text="+ Add Key", command=on_add_key)
        self._addkeybtn.pack(side="left", padx=(8, 0))
        self._capturelbl = ttk.Label(krow, text="")
        self._capturelbl.pack(side="left", padx=(8, 0))
        self._gkb_frame = ttk.Frame(inner)
        self._gkb_frame.pack(fill="x", pady=(6, 0))

    def _build_delay_row(self, inner, initial_delay, on_delay_change, on_delay_commit, restarted):
        drow = ttk.Frame(inner)
        drow.pack(fill="x")
        if restarted:
            ttk.Label(drow, text="⟳ Restarted after a firewall init failure").pack(side="left", padx=(0, 10))
        ttk.Label(drow, text="Reconnect Delay (ms)", font=("Segoe UI", 11, "bold")).pack(side="left")
        self._dvar = tk.IntVar(value=initial_delay)
        self._dvar.trace_add("write", lambda *_: on_delay_change(self._dvar.get()))
        self._delay_value_lbl = ttk.Label(drow, text=f"{initial_delay} ms")
        self._delay_value_lbl.pack(side="right")
        # ttk.Scale is continuous with no resolution option — snap to 10ms steps
        # here instead of binding it straight to an IntVar (which would throw on
        # a fractional value the moment the slider is dragged).
        scale_var = tk.DoubleVar(value=initial_delay)
        def _on_scale_move(raw):
            snapped = max(0, min(10000, round(float(raw) / 10) * 10))
            if snapped != self._dvar.get():
                self._dvar.set(snapped)
            self._delay_value_lbl.config(text=f"{snapped} ms")
        scale = ttk.Scale(inner, variable=scale_var, from_=0, to=10000, orient="horizontal",
                           command=_on_scale_move)
        scale.pack(fill="x", pady=(2, 0))
        scale.bind("<ButtonRelease-1>", lambda _: on_delay_commit())

    def _build_overlay_row(self, inner, initial_enabled, initial_position, initial_offset_x, initial_offset_y,
                            initial_size, initial_colors, initial_show_connected, initial_show_disconnected,
                            initial_blink_enabled, initial_countdown_enabled,
                            on_toggle, on_position_change, on_offset_change, on_size_change, on_color_pick,
                            on_option_change):
        orow = ttk.Frame(inner)
        orow.pack(fill="x")
        ttk.Label(orow, text="Network Overlay", font=("Segoe UI", 11, "bold")).pack(side="left")
        self._ovar = tk.BooleanVar(value=initial_enabled)
        ttk.Checkbutton(orow, variable=self._ovar, command=lambda: on_toggle(self._ovar.get())).pack(
            side="left", padx=(8, 0))
        ttk.Label(orow, text="Position:").pack(side="left", padx=(10, 4))
        self._opos_var = tk.StringVar(value=initial_position)
        opos_combo = ttk.Combobox(orow, state="readonly", width=14, values=OVERLAY_POSITIONS,
                                   textvariable=self._opos_var)
        opos_combo.pack(side="left")
        opos_combo.bind("<<ComboboxSelected>>", lambda e: on_position_change(self._opos_var.get()))

        options_row = ttk.Frame(inner)
        options_row.pack(fill="x", pady=(6, 0))
        ttk.Label(options_row, text="Show:").pack(side="left")
        self._overlay_option_vars = {}
        for name, label, initial in (
            ("connected", "Connected", initial_show_connected),
            ("disconnected", "Disconnected", initial_show_disconnected),
            ("blink", "Blink", initial_blink_enabled),
            ("countdown", "Countdown", initial_countdown_enabled),
        ):
            var = tk.BooleanVar(value=initial)
            self._overlay_option_vars[name] = var
            ttk.Checkbutton(options_row, text=label, variable=var,
                             command=lambda n=name, v=var: on_option_change(n, v.get())).pack(
                side="left", padx=(8, 0))

        colors_row = ttk.Frame(inner)
        colors_row.pack(fill="x", pady=(6, 0))
        self._color_buttons = {}
        for state, label in (("connected", "Connected"), ("disconnected", "Disconnected"),
                              ("reconnecting", "Blink")):
            ttk.Label(colors_row, text=f"{label}:").pack(side="left", padx=(0, 4) if state == "connected" else (10, 4))
            color = initial_colors[state]
            btn = tk.Button(colors_row, text="  ", bg=color, activebackground=color, width=3,
                             command=lambda s=state: on_color_pick(s))
            btn.pack(side="left")
            self._color_buttons[state] = btn

        orow3 = ttk.Frame(inner)
        orow3.pack(fill="x", pady=(6, 0))
        ttk.Label(orow3, text="X offset:").pack(side="left")
        self._ox_var = tk.IntVar(value=initial_offset_x)
        self._ox_var.trace_add("write", lambda *_: on_offset_change(
            self._safe_int(self._ox_var, initial_offset_x, OVERLAY_OFFSET_MIN, OVERLAY_OFFSET_MAX),
            self._safe_int(self._oy_var, initial_offset_y, OVERLAY_OFFSET_MIN, OVERLAY_OFFSET_MAX)))
        ttk.Spinbox(orow3, textvariable=self._ox_var, width=6, from_=OVERLAY_OFFSET_MIN, to=OVERLAY_OFFSET_MAX,
                    increment=10).pack(side="left", padx=(4, 12))
        ttk.Label(orow3, text="Y offset:").pack(side="left")
        self._oy_var = tk.IntVar(value=initial_offset_y)
        self._oy_var.trace_add("write", lambda *_: on_offset_change(
            self._safe_int(self._ox_var, initial_offset_x, OVERLAY_OFFSET_MIN, OVERLAY_OFFSET_MAX),
            self._safe_int(self._oy_var, initial_offset_y, OVERLAY_OFFSET_MIN, OVERLAY_OFFSET_MAX)))
        ttk.Spinbox(orow3, textvariable=self._oy_var, width=6, from_=OVERLAY_OFFSET_MIN, to=OVERLAY_OFFSET_MAX,
                    increment=10).pack(side="left", padx=(4, 0))
        ttk.Label(orow3, text="Size:").pack(side="left", padx=(16, 0))
        self._osize_var = tk.IntVar(value=initial_size)
        self._osize_var.trace_add("write", lambda *_: on_size_change(
            self._safe_int(self._osize_var, initial_size, OVERLAY_SIZE_MIN, OVERLAY_SIZE_MAX)))
        ttk.Spinbox(orow3, textvariable=self._osize_var, width=5, from_=OVERLAY_SIZE_MIN,
                    to=OVERLAY_SIZE_MAX).pack(side="left", padx=(4, 0))

    @staticmethod
    def _safe_int(var, default, lo=0, hi=None):
        try: v = int(var.get())
        except (tk.TclError, ValueError): return default
        v = max(lo, v)
        return min(hi, v) if hi is not None else v

    def set_overlay_color(self, state, color):
        btn = self._color_buttons.get(state)
        if btn: btn.config(bg=color, activebackground=color)

    def reset_overlay_offsets(self):
        self._ox_var.set(0)
        self._oy_var.set(0)

    def _on_root_configure(self, _=None):
        zoomed = self.root.state() == "zoomed"
        if self._was_zoomed and not zoomed: self.fit()
        self._was_zoomed = zoomed

    def fit(self):
        self.root.update_idletasks()
        h = max(self._outer.winfo_reqheight(), MIN_H)
        self.root.minsize(WIN_W, MIN_H)
        self.root.geometry(f"{WIN_W}x{h}")

    def set_disconnect_timing(self, text):
        self._disconnect_var.set(text)

    def set_reconnect_timing(self, text):
        self._reconnect_var.set(text)

    def set_capturing(self, active):
        self._addkeybtn.state(["disabled" if active else "!disabled"])
        self._capturelbl.config(text="Press a key…" if active else "")

    def rebuild_keybinds(self, keybinds):
        for w in self._gkb_frame.winfo_children(): w.destroy()
        if not keybinds:
            ttk.Label(self._gkb_frame, text="No key bound yet — add one above.").pack(side="left")
            return
        for label in keybinds:
            r = ttk.Frame(self._gkb_frame)
            r.pack(side="left", padx=(0, 6))
            ttk.Label(r, text=disp(label)).pack(side="left", padx=(8, 2), pady=3)
            ttk.Button(r, text="✕", width=3,
                       command=lambda l=label: self._on_remove_key(l)).pack(side="left", padx=(0, 6))
