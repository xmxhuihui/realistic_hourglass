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


def flash_taskbar(root, on):
    """Blink the window's taskbar button until it is brought to the front.

    This is what keeps a finished timer noticeable while its window is
    minimised. It is a no-op anywhere but Windows.
    """
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
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        # Tk's toplevel is a child of the real window Windows shows in the
        # taskbar, so ask for the root of the chain (GA_ROOT).
        hwnd = user32.GetAncestor(root.winfo_id(), 2) or root.winfo_id()
        # FLASHW_ALL | FLASHW_TIMERNOFG: keep blinking until it is foreground.
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x0F if on else 0, 0, 0)
        user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


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
            self._frame()
        except tk.TclError:                 # window closed mid-frame
            pass

    def _frame(self):
        started = now = time.perf_counter()
        dt = min(0.1, now - self._last)
        self._last = now

        angle = 0.0
        if self.flip_start is not None:
            u = (now - self.flip_start) / FLIP_SECONDS
            if u >= 1.0:
                self._finish_flip()
            else:
                angle = 180.0 * (u * u * (3.0 - 2.0 * u))
        elif self.running:
            self.remaining -= dt
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
        if not moving or self.root.state() == "iconic":
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
