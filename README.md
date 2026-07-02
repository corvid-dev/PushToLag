# PushToLag

Push-to-disconnect for Windows 11. Hold a global hotkey to cut network access to whichever apps you've configured, release to reconnect them after a delay. Uses the in-process Windows Firewall COM API — no `netsh.exe` calls.

## Requirements

- Windows 11
- Python 3.10+
- Must run elevated (Administrator)

**Install these first — both running from source and building the .exe need them:**

```
pip install pynput psutil comtypes
```

## Run

```
python PushToLag.py
```

Launch via a shortcut set to "Run as administrator"; it warns and exits if not elevated.

## Build an .exe

Make sure the dependencies above are installed, then build from the spec file:

```
pip install pyinstaller
pyinstaller PushToLag.spec
```

Output lands in `dist\PushToLag.exe`. Point an admin-elevated shortcut at it.

The spec already bundles `PushToLagIcon.ico` (both as the .exe's file icon and for the app's runtime window icon) — just make sure that file exists in this folder before building. If you swap in a different icon file, update the `icon=` and `datas=` entries in `PushToLag.spec` to match.

## Settings

Stored at `%APPDATA%\PushToLag\prefs.json` — configured apps, keybinds, and reconnect delay.
