"""Elevation check + one-shot restart-on-init-failure."""
from __future__ import annotations

import ctypes
import os
import sys

RESTART_ENV_VAR = "PUSHTOLAG_RESTARTED"  # set on the one auto-restart we allow after a backend init failure


def is_admin():
    try: return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception: return False


def was_restarted():
    return os.environ.get(RESTART_ENV_VAR) == "1"


def restart_self():
    """Re-execs the current (already-elevated) process in place — no new
    UAC prompt needed, since the child inherits this process's token.
    Raises if the exec itself fails; caller decides how to surface that."""
    os.environ[RESTART_ENV_VAR] = "1"
    os.execv(sys.executable, [sys.executable] + sys.argv[1:])
