"""
PushToLag v1.0 — Push-to-Disconnect for Windows 11
Hold ONE global key to cut network access to every app you've added, release to
reconnect them all after a shared delay. Uses Windows Firewall rules per app,
driven entirely through the in-process Windows Firewall COM API (no netsh.exe
shelling out — every rule toggle is just a property set).

pip install pynput psutil comtypes

Must run elevated (Administrator) — launch it via a shortcut set to "Run as administrator".
It no longer self-elevates; if launched without admin rights it warns and exits.
"""
import os, sys, json, uuid, queue, threading, hashlib, ctypes, time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Optional
import tkinter as tk
from tkinter import ttk, messagebox
from pynput import keyboard, mouse

try:
    import psutil
except ImportError:
    psutil = None

try:
    import comtypes.client as com
    from comtypes import CoInitialize, CoUninitialize
except ImportError:
    com = None

VERSION = "1.0"

APP_BG="#1e1e1e"; PANEL_BG="#2a2a2a"; COL_BG="#252525"; DIVIDER="#333333"
ACCENT="#00b894"; ACCENT_OFF="#e17055"; TEXT="#ececec"; MUTED="#888888"
WIN_W=460; MIN_H=260
FW_PREFIX = "PushToLag"

FONT_XS   = ("Segoe UI",7)          # reorder arrows
FONT_SM   = ("Segoe UI",9)          # secondary text, small buttons
FONT_SM_B = ("Segoe UI",9,"bold")   # per-app status pill
FONT_MD   = ("Segoe UI",10)         # body text, default buttons/combos
FONT_LG_B = ("Segoe UI",11,"bold")  # section headers
FONT_ICON = ("Segoe UI",12)         # remove-row icon button

# Windows Firewall COM constants (netfw.h)
NET_FW_RULE_DIR_IN, NET_FW_RULE_DIR_OUT = 1, 2
NET_FW_ACTION_BLOCK = 0
NET_FW_PROFILE2_ALL = 0x7FFFFFFF

PREFS_PATH = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "PushToLag", "prefs.json")
RESTART_ENV_VAR = "PUSHTOLAG_RESTARTED"  # set on the one auto-restart we allow after a backend init failure

MOUSE_LABELS = {
    mouse.Button.left:"Mouse-Left", mouse.Button.right:"Mouse-Right",
    mouse.Button.middle:"Mouse-Middle", mouse.Button.x1:"Mouse-X1", mouse.Button.x2:"Mouse-X2",
}
DISPLAY_NAMES = {
    "ctrl_l":"L-Ctrl","ctrl_r":"R-Ctrl","shift_l":"L-Shift","shift_r":"R-Shift","shift":"Shift",
    "alt_l":"L-Alt","alt_r":"R-Alt","Mouse-Left":"Mouse L","Mouse-Right":"Mouse R",
    "Mouse-Middle":"Mouse M","Mouse-X1":"Mouse 4","Mouse-X2":"Mouse 5",
}

def key_label(k):
    if isinstance(k, str): return k
    try: return k.char or str(k).replace("Key.","")
    except: return str(k).replace("Key.","")

@lru_cache(maxsize=256)
def disp(label):
    return DISPLAY_NAMES.get(label, label.upper() if len(label)==1 else label.title())

def _btn(parent, text, command, bg, fg=TEXT, afg=None, **kw):
    kw.setdefault("relief","flat"); kw.setdefault("cursor","hand2"); kw.setdefault("bd",0)
    kw.setdefault("activebackground", bg); kw.setdefault("activeforeground", afg or fg)
    kw.setdefault("font", FONT_MD)
    return tk.Button(parent, text=text, command=command, bg=bg, fg=fg, **kw)

def _lbl(parent, text, bg, fg=MUTED, **kw):
    kw.setdefault("font", ("Segoe UI",11)); kw.setdefault("anchor","w")
    return tk.Label(parent, text=text, bg=bg, fg=fg, **kw)

def _panel(parent, pady, divider=False):
    """PANEL_BG bar with a padded inner content frame — shared shell for the
    status bar and the global keybind/delay bar. `divider` adds a 1px line
    below the whole bar (used by the status bar, not the keybind/delay one)."""
    bar = tk.Frame(parent, bg=PANEL_BG); bar.pack(side="top", fill="x")
    inner = tk.Frame(bar, bg=PANEL_BG); inner.pack(fill="x", padx=12, pady=pady)
    if divider: tk.Frame(parent, bg=DIVIDER, height=1).pack(fill="x")
    return inner

