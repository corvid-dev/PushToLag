"""Orchestrator for the new UI layer — ties MainWindowView/AppRowView/
OverlayView to backend.controller.AppController. This is the only module
that owns confirm dialogs and the color picker (messagebox/colorchooser):
AppController deliberately has no opinion about how those are presented.
"""
from __future__ import annotations

import ctypes
import os
import sys
import tkinter as tk
from tkinter import colorchooser, messagebox, ttk

from backend import firewall, process_scan
from backend.controller import AppController, AppControllerListener
from backend.lifecycle import is_admin

from .app_row_view import AppRowView
from .main_window_view import MainWindowView
from .overlay_view import OverlayView
from .tray import TrayIcon
from version import APP_VERSION


class App(AppControllerListener):
    def __init__(self, root):
        self.root = root
        root.title(f"PushToLag v{APP_VERSION}")
        root.resizable(True, True)
        # The X button quits outright; the window's own minimize button sends
        # it to tray instead of the taskbar (caught via <Unmap>, since Tk has
        # no direct "minimize" event/protocol).
        root.protocol("WM_DELETE_WINDOW", self._quit)
        root.bind("<Unmap>", self._on_unmap)
        root.withdraw()  # hidden until the end of __init__ — avoids a startup flash if launch_minimized is on

        self._controller = AppController(dispatch=lambda fn, *a: root.after(0, fn, *a), listener=self)
        self._rows: dict[str, AppRowView] = {}
        self._overlay = OverlayView(root)
        self._last_overlay_state = "connected"
        # Tray callbacks fire on pystray's own thread — marshal both onto the Tk thread.
        self._tray = TrayIcon(on_open=lambda: root.after(0, self._restore_window),
                               on_exit=lambda: root.after(0, self._quit))

        c = self._controller
        self._main_window = MainWindowView(
            root, initial_delay=c.delay_ms, initial_keybinds=c.keybinds,
            on_add_app=self._on_add_app, on_add_key=c.begin_add_key,
            on_remove_key=c.remove_key, on_clear_rules=self._on_clear_rules,
            on_delay_change=c.set_delay, on_delay_commit=c.commit_delay,
            on_search_change=c.set_search_query, on_refresh=c.request_scan,
            initial_overlay_enabled=c.overlay_enabled, initial_overlay_position=c.overlay_position,
            initial_overlay_offset_x=c.overlay_offset_x, initial_overlay_offset_y=c.overlay_offset_y,
            initial_overlay_size=c.overlay_size, initial_overlay_colors=c.overlay_colors,
            initial_overlay_show_connected=c.overlay_show_connected,
            initial_overlay_show_disconnected=c.overlay_show_disconnected,
            initial_overlay_blink_enabled=c.overlay_blink_enabled,
            initial_overlay_countdown_enabled=c.overlay_countdown_enabled,
            on_overlay_toggle=c.set_overlay_enabled, on_overlay_position_change=self._on_overlay_position_change,
            on_overlay_offset_change=c.set_overlay_offset, on_overlay_size_change=c.set_overlay_size,
            on_overlay_color_pick=self._on_overlay_color_pick, on_overlay_option_change=c.set_overlay_option,
            initial_launch_minimized=c.launch_minimized, on_launch_minimized_change=c.set_launch_minimized,
            initial_search=c.search_query, restarted=c.restarted,
        )
        self._sync_overlay_view()
        self._controller.start()  # fires on_entry_added for every restored app via the listener
        self._apply_filter_to_rows()
        root.update_idletasks(); self._main_window.fit()
        try:
            base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            root.wm_iconbitmap(os.path.join(base, "PushToLag.ico"))
        except Exception:
            pass
        self._tray.start()
        self._update_tray_color()  # paint the user's actual configured "connected" color, not the generic default
        if not self._controller.launch_minimized:
            self._restore_window()

    def _minimize_to_tray(self):
        self.root.withdraw()

    def _on_unmap(self, event):
        # <Unmap> also fires for child widgets, menus, etc. — only react when
        # the root itself was iconified (the window's minimize button).
        if event.widget is self.root and self.root.state() == "iconic":
            self._minimize_to_tray()

    def _restore_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _apply_filter_to_rows(self):
        live_map, filtered = self._controller.filtered_apps()
        for row in self._rows.values(): row.refresh_apps(live_map, filtered)

    def _refresh_arrows(self):
        apps = self._controller.apps
        n = len(apps)
        for i, entry in enumerate(apps):
            self._rows[entry.state.uid].update_arrows(is_first=i == 0, is_last=i == n - 1)

    def _on_add_app(self):
        self._controller.add_app()

    def _on_clear_rules(self):
        if not messagebox.askyesno("Clear All Firewall Rules",
                "This deletes every firewall rule PushToLag has created (including ones "
                "from previous sessions). Any currently disconnected app will reconnect. Continue?"):
            return
        self._controller.clear_all_rules()

    def _on_overlay_position_change(self, position):
        self._controller.set_overlay_position(position)
        self._main_window.reset_overlay_offsets()

    def _on_overlay_color_pick(self, state):
        current = self._controller.overlay_colors[state]
        title = f"{state.title()} overlay color"
        _rgb, hex_color = colorchooser.askcolor(color=current, title=title)
        if hex_color is None: return
        self._controller.set_overlay_color(state, hex_color)
        self._main_window.set_overlay_color(state, hex_color)

    def _sync_overlay_view(self):
        c = self._controller
        self._overlay.set_position(c.overlay_position)
        self._overlay.set_offset(c.overlay_offset_x, c.overlay_offset_y)
        self._overlay.set_size(c.overlay_size)
        for state, color in c.overlay_colors.items(): self._overlay.set_color(state, color)
        self._overlay.set_option("connected", c.overlay_show_connected)
        self._overlay.set_option("disconnected", c.overlay_show_disconnected)
        self._overlay.set_option("blink", c.overlay_blink_enabled)
        self._overlay.set_option("countdown", c.overlay_countdown_enabled)

    # ── AppControllerListener ────────────────────────────────────────────
    def on_entry_added(self, entry):
        row = AppRowView(entry, on_select=self._controller.set_app,
                          on_move=self._controller.move_app, on_remove=self._controller.remove_app)
        row.build(self._main_window.rows_frame)
        self._rows[entry.state.uid] = row
        self._refresh_arrows(); self._main_window.fit()
        live_map, filtered = self._controller.filtered_apps()
        row.refresh_apps(live_map, filtered)

    def on_entry_removed(self, entry):
        row = self._rows.pop(entry.state.uid, None)
        if row: row.destroy()
        self._main_window.fit()

    def on_entries_reordered(self):
        for entry in self._controller.apps:
            row = self._rows[entry.state.uid]
            row.set_index(entry.index)
            row.frame.pack_forget()
        for entry in self._controller.apps:
            self._rows[entry.state.uid].frame.pack(fill="x", pady=3)
        self._refresh_arrows()

    def on_entry_status(self, entry):
        pass  # AppRowView already subscribes to entry.on_status_change directly

    def on_app_conflict(self, entry):
        messagebox.showwarning("App already configured",
            "That executable is already selected in another row.")
        row = self._rows.get(entry.state.uid)
        if row: row.revert_selection(*self._controller.filtered_apps())

    def on_apps_refreshed(self):
        self._apply_filter_to_rows()

    def on_disconnect_timing(self, text):
        self._main_window.set_disconnect_timing(text)

    def on_reconnect_timing(self, text):
        self._main_window.set_reconnect_timing(text)

    def on_keybinds_changed(self, keybinds):
        self._main_window.rebuild_keybinds(keybinds)

    def on_capture_state(self, active):
        self._main_window.set_capturing(active)

    def on_overlay_settings_changed(self):
        self._sync_overlay_view()
        for state, color in self._controller.overlay_colors.items():
            self._main_window.set_overlay_color(state, color)
        self._update_tray_color()

    def on_overlay_visibility(self, visible):
        if visible: self._overlay.show()
        else: self._overlay.hide()

    def on_overlay_state(self, state):
        self._overlay.set_state(state)
        self._last_overlay_state = state
        self._update_tray_color()

    def _update_tray_color(self):
        color = self._controller.overlay_colors[self._last_overlay_state]
        tooltip = f"PushToLag — {self._last_overlay_state.title()}"
        self._tray.set_color(color, tooltip)

    def on_overlay_countdown(self, text, blink_on):
        self._overlay.set_countdown(text, blink_on)

    def on_fatal_error(self, message):
        messagebox.showerror("Windows Firewall unavailable", message)

    def on_rules_cleared(self, removed, failed):
        detail = f"\n\n{failed} configured app rule(s) could not be recreated." if failed else ""
        messagebox.showinfo("Done", f"Removed {removed} rule(s).{detail}")

    def _quit(self):
        self._tray.stop()
        self._controller.quit()
        self._overlay.hide()
        self.root.destroy()


def main():
    if os.name != "nt":
        print("PushToLag only works on Windows (it drives Windows Firewall)."); return
    if process_scan.psutil is None:
        print("Missing dependency: run  pip install psutil"); return
    if firewall.com is None:
        print("Missing dependency: run  pip install comtypes"); return
    if not is_admin():
        ctypes.windll.user32.MessageBoxW(0,
            "PushToLag needs to run elevated to manage Windows Firewall rules.\n\n"
            "Launch it via a shortcut set to \"Run as administrator\".",
            "PushToLag — Elevation required", 0x30)  # MB_ICONWARNING
        return
    try:
        windll = ctypes.windll; windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    root.minsize(460, 260)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
