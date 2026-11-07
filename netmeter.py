"""
NetMeter - A desktop network monitor for Windows.

Features
--------
1. A small, always-on-top, draggable "corner widget" (like a mini taskbar
   gadget) that shows a live line graph of download/upload speed plus
   the current numeric speeds.
2. A system tray icon whose tooltip shows the live down/up speed, with a
   right-click menu to show/hide the widget, snap it to a corner, open
   the Active Connections window, and quit.
3. An "Active Connections" window listing which processes currently have
   open internet connections (remote address, port, status) -- this is
   the closest cross-platform equivalent to "which app is using my
   internet" without needing kernel-level packet capture.
4. A running total of bytes sent/received today, saved to a small CSV
   log (one row per day) so usage persists across restarts.

Requirements
------------
    pip install psutil pystray pillow

Run
---
    python netmeter.py

Tested target: Windows 10/11. Will also run on Linux/macOS for the
speed graph + tray parts (per-process connection owner lookup may need
admin/root rights on some platforms).
"""

import json
import os
import sys
import csv
import threading
from collections import deque
from datetime import date, datetime, timedelta

import psutil
import tkinter as tk
from tkinter import ttk, colorchooser, messagebox

if sys.platform == "win32":
    import winreg
else:
    winreg = None

try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except Exception:
    # Covers missing packages AND missing platform tray backends
    # (e.g. no GTK/AppIndicator on some Linux setups). On Windows with
    # `pip install pystray pillow` this will succeed normally.
    TRAY_AVAILABLE = False

# The window/taskbar icon only needs Pillow (not a tray backend).
try:
    from PIL import Image, ImageDraw, ImageTk
    ICON_AVAILABLE = True
except Exception:
    ICON_AVAILABLE = False


# --------------------------------------------------------------------------
# Config / persistence
# --------------------------------------------------------------------------

APP_NAME = "NetMeter"

if sys.platform == "win32":
    CONFIG_DIR = os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME
    )
else:
    # Linux/macOS: follow the XDG base directory convention
    # (~/.config/NetMeter), same spirit as %APPDATA% on Windows.
    xdg_config = os.environ.get("XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config"))
    CONFIG_DIR = os.path.join(xdg_config, APP_NAME)

CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
LOG_FILE = os.path.join(CONFIG_DIR, "daily_usage.csv")

DEFAULT_CONFIG = {
    "x": None,
    "y": None,
    "width": 230,
    "height": 100,
    "opacity": 0.90,
    "history_len": 60,       # seconds of history shown in the graph
    "update_ms": 1000,       # refresh interval
    "bg_color": "#0c0c0f",
    "down_color": "#00d4ff",
    "up_color": "#ff7a45",
    "grid_color": "#242430",
    "text_color": "#eaeaea",
    "click_through_drag": True,
    "always_on_top": True,
    "start_on_boot": False,
}

MIN_WIDTH, MAX_WIDTH = 150, 600
MIN_HEIGHT, MAX_HEIGHT = 70, 400
RESIZE_GRIP = 14  # px hotspot in the bottom-right corner used to resize


def ensure_config_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)


def load_config():
    ensure_config_dir()
    cfg = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def save_config(cfg):
    ensure_config_dir()
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


# --------------------------------------------------------------------------
# "Start at login" support
#   - Windows: HKCU Run registry key (no admin rights needed)
#   - Linux:   XDG autostart .desktop file in ~/.config/autostart
#   - macOS:   not implemented (falls through to "unsupported")
# --------------------------------------------------------------------------

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

AUTOSTART_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config")),
    "autostart",
)
AUTOSTART_DESKTOP_FILE = os.path.join(AUTOSTART_DIR, "netmeter.desktop")

STARTUP_SUPPORTED = sys.platform == "win32" or sys.platform.startswith("linux")


def _startup_launch_command():
    """Build the command line used to auto-launch NetMeter at login."""
    if getattr(sys, "frozen", False):
        # Built with PyInstaller (--onefile/--windowed) -> just the binary
        return f'"{sys.executable}"'

    script = os.path.abspath(__file__)

    if sys.platform == "win32":
        # Launch with pythonw.exe so no console window pops up at logon.
        py_dir = os.path.dirname(sys.executable)
        pythonw = os.path.join(py_dir, "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = sys.executable  # fallback if pythonw isn't found
        return f'"{pythonw}" "{script}"'

    # Linux: run with the same interpreter currently running us.
    return f'"{sys.executable}" "{script}"'


def is_startup_enabled():
    """Check the actual OS-level state (source of truth, not just cfg)."""
    if sys.platform == "win32":
        if winreg is None:
            return False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
                winreg.QueryValueEx(key, APP_NAME)
                return True
        except OSError:
            return False

    if sys.platform.startswith("linux"):
        return os.path.exists(AUTOSTART_DESKTOP_FILE)

    return False


def set_startup_enabled(enable):
    """Add/remove the OS-level entry that starts NetMeter at login."""
    if sys.platform == "win32":
        if winreg is None:
            return
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
                if enable:
                    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _startup_launch_command())
                else:
                    try:
                        winreg.DeleteValue(key, APP_NAME)
                    except FileNotFoundError:
                        pass
        except OSError as e:
            print(f"[NetMeter] Could not update startup registry entry: {e}")
        return

    if sys.platform.startswith("linux"):
        try:
            if enable:
                os.makedirs(AUTOSTART_DIR, exist_ok=True)
                content = (
                    "[Desktop Entry]\n"
                    "Type=Application\n"
                    f"Name={APP_NAME}\n"
                    f"Exec={_startup_launch_command()}\n"
                    "X-GNOME-Autostart-enabled=true\n"
                    "NoDisplay=false\n"
                    "Terminal=false\n"
                    "Comment=Live network speed graph and bandwidth monitor\n"
                )
                with open(AUTOSTART_DESKTOP_FILE, "w") as f:
                    f.write(content)
                os.chmod(AUTOSTART_DESKTOP_FILE, 0o755)
            else:
                if os.path.exists(AUTOSTART_DESKTOP_FILE):
                    os.remove(AUTOSTART_DESKTOP_FILE)
        except OSError as e:
            print(f"[NetMeter] Could not update autostart entry: {e}")