class Settings:
    """Owns prefs.json — nothing else in the app reads or writes it directly."""
    def __init__(self):
        try:
            with open(PREFS_PATH, encoding="utf-8") as f: self._data = json.load(f)
        except Exception:
            self._data = {}
    def save(self, app_count, keybinds, delay_ms, app_states):
        data = {"app_count": app_count, "keybinds": list(keybinds), "delay_ms": delay_ms}
        data.update({f"ch{i}": s.to_dict() for i, s in enumerate(app_states)})
        self._data = data
        try:
            os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
            with open(PREFS_PATH, "w", encoding="utf-8") as f: json.dump(data, f, indent=2)
        except Exception:
            pass
    @property
    def app_count(self): return self._data.get("app_count", self._data.get("channel_count", 1))
    @property
    def keybinds(self): return list(self._data.get("keybinds", []))
    @property
    def delay_ms(self): return int(self._data.get("delay_ms", 250))
    def app_dict(self, i): return self._data.get(f"ch{i}", {})

def is_admin():
    try: return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception: return False

def restart_self():
    """Re-execs the current (already-elevated) process in place — no new
    UAC prompt needed, since the child inherits this process's token.
    Raises if the exec itself fails; caller decides how to surface that."""
    os.environ[RESTART_ENV_VAR] = "1"
    os.execv(sys.executable, [sys.executable] + sys.argv[1:])

@dataclass
class AppState:
    uid: str = field(default_factory=lambda: str(uuid.uuid4()))  # runtime only
    app_path: str = ""
    @staticmethod
    def from_dict(d): return AppState(app_path=d.get("app_path",""))
    def to_dict(self): return {"app_path": self.app_path}

def app_rule_names(app_path):
    h = hashlib.md5(app_path.encode("utf-8")).hexdigest()[:12]
    return f"{FW_PREFIX}_{h}_out", f"{FW_PREFIX}_{h}_in"

# ── firewall backend ────────────────────────────────────────────────────────
# In-process COM only. Rule toggles are property sets on live COM objects —
# no process spawn in the hot path.

class FirewallBackend:
    def __init__(self, fw): self._fw = fw
    def _new_rule(self, name, direction, app_path):
        try: self._fw.Rules.Remove(name)  # clear any stale same-named rule first
        except Exception: pass
        rule = com.CreateObject("HNetCfg.FWRule")
        rule.Name=name; rule.Direction=direction; rule.Action=NET_FW_ACTION_BLOCK
        rule.ApplicationName=app_path; rule.Enabled=False; rule.Profiles=NET_FW_PROFILE2_ALL
        self._fw.Rules.Add(rule)
        return rule
    def new_rule_pair(self, app_path):
        out_name, in_name = app_rule_names(app_path)
        try:
            return (self._new_rule(out_name, NET_FW_RULE_DIR_OUT, app_path),
                    self._new_rule(in_name,  NET_FW_RULE_DIR_IN,  app_path))
        except Exception:
            self.remove_by_name(out_name); self.remove_by_name(in_name)
            return None
    def set_enabled(self, handle, enabled):
        try: handle[0].Enabled = enabled; handle[1].Enabled = enabled
        except Exception: pass
    def remove_by_name(self, name):
        try: self._fw.Rules.Remove(name); return True
        except Exception: return False
    def list_rule_names(self):
        names = set()
        try:
            for rule in self._fw.Rules:
                try:
                    if rule.Name and rule.Name.startswith(FW_PREFIX+"_"): names.add(rule.Name)
                except Exception: continue
        except Exception:
            pass
        return names

def create_backend():
    """Creates the in-process Windows Firewall COM backend. Raises on failure —
    there's no netsh fallback, so the caller surfaces the error (App auto-
    restarts once via restart_self(), then gives up and tells the user)."""
    if com is None:
        raise RuntimeError("comtypes is not installed (pip install comtypes)")
    return FirewallBackend(com.CreateObject("HNetCfg.FwPolicy2"))

def cleanup_all_rules(backend):
    names = backend.list_rule_names()
    for n in names: backend.remove_by_name(n)
    return len(names)

