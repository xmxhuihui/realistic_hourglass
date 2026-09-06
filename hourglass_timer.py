"""Hourglass Timer - a countdown timer for Windows with a real hourglass.

The hourglass is drawn from scratch each frame by ``sand_render``: the sand
level tracks the remaining time by volume, a stream of grains falls through the
neck, and the whole thing turns over when you flip it.

Run with:  pythonw hourglass_timer.py     (or double-click run.bat)
"""

from __future__ import annotations

import base64
import io
import os
import struct
import sys
import tempfile
import time
import tkinter as tk
import wave
import zlib
from tkinter import font as tkfont

import numpy as np

from sand_render import HourglassRenderer

try:                                    # optional: a much faster canvas blit
    from PIL import Image, ImageTk
except Exception:                       # pragma: no cover - fallback path
    Image = ImageTk = None


# ---------------------------------------------------------------------------
# theme
# ---------------------------------------------------------------------------
BG = "#0e1015"
PANEL = "#171b24"
PANEL_HI = "#222836"
LINE = "#252b38"
FG = "#e9e4d8"
MUTED = "#8b93a4"
SAND = "#e3ad55"
SAND_HI = "#f6cf8a"

PRESETS = [("1", 60), ("3", 180), ("5", 300), ("10", 600),
           ("15", 900), ("25", 1500), ("45", 2700), ("60", 3600)]

ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hourglass.ico")

FLIP_SECONDS = 0.85
FRAME_MS = 33


def fmt_time(seconds):
    seconds = max(0, int(seconds + 0.999))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_time(text):
    """Accept '90', '5:30' or '1:02:30'; minutes when there is no colon."""
    text = text.strip().replace("：", ":")
    if not text:
        return None
    try:
        parts = [float(p) for p in text.split(":")]
    except ValueError:
        return None
    if any(p < 0 for p in parts) or len(parts) > 3:
        return None
    if len(parts) == 1:
        total = parts[0] * 60.0
    elif len(parts) == 2:
        total = parts[0] * 60.0 + parts[1]
    else:
        total = parts[0] * 3600.0 + parts[1] * 60.0 + parts[2]
    return total if 1.0 <= total <= 24 * 3600 else None


def enable_dpi_awareness():
    """Ask Windows for real pixels, so the render is not bitmap-stretched."""
    try:
        import ctypes
    except Exception:
        return
    try:                                    # Windows 8.1 and later
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:                                    # older Windows
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


ALARM_NOTES = (988, 1319, 988, 1319, 880)


def alarm_wav_bytes(rate=22050):
    """The chime as a WAV: five notes and then a breath, ready to be looped."""
    parts = []
    for freq in ALARM_NOTES:
        n = int(rate * 0.20)
        t = np.arange(n) / rate
        # fade each note in and out so looping never clicks
        env = np.minimum(1.0, np.minimum(t * 120.0, (n / rate - t) * 40.0))
        parts.append(np.sin(2.0 * np.pi * freq * t) * env * 0.5)
        parts.append(np.zeros(int(rate * 0.06)))
    parts.append(np.zeros(int(rate * 1.2)))        # the pause between rings
    pcm = (np.concatenate(parts) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def window_hwnd(root):
    """The window Windows itself knows about, or None off Windows.

    ``winfo_id`` hands back Tk's inner frame; the taskbar button and the
    foreground rules belong to the top of that chain (GA_ROOT).
    """
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        inner = root.winfo_id()
        return user32.GetAncestor(inner, 2) or inner
    except Exception:
        return None


def flash_taskbar(root, on):
    """Blink the window's taskbar button until it is brought to the front.

    This is what keeps a finished timer noticeable while its window is
    minimised. It is a no-op anywhere but Windows.
    """
    hwnd = window_hwnd(root)
    if not hwnd:
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
    except Exception:
        return

    class FLASHWINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND),
                    ("dwFlags", wintypes.DWORD), ("uCount", wintypes.UINT),
                    ("dwTimeout", wintypes.DWORD)]

    try:
        # FLASHW_ALL | FLASHW_TIMERNOFG: keep blinking until it is foreground.
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x0F if on else 0, 0, 0)
        user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