def read_usage_history():
    """Return [(date_str, down_bytes, up_bytes), ...] from the usage CSV."""
    rows = []
    if not os.path.exists(LOG_FILE):
        return rows
    try:
        with open(LOG_FILE, "r", newline="") as f:
            for r in csv.reader(f):
                if len(r) < 3 or r[0] == "date":
                    continue
                try:
                    rows.append((r[0], int(r[1]), int(r[2])))
                except ValueError:
                    continue
    except OSError:
        pass
    rows.sort(key=lambda r: r[0])
    return rows


def log_daily_usage(down_total, up_total, day=None):
    """Append/update a day's row (default: today) in the usage CSV.
    down_total/up_total must be the FULL totals for that day."""
    ensure_config_dir()
    today = day or date.today().isoformat()
    rows = []
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", newline="") as f:
                rows = list(csv.reader(f))
        except OSError:
            rows = []

    header = ["date", "downloaded_bytes", "uploaded_bytes"]
    if not rows or rows[0] != header:
        rows = [header] + [r for r in rows if r and r[0] != "date"]

    found = False
    for r in rows[1:]:
        if r[0] == today:
            r[1] = str(down_total)
            r[2] = str(up_total)
            found = True
            break
    if not found:
        rows.append([today, str(down_total), str(up_total)])

    try:
        with open(LOG_FILE, "w", newline="") as f:
            csv.writer(f).writerows(rows)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------

def format_speed(bytes_per_sec):
    """Return a human readable speed string, e.g. '1.24 MB/s'."""
    bits = bytes_per_sec
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bits < 1024 or unit == "GB/s":
            return f"{bits:.1f} {unit}" if unit != "B/s" else f"{int(bits)} {unit}"
        bits /= 1024.0


def format_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024.0


# --------------------------------------------------------------------------
# Network sampler - runs on a background thread, thread-safe snapshot
# --------------------------------------------------------------------------

class NetworkSampler:
    def __init__(self, history_len=60, interval_ms=1000):
        self.interval = interval_ms / 1000.0
        self.down_history = deque([0] * history_len, maxlen=history_len)
        self.up_history = deque([0] * history_len, maxlen=history_len)
        self.down_speed = 0.0
        self.up_speed = 0.0
        self.session_down = 0
        self.session_up = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

        counters = psutil.net_io_counters()
        self._last_recv = counters.bytes_recv
        self._last_sent = counters.bytes_sent
        self._start_recv = counters.bytes_recv
        self._start_sent = counters.bytes_sent

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            self._stop.wait(self.interval)
            if self._stop.is_set():
                break
            counters = psutil.net_io_counters()
            recv, sent = counters.bytes_recv, counters.bytes_sent
            d_speed = max(0, recv - self._last_recv) / self.interval
            u_speed = max(0, sent - self._last_sent) / self.interval
            self._last_recv, self._last_sent = recv, sent

            with self._lock:
                self.down_speed = d_speed
                self.up_speed = u_speed
                self.down_history.append(d_speed)
                self.up_history.append(u_speed)
                self.session_down = recv - self._start_recv
                self.session_up = sent - self._start_sent

    def snapshot(self):
        with self._lock:
            return {
                "down_speed": self.down_speed,
                "up_speed": self.up_speed,
                "down_history": list(self.down_history),
                "up_history": list(self.up_history),
                "session_down": self.session_down,
                "session_up": self.session_up,
            }

    def reconfigure(self, history_len=None, interval_ms=None):
        """Apply new settings live, without losing session totals."""
        with self._lock:
            if history_len is not None:
                for attr in ("down_history", "up_history"):
                    old = list(getattr(self, attr))
                    trimmed = old[-history_len:]
                    padded = [0] * max(0, history_len - len(trimmed)) + trimmed
                    setattr(self, attr, deque(padded, maxlen=history_len))
            if interval_ms is not None:
                self.interval = interval_ms / 1000.0


# --------------------------------------------------------------------------
# Corner widget (the small always-on-top overlay)
# --------------------------------------------------------------------------