def sweep_orphan_rules(backend, keep_app_paths):
    """Deletes rules that don't belong to any currently configured app — runs on
    startup so a crash/force-kill can't leave permanent clutter behind."""
    keep = set()
    for p in keep_app_paths:
        if p: keep.update(app_rule_names(p))
    removed = 0
    for n in backend.list_rule_names():
        if n not in keep and backend.remove_by_name(n): removed += 1
    return removed

class NetGate:
    """Owns one app's in/out rule pair. set_blocked() is the hot path (every key
    press/release) — just an in-process COM property set."""
    def __init__(self, backend):
        self._backend = backend; self._app_path = None; self._handle = None
    def activate(self, app_path):
        self.deactivate()
        handle = self._backend.new_rule_pair(app_path)
        if handle is None: return False
        self._app_path, self._handle = app_path, handle
        return True
    def set_blocked(self, blocked):
        if self._handle is not None: self._backend.set_enabled(self._handle, blocked)
    def deactivate(self):
        if not self._app_path: return
        for name in app_rule_names(self._app_path): self._backend.remove_by_name(name)
        self._app_path = self._handle = None

class FirewallService:
    """Background thread owning all firewall state. Arm/disarm go through block_batch
    so disconnecting/reconnecting N apps is one queue message + one UI callback, not N."""
    def __init__(self, root, on_error=None):
        self._root = root; self._q = queue.Queue(); self._gates = {}; self._backend = None
        self._on_error = on_error
    def send(self, cmd, arg=None): self._q.put((cmd, arg))
    def start(self): threading.Thread(target=self._run, daemon=True).start()
    def _run(self):
        CoInitialize()
        try:
            try:
                self._backend = create_backend()
            except Exception as e:
                if self._on_error: self._root.after(0, self._on_error, str(e))
                return
            while True:
                cmd, arg = self._q.get()  # blocks — no periodic wakeup needed
                if cmd == "quit":
                    for g in self._gates.values(): g.deactivate()
                    break
                self._handle(cmd, arg)
        finally:
            CoUninitialize()
    def _handle(self, cmd, arg):
        if cmd == "attach":
            uid, app_path, cb = arg
            g = self._gates.setdefault(uid, NetGate(self._backend))
            g.deactivate()
            ok = g.activate(app_path) if app_path else None
            self._root.after(0, cb, ok)
        elif cmd == "remove":
            g = self._gates.pop(arg, None)
            if g: g.deactivate()
        elif cmd == "block_batch":
            uids, blocked, cb = arg
            t0 = time.perf_counter()
            for uid in uids:
                g = self._gates.get(uid)
                if g: g.set_blocked(blocked)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if cb: self._root.after(0, cb, uids, blocked, elapsed_ms)
        elif cmd == "cleanup":
            self._root.after(0, arg, cleanup_all_rules(self._backend))
        elif cmd == "sweep":
            sweep_orphan_rules(self._backend, arg)

def enumerate_running_apps():
    """Running processes with a resolvable exe path -> {exe_path: display_name}.
    Pure psutil, no COM involved — callers should run this on its own throwaway
    thread rather than the FirewallService queue, so a slow scan can never
    delay a pending block_batch behind it."""
    result, name_counts, seen = {}, {}, set()
    if psutil is None: return result
    for p in psutil.process_iter(["name","exe"]):
        try:
            exe, name = p.info.get("exe"), p.info.get("name")
            if not exe or not name or exe in seen: continue
            seen.add(exe)
            name_counts[name] = name_counts.get(name,0)+1
            result[exe] = f"[{name_counts[name]}] {name}" if name_counts[name]>1 else name
        except Exception:
            pass
    return dict(sorted(result.items(), key=lambda kv: kv[1].lower()))

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
                t = threading.Timer(delay/1000.0, self._fire); t.daemon = True
                self._timer = t; immediate = False
        if immediate: self._on_fire()
        else: t.start()
    def _fire(self):
        with self._lock:
            if self._timer is None: return  # cancelled between firing and lock acquisition
            self._timer = None
        self._on_fire()
    def cancel(self):
        with self._lock:
            if self._timer: self._timer.cancel(); self._timer = None

class AppEntry:
    """Plain state/status holder — App + ReconnectScheduler drive all transitions."""
    def __init__(self, state, index):
        self.state = state; self.index = index
        self.status_text, self.status_color = "● INACTIVE", MUTED
        self.on_status_change = None
    def set_status(self, text, color):
        self.status_text, self.status_color = text, color
        if self.on_status_change: self.on_status_change(text, color)

