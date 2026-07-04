# DJ Set Builder Track Paster

This is a tiny Windows 11 Python app that pastes the next track name into whichever app currently has keyboard focus.

## Hotkeys

- Press the backtick key `` ` `` to paste the next track.
- Press `Shift + backtick` to reset back to the first track.
- Press `Esc` to quit the app.

## Setup

```powershell
py -m pip install -r requirements.txt
py app.py
```

## Notes

- The script loops back to the first track after the last one.
- It copies the track to the clipboard, sends `Ctrl+V`, then attempts to restore your previous clipboard text.
- If a target app blocks simulated paste, try running the script as Administrator.