def show_window(root):
    """Bring a hidden or minimised window back, and put it in front."""
    try:
        root.deiconify()
        root.lift()
    except tk.TclError:
        return
    hwnd = window_hwnd(root)
    if not hwnd:
        return
    try:
        import ctypes
        ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


WM_TRAY = 0x0400 + 20                   # WM_APP + 20: our own tray callback
TRAY_SHOW, TRAY_EXIT = 1, 2             # the two menu commands
TRAY_MENU = 3                           # internal: put the menu up
_tray_serial = 0


class TrayIcon:
    """An icon in the Windows notification area, run from Tk's own loop.

    There is no thread and no extra package behind it: a hidden window takes
    the shell's callbacks and the frame loop drains that one window's queue,
    so Show and Exit arrive on the main thread like any other click. The
    constructor raises if the icon cannot be created; use :func:`make_tray`.
    """


    def __init__(self, title, icon_path, on_show, on_exit):
        global _tray_serial
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.on_show = on_show
        self.on_exit = on_exit
        self.hwnd = self.hmenu = None
        self._added = False
        # both are read by the callback while the window is still being created
        self._taskbar_created = 0
        self._pending = []
        user32 = self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)

        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                        ("lpszMenuName", wintypes.LPCWSTR),
                        ("lpszClassName", wintypes.LPCWSTR)]

        class NOTIFYICONDATA(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                        ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                        ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                        ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                        ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16),
                        ("hBalloonIcon", wintypes.HICON)]

        class MSG(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                        ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                        ("time", wintypes.DWORD), ("pt", wintypes.POINT)]

        self._MSG = MSG
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Handles are pointer-sized: without a restype ctypes hands back a
        # 32-bit int and the top half of the module handle is lost.
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.DefWindowProcW.restype = LRESULT
        # Without these the window parameters are squeezed into a plain int,
        # which overflows: the callback would then raise on the very first
        # message and creating the window would fail.
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.TrackPopupMenu.argtypes = [
            wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, wintypes.HWND, wintypes.LPVOID]
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]
        user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND,
                                        wintypes.UINT, wintypes.UINT, wintypes.UINT]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadIconW.restype = wintypes.HANDLE
        user32.CreatePopupMenu.restype = wintypes.HMENU

        # the callback has to outlive the window, so it is kept on the instance
        self._proc = WNDPROC(self._wndproc)
        _tray_serial += 1
        self._class = "HourglassTimerTray_%d_%d" % (os.getpid(), _tray_serial)
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASS()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = self._class
        self._wc = wc                       # keeps the class name buffer alive
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd = user32.CreateWindowExW(0, self._class, title, 0,
                                           0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

        self.hicon = 0
        if icon_path and os.path.exists(icon_path):
            # IMAGE_ICON, LR_LOADFROMFILE | LR_DEFAULTSIZE: the shell's own size
            self.hicon = user32.LoadImageW(None, icon_path, 1, 0, 0, 0x0010 | 0x0040)
        if not self.hicon:                  # IDI_APPLICATION
            self.hicon = user32.LoadIconW(
                None, ctypes.cast(ctypes.c_void_p(32512), ctypes.c_wchar_p))

        self.hmenu = user32.CreatePopupMenu()
        user32.AppendMenuW(self.hmenu, 0, TRAY_SHOW, "Show")
        user32.AppendMenuW(self.hmenu, 0, TRAY_EXIT, "Exit")

        self._nid = NOTIFYICONDATA()
        self._nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        self._nid.hWnd = self.hwnd
        self._nid.uID = 1
        self._nid.uFlags = 0x01 | 0x02 | 0x04       # MESSAGE | ICON | TIP
        self._nid.uCallbackMessage = WM_TRAY
        self._nid.hIcon = self.hicon
        self._nid.szTip = title[:127]
        # explorer sends this when it restarts, and every icon has to be re-added
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")  # noqa: E501
        if not self._add():
            self.close()
            raise OSError("Shell_NotifyIcon(NIM_ADD) failed")

    def _add(self):
        self._added = bool(self.shell32.Shell_NotifyIconW(      # NIM_ADD
            0, self.ctypes.byref(self._nid)))
        return self._added

    def pump(self):
        """Take what the shell sent us, then act on it.

        Only this window's messages are peeked, so Tk's own queue is left
        alone. Acting happens out here rather than in the window procedure:
        Windows may run that procedure from inside Tcl's message dispatch,
        where calling back into Tk would crash the interpreter.
        """
        if not self.hwnd:
            return
        msg = self._MSG()
        ref = self.ctypes.byref(msg)
        while self.user32.PeekMessageW(ref, self.hwnd, 0, 0, 1):    # PM_REMOVE
            self.user32.TranslateMessage(ref)
            self.user32.DispatchMessageW(ref)
        while self._pending and self.hwnd:
            self._invoke(self._pending.pop(0))

    def _wndproc(self, hwnd, msg, wparam, lparam):
        """Note down what happened; :meth:`pump` is what acts on it."""
        try:
            if msg == WM_TRAY:
                event = lparam & 0xFFFF
                if event in (0x0202, 0x0203):       # WM_LBUTTONUP, double click
                    self._pending.append(TRAY_SHOW)
                elif event == 0x0205:               # WM_RBUTTONUP
                    self._pending.append(TRAY_MENU)
                return 0
            if msg == 0x0111:                       # WM_COMMAND, from the menu
                self._pending.append(wparam & 0xFFFF)
                return 0
            if msg and msg == self._taskbar_created:
                self._added = False
                self._add()
                return 0
        except Exception:
            pass                        # never stall the window over a slip
        return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _popup(self):
        from ctypes import wintypes
        ctypes, user32 = self.ctypes, self.user32
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        # the owner has to be in front or the menu will not close again
        user32.SetForegroundWindow(self.hwnd)
        # TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD: hand the choice back
        cmd = user32.TrackPopupMenu(self.hmenu, 0x0002 | 0x0080 | 0x0100,
                                    pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, 0, 0, 0)     # lets the menu tidy up
        self._invoke(cmd)

    def _invoke(self, cmd):
        if cmd == TRAY_MENU:
            self._popup()
        elif cmd == TRAY_SHOW:
            self.on_show()
        elif cmd == TRAY_EXIT:
            self.on_exit()

    def close(self):
        """Take the icon out of the notification area for good."""
        try:
            if self._added:
                self.shell32.Shell_NotifyIconW(2, self.ctypes.byref(self._nid))
                self._added = False
            if self.hmenu:
                self.user32.DestroyMenu(self.hmenu)
                self.hmenu = None
            if self.hwnd:
                self.user32.DestroyWindow(self.hwnd)
                self.hwnd = None
        except Exception:
            pass


def make_tray(title, icon_path, on_show, on_exit):
    """A tray icon, or None where there is no notification area to put one in."""
    try:
        return TrayIcon(title, icon_path, on_show, on_exit)
    except Exception:
        return None


def png_bytes(arr):
    """Minimal PNG encoder, used only when Pillow is unavailable."""
    h, w, _ = arr.shape
    rows = np.zeros((h, w * 3 + 1), np.uint8)     # leading per-row filter byte
    rows[:, 1:] = arr.reshape(h, w * 3)

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows.tobytes(), 1))
            + chunk(b"IEND", b""))


