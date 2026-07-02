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

Make sure the dependencies above are installed, then:

```
pip install pyinstaller
pyinstaller --onefile --windowed --name PushToLag PushToLag.py
```

Output lands in `dist\PushToLag.exe`. Point an admin-elevated shortcut at it. `--windowed` avoids a console window flash.

To bundle an icon (`PushToLagIcon.ico` in this folder):

```
pyinstaller --onefile --windowed --name PushToLag --icon PushToLagIcon.ico --add-data "PushToLagIcon.ico;." PushToLag.py
```

## Settings

Stored at `%APPDATA%\PushToLag\prefs.json` — configured apps, keybinds, and reconnect delay.