class MeterWidget:
    def __init__(self, root, cfg, sampler, on_close, on_open_settings=None, on_open_stats=None):
        self.root = root
        self.cfg = cfg
        self.sampler = sampler
        self.on_close = on_close
        self.on_open_settings = on_open_settings
        self.on_open_stats = on_open_stats

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)         # no title bar / borders
        self.win.attributes("-topmost", True)   # always on top
        try:
            self.win.attributes("-alpha", cfg["opacity"])
        except tk.TclError:
            pass
        self.win.configure(bg=cfg["bg_color"])

        w, h = cfg["width"], cfg["height"]
        x, y = self._resolve_position(w, h)
        self.win.geometry(f"{w}x{h}+{x}+{y}")

        self.canvas = tk.Canvas(
            self.win, width=w, height=h, bg=cfg["bg_color"],
            highlightthickness=0
        )
        self.canvas.pack(fill="both", expand=True)

        # Dragging
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        # Right click -> quick menu
        self.canvas.bind("<Button-3>", self._show_menu)

        self._drag_offset = (0, 0)
        self._drag_mode = None
        self._resize_origin = None
        self._build_menu()
        self._redraw(first=True)

    def _resolve_position(self, w, h):
        cfg = self.cfg
        if cfg.get("x") is not None and cfg.get("y") is not None:
            return cfg["x"], cfg["y"]
        sw = self.root.winfo_screenwidth()
        margin = 12
        x = sw - w - margin
        y = margin  # top-right corner, near the taskbar clock on most setups
        return x, y

    def _build_menu(self):
        self.menu = tk.Menu(self.win, tearoff=0)
        self.menu.add_command(label="Snap: Top-Right", command=lambda: self._snap("tr"))
        self.menu.add_command(label="Snap: Bottom-Right", command=lambda: self._snap("br"))
        self.menu.add_command(label="Snap: Top-Left", command=lambda: self._snap("tl"))
        self.menu.add_command(label="Snap: Bottom-Left", command=lambda: self._snap("bl"))
        self.menu.add_separator()
        self.menu.add_command(label="Usage statistics...", command=self._open_stats)
        self.menu.add_command(label="Settings...", command=self._open_settings)
        self.menu.add_command(label="Hide (tray icon keeps running)", command=self._hide)
        self.menu.add_command(label="Quit NetMeter", command=self.on_close)

    def _open_settings(self):
        if self.on_open_settings:
            self.on_open_settings()

    def _open_stats(self):
        if self.on_open_stats:
            self.on_open_stats()

    def _snap(self, corner):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w, h = self.cfg["width"], self.cfg["height"]
        margin = 12
        positions = {
            "tr": (sw - w - margin, margin),
            "br": (sw - w - margin, sh - h - margin - 48),  # leave room for taskbar
            "tl": (margin, margin),
            "bl": (margin, sh - h - margin - 48),
        }
        x, y = positions[corner]
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self.cfg["x"], self.cfg["y"] = x, y
        save_config(self.cfg)

    def _hide(self):
        self.win.withdraw()

    def show(self):
        self.win.deiconify()

    def _show_menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _in_resize_grip(self, event):
        w, h = self.cfg["width"], self.cfg["height"]
        return event.x >= w - RESIZE_GRIP and event.y >= h - RESIZE_GRIP

    def _drag_start(self, event):
        if self._in_resize_grip(event):
            self._drag_mode = "resize"
            self._resize_origin = (
                event.x_root, event.y_root, self.cfg["width"], self.cfg["height"]
            )
        else:
            self._drag_mode = "move"
            self._drag_offset = (event.x, event.y)

    def _drag_move(self, event):
        if self._drag_mode == "resize":
            ox, oy, ow, oh = self._resize_origin
            new_w = min(MAX_WIDTH, max(MIN_WIDTH, ow + (event.x_root - ox)))
            new_h = min(MAX_HEIGHT, max(MIN_HEIGHT, oh + (event.y_root - oy)))
            self.cfg["width"], self.cfg["height"] = new_w, new_h
            self.win.geometry(f"{new_w}x{new_h}")
            self.canvas.configure(width=new_w, height=new_h)
        elif self._drag_mode == "move":
            x = self.win.winfo_x() + (event.x - self._drag_offset[0])
            y = self.win.winfo_y() + (event.y - self._drag_offset[1])
            self.win.geometry(f"+{x}+{y}")

    def _drag_end(self, event):
        if self._drag_mode == "move":
            self.cfg["x"] = self.win.winfo_x()
            self.cfg["y"] = self.win.winfo_y()
        # width/height already live-updated in cfg during resize drag
        save_config(self.cfg)
        self._drag_mode = None
        self._resize_origin = None

    def apply_cfg_changes(self):
        """Push current cfg (colors/size/opacity/etc.) onto the live window.
        Called after the Settings dialog applies changes."""
        cfg = self.cfg
        try:
            self.win.attributes("-alpha", cfg["opacity"])
        except tk.TclError:
            pass
        self.win.configure(bg=cfg["bg_color"])
        self.canvas.configure(bg=cfg["bg_color"], width=cfg["width"], height=cfg["height"])
        x, y = self.win.winfo_x(), self.win.winfo_y()
        self.win.geometry(f"{cfg['width']}x{cfg['height']}+{x}+{y}")
        save_config(cfg)

    def _redraw(self, first=False):
        data = self.sampler.snapshot()
        cfg = self.cfg
        w, h = cfg["width"], cfg["height"]
        self.canvas.delete("all")

        graph_h = h - 34
        graph_top = 4
        down_hist = data["down_history"]
        up_hist = data["up_history"]
        max_val = max(max(down_hist, default=0), max(up_hist, default=0), 1024)

        # grid lines
        for frac in (0.0, 0.5, 1.0):
            gy = graph_top + graph_h * (1 - frac)
            self.canvas.create_line(0, gy, w, gy, fill=cfg["grid_color"])

        def plot(history, color):
            n = len(history)
            if n < 2:
                return
            step = w / (n - 1)
            points = []
            for i, v in enumerate(history):
                px = i * step
                py = graph_top + graph_h - (v / max_val) * graph_h
                points.extend([px, py])
            self.canvas.create_line(*points, fill=color, width=2, smooth=True)

        plot(down_hist, cfg["down_color"])
        plot(up_hist, cfg["up_color"])

        # text overlay
        self.canvas.create_text(
            6, h - 26, anchor="w",
            text=f"\u25bc {format_speed(data['down_speed'])}",
            fill=cfg["down_color"], font=("Segoe UI", 10, "bold")
        )
        self.canvas.create_text(
            6, h - 10, anchor="w",
            text=f"\u25b2 {format_speed(data['up_speed'])}",
            fill=cfg["up_color"], font=("Segoe UI", 10, "bold")
        )
        self.canvas.create_text(
            w - 6, h - 18, anchor="e",
            text=f"\u2193{format_bytes(data['session_down'])}  \u2191{format_bytes(data['session_up'])}",
            fill=cfg["text_color"], font=("Segoe UI", 8)
        )

        # resize grip (small diagonal dots, bottom-right corner)
        for i in range(3):
            off = i * 4
            self.canvas.create_line(
                w - 4 - off, h - 3, w - 3, h - 4 - off,
                fill=cfg["text_color"], width=1
            )

        self.win.after(cfg["update_ms"], self._redraw)