NONE_OPTION = "NONE"

class AppRow:
    """One compact row: reorder arrows, app picker, status, remove. The
    picker is a plain readonly combobox — filtering now happens once, up in
    the header search box, which narrows what every row's dropdown offers.
    (Earlier revisions tried filtering per-row, live, as you typed into each
    combobox — first by forcing ttk's native popdown to redraw mid-keystroke,
    then via inline autocomplete, then a custom popup Listbox. All three
    were real complexity for a job a single shared filter does more simply.)
    refresh_apps() takes both the full process map (to correctly tell "not
    running" apart from "just filtered out") and the header-filtered map
    (what the dropdown actually offers)."""
    def __init__(self, entry, on_select, on_move, on_remove, on_refresh):
        self._entry, self._on_select = entry, on_select
        self._on_move, self._on_remove, self._on_refresh = on_move, on_remove, on_refresh
        self._display_to_path = {}   # display text -> app_path, rebuilt each refresh
        self.frame = self._idxlbl = self._svar = self._slbl = self._combo = None
        self._btn_u = self._btn_d = None
    def build(self, parent):
        self.frame = tk.Frame(parent, bg=COL_BG); self.frame.pack(fill="x", pady=3)
        inner = tk.Frame(self.frame, bg=COL_BG); inner.pack(fill="x", ipady=6, padx=2)
        self._idxlbl = _lbl(inner, self._idx_text(), COL_BG, font=FONT_SM, width=3, anchor="e")
        self._idxlbl.pack(side="left", padx=(6,2))
        mv = tk.Frame(inner, bg=COL_BG); mv.pack(side="left", padx=(0,6))
        self._btn_u = _btn(mv, "▲", lambda: self._on_move(self._entry,-1), COL_BG, MUTED, TEXT, font=FONT_XS, width=2)
        self._btn_u.pack()
        self._btn_d = _btn(mv, "▼", lambda: self._on_move(self._entry,+1), COL_BG, MUTED, TEXT, font=FONT_XS, width=2)
        self._btn_d.pack()
        self._combo = ttk.Combobox(inner, state="readonly", font=FONT_MD, height=50)
        self._combo["values"] = [NONE_OPTION]; self._combo.current(0)
        self._combo.pack(side="left", fill="x", expand=True, padx=4)
        self._combo.bind("<<ComboboxSelected>>", self._on_app_selected)
        _btn(inner, "⟳", lambda: self._on_refresh(), COL_BG, MUTED, TEXT, font=FONT_MD).pack(side="left", padx=2)
        self._svar = tk.StringVar(value=self._entry.status_text)
        self._slbl = _lbl(inner, "", COL_BG, textvariable=self._svar, font=FONT_SM_B, width=13, anchor="e")
        self._slbl.pack(side="left", padx=4); self._slbl.config(fg=self._entry.status_color)
        _btn(inner, "✕", lambda: self._on_remove(self._entry), COL_BG, TEXT, ACCENT_OFF, font=FONT_ICON).pack(side="left", padx=(2,6))
        self._entry.on_status_change = self.set_status
        return self.frame
    def update_arrows(self, is_first, is_last):
        if self._btn_u: self._btn_u.config(state="disabled" if is_first else "normal")
        if self._btn_d: self._btn_d.config(state="disabled" if is_last  else "normal")
    def _idx_text(self): return f"{self._entry.index+1}."
    def set_index(self, i):
        self._entry.index = i
        if self._idxlbl: self._idxlbl.config(text=self._idx_text())
    def set_status(self, text, color):
        if self._svar: self._svar.set(text)
        if self._slbl: self._slbl.config(fg=color)
    def refresh_apps(self, app_map, filtered_map):
        self._display_to_path = {name: path for path, name in filtered_map.items()}
        values = [NONE_OPTION] + list(filtered_map.values())
        path = self._entry.state.app_path
        if path in app_map:
            display = app_map[path]
            if display not in self._display_to_path:
                # currently selected but filtered out by the search box —
                # keep it visible/selectable rather than pretending it's gone
                values.append(display); self._display_to_path[display] = path
            current = display
        elif path:
            tag = f"(not running) {path}"
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
    def destroy(self):
        self._entry.on_status_change = None
        if self.frame: self.frame.destroy(); self.frame = None

