# NetMeter

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey)

A lightweight desktop network monitor. A small always-on-top widget sits in a
corner of your screen and shows live download/upload speed as a scrolling
graph, while a tray icon and a usage-statistics window keep track of how much
data you have used per day and per month.

Built with Python, Tkinter and psutil. No account, no telemetry, no network
access of its own: everything stays on your machine.

<!--
Screenshots: add images to a docs/ folder and uncomment, e.g.
![Widget](docs/widget.png)
![Usage statistics](docs/stats.png)
-->

## Features

- **Corner widget**: borderless, always on top, draggable and resizable. Shows
  a live download (cyan) and upload (orange) graph, current speeds, and the
  data used this session.
- **System tray icon**: hover for live speeds; right-click for the menu.
- **Usage statistics** with two tabs:
  - **Daily**: totals for today, last 7 days, this month, last month, all
    recorded time and daily average, a 30-day bar chart and a per-day history
    table.
  - **Monthly**: a 12-month bar chart and a table with one row per month.
- **Active connections window**: lists which processes currently have open
  internet connections (process, local and remote address, status).
- **Settings**: widget size, opacity, refresh interval, graph history length,
  colors, "start at login", and reset to defaults. Changes apply live.
- **Start at login** on Windows and Linux, from the Settings window.
- **Persistent history**: daily totals survive restarts and roll over at
  midnight.

## Quick start

Requires Python 3.9 or newer.

```bash
pip install -r requirements.txt
python netmeter.py
```

### Windows

```powershell
pip install -r requirements.txt
python netmeter.py
```

### Linux (tested on Zorin OS / Ubuntu-based distros)

The install script sets up an isolated virtual environment, installs the
dependencies and adds NetMeter to your application menu:

```bash
sudo apt install python3-tk
chmod +x install_linux.sh
./install_linux.sh
```

Then launch **NetMeter** from your app menu, or run `~/.local/bin/netmeter`.

For the tray icon your desktop needs an AppIndicator backend:

```bash
sudo apt install gir1.2-ayatanaappindicator3-0.1   # or gir1.2-appindicator3-0.1
```

Without it the app still runs; it just skips the tray icon.

## Using it

| Action | How |
| --- | --- |
| Move the widget | Drag it anywhere except the bottom-right corner |
| Resize the widget | Drag the dotted grip in the bottom-right corner (150 to 600 px wide, 70 to 400 px tall) |
| Snap to a corner | Right-click the widget and pick a corner |
| Open statistics, settings or connections | Right-click the widget or the tray icon |
| Hide the widget | Right-click, then Hide (the tray icon keeps running) |
| Quit | Tray icon, then Quit; or right-click the widget, then Quit NetMeter |

### Settings

Open **Settings...** from the tray or widget menu.

- **Widget size**: exact width and height.
- **Opacity**: how see-through the widget is.
- **Update interval**: 200 ms to 5 s. Lower is snappier, higher uses less CPU.
- **Graph history**: 10 to 300 points.
- **Colors**: background, download line, upload line, text and grid.
- **Start NetMeter when I log in**: adds or removes the startup entry.

## Where your data is stored

| OS | Folder |
| --- | --- |
| Windows | `%APPDATA%\NetMeter\` |
| Linux | `~/.config/NetMeter/` |

- `config.json`: your settings and widget position.
- `daily_usage.csv`: one row per day with downloaded and uploaded bytes. You
  can open it in any spreadsheet app. Use **Clear history...** in the
  statistics window to reset it.

"Start at login" uses the current-user registry Run key on Windows and an XDG
autostart file (`~/.config/autostart/netmeter.desktop`) on Linux. Neither needs
admin rights.

## Build a standalone Windows .exe

```powershell
pip install pyinstaller
python -m PyInstaller --onefile --windowed --name NetMeter --icon netmeter.png netmeter.py
```

The result is `dist\NetMeter.exe`. The window, taskbar and tray icons are drawn
in code, so the exe does not need `netmeter.png` at runtime.

Notes:

- If `pyinstaller` is "not recognized", use `python -m PyInstaller` as above.
- If the build fails with *Access is denied* on `dist\NetMeter.exe`, the old
  copy is still running. Quit it (or run `taskkill /F /IM NetMeter.exe`) and
  build again.
- If it fails with *Icon input file not found*, put `netmeter.png` next to
  `netmeter.py` or drop the `--icon` flag.
- An unsigned exe can trigger a Windows SmartScreen warning on other PCs.
  Signing it with a code-signing certificate is the way to avoid that.

## How it works

A background thread reads the system-wide byte counters from
`psutil.net_io_counters()` once per interval and turns the difference into
bytes per second. The widget redraws from that data. Daily totals are written
to `daily_usage.csv` every 30 seconds and when you quit.

## Limitations

- Speeds are system-wide totals across all network adapters.
- The **Active connections** window shows which processes have open
  connections, not how much bandwidth each one is using. Real per-process
  bandwidth needs packet capture (for example WinDivert on Windows), which is
  not included.
- Listing connections for other users' processes may require running as
  Administrator (Windows) or root (Linux).
- macOS is not tested, and "start at login" is not implemented there.

## Project layout

```
netmeter.py          the whole application
requirements.txt     psutil, pystray, pillow
install_linux.sh     Linux installer (venv + app menu entry)
netmeter.png         app icon source (used for the .exe icon and Linux launcher)
```

## Contributing

Issues and pull requests are welcome. Ideas on the roadmap: per-process
bandwidth, per-adapter graphs, CSV/PNG export of statistics.