# --------------------------------------------------------------------------
# Active connections window (per-process "internet activity")
# --------------------------------------------------------------------------

class ConnectionsWindow:
    def __init__(self, root):
        self.win = tk.Toplevel(root)
        self.win.title("NetMeter - Active Internet Connections")
        self.win.geometry("720x420")

        cols = ("pid", "process", "local", "remote", "status")
        self.tree = ttk.Treeview(self.win, columns=cols, show="headings")
        headings = {
            "pid": "PID", "process": "Process", "local": "Local Address",
            "remote": "Remote Address", "status": "Status",
        }
        widths = {"pid": 60, "process": 160, "local": 170, "remote": 190, "status": 100}
        for c in cols:
            self.tree.heading(c, text=headings[c])
            self.tree.column(c, width=widths[c])
        self.tree.pack(fill="both", expand=True, side="left")

        scroll = ttk.Scrollbar(self.win, command=self.tree.yview)
        scroll.pack(fill="y", side="right")
        self.tree.configure(yscrollcommand=scroll.set)

        self._refresh()

    def _refresh(self):
        self.tree.delete(*self.tree.get_children())
        try:
            conns = psutil.net_connections(kind="inet")
        except (psutil.AccessDenied, PermissionError):
            self.tree.insert("", "end", values=("-", "Run as admin to see", "-", "-", "-"))
            self.win.after(3000, self._refresh)
            return

        for c in conns:
            if not c.raddr:
                continue  # only show connections that actually go out to a remote host
            try:
                pname = psutil.Process(c.pid).name() if c.pid else "?"
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pname = "?"
            local = f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else "-"
            remote = f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else "-"
            self.tree.insert("", "end", values=(c.pid or "-", pname, local, remote, c.status))

        self.win.after(3000, self._refresh)


class SettingsWindow:
    """Lets the user resize the widget precisely, tweak opacity/refresh
    rate/graph history, and pick colors -- all applied live."""

    def __init__(self, root, cfg, on_apply):
        self.cfg = cfg
        self.on_apply = on_apply

        self.win = tk.Toplevel(root)
        self.win.title("NetMeter Settings")
        self.win.resizable(False, False)
        self.win.attributes("-topmost", True)

        pad = {"padx": 10, "pady": 6}

        # --- Size -----------------------------------------------------
        frame_size = ttk.LabelFrame(self.win, text="Widget size")
        frame_size.grid(row=0, column=0, sticky="ew", **pad)

        ttk.Label(frame_size, text="Width (px)").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.width_var = tk.IntVar(value=cfg["width"])
        ttk.Spinbox(
            frame_size, from_=MIN_WIDTH, to=MAX_WIDTH, increment=10,
            textvariable=self.width_var, width=6
        ).grid(row=0, column=1, padx=6)

        ttk.Label(frame_size, text="Height (px)").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        self.height_var = tk.IntVar(value=cfg["height"])
        ttk.Spinbox(
            frame_size, from_=MIN_HEIGHT, to=MAX_HEIGHT, increment=10,
            textvariable=self.height_var, width=6
        ).grid(row=1, column=1, padx=6)

        ttk.Label(
            frame_size, text="Tip: you can also drag the bottom-right\ncorner of the widget itself to resize.",
            foreground="#666", justify="left"
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=6, pady=(2, 4))

        # --- Behaviour --------------------------------------------------
        frame_disp = ttk.LabelFrame(self.win, text="Behavior")
        frame_disp.grid(row=1, column=0, sticky="ew", **pad)

        ttk.Label(frame_disp, text="Opacity").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.opacity_var = tk.DoubleVar(value=cfg["opacity"])
        ttk.Scale(
            frame_disp, from_=0.3, to=1.0, variable=self.opacity_var,
            orient="horizontal", length=160
        ).grid(row=0, column=1, padx=6)

        ttk.Label(frame_disp, text="Update interval (ms)").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        self.interval_var = tk.IntVar(value=cfg["update_ms"])
        ttk.Spinbox(
            frame_disp, from_=200, to=5000, increment=100,
            textvariable=self.interval_var, width=6
        ).grid(row=1, column=1, padx=6)

        ttk.Label(frame_disp, text="Graph history (points)").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        self.history_var = tk.IntVar(value=cfg["history_len"])
        ttk.Spinbox(
            frame_disp, from_=10, to=300, increment=10,
            textvariable=self.history_var, width=6
        ).grid(row=2, column=1, padx=6)

        # --- Startup ----------------------------------------------------
        frame_startup = ttk.LabelFrame(self.win, text="Startup")
        frame_startup.grid(row=2, column=0, sticky="ew", **pad)

        self.startup_var = tk.BooleanVar(value=is_startup_enabled())
        self.startup_var.set(is_startup_enabled() if STARTUP_SUPPORTED else False)
        startup_label = (
            "Start NetMeter when I log in"
            if STARTUP_SUPPORTED else
            "Start NetMeter when I log in (unsupported on this OS)"
        )
        startup_check = ttk.Checkbutton(
            frame_startup, text=startup_label, variable=self.startup_var
        )
        startup_check.grid(row=0, column=0, sticky="w", padx=6, pady=4)
        if not STARTUP_SUPPORTED:
            startup_check.state(["disabled"])

        # --- Colors -------------------------------------------------------
        frame_color = ttk.LabelFrame(self.win, text="Colors")
        frame_color.grid(row=3, column=0, sticky="ew", **pad)

        self.color_keys = [
            ("bg_color", "Background"),
            ("down_color", "Download line"),
            ("up_color", "Upload line"),
            ("text_color", "Text"),
            ("grid_color", "Grid lines"),
        ]
        self.swatches = {}
        for i, (key, label) in enumerate(self.color_keys):
            ttk.Label(frame_color, text=label).grid(row=i, column=0, sticky="w", padx=6, pady=3)
            swatch = tk.Label(frame_color, text="        ", bg=cfg[key], relief="sunken")
            swatch.grid(row=i, column=1, padx=6)
            self.swatches[key] = swatch
            ttk.Button(
                frame_color, text="Choose...",
                command=lambda k=key: self._pick_color(k)
            ).grid(row=i, column=2, padx=6)

        # --- Buttons --------------------------------------------------
        frame_btn = ttk.Frame(self.win)
        frame_btn.grid(row=4, column=0, sticky="ew", **pad)
        ttk.Button(frame_btn, text="Apply", command=self._apply).pack(side="left", padx=4)
        ttk.Button(frame_btn, text="Reset to defaults", command=self._reset_defaults).pack(side="left", padx=4)
        ttk.Button(frame_btn, text="Close", command=self.win.destroy).pack(side="right", padx=4)

    def _pick_color(self, key):
        _, hex_color = colorchooser.askcolor(color=self.cfg[key], title="Choose color", parent=self.win)
        if hex_color:
            self.cfg[key] = hex_color
            self.swatches[key].configure(bg=hex_color)

    def _apply(self):
        self.cfg["width"] = min(MAX_WIDTH, max(MIN_WIDTH, self.width_var.get()))
        self.cfg["height"] = min(MAX_HEIGHT, max(MIN_HEIGHT, self.height_var.get()))
        self.cfg["opacity"] = round(self.opacity_var.get(), 2)
        self.cfg["update_ms"] = max(200, self.interval_var.get())
        self.cfg["history_len"] = max(10, self.history_var.get())

        want_startup = self.startup_var.get()
        if want_startup != is_startup_enabled():
            set_startup_enabled(want_startup)
        self.cfg["start_on_boot"] = is_startup_enabled()

        self.on_apply()

    def _reset_defaults(self):
        for k, v in DEFAULT_CONFIG.items():
            if k not in ("x", "y", "start_on_boot"):
                self.cfg[k] = v
        self.width_var.set(self.cfg["width"])
        self.height_var.set(self.cfg["height"])
        self.opacity_var.set(self.cfg["opacity"])
        self.interval_var.set(self.cfg["update_ms"])
        self.history_var.set(self.cfg["history_len"])
        for key, _ in self.color_keys:
            self.swatches[key].configure(bg=self.cfg[key])
        self.on_apply()