class KeybindManager:
    """Owns the pynput listeners + held-keys set. Keyboard and mouse each run on
    their own pynput thread, so _dispatch() can be entered concurrently from both —
    the lock guards `_active` and the capture flag. on_arm/on_disarm fire on the
    empty<->non-empty transitions of the held-keys set, marshaled onto the Tk thread."""
    def __init__(self, root, keybinds, on_arm, on_disarm, on_capture_done):
        self._root = root; self._keybinds = list(keybinds)
        self._on_arm, self._on_disarm, self._on_capture_done = on_arm, on_disarm, on_capture_done
        self._active = set(); self._capturing = False
        self._lock = threading.Lock(); self._kb = self._ms = None
    @property
    def keybinds(self): return list(self._keybinds)
    def start(self):
        self._kb = keyboard.Listener(
            on_press=  lambda k: self._dispatch(key_label(k), True),
            on_release=lambda k: self._dispatch(key_label(k), False))
        self._kb.start()
        self._ms = mouse.Listener(on_click=lambda x,y,b,p: self._dispatch(MOUSE_LABELS.get(b,str(b)), p))
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
    def _dispatch(self, label, pressed):
        if pressed and self._capturing:
            with self._lock:
                if not self._capturing: return
                self._capturing = False
                if label and label not in self._keybinds: self._keybinds.append(label)
            self._root.after(0, self._on_capture_done)
            return
        if label not in self._keybinds: return
        fire_arm = fire_disarm = False
        with self._lock:
            if pressed:
                fire_arm = not self._active; self._active.add(label)
            else:
                self._active.discard(label); fire_disarm = not self._active
        if fire_arm:    self._root.after(0, self._on_arm)
        if fire_disarm: self._root.after(0, self._on_disarm)

