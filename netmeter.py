"""
NetMeter - A mini network monitor for Windows.

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
from datetime import date, datetime

import psutil
import tkinter as tk
from tkinter import ttk, colorchooser

try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except Exception:
    # Covers missing packages AND missing platform tray backends
    # (e.g. no GTK/AppIndicator on some Linux setups). On Windows with
    # `pip install pystray pillow` this will succeed normally.
    TRAY_AVAILABLE = False


# --------------------------------------------------------------------------
# Config / persistence
# --------------------------------------------------------------------------

APP_NAME = "NetMeter"
CONFIG_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME
)
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


def log_daily_usage(down_total, up_total):
    """Append/update today's row in the usage CSV."""
    ensure_config_dir()
    today = date.today().isoformat()
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
# Corner widget
# --------------------------------------------------------------------------

class MeterWidget:
    def __init__(self, root, cfg, sampler, on_close, on_open_settings=None):
        self.root = root
        self.cfg = cfg
        self.sampler = sampler
        self.on_close = on_close
        self.on_open_settings = on_open_settings

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
        self.menu.add_command(label="Settings...", command=self._open_settings)
        self.menu.add_command(label="Hide (tray icon keeps running)", command=self._hide)
        self.menu.add_command(label="Quit NetMeter", command=self.on_close)

    def _open_settings(self):
        if self.on_open_settings:
            self.on_open_settings()

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

        # --- Colors -------------------------------------------------------
        frame_color = ttk.LabelFrame(self.win, text="Colors")
        frame_color.grid(row=2, column=0, sticky="ew", **pad)

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
        frame_btn.grid(row=3, column=0, sticky="ew", **pad)
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
        self.on_apply()

    def _reset_defaults(self):
        for k, v in DEFAULT_CONFIG.items():
            if k not in ("x", "y"):
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
# System tray icon
# --------------------------------------------------------------------------

def make_tray_image(color="#00d4ff"):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=color)
    d.polygon([(20, 40), (32, 24), (44, 40)], fill="#0c0c0f")  # up arrow
    return img


class TrayIcon:
    def __init__(self, sampler, on_show, on_hide, on_connections, on_settings, on_quit):
        self.sampler = sampler
        self.on_show = on_show
        self.on_hide = on_hide
        self.on_connections = on_connections
        self.on_settings = on_settings
        self.on_quit = on_quit
        self.icon = None

    def build(self):
        menu = pystray.Menu(
            pystray.MenuItem("Show widget", lambda: self.on_show()),
            pystray.MenuItem("Hide widget", lambda: self.on_hide()),
            pystray.MenuItem("Active connections...", lambda: self.on_connections()),
            pystray.MenuItem("Settings...", lambda: self.on_settings()),
            pystray.MenuItem("Quit", lambda: self.on_quit()),
        )
        self.icon = pystray.Icon(APP_NAME, make_tray_image(), "NetMeter", menu)
        return self.icon

    def update_tooltip(self):
        if not self.icon:
            return
        data = self.sampler.snapshot()
        self.icon.title = (
            f"NetMeter  \u2193{format_speed(data['down_speed'])}"
            f"  \u2191{format_speed(data['up_speed'])}"
        )

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

        self.sampler = NetworkSampler(
            history_len=self.cfg["history_len"], interval_ms=self.cfg["update_ms"]
        )
        self.sampler.start()

        self.widget = MeterWidget(
            self.root, self.cfg, self.sampler, self.quit,
            on_open_settings=self.open_settings,
        )
        self.connections_win = None
        self.settings_win = None

        self.tray = None
        if TRAY_AVAILABLE:
            self.tray = TrayIcon(
                self.sampler,
                on_show=self.widget.show,
                on_hide=lambda: self.widget._hide(),
                on_connections=self.open_connections,
                on_settings=lambda: self.root.after(0, self.open_settings),
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

    def _tick_tray(self):
        if self.tray:
            self.tray.update_tooltip()
        self.root.after(1000, self._tick_tray)

    def _tick_logger(self):
        data = self.sampler.snapshot()
        log_daily_usage(data["session_down"], data["session_up"])
        self.root.after(30_000, self._tick_logger)

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
    app = NetMeterApp()
    app.run()
