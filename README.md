# PushToLag

Windows utility that blocks network access for selected running applications while a global hotkey is held. Access is restored after the configured reconnect delay.

Firewall changes use the Windows Firewall COM API. Only configured applications that are currently running are included in each operation.

## Requirements

- Windows 11
- Python 3.10+
- Administrator privileges

Install the runtime dependencies:

```text
pip install pynput psutil comtypes
```

## Run

```text
python PushToLag.py
```

The program exits with a warning if it is not running as administrator.

## Overlay

The optional overlay provides:

- Connected and disconnected indicators
- Estimated reconnect countdown
- Accelerating blink near reconnect
- Configurable colors and indicator size
- Nine anchor positions
- Signed X/Y offsets in 10-pixel steps
- Separate Connected, Disconnected, Blink, and Countdown controls

Changing the anchor position resets both offsets to zero. Positive X moves right; positive Y moves up.

## Build

```text
pip install pyinstaller
pyinstaller --noconfirm --clean PushToLag.spec
```

The executable is written to `dist\PushToLag.exe`.

## Settings

Settings are stored at `%APPDATA%\PushToLag\prefs.json`.

Reconnect timing samples are kept only for the current run. The first reconnect estimate after launch uses a 50 ms fallback; later estimates average up to the five most recent successful reconnects.