class MainWindow:
    """Owns every Tk widget and pure layout — the disconnect/reconnect timing
    displays, the keybind/delay bar, the app search box, the app-row
    container, and window (re)sizing. Nothing here knows about firewall
    state, settings, or pynput; user actions are reported back through the
    callbacks given at construction, and App drives visible state through
    the methods below (set_disconnect_timing, rebuild_keybinds, …).
    Same pattern AppRow/KeybindManager/FirewallService already use."""
    def __init__(self, root, *, initial_delay, initial_keybinds, on_add_app, on_add_key,
                 on_remove_key, on_clear_rules, on_delay_change, on_delay_commit, on_search_change,
                 restarted=False):
        self.root = root; self._on_remove_key = on_remove_key
        self._was_zoomed = False
        root.bind("<Configure>", self._on_root_configure)
        style = ttk.Style(); style.theme_use("default")
        style.configure("TCombobox", fieldbackground=PANEL_BG, background=PANEL_BG,
                        foreground=TEXT, selectbackground=PANEL_BG, selectforeground=TEXT,
                        bordercolor="#444", arrowcolor=TEXT)
        style.map("TCombobox", fieldbackground=[("readonly",PANEL_BG)], foreground=[("readonly",TEXT)])
        outer = tk.Frame(root, bg=APP_BG); outer.pack(fill="both", expand=True)
        self._outer = outer
        self._build_status_bar(outer)
        self._build_global_bar(outer, initial_delay, on_add_key, on_delay_change, on_delay_commit, restarted)
        hdr = tk.Frame(outer, bg=APP_BG); hdr.pack(fill="x", padx=12, pady=(10,4))
        _lbl(hdr, "Apps", APP_BG, font=FONT_LG_B).pack(side="left")
        _btn(hdr, "+ Add App", lambda: on_add_app(), "#3a3a3a").pack(side="right")
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: on_search_change(self._search_var.get()))
        search = tk.Entry(hdr, textvariable=self._search_var, bg=PANEL_BG, fg=TEXT,
                           insertbackground=TEXT, relief="flat", font=FONT_MD)
        search.pack(side="left", fill="x", expand=True, padx=10, ipady=3)
        self.rows_frame = tk.Frame(outer, bg=APP_BG); self.rows_frame.pack(fill="both", expand=True, padx=12, pady=(0,4))
        bottom = tk.Frame(outer, bg=APP_BG); bottom.pack(side="bottom", fill="x")
        _btn(bottom, "Clear All Firewall Rules", lambda: on_clear_rules(), APP_BG, MUTED, ACCENT_OFF,
             font=FONT_SM).pack(side="left", padx=6, pady=4)
        ttk.Sizegrip(bottom).pack(side="right")
        self.rebuild_keybinds(initial_keybinds)
    def _build_status_bar(self, parent):
        inner = _panel(parent, pady=6, divider=True)
        self._disconnect_var = tk.StringVar(value="Last disconnect: —")
        self._reconnect_var = tk.StringVar(value="Last reconnect: —")
        _lbl(inner, "", PANEL_BG, textvariable=self._disconnect_var, font=FONT_SM).pack(side="left")
        _lbl(inner, "", PANEL_BG, textvariable=self._reconnect_var, font=FONT_SM).pack(side="right")
    def _build_global_bar(self, parent, initial_delay, on_add_key, on_delay_change, on_delay_commit, restarted):
        inner = _panel(parent, pady=8, divider=False)
        self._build_keybind_row(inner, on_add_key)
        tk.Frame(inner, bg=DIVIDER, height=1).pack(fill="x", pady=8)
        self._build_delay_row(inner, initial_delay, on_delay_change, on_delay_commit, restarted)
    def _build_keybind_row(self, inner, on_add_key):
        krow = tk.Frame(inner, bg=PANEL_BG); krow.pack(fill="x")
        _lbl(krow, "Global Keybind (holds ALL apps)", PANEL_BG, font=FONT_LG_B).pack(side="left")
        self._addkeybtn = _btn(krow, "+ Add Key", lambda: on_add_key(), "#3a3a3a"); self._addkeybtn.pack(side="left", padx=(8,0))
        self._capturelbl = tk.Label(krow, text="", bg=PANEL_BG, fg=ACCENT, font=FONT_SM); self._capturelbl.pack(side="left", padx=(8,0))
        self._gkb_frame = tk.Frame(inner, bg=PANEL_BG); self._gkb_frame.pack(fill="x", pady=(6,0))
    def _build_delay_row(self, inner, initial_delay, on_delay_change, on_delay_commit, restarted):
        drow = tk.Frame(inner, bg=PANEL_BG); drow.pack(fill="x")
        if restarted:
            _lbl(drow, "⟳ Restarted after a firewall init failure", PANEL_BG, ACCENT_OFF,
                 font=FONT_SM).pack(side="left", padx=(0,10))
        _lbl(drow, "Reconnect Delay (ms)", PANEL_BG, font=FONT_LG_B).pack(side="left")
        self._dvar = tk.IntVar(value=initial_delay)
        self._dvar.trace_add("write", lambda *_: on_delay_change(self._dvar.get()))
        scale = tk.Scale(inner, variable=self._dvar, from_=0, to=10000, orient="horizontal",
                 bg=PANEL_BG, fg=TEXT, troughcolor=COL_BG, highlightthickness=0,
                 bd=0, activebackground=ACCENT, font=FONT_SM, sliderlength=40, width=16)
        scale.pack(fill="x", pady=(2,0))
        scale.bind("<ButtonRelease-1>", lambda _: on_delay_commit())
        scale.bind("<Button-2>", lambda e: "break")
        scale.bind("<Button-3>", lambda e: "break")
    def _on_root_configure(self, _=None):
        zoomed = self.root.state() == "zoomed"
        if self._was_zoomed and not zoomed: self.fit()
        self._was_zoomed = zoomed
    def fit(self):
        self.root.update_idletasks()
        h = max(self._outer.winfo_reqheight(), MIN_H)
        self.root.minsize(WIN_W, MIN_H); self.root.geometry(f"{WIN_W}x{h}")
    def set_disconnect_timing(self, text):
        self._disconnect_var.set(text)
    def set_reconnect_timing(self, text):
        self._reconnect_var.set(text)
    def set_capturing(self, active):
        self._addkeybtn.config(state="disabled" if active else "normal")
        self._capturelbl.config(text="Press a key…" if active else "")
    def rebuild_keybinds(self, keybinds):
        for w in self._gkb_frame.winfo_children(): w.destroy()
        if not keybinds:
            _lbl(self._gkb_frame, "No key bound yet — add one above.", PANEL_BG, font=FONT_SM).pack(side="left")
            return
        for label in keybinds:
            r = tk.Frame(self._gkb_frame, bg=COL_BG); r.pack(side="left", padx=(0,6))
            tk.Label(r, text=disp(label), bg=COL_BG, fg=TEXT, font=FONT_MD).pack(side="left", padx=(8,2), pady=3)
            _btn(r, "✕", lambda l=label: self._on_remove_key(l), COL_BG, MUTED, ACCENT_OFF,
                 font=FONT_SM).pack(side="left", padx=(0,6))