class Blitter:
    """Pushes numpy RGB frames onto a Tk canvas."""

    def __init__(self, canvas, w, h):
        self.canvas = canvas
        if ImageTk is not None:
            self.photo = ImageTk.PhotoImage(Image.new("RGB", (w, h), BG))
        else:
            self.photo = tk.PhotoImage(width=w, height=h)
        self.item = canvas.create_image(0, 0, anchor="nw", image=self.photo)

    def show(self, arr):
        if ImageTk is not None:
            self.photo.paste(Image.fromarray(arr))
        else:
            self.photo = tk.PhotoImage(data=base64.b64encode(png_bytes(arr)).decode())
            self.canvas.itemconfig(self.item, image=self.photo)


class FlatButton(tk.Label):
    """A dark, flat button; tk.Button on Windows insists on a 3-D border."""

    def __init__(self, master, text, command, primary=False, pad=(14, 7)):
        self.primary = primary
        self.command = command
        super().__init__(master, text=text, bd=0, highlightthickness=0,
                         padx=pad[0], pady=pad[1], cursor="hand2")
        self._paint(False)
        self.bind("<Enter>", lambda e: self._paint(True))
        self.bind("<Leave>", lambda e: self._paint(False))
        self.bind("<Button-1>", self._click)

    def _paint(self, hot):
        if self.primary:
            self.configure(bg=SAND_HI if hot else SAND, fg="#241a08")
        else:
            self.configure(bg=PANEL_HI if hot else PANEL, fg=FG)

    def _click(self, _event):
        self.command()

    def set_text(self, text):
        self.configure(text=text)

    def set_primary(self, on):
        self.primary = bool(on)
        self._paint(False)


