# NetMeter

A lightweight, Mini network monitor for Windows, built in Python.

## What it does

- **Corner widget** — a small, borderless, always-on-top overlay in the
  top-right corner of your screen (drag it anywhere you like) showing a
  live scrolling graph of download (cyan) and upload (orange) speed, plus
  the current numeric speeds and today's total data used.
- **System tray icon** — right-click for a menu; hover to see live
  down/up speed in the tooltip, right next to your Windows clock.
- **Active Connections window** — a real-time list of which processes on
  your PC currently have open internet connections (process name, local
  address, remote address, status). This is the practical way to see
  "what's using my internet" without needing raw packet capture.
- **Daily usage log** — total bytes down/up per day, saved to
  `%APPDATA%\NetMeter\daily_usage.csv` so history survives restarts.

## Setup

1. Install Python 3.9+ from python.org (tick "Add to PATH" during install).
2. Open a terminal in this folder and install dependencies:

   ```
   pip install -r requirements.txt
   ```

3. Run it:

   ```
   python netmeter.py
   ```

The widget will appear in the top-right corner and a tray icon will show
up near your clock.

## Using it

- **Move the widget**: left-click anywhere except the bottom-right corner
  and drag.
- **Resize the widget**: click-and-drag the small dotted grip in the
  bottom-right corner, same as resizing a normal window. It's clamped
  between 150–600px wide and 70–400px tall so the graph stays readable.
- **Snap to a corner**: right-click the widget → pick a corner
  (Top-Right / Bottom-Right / Top-Left / Bottom-Left, the last two are
  handy if you want it to sit right above the taskbar clock area).
- **Hide/show**: right-click the tray icon, or right-click the widget
  itself and choose Hide. Bringing it back is "Show widget" from the
  tray menu.
- **See what's using your internet**: tray icon → "Active connections..."
  This opens a table of processes with active remote connections,
  refreshing every 3 seconds.
- **Quit**: tray icon → Quit, or right-click the widget → "Quit NetMeter".

### Settings window

Open it from the tray icon ("Settings...") or by right-clicking the
widget and choosing "Settings...". You can adjust, all applied live:

- **Widget size** — precise width/height fields (same effect as dragging
  the resize grip, just exact numbers).
- **Opacity** — how see-through the widget is.
- **Update interval** — how often the speed/graph refreshes (200ms–5s).
  Lower = snappier but slightly more CPU; higher = smoother on CPU.
- **Graph history** — how many points the scrolling graph keeps (10–300).
- **Colors** — pick custom colors for background, download line, upload
  line, text, and grid lines via a color picker.
- **Reset to defaults** — puts everything back to the original look
  (keeps your saved screen position).

All changes are saved automatically to
`%APPDATA%\NetMeter\config.json`, so they persist between runs.

Your widget position and size are remembered automatically between runs
(saved to `%APPDATA%\NetMeter\config.json`).

## Making it start automatically / run in the background

Two easy options on Windows:

1. **Startup folder (simplest)**: press `Win+R`, type `shell:startup`,
   and drop a shortcut to `netmeter.py` (or a `.bat` file that runs
   `pythonw netmeter.py`) into that folder. Using `pythonw.exe` instead
   of `python.exe` avoids a console window popping up.
2. **Task Scheduler**: create a task that runs
   `pythonw.exe C:\path\to\netmeter.py` at log-on, for more control
   (e.g. run with highest privileges so the Active Connections window can
   always resolve process owners).

## Turning it into a standalone .exe (optional)

If you don't want to keep Python installed just to run this, package it
with PyInstaller:

```
pip install pyinstaller
pyinstaller --onefile --windowed --name NetMeter netmeter.py
```

The resulting `dist\NetMeter.exe` runs standalone — no console window,
no Python required on the target machine.

## Notes & limitations

- Speed graph is based on total system-wide network throughput
  (`psutil.net_io_counters`), refreshed every second — accurate for "how
  fast is my internet right now" but not split out per network adapter
  by default. If you have multiple adapters (e.g. Wi-Fi + Ethernet) and
  want a specific one, this is a good next feature to add.
- The Active Connections list shows *which processes have open
  connections*, not their live bandwidth split — Windows doesn't expose
  true per-process bandwidth without a packet-capture driver (like
  Npcap), which is a much heavier dependency. This gives you the "who's
  talking to the internet right now" view without needing that.
- Run as Administrator if you want every process name to resolve in the
  Active Connections window — otherwise system-level connections may
  show as PID `-`/`?`.