class App:
    """Owns firewall/keybind/settings state and drives MainWindow; knows
    nothing about how a widget is built, only which MainWindow method to
    call when something changes."""
    def __init__(self, root):
        self.root = root
        root.title(f"PushToLag v{VERSION}")
        root.resizable(True,True)
        root.protocol("WM_DELETE_WINDOW", self._quit)
        self._settings = Settings()
        self._apps: list[AppEntry] = []
        self._rows: list[AppRow] = []
        self._g_delay = self._settings.delay_ms
        self._last_app_map = {}
        self._search_query = ""
        self._fw = FirewallService(root, on_error=self._on_backend_error)
        self._fw.start()
        self._reconnect = ReconnectScheduler(lambda: self._g_delay, self._on_reconnect_fire)
        self._keys = KeybindManager(root, self._settings.keybinds,
                                     on_arm=self._on_arm, on_disarm=self._on_disarm,
                                     on_capture_done=self._on_capture_done)
        self._ui = MainWindow(root, initial_delay=self._g_delay, initial_keybinds=self._keys.keybinds,
                               on_add_app=self._add, on_add_key=self._on_add_key,
                               on_remove_key=self._rm_key, on_clear_rules=self._clear_all_rules,
                               on_delay_change=self._on_delay_change, on_delay_commit=self._save,
                               on_search_change=self._on_search_change,
                               restarted=os.environ.get(RESTART_ENV_VAR) == "1")
        self._restore()
        self._keys.start()
        root.update_idletasks(); self._ui.fit()
        self._enumerate_all()
        self._fw.send("sweep", [e.state.app_path for e in self._apps])
        try:
            base = getattr(sys,"_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            root.wm_iconbitmap(os.path.join(base,"PushToLagIcon.ico"))
        except Exception:
            pass
    def _on_add_key(self):
        if not self._keys.begin_capture(): return
        self._ui.set_capturing(True)
    def _on_capture_done(self):
        self._save(); self._ui.rebuild_keybinds(self._keys.keybinds)
        self._ui.set_capturing(False)
    def _rm_key(self, label):
        now_empty = self._keys.remove(label)
        self._save()
        if now_empty:
            self._reconnect.cancel(); self._on_disarm()
        self._ui.rebuild_keybinds(self._keys.keybinds)
    def _clear_all_rules(self):
        if not messagebox.askyesno("Clear All Firewall Rules",
                "This deletes every firewall rule PushToLag has created (including ones "
                "from previous sessions). Any currently disconnected app will reconnect. Continue?"):
            return
        self._fw.send("cleanup", lambda n: messagebox.showinfo("Done", f"Removed {n} rule(s)."))
    def _on_backend_error(self, message):
        # try exactly one auto-restart (the env var makes it a one-shot, not
        # a crash loop); if we already restarted once and it failed again,
        # give up and tell the user instead of retrying forever
        if os.environ.get(RESTART_ENV_VAR) != "1":
            try:
                restart_self(); return
            except Exception:
                pass
        messagebox.showerror("Windows Firewall unavailable",
            f"Couldn't initialize the Windows Firewall COM API (even after an automatic "
            f"restart):\n\n{message}\n\nMake sure the Windows Defender Firewall service is running.")

    def _apply_app_map(self, app_map):
        self._last_app_map = app_map
        self._apply_filter()
    def _on_search_change(self, query):
        self._search_query = query
        self._apply_filter()
    def _apply_filter(self):
        q = self._search_query.strip().lower()
        filtered = self._last_app_map if not q else \
            {path: name for path, name in self._last_app_map.items() if q in name.lower()}
        for w in self._rows: w.refresh_apps(self._last_app_map, filtered)
    def _enumerate_all(self):
        # Runs off the firewall thread's queue so a slow process scan can
        # never delay a pending hotkey toggle.
        def worker():
            app_map = enumerate_running_apps()
            self.root.after(0, self._apply_app_map, app_map)
        threading.Thread(target=worker, daemon=True).start()
    # arm/disarm are batched across every configured app instead of looping per-entry
    def _on_arm(self):
        self._reconnect.cancel()
        uids = [e.state.uid for e in self._apps if e.state.app_path]
        if not uids: return
        self._fw.send("block_batch", (uids, True, self._on_block_timing))
        for e in self._apps:
            if e.state.app_path: e.set_status("● DISCONNECTED", ACCENT_OFF)
    def _on_disarm(self):
        self._reconnect.schedule()
    def _on_reconnect_fire(self):
        self.root.after(0, self._do_reconnect)  # hop from Timer thread to Tk thread
    def _do_reconnect(self):
        uids = [e.state.uid for e in self._apps if e.state.app_path]
        if not uids: return
        self._fw.send("block_batch", (uids, False, self._on_block_timing))
        for e in self._apps:
            if e.state.app_path: e.set_status("● CONNECTED", ACCENT)
    def _on_block_timing(self, uids, blocked, elapsed_ms):
        action = "disconnect" if blocked else "reconnect"
        text = f"Last {action} ({len(uids)} app{'s' if len(uids)!=1 else ''}): {elapsed_ms:.2f} ms"
        if blocked: self._ui.set_disconnect_timing(text)
        else: self._ui.set_reconnect_timing(text)
    def _on_delay_change(self, value):
        self._g_delay = value
    def _move(self, entry, direction):
        idx = self._apps.index(entry); new_idx = idx + direction
        if new_idx < 0 or new_idx >= len(self._apps): return
        self._apps[idx], self._apps[new_idx] = self._apps[new_idx], self._apps[idx]
        self._rows[idx],  self._rows[new_idx]  = self._rows[new_idx],  self._rows[idx]
        for i,(a,w) in enumerate(zip(self._apps, self._rows)): a.index = i; w.set_index(i)
        for w in self._rows: w.frame.pack_forget()
        for w in self._rows: w.frame.pack(fill="x", pady=3)
        self._refresh_arrows(); self._save()
    def _refresh_arrows(self):
        n = len(self._rows)
        for i,w in enumerate(self._rows): w.update_arrows(is_first=i==0, is_last=i==n-1)
    def _restore(self):
        for i in range(self._settings.app_count):
            self._add(AppState.from_dict(self._settings.app_dict(i)), restored=True)
    def _add(self, state=None, restored=False):
        state = state or AppState()
        entry = AppEntry(state, len(self._apps))
        row = AppRow(entry, on_select=self._set_app, on_move=self._move,
                     on_remove=self._remove, on_refresh=self._enumerate_all)
        self._apps.append(entry); self._rows.append(row)
        row.build(self._ui.rows_frame)
        self._refresh_arrows(); self._ui.fit()
        if restored: self._attach(entry)
        else: self._save(); self._enumerate_all()
    def _set_app(self, entry, app_path):
        entry.state.app_path = app_path
        self._attach(entry); self._save()
    def _attach(self, entry):
        def cb(ok):
            if ok is None:  entry.set_status("● INACTIVE",   MUTED)
            elif ok:        entry.set_status("● CONNECTED",  ACCENT)
            else:           entry.set_status("⚠ Rule error", ACCENT_OFF)
        self._fw.send("attach", (entry.state.uid, entry.state.app_path, cb))
    def _remove(self, entry):
        idx = self._apps.index(entry)
        self._apps.pop(idx); row = self._rows.pop(idx)
        self._fw.send("remove", entry.state.uid)
        row.destroy()
        for i,(a,w) in enumerate(zip(self._apps, self._rows)): a.index = i; w.set_index(i)
        self._save(); self._enumerate_all(); self._refresh_arrows(); self._ui.fit()
    def _save(self):
        self._settings.save(len(self._apps), self._keys.keybinds, self._g_delay,
                             [e.state for e in self._apps])
    def _quit(self):
        self._save(); self._reconnect.cancel(); self._keys.stop()
        self._fw.send("quit"); self.root.destroy()

def main():
    if os.name != "nt":
        print("PushToLag only works on Windows (it drives Windows Firewall)."); return
    if psutil is None:
        print("Missing dependency: run  pip install psutil"); return
    if com is None:
        print("Missing dependency: run  pip install comtypes"); return
    if not is_admin():
        ctypes.windll.user32.MessageBoxW(0,
            "PushToLag needs to run elevated to manage Windows Firewall rules.\n\n"
            "Launch it via a shortcut set to \"Run as administrator\".",
            "PushToLag — Elevation required", 0x30)  # MB_ICONWARNING
        return
    try:
        from ctypes import windll; windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk(); root.minsize(WIN_W, MIN_H); App(root); root.mainloop()

if __name__ == "__main__": main()
