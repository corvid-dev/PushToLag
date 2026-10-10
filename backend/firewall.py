"""Windows Firewall COM backend.

In-process COM only — rule toggles are property sets on live COM objects, no
process spawn in the hot path. FirewallService marshals its callbacks via a
`dispatch(fn, *args)` callable, so this module has no UI-toolkit dependency.
"""
from __future__ import annotations

import hashlib
import queue
import threading
import time
from typing import Callable

try:
    import comtypes.client as com
    from comtypes import CoInitialize, CoUninitialize
except ImportError:
    com = None

FW_PREFIX = "PushToLag"

# Windows Firewall COM constants (netfw.h)
NET_FW_RULE_DIR_IN, NET_FW_RULE_DIR_OUT = 1, 2
NET_FW_ACTION_BLOCK = 0
NET_FW_PROFILE2_ALL = 0x7FFFFFFF

try:
    import ctypes
    _kernel32 = ctypes.windll.kernel32
    _kernel32.GetCurrentThread.restype = ctypes.c_void_p
    _kernel32.SetThreadPriority.restype = ctypes.c_bool
    _kernel32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
    THREAD_PRIORITY_HIGHEST = 2
except Exception:
    _kernel32 = None
    THREAD_PRIORITY_HIGHEST = 2


def boost_current_thread():
    """Fail-soft priority boost for short latency-critical Windows workers.

    HIGHEST is deliberately used instead of TIME_CRITICAL: it reduces dispatch
    jitter without allowing this small utility to starve system threads.
    """
    if _kernel32 is None:
        return False
    try:
        return bool(_kernel32.SetThreadPriority(
            _kernel32.GetCurrentThread(), THREAD_PRIORITY_HIGHEST))
    except Exception:
        return False


def app_rule_names(app_path):
    h = hashlib.md5(app_path.encode("utf-8")).hexdigest()[:12]
    return f"{FW_PREFIX}_{h}_out", f"{FW_PREFIX}_{h}_in"


def app_rule_group(app_path):
    """Stable private group shared by one app's inbound/outbound rule pair."""
    h = hashlib.md5(app_path.encode("utf-8")).hexdigest()[:12]
    return f"{FW_PREFIX}_{h}"


class FirewallBackend:
    def __init__(self, fw):
        self._fw = fw

    def _new_rule(self, name, direction, app_path, group):
        try:
            self._fw.Rules.Remove(name)  # clear any stale same-named rule first
        except Exception:
            pass
        rule = com.CreateObject("HNetCfg.FWRule")
        rule.Name = name; rule.Direction = direction; rule.Action = NET_FW_ACTION_BLOCK
        rule.ApplicationName = app_path; rule.Enabled = False; rule.Profiles = NET_FW_PROFILE2_ALL
        rule.Grouping = group
        self._fw.Rules.Add(rule)
        return rule

    def new_rule_pair(self, app_path):
        out_name, in_name = app_rule_names(app_path)
        group = app_rule_group(app_path)
        try:
            outbound = self._new_rule(out_name, NET_FW_RULE_DIR_OUT, app_path, group)
            inbound = self._new_rule(in_name, NET_FW_RULE_DIR_IN, app_path, group)
            return group, outbound, inbound
        except Exception:
            self.remove_by_name(out_name); self.remove_by_name(in_name)
            return None

    def set_enabled(self, handle, enabled):
        group, outbound, inbound = handle
        try:
            # One policy commit toggles both directions for this app. This is
            # materially cheaper than committing each INetFwRule separately.
            self._fw.EnableRuleGroup(NET_FW_PROFILE2_ALL, group, bool(enabled))
            return True
        except Exception:
            # Older/unusual policy providers may reject group operations.
            # Preserve functionality with the original two-write path.
            try:
                outbound.Enabled = enabled
            except Exception:
                return False
            try:
                inbound.Enabled = enabled
                return True
            except Exception:
                # Best-effort rollback prevents a half-blocked rule pair from
                # being reported using a single boolean state.
                try: outbound.Enabled = not enabled
                except Exception: pass
                return False

    def remove_by_name(self, name):
        try: self._fw.Rules.Remove(name); return True
        except Exception: return False

    def list_rule_names(self):
        names = set()
        try:
            for rule in self._fw.Rules:
                try:
                    if rule.Name and rule.Name.startswith(FW_PREFIX + "_"): names.add(rule.Name)
                except Exception: continue
        except Exception:
            pass
        return names


def create_backend():
    """Creates the in-process Windows Firewall COM backend. Raises on failure —
    there's no netsh fallback, so the caller surfaces the error."""
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

    @property
    def app_path(self): return self._app_path

    @property
    def blocked(self): return self._blocked

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
    so disconnecting/reconnecting N apps is one queue message + one dispatch callback,
    not N."""

    def __init__(self, dispatch: Callable, on_error=None):
        self._dispatch = dispatch
        self._q = queue.Queue(); self._gates = {}; self._backend = None
        self._on_error = on_error

    def send(self, cmd, arg=None): self._q.put((cmd, arg))

    def start(self): threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        # As in the FCPE worker, priority belongs on the worker itself. This
        # thread is normally asleep in Queue.get(), so the boost only matters
        # during the brief firewall transition.
        boost_current_thread()
        CoInitialize()
        try:
            try:
                self._backend = create_backend()
            except Exception as e:
                if self._on_error: self._dispatch(self._on_error, str(e))
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
                    deferred = []
                    while True:
                        try: nxt_cmd, nxt_arg = self._q.get_nowait()
                        except queue.Empty: break
                        if nxt_cmd == "block_batch": cmd, arg = nxt_cmd, nxt_arg
                        elif nxt_cmd == "quit": quit_pending = True
                        elif nxt_cmd in ("sweep", "cleanup", "reset"):
                            deferred.append((nxt_cmd, nxt_arg))
                        else:
                            # Attach/remove establish the gate set consumed by
                            # block_batch, so preserve their ordering.
                            self._handle(nxt_cmd, nxt_arg)
                    self._handle(cmd, arg)
                    if quit_pending:
                        for g in self._gates.values(): g.deactivate()
                        break
                    # A sweep/attach already in the queue must not delay a
                    # disconnect or reconnect. Put maintenance back only after
                    # the newest latency-critical toggle has completed.
                    for deferred_item in deferred:
                        self._q.put(deferred_item)
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
            self._dispatch(cb, ok)
        elif cmd == "remove":
            g = self._gates.pop(arg, None)
            if g: g.deactivate()
        elif cmd == "block_batch":
            uids, blocked, cb = arg
            t0 = time.perf_counter()
            results = {}
            for uid in uids:
                g = self._gates.get(uid)
                ok = g is not None and g.set_blocked(blocked)
                results[uid] = (ok, g.blocked if g is not None else False)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if cb: self._dispatch(cb, uids, blocked, elapsed_ms, results)
        elif cmd == "cleanup":
            self._dispatch(arg, cleanup_all_rules(self._backend))
        elif cmd == "reset":
            cb = arg
            removed = cleanup_all_rules(self._backend)
            results = {}
            for uid, g in self._gates.items():
                app_path = g.app_path
                results[uid] = g.activate(app_path) if app_path else None
            self._dispatch(cb, removed, results)
        elif cmd == "sweep":
            sweep_orphan_rules(self._backend, arg)