class HourglassTimer:
    def __init__(self):
        enable_dpi_awareness()
        self.root = tk.Tk()
        self.root.title("Hourglass Timer")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)

        cw, ch, self.scale = self._pick_size()
        self.renderer = HourglassRenderer(cw, ch)

        self.total = 300.0
        self.remaining = 300.0
        self.running = False
        self.finished = False
        self.flip_start = None
        self.flip_from = None
        self.alarming = False
        self._wav_path = None
        self._bell_after = None
        self._flash_at = 0.0
        self._shown = None
        self._redraw = True
        self._last = time.perf_counter()

        self._build_ui(cw, ch)
        self._set_icon()
        self._bind_keys()
        self._sync_labels()
        # Closing the window parks the app in the notification area instead of
        # ending it, so a running timer survives a stray click on the X.
        self.tray = make_tray("Hourglass Timer", ICON_PATH, self._show, self.quit)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(16, self._loop)

    # -- window -----------------------------------------------------------
    def _pick_size(self):
        """Render at the monitor's real pixel density, but keep the window on
        screen when Windows is scaling at 125-200%."""
        scale = max(1.0, min(2.0, self.root.winfo_fpixels("1i") / 96.0))
        avail_h = self.root.winfo_screenheight() * 0.86
        base_h = 520.0
        base_w = 390.0
        chrome = 250.0 * scale          # digits, presets and buttons below
        while base_h * scale + chrome > avail_h and base_h > 340:
            base_h -= 20
            base_w -= 15
        return int(base_w * scale), int(base_h * scale), scale

    def _f(self, points, weight="normal", family="Segoe UI"):
        """Font sizes stay in points: Tk already scales those by the screen
        DPI, so multiplying by our own factor would scale them twice."""
        return tkfont.Font(family=family, size=int(round(points)), weight=weight)

    def _build_ui(self, cw, ch):
        s = self.scale
        pad = int(16 * s)

        self.canvas = tk.Canvas(self.root, width=cw, height=ch, bd=0,
                                highlightthickness=0, bg=BG, cursor="hand2")
        self.canvas.pack()
        self.blit = Blitter(self.canvas, cw, ch)
        self.canvas.bind("<Button-1>", lambda e: self.flip())
        self.canvas.bind("<MouseWheel>", self._on_wheel)

        panel = tk.Frame(self.root, bg=BG, padx=pad, pady=int(6 * s))
        panel.pack(fill="x")

        head = tk.Frame(panel, bg=BG)
        head.pack(fill="x")
        self.time_label = tk.Label(head, text="05:00", bg=BG, fg=FG,
                                   font=self._f(26, "bold", "Consolas"))
        self.time_label.pack(side="left")
        self.status = tk.Label(head, text="ready", bg=BG, fg=MUTED,
                               font=self._f(9))
        self.status.pack(side="right", pady=int(14 * s))

        tk.Frame(panel, bg=LINE, height=1).pack(fill="x", pady=(int(6 * s), int(10 * s)))

        chips = tk.Frame(panel, bg=BG)
        chips.pack(fill="x")
        self.chips = {}
        for i, (label, secs) in enumerate(PRESETS):
            b = FlatButton(chips, f"{label}m", lambda v=secs: self.set_duration(v),
                           pad=(int(9 * s), int(5 * s)))
            b.configure(font=self._f(9))
            b.grid(row=i // 4, column=i % 4, sticky="ew",
                   padx=(0 if i % 4 == 0 else int(6 * s), 0),
                   pady=(0, int(6 * s)))
            self.chips[secs] = b
        for c in range(4):
            chips.grid_columnconfigure(c, weight=1, uniform="chip")

        custom = tk.Frame(panel, bg=BG)
        custom.pack(fill="x", pady=(int(2 * s), int(10 * s)))
        tk.Label(custom, text="custom", bg=BG, fg=MUTED,
                 font=self._f(9)).pack(side="left", padx=(0, int(8 * s)))
        self.entry = tk.Entry(custom, bg=PANEL, fg=FG, bd=0, insertbackground=SAND,
                              highlightthickness=1, highlightbackground=LINE,
                              highlightcolor=SAND, font=self._f(10),
                              justify="center")
        self.entry.insert(0, "mm:ss")
        self.entry.configure(fg=MUTED)
        self.entry.pack(side="left", fill="x", expand=True, ipady=int(5 * s))
        self.entry.bind("<FocusIn>", self._entry_focus)
        self.entry.bind("<Return>", lambda e: self._apply_custom())
        apply_btn = FlatButton(custom, "set", self._apply_custom,
                               pad=(int(11 * s), int(6 * s)))
        apply_btn.configure(font=self._f(9))
        apply_btn.pack(side="left", padx=(int(6 * s), 0))

        row = tk.Frame(panel, bg=BG)
        row.pack(fill="x")
        self.start_btn = FlatButton(row, "Start", self.toggle, primary=True,
                                    pad=(int(10 * s), int(9 * s)))
        self.start_btn.configure(font=self._f(11, "bold"))
        self.start_btn.pack(side="left", fill="x", expand=True)
        self.reset_btn = FlatButton(row, "Reset", self.reset,
                                    pad=(int(10 * s), int(9 * s)))
        self.reset_btn.configure(font=self._f(11))
        self.reset_btn.pack(side="left", fill="x", expand=True, padx=int(6 * s))
        self.flip_btn = FlatButton(row, "Flip", self.flip,
                                   pad=(int(10 * s), int(9 * s)))
        self.flip_btn.configure(font=self._f(11))
        self.flip_btn.pack(side="left", fill="x", expand=True)

        foot = tk.Frame(panel, bg=BG)
        foot.pack(fill="x", pady=(int(9 * s), int(4 * s)))
        self.on_top = False
        self.top_btn = FlatButton(foot, "always on top", self._toggle_topmost,
                                  pad=(int(10 * s), int(5 * s)))
        self.top_btn.configure(font=self._f(9))
        self.top_btn.pack(side="left")
        tk.Label(foot, text="space · r · f", bg=BG, fg="#5c6373",
                 font=self._f(8)).pack(side="right", pady=int(6 * s))

    def _set_icon(self):
        if Image is None:
            return
        try:
            small = Image.fromarray(self.renderer.frame()).resize((64, 64), Image.LANCZOS)
            self._icon = ImageTk.PhotoImage(small)
            self.root.iconphoto(True, self._icon)
        except Exception:
            pass

    def _bind_keys(self):
        r = self.root
        r.bind("<space>", lambda e: self._key(self.toggle))
        r.bind("<r>", lambda e: self._key(self.reset))
        r.bind("<R>", lambda e: self._key(self.reset))
        r.bind("<f>", lambda e: self._key(self.flip))
        r.bind("<F>", lambda e: self._key(self.flip))

    def _key(self, action):
        if self.root.focus_get() is self.entry:
            return None
        action()
        return "break"

    # -- timer ------------------------------------------------------------
    def set_duration(self, seconds):
        self.total = float(seconds)
        self.remaining = float(seconds)
        self.running = False
        self.finished = False
        self._stop_alarm()
        self._sync_labels()

    def toggle(self):
        if self.finished:
            self.reset()
            return
        self.running = not self.running
        self._stop_alarm()
        self._sync_labels()

    def reset(self):
        self.running = False
        self.finished = False
        self.remaining = self.total
        self._stop_alarm()
        self._sync_labels()

    def flip(self):
        if self.flip_start is not None:
            return
        self.flip_start = time.perf_counter()
        self.flip_from = self.remaining
        self._stop_alarm()

    def _finish_flip(self):
        self.remaining = max(0.0, self.total - self.flip_from)
        self.finished = False
        if self.remaining <= 0.0:            # flipped an empty glass: run again
            self.remaining = self.total
        self.running = True
        self.flip_start = None
        self._sync_labels()

    def _apply_custom(self):
        value = parse_time(self.entry.get())
        if value is None:
            self.status.configure(text="use mm:ss", fg="#d9704f")
            self.root.after(1400, self._sync_labels)
            return
        self.set_duration(value)
        self.root.focus_set()

    def _entry_focus(self, _event):
        if self.entry.get() == "mm:ss":
            self.entry.delete(0, "end")
        self.entry.configure(fg=FG)

    def _on_wheel(self, event):
        if self.running or self.flip_start is not None:
            return
        step = 60.0 if self.total >= 120 else 30.0
        self.set_duration(min(24 * 3600.0, max(30.0, self.total + (step if event.delta > 0 else -step))))

    def _on_close(self):
        """The X button hides to the tray; the timer keeps counting."""
        if self.tray is None:               # nowhere to hide: really quit
            self.quit()
        else:
            self.root.withdraw()

    def _show(self):
        show_window(self.root)

    def quit(self):
        """End the application, icon and all."""
        self._stop_alarm()
        if self.tray is not None:
            self.tray.close()
            self.tray = None
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _toggle_topmost(self):
        self.on_top = not self.on_top
        self.root.attributes("-topmost", self.on_top)
        self.top_btn.set_primary(self.on_top)

    def _sync_labels(self):
        self._redraw = True
        self._paint_labels()

    def _show_time(self):
        text = fmt_time(self.remaining)
        if text != self._shown:
            self._shown = text
            self.time_label.configure(text=text)

    def _paint_labels(self):
        self._show_time()
        if self.finished:
            text, colour = "time's up - press Reset", SAND
        elif self.running:
            text, colour = "running", MUTED
        elif self.remaining < self.total:
            text, colour = "paused", MUTED
        else:
            text, colour = f"{fmt_time(self.total)} ready", MUTED
        self.status.configure(text=text, fg=colour)
        self.start_btn.set_text("Restart" if self.finished else
                                ("Pause" if self.running else "Start"))
        for secs, chip in self.chips.items():
            chip.set_primary(abs(secs - self.total) < 0.5)

    def _start_alarm(self):
        """Ring, blink and flash the taskbar until the timer is reset.

        Windows loops the wave file on its own, so the chime keeps going
        whatever the window is doing - minimising it does not silence it.
        """
        if self.alarming:
            return
        self.alarming = True
        if not self._alarm_sound(True):     # no winsound: the terminal bell
            self._bell()
        try:                                # come back into view, but do not
            self.root.deiconify()           # steal the keyboard focus
            self.root.lift()
        except tk.TclError:
            pass
        self._flash_at = 0.0

    def _stop_alarm(self):
        """Silence the alarm at once. Safe to call when nothing is ringing.

        Nothing here waits on anything, so Reset cuts the sound in the same
        click that handles it.
        """
        self.alarming = False
        if self._bell_after is not None:
            try:
                self.root.after_cancel(self._bell_after)
            except tk.TclError:
                pass
            self._bell_after = None
        self._alarm_sound(False)
        flash_taskbar(self.root, False)
        try:
            self.time_label.configure(fg=FG)
        except tk.TclError:
            pass

    def _alarm_sound(self, on):
        """Start or stop the looping chime; True if winsound handled it.

        SND_ASYNC hands the loop to Windows and SND_PURGE cuts it off in a
        single call, so stopping never has to wait for a note to finish.
        """
        try:
            import winsound
        except Exception:
            return False
        try:
            if not on:
                winsound.PlaySound(None, winsound.SND_PURGE)
                return True
            if self._wav_path is None:
                path = os.path.join(tempfile.gettempdir(), "hourglass_alarm.wav")
                try:
                    with open(path, "wb") as fh:
                        fh.write(alarm_wav_bytes())
                except OSError:
                    # a second copy of the app may have it open and playing;
                    # what is already on disk is the same chime
                    if not os.path.exists(path):
                        raise
                self._wav_path = path
            winsound.PlaySound(self._wav_path,
                               winsound.SND_FILENAME | winsound.SND_ASYNC
                               | winsound.SND_LOOP | winsound.SND_NODEFAULT)
            return True
        except Exception:
            self._wav_path = None
            return False

    def _bell(self):
        """Fallback ring for platforms without winsound, on the main thread so
        that cancelling it is instant too."""
        self._bell_after = None
        if not self.alarming:
            return
        try:
            self.root.bell()
            self._bell_after = self.root.after(700, self._bell)
        except tk.TclError:
            pass

    # -- frame loop -------------------------------------------------------
    def _loop(self):
        try:
            if self.tray is not None:
                self.tray.pump()
            self._frame()
        except tk.TclError:                 # window closed mid-frame
            pass

    def _frame(self):
        started = now = time.perf_counter()
        elapsed = now - self._last
        self._last = now
        # The clock counts real time, whatever the window is doing; only the
        # sand is capped, so a slow frame cannot spit out a burst of grains.
        dt = min(0.1, elapsed)

        angle = 0.0
        if self.flip_start is not None:
            u = (now - self.flip_start) / FLIP_SECONDS
            if u >= 1.0:
                self._finish_flip()
            else:
                angle = 180.0 * (u * u * (3.0 - 2.0 * u))
        elif self.running:
            self.remaining -= elapsed
            if self.remaining <= 0.0:
                self.remaining = 0.0
                self.running = False
                self.finished = True
                self._start_alarm()
            if self.remaining <= 0.0:
                self._sync_labels()
            else:
                self._show_time()

        if self.alarming:
            on = int(now * 3) % 2
            self.time_label.configure(fg=SAND if on else FG)
            self.status.configure(fg=SAND if on else MUTED)
            if now >= self._flash_at:
                # Re-arm every couple of seconds: Windows stops the blink once
                # the window reaches the foreground, and the user may well
                # minimise it again without touching Reset.
                self._flash_at = now + 2.0
                flash_taskbar(self.root, True)

        # Nothing moves while the glass is paused, so stop drawing entirely
        # rather than burn a core for the length of a one-hour timer.
        moving = (self.running or self.flip_start is not None
                  or bool(self.renderer.particles) or self._redraw)
        # there is nothing to draw for a window in the tray either
        hidden = self.root.state() in ("iconic", "withdrawn")
        if not moving or hidden:
            self.root.after(150, self._loop)
            return

        frac = self.remaining / self.total if self.total else 0.0
        if self.flip_start is not None:
            frac = self.flip_from / self.total if self.total else 0.0
        flowing = self.running and self.flip_start is None
        self.renderer.step(dt, frac, flowing)
        self.blit.show(self.renderer.frame_rotated(angle) if angle else
                       self.renderer.frame())
        self._redraw = False

        # aim for ~30 fps, counting the time this frame actually took
        cost = int((time.perf_counter() - started) * 1000)
        self.root.after(max(3, FRAME_MS - cost), self._loop)

    def run(self):
        self.root.update_idletasks()
        w = self.root.winfo_reqwidth()
        h = self.root.winfo_reqheight()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = max(0, (self.root.winfo_screenheight() - h) // 2 - int(20 * self.scale))
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.mainloop()


def main():
    app = HourglassTimer()
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
