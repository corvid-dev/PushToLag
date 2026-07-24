# PushToLag

Push-to-disconnect for Windows 11. Hold a global hotkey to cut network access to whichever apps you've configured, release to reconnect them after a delay. Uses the in-process Windows Firewall COM API — no `netsh.exe` calls.

Includes an optional on-screen overlay: a small square (green when connected, red when lagging, both colors and size configurable) that only lights up while it's actually affecting a configured app.

Rapid hotkey taps don't queue up a backlog of stale firewall transitions — only the latest desired state is ever applied. Redundant Windows Firewall COM writes are skipped when a rule is already in the requested state, and the running-process scan runs on one persistent worker thread. Apps you've configured stay listed and selectable even while they're not currently running.

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

Make sure the dependencies above are installed, then either use the included spec file:

```
pip install pyinstaller
pyinstaller PushToLag.spec
```

or the equivalent one-line command (bundles the icon, `PushToLag.ico`, in this folder):

```
pyinstaller --onefile --windowed --name PushToLag --icon PushToLag.ico --add-data "PushToLag.ico;." --hidden-import pynput.keyboard._win32 --hidden-import pynput.mouse._win32 --hidden-import comtypes.stream PushToLag.py
```

Output lands in `dist\PushToLag.exe`. Point an admin-elevated shortcut at it. `--windowed` avoids a console window flash.

## Settings

Stored at `%APPDATA%\PushToLag\prefs.json` — configured apps, keybinds, reconnect delay, and overlay preferences (enabled, position, size, colors, offset).