# --------------------------------------------------------------------------
# Traffic usage statistics window
# --------------------------------------------------------------------------

class StatsWindow:
    """Shows today / 7-day / month / all-time totals, a 30-day bar chart
    and a per-day history table. Refreshes itself every couple of seconds."""

    def __init__(self, root, cfg, app):
        self.cfg = cfg
        self.app = app
        self._iids = {}

        self.win = tk.Toplevel(root)
        self.win.title("NetMeter - Traffic Usage Statistics")
        self.win.geometry("680x680")
        self.win.minsize(560, 560)

        # --- Buttons (packed first so they stay visible at the bottom) -----
        btns = ttk.Frame(self.win)
        btns.pack(side="bottom", fill="x", padx=10, pady=(4, 10))
        ttk.Button(btns, text="Clear history...", command=self._clear).pack(side="left")
        ttk.Button(btns, text="Close", command=self.win.destroy).pack(side="right")

        notebook = ttk.Notebook(self.win)
        notebook.pack(fill="both", expand=True, padx=4, pady=(6, 0))
        tab_daily = ttk.Frame(notebook)
        tab_monthly = ttk.Frame(notebook)
        notebook.add(tab_daily, text="  Daily  ")
        notebook.add(tab_monthly, text="  Monthly  ")

        # --- Summary -----------------------------------------------------
        summary = ttk.LabelFrame(tab_daily, text="Summary")
        summary.pack(fill="x", padx=10, pady=(10, 4))
        self.sum_tree = ttk.Treeview(
            summary, columns=("period", "down", "up", "total"),
            show="headings", height=6
        )
        for col, text, width in (
            ("period", "Period", 170), ("down", "Downloaded", 150),
            ("up", "Uploaded", 150), ("total", "Total", 150),
        ):
            self.sum_tree.heading(col, text=text)
            self.sum_tree.column(col, width=width, anchor="w" if col == "period" else "e")
        self.sum_tree.pack(fill="x", padx=6, pady=6)

        # --- Chart -------------------------------------------------------
        chart_frame = ttk.LabelFrame(tab_daily, text="Last 30 days  (cyan = download, orange = upload)")
        chart_frame.pack(fill="x", padx=10, pady=4)
        self.chart = tk.Canvas(chart_frame, height=170, bg=cfg["bg_color"], highlightthickness=0)
        self.chart.pack(fill="x", padx=6, pady=6)
        self.chart.bind("<Configure>", lambda e: self._draw_chart())

        # --- History table -----------------------------------------------
        hist = ttk.LabelFrame(tab_daily, text="Daily history")
        hist.pack(fill="both", expand=True, padx=10, pady=4)
        self.hist_tree = ttk.Treeview(
            hist, columns=("date", "down", "up", "total"), show="headings"
        )
        for col, text, width in (
            ("date", "Date", 120), ("down", "Downloaded", 150),
            ("up", "Uploaded", 150), ("total", "Total", 150),
        ):
            self.hist_tree.heading(col, text=text)
            self.hist_tree.column(col, width=width, anchor="w" if col == "date" else "e")
        scroll = ttk.Scrollbar(hist, command=self.hist_tree.yview)
        self.hist_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y", pady=6)
        self.hist_tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)

        # --- Monthly tab -------------------------------------------------
        mchart_frame = ttk.LabelFrame(
            tab_monthly, text="Last 12 months  (cyan = download, orange = upload)"
        )
        mchart_frame.pack(fill="x", padx=10, pady=(10, 4))
        self.mchart = tk.Canvas(mchart_frame, height=190, bg=cfg["bg_color"], highlightthickness=0)
        self.mchart.pack(fill="x", padx=6, pady=6)
        self.mchart.bind("<Configure>", lambda e: self._draw_month_chart())

        mtable = ttk.LabelFrame(tab_monthly, text="Monthly totals")
        mtable.pack(fill="both", expand=True, padx=10, pady=4)
        self.month_tree = ttk.Treeview(
            mtable, columns=("month", "days", "down", "up", "total"), show="headings"
        )
        for col, text, width, anchor in (
            ("month", "Month", 110, "w"), ("days", "Days recorded", 100, "e"),
            ("down", "Downloaded", 130, "e"), ("up", "Uploaded", 130, "e"),
            ("total", "Total", 130, "e"),
        ):
            self.month_tree.heading(col, text=text)
            self.month_tree.column(col, width=width, anchor=anchor)
        mscroll = ttk.Scrollbar(mtable, command=self.month_tree.yview)
        self.month_tree.configure(yscrollcommand=mscroll.set)
        mscroll.pack(side="right", fill="y", pady=6)
        self.month_tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        self._month_iids = {}

        self.rows = []
        self._refresh()

    # -- helpers ----------------------------------------------------------
    def _clear(self):
        if messagebox.askyesno(
            "Clear history",
            "Delete all saved daily usage history?\nThis cannot be undone.",
            parent=self.win,
        ):
            self.app.clear_history()
            self._iids = {}
            self.hist_tree.delete(*self.hist_tree.get_children())
            self._refresh(reschedule=False)

    def _update_summary(self):
        today = date.today()
        month_prefix = today.strftime("%Y-%m")
        week_start = (today - timedelta(days=6)).isoformat()
        prev = (today.replace(day=1) - timedelta(days=1))
        prev_prefix = prev.strftime("%Y-%m")

        def total(pred):
            d = sum(r[1] for r in self.rows if pred(r[0]))
            u = sum(r[2] for r in self.rows if pred(r[0]))
            return d, u

        n_days = max(1, len(self.rows))
        all_d, all_u = total(lambda k: True)
        entries = [
            ("Today", total(lambda k: k == today.isoformat())),
            ("Last 7 days", total(lambda k: k >= week_start)),
            ("This month", total(lambda k: k.startswith(month_prefix))),
            ("Last month", total(lambda k: k.startswith(prev_prefix))),
            ("All recorded time", (all_d, all_u)),
            (f"Daily average ({len(self.rows)} day{'s' if len(self.rows) != 1 else ''})",
             (all_d // n_days, all_u // n_days)),
        ]
        self.sum_tree.delete(*self.sum_tree.get_children())
        for label, (d, u) in entries:
            self.sum_tree.insert(
                "", "end",
                values=(label, format_bytes(d), format_bytes(u), format_bytes(d + u))
            )

    def _update_history(self):
        newest_first = sorted(self.rows, key=lambda r: r[0], reverse=True)
        dates = [r[0] for r in newest_first]
        if set(dates) != set(self._iids):
            # A day was added/removed -> rebuild (rare: once per day)
            self.hist_tree.delete(*self.hist_tree.get_children())
            self._iids = {}
            for r in newest_first:
                self._iids[r[0]] = self.hist_tree.insert("", "end", values=self._fmt_row(r))
        else:
            # Same days -> update values in place so scroll position is kept
            for r in newest_first:
                self.hist_tree.item(self._iids[r[0]], values=self._fmt_row(r))

    @staticmethod
    def _fmt_row(r):
        return (r[0], format_bytes(r[1]), format_bytes(r[2]), format_bytes(r[1] + r[2]))

    def _draw_chart(self):
        c = self.chart
        cfg = self.cfg
        c.delete("all")
        c.configure(bg=cfg["bg_color"])
        w = max(300, c.winfo_width())
        h = int(c["height"])
        left, right, top, bottom = 62, 8, 10, 20

        today = date.today()
        days = [(today - timedelta(days=29 - i)) for i in range(30)]
        by_date = {r[0]: (r[1], r[2]) for r in self.rows}
        series = [by_date.get(d.isoformat(), (0, 0)) for d in days]
        peak = max([max(d, u) for d, u in series] + [1024])

        plot_h = h - top - bottom
        plot_w = w - left - right
        c.create_line(left, top, w - right, top, fill=cfg["grid_color"])
        c.create_line(left, top + plot_h / 2, w - right, top + plot_h / 2, fill=cfg["grid_color"])
        c.create_line(left, top + plot_h, w - right, top + plot_h, fill=cfg["grid_color"])
        c.create_text(left - 6, top, text=format_bytes(peak), anchor="e",
                      fill=cfg["text_color"], font=("Segoe UI", 8))
        c.create_text(left - 6, top + plot_h / 2, text=format_bytes(peak / 2), anchor="e",
                      fill=cfg["text_color"], font=("Segoe UI", 8))
        c.create_text(left - 6, top + plot_h, text="0", anchor="e",
                      fill=cfg["text_color"], font=("Segoe UI", 8))

        group_w = plot_w / 30.0
        bar_w = max(2, group_w * 0.38)
        for i, (d, u) in enumerate(series):
            x0 = left + i * group_w + group_w * 0.1
            for j, (val, color) in enumerate(((d, cfg["down_color"]), (u, cfg["up_color"]))):
                bh = (val / peak) * plot_h
                if bh > 0:
                    bx = x0 + j * bar_w
                    c.create_rectangle(bx, top + plot_h - bh, bx + bar_w - 1,
                                       top + plot_h, fill=color, outline="")
        c.create_text(left, h - 4, text=days[0].strftime("%b %d"), anchor="sw",
                      fill=cfg["text_color"], font=("Segoe UI", 8))
        c.create_text(w - right, h - 4, text=days[-1].strftime("%b %d"), anchor="se",
                      fill=cfg["text_color"], font=("Segoe UI", 8))

    def _monthly(self):
        """Group the daily rows by month -> {'YYYY-MM': [down, up, days]}."""
        months = {}
        for d, dn, up in self.rows:
            m = months.setdefault(d[:7], [0, 0, 0])
            m[0] += dn
            m[1] += up
            m[2] += 1
        return months

    @staticmethod
    def _month_label(key, short=False):
        try:
            y, m = key.split("-")
            return date(int(y), int(m), 1).strftime("%b" if short else "%b %Y")
        except (ValueError, TypeError):
            return key

    def _update_monthly(self):
        months = self._monthly()
        keys = sorted(months, reverse=True)

        def vals(k):
            dn, up, n = months[k]
            return (self._month_label(k), n, format_bytes(dn), format_bytes(up),
                    format_bytes(dn + up))

        if set(keys) != set(self._month_iids):
            self.month_tree.delete(*self.month_tree.get_children())
            self._month_iids = {}
            for k in keys:
                self._month_iids[k] = self.month_tree.insert("", "end", values=vals(k))
        else:
            for k in keys:
                self.month_tree.item(self._month_iids[k], values=vals(k))

    def _draw_month_chart(self):
        c = self.mchart
        cfg = self.cfg
        c.delete("all")
        c.configure(bg=cfg["bg_color"])
        w = max(300, c.winfo_width())
        h = int(c["height"])
        left, right, top, bottom = 62, 8, 10, 22

        today = date.today()
        keys = []
        y, m = today.year, today.month
        for _ in range(12):
            keys.append(f"{y:04d}-{m:02d}")
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        keys.reverse()

        months = self._monthly()
        series = [tuple(months.get(k, [0, 0, 0])[:2]) for k in keys]
        peak = max([max(d, u) for d, u in series] + [1024])

        plot_h = h - top - bottom
        plot_w = w - left - right
        for frac in (0, 0.5, 1):
            gy = top + plot_h * frac
            c.create_line(left, gy, w - right, gy, fill=cfg["grid_color"])
        c.create_text(left - 6, top, text=format_bytes(peak), anchor="e",
                      fill=cfg["text_color"], font=("Segoe UI", 8))
        c.create_text(left - 6, top + plot_h / 2, text=format_bytes(peak / 2), anchor="e",
                      fill=cfg["text_color"], font=("Segoe UI", 8))
        c.create_text(left - 6, top + plot_h, text="0", anchor="e",
                      fill=cfg["text_color"], font=("Segoe UI", 8))

        group_w = plot_w / 12.0
        bar_w = max(3, group_w * 0.34)
        for i, (k, (d, u)) in enumerate(zip(keys, series)):
            x0 = left + i * group_w + group_w * 0.15
            for j, (val, color) in enumerate(((d, cfg["down_color"]), (u, cfg["up_color"]))):
                bh = (val / peak) * plot_h
                if bh > 0:
                    bx = x0 + j * bar_w
                    c.create_rectangle(bx, top + plot_h - bh, bx + bar_w - 1,
                                       top + plot_h, fill=color, outline="")
            label = self._month_label(k, short=True)
            if i == 0 or k.endswith("-01"):
                label += " '" + k[2:4]
            c.create_text(left + i * group_w + group_w / 2, h - 4, text=label,
                          anchor="s", fill=cfg["text_color"], font=("Segoe UI", 8))

    def _refresh(self, reschedule=True):
        if not self.win.winfo_exists():
            return
        self.rows = self.app.usage_rows()
        self._update_summary()
        self._update_history()
        self._draw_chart()
        self._update_monthly()
        self._draw_month_chart()
        if reschedule:
            self.win.after(2000, self._refresh)


# --------------------------------------------------------------------------
# System tray icon
# --------------------------------------------------------------------------

def make_app_icon(size=64):
    """Draw the NetMeter icon (cyan down arrow + orange up arrow) in code,
    so it works inside a PyInstaller exe with no image file needed."""
    big = 256
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 248, 248), fill="#0c0c0f", outline="#00d4ff", width=10)
    d.polygon([(90, 90), (90, 150), (70, 150), (100, 190), (130, 150), (110, 150), (110, 90)],
              fill="#00d4ff")
    d.polygon([(146, 190), (146, 130), (126, 130), (156, 90), (186, 130), (166, 130), (166, 190)],
              fill="#ff7a45")
    if size != big:
        img = img.resize((size, size), Image.LANCZOS)
    return img


