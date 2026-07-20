"""
PushToLag v1.2 — Push-to-Disconnect for Windows 11
Hold ONE global key to cut network access to every app you've added, release to
reconnect them all after a shared delay. Uses Windows Firewall rules per app,
driven entirely through the in-process Windows Firewall COM API (no netsh.exe
shelling out — every rule toggle is just a property set).

v1.2 adds an optional on-screen overlay: a small square (green/red, both
colors user-configurable) that shows whether the network is currently
connected or disconnected.

pip install pynput psutil comtypes

Must run elevated (Administrator) — launch it via a shortcut set to "Run as administrator".
It no longer self-elevates; if launched without admin rights it warns and exits.
"""
import os, sys, json, uuid, queue, threading, hashlib, ctypes, time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Optional
import tkinter as tk
from tkinter import ttk, messagebox, colorchooser
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

VERSION = "1.3"

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
AUTO_REFRESH_MS = 5000  # how often to re-scan running processes, to keep status pills/overlay honest

# ── screen overlay (Windows only; degrades to a plain window elsewhere) ─────
try:
    _user32 = ctypes.windll.user32
    HAVE_WIN32 = True
except AttributeError:
    _user32 = None
    HAVE_WIN32 = False

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WDA_EXCLUDEFROMCAPTURE = 0x00000011
LWA_COLORKEY = 0x00000001

if HAVE_WIN32:
    _user32.GetWindowLongPtrW.restype = ctypes.c_void_p
    _user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.SetWindowLongPtrW.restype = ctypes.c_void_p
    _user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    _user32.SetWindowDisplayAffinity.restype = ctypes.c_bool
    _user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _user32.SetLayeredWindowAttributes.restype = ctypes.c_bool
    _user32.SetLayeredWindowAttributes.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_ubyte, ctypes.c_uint32
    ]

OVERLAY_POSITIONS = ("Top-left", "Top-right", "Bottom-left", "Bottom-right")
DEFAULT_OVERLAY_POSITION = "Top-right"
DEFAULT_OVERLAY_OFFSET = 24
DEFAULT_OVERLAY_COLORS = {"connected": ACCENT, "disconnected": ACCENT_OFF}
DEFAULT_OVERLAY_SIZE = 12
OVERLAY_SIZE_MIN, OVERLAY_SIZE_MAX = 4, 64

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
    def save(self, app_count, keybinds, delay_ms, app_states, search_query, overlay):
        data = {"app_count": app_count, "keybinds": list(keybinds), "delay_ms": delay_ms,
                "search_query": search_query}
        data.update({f"ch{i}": s.to_dict() for i, s in enumerate(app_states)})
        data.update(overlay)
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
    def delay_ms(self): return max(0, min(10000, int(self._data.get("delay_ms", 250))))
    @property
    def search_query(self): return self._data.get("search_query", "")
    def app_dict(self, i): return self._data.get(f"ch{i}", {})
    @property
    def overlay_enabled(self): return bool(self._data.get("overlay_enabled", False))
    @property
    def overlay_position(self): return self._data.get("overlay_position", DEFAULT_OVERLAY_POSITION)
    @property
    def overlay_offset_x(self): return int(self._data.get("overlay_offset_x", DEFAULT_OVERLAY_OFFSET))
    @property
    def overlay_offset_y(self): return int(self._data.get("overlay_offset_y", DEFAULT_OVERLAY_OFFSET))
    def overlay_color(self, state): return self._data.get(f"overlay_color_{state}", DEFAULT_OVERLAY_COLORS[state])
    @property
    def overlay_size(self): return int(self._data.get("overlay_size", DEFAULT_OVERLAY_SIZE))

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
        try:
            handle[0].Enabled = enabled; handle[1].Enabled = enabled
            return True
        except Exception:
            return False
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
    press/release) — just an in-process COM property set. Tracks the last
    applied state so a repeat call for a state we're already in is a no-op
    instead of a redundant COM write."""
    def __init__(self, backend):
        self._backend = backend; self._app_path = None; self._handle = None
        self._blocked = False
    def activate(self, app_path):
        self.deactivate()
        handle = self._backend.new_rule_pair(app_path)
        if handle is None: return False
        self._app_path, self._handle = app_path, handle
        self._blocked = False
        return True
    def set_blocked(self, blocked):
        blocked = bool(blocked)
        if self._handle is None: return False
        if self._blocked == blocked: return True
        ok = self._backend.set_enabled(self._handle, blocked)
        if ok: self._blocked = blocked
        return ok
    def deactivate(self):
        if not self._app_path: return
        for name in app_rule_names(self._app_path): self._backend.remove_by_name(name)
        self._app_path = self._handle = None
        self._blocked = False

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
                if cmd == "block_batch":
                    # Coalesce back-to-back block_batch requests: if more are
                    # already queued behind this one, jump straight to the
                    # newest (running any other queued commands found along
                    # the way) so a rapid hotkey tap/release/tap can't build a
                    # backlog of stale, already-superseded COM transitions.
                    quit_pending = False
                    while True:
                        try: nxt_cmd, nxt_arg = self._q.get_nowait()
                        except queue.Empty: break
                        if nxt_cmd == "block_batch": cmd, arg = nxt_cmd, nxt_arg
                        elif nxt_cmd == "quit": quit_pending = True
                        else: self._handle(nxt_cmd, nxt_arg)
                    self._handle(cmd, arg)
                    if quit_pending:
                        for g in self._gates.values(): g.deactivate()
                        break
                    continue
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
            ok = True
            for uid in uids:
                g = self._gates.get(uid)
                if g is None or not g.set_blocked(blocked): ok = False
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if cb: self._root.after(0, cb, uids, blocked, elapsed_ms, ok)
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

class ProcessScanner:
    """Long-lived worker for running-process discovery: one persistent thread
    woken by an Event, rather than a fresh threading.Thread spawned on every
    refresh. Repeated requests (e.g. app-add, search refresh, manual refresh
    firing close together) coalesce into a single scan instead of piling up
    redundant psutil sweeps concurrently."""
    def __init__(self, root, on_result):
        self._root = root; self._on_result = on_result
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
            if not self._stop.is_set(): self._root.after(0, self._on_result, app_map)

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
        self.attached = None  # None: no app selected, True: rule attached ok, False: rule error
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
    def __init__(self, entry, on_select, on_move, on_remove):
        self._entry, self._on_select = entry, on_select
        self._on_move, self._on_remove = on_move, on_remove
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
                 on_remove_key, on_clear_rules, on_delay_change, on_delay_commit, on_search_change, on_refresh,
                 initial_overlay_enabled, initial_overlay_position, initial_overlay_offset_x,
                 initial_overlay_offset_y, initial_overlay_size, initial_overlay_color_connected,
                 initial_overlay_color_disconnected,
                 on_overlay_toggle, on_overlay_position_change, on_overlay_offset_change,
                 on_overlay_size_change, on_overlay_color_click,
                 initial_search="", restarted=False):
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
        self._build_global_bar(outer, initial_delay, on_add_key, on_delay_change, on_delay_commit, restarted,
                                initial_overlay_enabled, initial_overlay_position, initial_overlay_offset_x,
                                initial_overlay_offset_y, initial_overlay_size, initial_overlay_color_connected,
                                initial_overlay_color_disconnected, on_overlay_toggle,
                                on_overlay_position_change, on_overlay_offset_change, on_overlay_size_change,
                                on_overlay_color_click)
        hdr = tk.Frame(outer, bg=APP_BG); hdr.pack(fill="x", padx=12, pady=(10,4))
        _lbl(hdr, "Apps", APP_BG, font=FONT_LG_B).pack(side="left")
        _btn(hdr, "+ Add App", lambda: on_add_app(), "#3a3a3a").pack(side="right")
        _btn(hdr, "⟳ Refresh", lambda: on_refresh(), "#3a3a3a").pack(side="right", padx=(0,6))
        self._search_var = tk.StringVar(value=initial_search)
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
    def _build_global_bar(self, parent, initial_delay, on_add_key, on_delay_change, on_delay_commit, restarted,
                           initial_overlay_enabled, initial_overlay_position, initial_overlay_offset_x,
                           initial_overlay_offset_y, initial_overlay_size, initial_overlay_color_connected,
                           initial_overlay_color_disconnected, on_overlay_toggle,
                           on_overlay_position_change, on_overlay_offset_change, on_overlay_size_change,
                           on_overlay_color_click):
        inner = _panel(parent, pady=8, divider=False)
        self._build_keybind_row(inner, on_add_key)
        tk.Frame(inner, bg=DIVIDER, height=1).pack(fill="x", pady=8)
        self._build_delay_row(inner, initial_delay, on_delay_change, on_delay_commit, restarted)
        tk.Frame(inner, bg=DIVIDER, height=1).pack(fill="x", pady=8)
        self._build_overlay_row(inner, initial_overlay_enabled, initial_overlay_position,
                                 initial_overlay_offset_x, initial_overlay_offset_y, initial_overlay_size,
                                 initial_overlay_color_connected, initial_overlay_color_disconnected,
                                 on_overlay_toggle, on_overlay_position_change, on_overlay_offset_change,
                                 on_overlay_size_change, on_overlay_color_click)
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
    def _build_overlay_row(self, inner, initial_enabled, initial_position, initial_offset_x,
                            initial_offset_y, initial_size, initial_color_connected, initial_color_disconnected,
                            on_toggle, on_position_change, on_offset_change, on_size_change, on_color_click):
        orow = tk.Frame(inner, bg=PANEL_BG); orow.pack(fill="x")
        _lbl(orow, "Network Overlay", PANEL_BG, font=FONT_LG_B).pack(side="left")
        self._ovar = tk.BooleanVar(value=initial_enabled)
        tk.Checkbutton(orow, variable=self._ovar, bg=PANEL_BG, fg=TEXT, activebackground=PANEL_BG,
                       activeforeground=TEXT, selectcolor=COL_BG, highlightthickness=0,
                       command=lambda: on_toggle(self._ovar.get())).pack(side="left", padx=(8,0))
        _lbl(orow, "Position:", PANEL_BG, font=FONT_SM).pack(side="left", padx=(10,4))
        self._opos_var = tk.StringVar(value=initial_position)
        opos_combo = ttk.Combobox(orow, state="readonly", width=12, font=FONT_SM,
                                   values=OVERLAY_POSITIONS, textvariable=self._opos_var)
        opos_combo.pack(side="left")
        opos_combo.bind("<<ComboboxSelected>>", lambda e: on_position_change(self._opos_var.get()))
        orow2 = tk.Frame(inner, bg=PANEL_BG); orow2.pack(fill="x", pady=(6,0))
        _lbl(orow2, "Connected:", PANEL_BG, font=FONT_SM).pack(side="left")
        self._ocolor_connected_btn = _btn(orow2, "Color...", lambda: on_color_click("connected"), "#3a3a3a")
        self._ocolor_connected_btn.config(bg=initial_color_connected)
        self._ocolor_connected_btn.pack(side="left", padx=(4,16))
        _lbl(orow2, "Disconnected:", PANEL_BG, font=FONT_SM).pack(side="left")
        self._ocolor_disconnected_btn = _btn(orow2, "Color...", lambda: on_color_click("disconnected"), "#3a3a3a")
        self._ocolor_disconnected_btn.config(bg=initial_color_disconnected)
        self._ocolor_disconnected_btn.pack(side="left", padx=(4,0))
        orow3 = tk.Frame(inner, bg=PANEL_BG); orow3.pack(fill="x", pady=(6,0))
        _lbl(orow3, "X offset:", PANEL_BG, font=FONT_SM).pack(side="left")
        self._ox_var = tk.IntVar(value=initial_offset_x)
        self._ox_var.trace_add("write", lambda *_: on_offset_change(self._safe_int(self._ox_var, initial_offset_x),
                                                                     self._safe_int(self._oy_var, initial_offset_y)))
        tk.Entry(orow3, textvariable=self._ox_var, width=5, bg=COL_BG, fg=TEXT,
                 insertbackground=TEXT, relief="flat").pack(side="left", padx=(4,12))
        _lbl(orow3, "Y offset:", PANEL_BG, font=FONT_SM).pack(side="left")
        self._oy_var = tk.IntVar(value=initial_offset_y)
        self._oy_var.trace_add("write", lambda *_: on_offset_change(self._safe_int(self._ox_var, initial_offset_x),
                                                                     self._safe_int(self._oy_var, initial_offset_y)))
        tk.Entry(orow3, textvariable=self._oy_var, width=5, bg=COL_BG, fg=TEXT,
                 insertbackground=TEXT, relief="flat").pack(side="left", padx=(4,0))
        _lbl(orow3, "Size:", PANEL_BG, font=FONT_SM).pack(side="left", padx=(16,0))
        self._osize_var = tk.IntVar(value=initial_size)
        self._osize_var.trace_add("write", lambda *_: on_size_change(
            self._safe_int(self._osize_var, initial_size, OVERLAY_SIZE_MIN, OVERLAY_SIZE_MAX)))
        tk.Spinbox(orow3, textvariable=self._osize_var, width=4, bg=COL_BG, fg=TEXT,
                   insertbackground=TEXT, relief="flat", from_=OVERLAY_SIZE_MIN, to=OVERLAY_SIZE_MAX,
                   buttonbackground=PANEL_BG).pack(side="left", padx=(4,0))
    @staticmethod
    def _safe_int(var, default, lo=0, hi=None):
        try: v = int(var.get())
        except (tk.TclError, ValueError): return default
        v = max(lo, v)
        return min(hi, v) if hi is not None else v
    def set_overlay_color(self, state, color):
        btn = self._ocolor_connected_btn if state == "connected" else self._ocolor_disconnected_btn
        if btn: btn.config(bg=color)
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

class Overlay:
    """Persistent colored square showing network state: green while
    connected, red while lagging (both colors user-configurable).
    Uses Windows click-through and capture-exclusion features when available."""

    def __init__(self, root):
        self._root = root
        self._win = None
        self._square = None
        self._position = DEFAULT_OVERLAY_POSITION
        self._offset_x = DEFAULT_OVERLAY_OFFSET
        self._offset_y = DEFAULT_OVERLAY_OFFSET
        self._size = DEFAULT_OVERLAY_SIZE
        self._colors = dict(DEFAULT_OVERLAY_COLORS)
        self._state = "connected"

    def set_position(self, position):
        self._position = position if position in OVERLAY_POSITIONS else DEFAULT_OVERLAY_POSITION
        self._reposition()

    def set_size(self, size):
        self._size = max(OVERLAY_SIZE_MIN, min(OVERLAY_SIZE_MAX, int(size)))
        if self._win is not None:
            # The Frame's width/height are fixed at creation time — rebuild
            # it in place rather than trying to resize the existing widget.
            self.hide()
            self.show()

    def set_offset(self, offset_x, offset_y):
        """Set the horizontal and vertical distance from the selected corner."""
        self._offset_x = max(0, offset_x)
        self._offset_y = max(0, offset_y)
        self._reposition()

    def set_state(self, state):
        self._state = state
        if self._square is not None:
            self._square.config(bg=self._colors.get(state, DEFAULT_OVERLAY_COLORS["connected"]))

    def set_color(self, state, color):
        self._colors[state] = color
        if self._square is not None and self._state == state:
            self._square.config(bg=color)

    def show(self):
        if self._win is not None:
            return
        win = tk.Toplevel(self._root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg="black")
        try:
            win.attributes("-transparentcolor", "black")  # Windows-only Tk feature
        except tk.TclError:
            pass
        self._square = tk.Frame(
            win, bg=self._colors.get(self._state, DEFAULT_OVERLAY_COLORS["connected"]),
            width=self._size, height=self._size,
        )
        self._square.pack_propagate(False)
        self._square.pack()
        self._win = win
        self._reposition()
        self._apply_click_through()
        self._apply_capture_exclusion()

    def hide(self):
        if self._win is not None:
            self._win.destroy()
            self._win = None
            self._square = None

    def _reposition(self):
        if self._win is None:
            return
        self._win.update_idletasks()
        w = self._win.winfo_reqwidth()
        h = self._win.winfo_reqheight()
        sw = self._win.winfo_screenwidth()
        sh = self._win.winfo_screenheight()
        mx, my = self._offset_x, self._offset_y
        x, y = {
            "Top-left": (mx, my),
            "Top-right": (sw - w - mx, my),
            "Bottom-left": (mx, sh - h - my),
            "Bottom-right": (sw - w - mx, sh - h - my),
        }.get(self._position, (sw - w - mx, my))
        self._win.geometry(f"{w}x{h}+{x}+{y}")

    def _apply_click_through(self):
        if not HAVE_WIN32 or self._win is None:
            return
        hwnd = self._win.winfo_id()
        style = int(_user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) or 0)
        style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
        _user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
        _user32.SetLayeredWindowAttributes(hwnd, 0x000000, 0, LWA_COLORKEY)

    def _apply_capture_exclusion(self):
        if not HAVE_WIN32 or self._win is None:
            return
        _user32.SetWindowDisplayAffinity(self._win.winfo_id(), WDA_EXCLUDEFROMCAPTURE)


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
        self._search_query = self._settings.search_query
        self._disconnected = False  # true from the moment apps are blocked until reconnect fires
        self._ov_enabled = self._settings.overlay_enabled
        self._ov_position = self._settings.overlay_position
        self._ov_offset_x = self._settings.overlay_offset_x
        self._ov_offset_y = self._settings.overlay_offset_y
        self._ov_size = self._settings.overlay_size
        self._ov_color_connected = self._settings.overlay_color("connected")
        self._ov_color_disconnected = self._settings.overlay_color("disconnected")
        self._overlay = Overlay(root)
        self._overlay.set_position(self._ov_position)
        self._overlay.set_offset(self._ov_offset_x, self._ov_offset_y)
        self._overlay.set_size(self._ov_size)
        self._overlay.set_color("connected", self._ov_color_connected)
        self._overlay.set_color("disconnected", self._ov_color_disconnected)
        self._overlay.set_state("connected")
        self._fw = FirewallService(root, on_error=self._on_backend_error)
        self._fw.start()
        self._scanner = ProcessScanner(root, self._apply_app_map)
        self._scanner.start()
        self._reconnect = ReconnectScheduler(lambda: self._g_delay, self._on_reconnect_fire)
        self._keys = KeybindManager(root, self._settings.keybinds,
                                     on_arm=self._on_arm, on_disarm=self._on_disarm,
                                     on_capture_done=self._on_capture_done)
        self._ui = MainWindow(root, initial_delay=self._g_delay, initial_keybinds=self._keys.keybinds,
                               on_add_app=self._add, on_add_key=self._on_add_key,
                               on_remove_key=self._rm_key, on_clear_rules=self._clear_all_rules,
                               on_delay_change=self._on_delay_change, on_delay_commit=self._save,
                               on_search_change=self._on_search_change,
                               on_refresh=self._enumerate_all,
                               initial_overlay_enabled=self._ov_enabled,
                               initial_overlay_position=self._ov_position,
                               initial_overlay_offset_x=self._ov_offset_x,
                               initial_overlay_offset_y=self._ov_offset_y,
                               initial_overlay_size=self._ov_size,
                               initial_overlay_color_connected=self._ov_color_connected,
                               initial_overlay_color_disconnected=self._ov_color_disconnected,
                               on_overlay_toggle=self._on_overlay_toggle,
                               on_overlay_position_change=self._on_overlay_position_change,
                               on_overlay_offset_change=self._on_overlay_offset_change,
                               on_overlay_size_change=self._on_overlay_size_change,
                               on_overlay_color_click=self._on_overlay_color_click,
                               initial_search=self._search_query,
                               restarted=os.environ.get(RESTART_ENV_VAR) == "1")
        self._restore()
        self._refresh_overlay_visibility()
        self._keys.start()
        root.update_idletasks(); self._ui.fit()
        self._enumerate_all()
        self._fw.send("sweep", [e.state.app_path for e in self._apps])
        self._auto_refresh_id = None
        self._schedule_auto_refresh()
        try:
            base = getattr(sys,"_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            root.wm_iconbitmap(os.path.join(base,"PushToLag.ico"))
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
        for e in self._apps: self._refresh_entry_status(e)
        self._refresh_overlay_visibility()
    def _refresh_entry_status(self, entry):
        """Single source of truth for the status pill:
        INACTIVE — no app picked, rule failed, or the app just isn't running right now
        DISCONNECTED — app is running and hooked, but currently blocked by the hotkey
        CONNECTED — app is running and hooked, not currently blocked"""
        if entry.attached is None or not entry.attached:
            entry.set_status("● INACTIVE", MUTED)
            return
        if entry.state.app_path not in self._last_app_map:
            entry.set_status("● INACTIVE", MUTED)
        elif self._disconnected:
            entry.set_status("● DISCONNECTED", ACCENT_OFF)
        else:
            entry.set_status("● CONNECTED", ACCENT)
    def _on_search_change(self, query):
        self._search_query = query
        self._apply_filter()
        self._save()
    def _apply_filter(self):
        q = self._search_query.strip().lower()
        filtered = self._last_app_map if not q else \
            {path: name for path, name in self._last_app_map.items() if q in name.lower()}
        for w in self._rows: w.refresh_apps(self._last_app_map, filtered)
    def _enumerate_all(self):
        # Runs on its own persistent scanner thread so a slow process scan can
        # never delay a pending hotkey toggle.
        self._scanner.request_scan()
    def _schedule_auto_refresh(self):
        self._auto_refresh_id = self.root.after(AUTO_REFRESH_MS, self._auto_refresh_tick)
    def _auto_refresh_tick(self):
        # Keeps status pills/overlay honest as apps launch or close on their
        # own, without waiting on a manual Refresh click. Purely cosmetic
        # upkeep — a scan started here never re-touches firewall attach
        # state, only what _apply_app_map already does for the manual path.
        self._enumerate_all()
        self._schedule_auto_refresh()
    # arm/disarm are batched across every configured app instead of looping per-entry
    def _on_arm(self):
        self._reconnect.cancel()
        uids = [e.state.uid for e in self._apps if e.state.app_path]
        if not uids: return
        self._disconnected = True
        self._overlay.set_state("disconnected")
        self._fw.send("block_batch", (uids, True, self._on_block_timing))
        for e in self._apps: self._refresh_entry_status(e)
    def _on_disarm(self):
        self._reconnect.schedule()
    def _on_reconnect_fire(self):
        self.root.after(0, self._do_reconnect)  # hop from Timer thread to Tk thread
    def _do_reconnect(self):
        uids = [e.state.uid for e in self._apps if e.state.app_path]
        if not uids: return
        self._disconnected = False
        self._overlay.set_state("connected")
        self._fw.send("block_batch", (uids, False, self._on_block_timing))
        for e in self._apps: self._refresh_entry_status(e)
    def _on_block_timing(self, uids, blocked, elapsed_ms, ok=True):
        action = "disconnect" if blocked else "reconnect"
        if not ok:
            # A failed firewall write already leaves the app in whatever state
            # it was in before — don't report its duration as if it were a
            # successful disconnect/reconnect measurement.
            text = f"Last {action}: failed"
        else:
            text = f"Last {action} ({len(uids)} app{'s' if len(uids)!=1 else ''}): {elapsed_ms:.2f} ms"
        if blocked: self._ui.set_disconnect_timing(text)
        else: self._ui.set_reconnect_timing(text)
    def _on_delay_change(self, value):
        self._g_delay = value
    def _has_active_apps(self):
        return any(e.attached and e.state.app_path in self._last_app_map for e in self._apps)
    def _refresh_overlay_visibility(self):
        if self._ov_enabled and self._has_active_apps(): self._overlay.show()
        else: self._overlay.hide()
    def _on_overlay_toggle(self, enabled):
        self._ov_enabled = enabled
        self._refresh_overlay_visibility()
        self._save()
    def _on_overlay_position_change(self, position):
        self._ov_position = position
        self._overlay.set_position(position)
        self._save()
    def _on_overlay_offset_change(self, x, y):
        self._ov_offset_x, self._ov_offset_y = x, y
        self._overlay.set_offset(x, y)
        self._save()
    def _on_overlay_size_change(self, size):
        self._ov_size = size
        self._overlay.set_size(size)
        self._save()
    def _on_overlay_color_click(self, state):
        current = self._ov_color_connected if state == "connected" else self._ov_color_disconnected
        title = "Connected overlay color" if state == "connected" else "Disconnected overlay color"
        _rgb, hex_color = colorchooser.askcolor(color=current, title=title)
        if hex_color is None: return
        if state == "connected": self._ov_color_connected = hex_color
        else: self._ov_color_disconnected = hex_color
        self._overlay.set_color(state, hex_color)
        self._ui.set_overlay_color(state, hex_color)
        self._save()
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
        row = AppRow(entry, on_select=self._set_app, on_move=self._move, on_remove=self._remove)
        self._apps.append(entry); self._rows.append(row)
        row.build(self._ui.rows_frame)
        self._refresh_arrows(); self._ui.fit()
        if restored: self._attach(entry)
        else: self._save(); self._enumerate_all()
        self._refresh_overlay_visibility()
    def _set_app(self, entry, app_path):
        entry.state.app_path = app_path
        self._attach(entry); self._save()
        self._refresh_overlay_visibility()
    def _attach(self, entry):
        def cb(ok):
            entry.attached = ok
            self._refresh_entry_status(entry)
        self._fw.send("attach", (entry.state.uid, entry.state.app_path, cb))
    def _remove(self, entry):
        idx = self._apps.index(entry)
        self._apps.pop(idx); row = self._rows.pop(idx)
        self._fw.send("remove", entry.state.uid)
        row.destroy()
        for i,(a,w) in enumerate(zip(self._apps, self._rows)): a.index = i; w.set_index(i)
        if self._disconnected and not any(e.state.app_path for e in self._apps):
            self._disconnected = False
            self._overlay.set_state("connected")
        self._refresh_overlay_visibility()
        self._save(); self._enumerate_all(); self._refresh_arrows(); self._ui.fit()
    def _save(self):
        overlay = {
            "overlay_enabled": self._ov_enabled, "overlay_position": self._ov_position,
            "overlay_offset_x": self._ov_offset_x, "overlay_offset_y": self._ov_offset_y,
            "overlay_size": self._ov_size,
            "overlay_color_connected": self._ov_color_connected,
            "overlay_color_disconnected": self._ov_color_disconnected,
        }
        self._settings.save(len(self._apps), self._keys.keybinds, self._g_delay,
                             [e.state for e in self._apps], self._search_query, overlay)
    def _quit(self):
        if self._auto_refresh_id is not None:
            self.root.after_cancel(self._auto_refresh_id)
            self._auto_refresh_id = None
        self._save(); self._reconnect.cancel(); self._keys.stop()
        self._overlay.hide()
        self._scanner.stop()
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