def make_tray_image():
    return make_app_icon(64)


class TrayIcon:
    def __init__(self, sampler, on_show, on_hide, on_connections, on_settings, on_stats, on_quit):
        self.sampler = sampler
        self.on_show = on_show
        self.on_hide = on_hide
        self.on_connections = on_connections
        self.on_settings = on_settings
        self.on_stats = on_stats
        self.on_quit = on_quit
        self.icon = None

    def build(self):
        menu = pystray.Menu(
            pystray.MenuItem("Show widget", lambda: self.on_show()),
            pystray.MenuItem("Hide widget", lambda: self.on_hide()),
            pystray.MenuItem("Active connections...", lambda: self.on_connections()),
            pystray.MenuItem("Usage statistics...", lambda: self.on_stats()),
            pystray.MenuItem("Settings...", lambda: self.on_settings()),
            pystray.MenuItem("Quit", lambda: self.on_quit()),
        )
        self.icon = pystray.Icon(APP_NAME, make_tray_image(), "NetMeter", menu)
        return self.icon

    def update_tooltip(self):
        if not self.icon:
            return
        data = self.sampler.snapshot()
        # Plain ASCII on purpose: on Linux/X11, pystray sets the tray
        # tooltip via the WM_NAME property, which only supports Latin-1 --
        # Unicode arrows there raise UnicodeEncodeError and crash the
        # update loop. Windows/macOS backends would be fine with Unicode,
        # but ASCII keeps this one code path safe everywhere.
        title = (
            f"NetMeter  D:{format_speed(data['down_speed'])}"
            f"  U:{format_speed(data['up_speed'])}"
        )
        try:
            self.icon.title = title
        except Exception:
            # Tray backend hiccups (e.g. no systray manager running, or a
            # transient X11 error) shouldn't take down the whole app.
            pass

    def run(self):
        self.build()
        self.icon.run()

    def stop(self):
        if self.icon:
            self.icon.stop()


# --------------------------------------------------------------------------
# App wiring
# --------------------------------------------------------------------------

class NetMeterApp:
    def __init__(self):
        self.cfg = load_config()
        self.root = tk.Tk()
        self.root.withdraw()  # no main window, just the overlay + tray
        self._set_app_icon()

        self.sampler = NetworkSampler(
            history_len=self.cfg["history_len"], interval_ms=self.cfg["update_ms"]
        )
        self.sampler.start()

        self._load_day_baseline()

        self.widget = MeterWidget(
            self.root, self.cfg, self.sampler, self.quit,
            on_open_settings=self.open_settings,
            on_open_stats=self.open_stats,
        )
        self.connections_win = None
        self.settings_win = None
        self.stats_win = None

        self.tray = None
        if TRAY_AVAILABLE:
            self.tray = TrayIcon(
                self.sampler,
                on_show=self.widget.show,
                on_hide=lambda: self.widget._hide(),
                on_connections=lambda: self.root.after(0, self.open_connections),
                on_settings=lambda: self.root.after(0, self.open_settings),
                on_stats=lambda: self.root.after(0, self.open_stats),
                on_quit=self.quit,
            )
            threading.Thread(target=self.tray.run, daemon=True).start()
            self._tick_tray()
        else:
            print(
                "[NetMeter] pystray/Pillow not installed - running without a "
                "tray icon. Install with: pip install pystray pillow"
            )

        self._tick_logger()

    def _set_app_icon(self):
        """Replace Tk's default feather icon on every NetMeter window
        (taskbar + title bars of Settings/Statistics/Connections)."""
        if not ICON_AVAILABLE:
            return
        try:
            self._icon_photos = [ImageTk.PhotoImage(make_app_icon(sz)) for sz in (256, 64, 32)]
            self.root.iconphoto(True, *self._icon_photos)  # True = all future Toplevels too
        except Exception:
            pass

    def _tick_tray(self):
        if self.tray:
            self.tray.update_tooltip()
        self.root.after(1000, self._tick_tray)

    # -- daily usage accounting ------------------------------------------
    # The sampler only knows bytes since this launch. To keep true per-day
    # totals across restarts and midnight, we remember what the CSV already
    # held for today (base) and the sampler's counter at day start (offset).
    def _load_day_baseline(self):
        self.day = date.today()
        self.base_down = self.base_up = 0
        for d, dn, up in read_usage_history():
            if d == self.day.isoformat():
                self.base_down, self.base_up = dn, up
        snap = self.sampler.snapshot()
        self.offset_down = snap["session_down"]
        self.offset_up = snap["session_up"]

    def today_totals(self):
        snap = self.sampler.snapshot()
        if date.today() != self.day:
            # Midnight passed: save the finished day, start a fresh one.
            log_daily_usage(
                self.base_down + snap["session_down"] - self.offset_down,
                self.base_up + snap["session_up"] - self.offset_up,
                day=self.day.isoformat(),
            )
            self.day = date.today()
            self.base_down = self.base_up = 0
            self.offset_down = snap["session_down"]
            self.offset_up = snap["session_up"]
        return (
            self.base_down + snap["session_down"] - self.offset_down,
            self.base_up + snap["session_up"] - self.offset_up,
        )

    def usage_rows(self):
        """History from disk with today's row replaced by live numbers."""
        today_d, today_u = self.today_totals()
        rows = {d: (dn, up) for d, dn, up in read_usage_history()}
        rows[self.day.isoformat()] = (today_d, today_u)
        return sorted((d, v[0], v[1]) for d, v in rows.items())

    def clear_history(self):
        try:
            if os.path.exists(LOG_FILE):
                os.remove(LOG_FILE)
        except OSError:
            pass
        self._load_day_baseline()
        self.base_down = self.base_up = 0

    def _tick_logger(self):
        down, up = self.today_totals()
        log_daily_usage(down, up, day=self.day.isoformat())
        self.root.after(30_000, self._tick_logger)

    def open_stats(self):
        if self.stats_win is None or not tk.Toplevel.winfo_exists(self.stats_win.win):
            self.stats_win = StatsWindow(self.root, self.cfg, self)
        else:
            self.stats_win.win.lift()

    def open_connections(self):
        if self.connections_win is None or not tk.Toplevel.winfo_exists(self.connections_win.win):
            self.connections_win = ConnectionsWindow(self.root)

    def open_settings(self):
        if self.settings_win is None or not tk.Toplevel.winfo_exists(self.settings_win.win):
            self.settings_win = SettingsWindow(self.root, self.cfg, self.apply_settings)
        else:
            self.settings_win.win.lift()

    def apply_settings(self):
        """Push changes made in the Settings window onto the live widget
        and sampler, and persist them to disk."""
        self.widget.apply_cfg_changes()
        self.sampler.reconfigure(
            history_len=self.cfg["history_len"], interval_ms=self.cfg["update_ms"]
        )
        save_config(self.cfg)

    def quit(self):
        try:
            down, up = self.today_totals()
            log_daily_usage(down, up, day=self.day.isoformat())
        except Exception:
            pass
        self.sampler.stop()
        if self.tray:
            self.tray.stop()
        self.root.after(50, self.root.destroy)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FPSMotion.NetMeter")
        except Exception:
            pass
    app = NetMeterApp()
    app.run()
