# Hourglass Timer

A desktop countdown timer for Windows whose face is a hourglass: the sand
level in the upper bulb tracks the time you have left, a stream of grains falls
through the neck while it runs, a cone builds up underneath, and the whole piece
turns over when you flip it.

![the timer running](docs/screenshot.png)

## Setup

The virtual environment is not in the repository, so create it once after
cloning. On **Windows**:

```
git clone https://github.com/xmxhuihui/realistic_hourglass.git
cd realistic_hourglass
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If `python` is not on your PATH, use the launcher that ships with the
python.org installer instead: `py -3 -m venv .venv`.

On **macOS / Linux**:

```
git clone https://github.com/xmxhuihui/realistic_hourglass.git
cd realistic_hourglass
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

You do not need to *activate* the environment. Every command here — and
`run.bat` — calls the interpreter by path, so there is no `activate` step and no
PowerShell execution-policy detour.

If you would rather not use a virtual environment at all, install the two
packages into whatever Python is on your PATH with
`python -m pip install numpy pillow`; `run.bat` falls back to that automatically.

## Running it

Double-click **`run.bat`**, or:

```
.venv\Scripts\pythonw.exe hourglass_timer.py       # Windows
.venv/bin/python hourglass_timer.py                # macOS / Linux
```

`run.bat` uses `.venv` if it is present and otherwise falls back to the
`pythonw` on your PATH.

### A desktop shortcut

On Windows, one command puts a double-click launcher on your desktop:

```
powershell -ExecutionPolicy Bypass -File make_shortcut.ps1
```

That creates **Hourglass Timer** on the desktop, aimed at
`.venv\Scripts\pythonw.exe` rather than at `run.bat` so no console window flashes
on launch, and wearing `hourglass.ico` — an icon rendered by `sand_render.py`
itself. The shortcut stores absolute paths, so run the script again if you move
the project folder.

## Using it

| Control | What it does |
| --- | --- |
| **Start / Pause** | Runs or holds the countdown (`space`) |
| **Reset** | Back to the full duration (`r`) |
| **Flip** | Turns the glass over: what has run out becomes the time remaining (`f`). Greyed out until the timer has started, since a full glass has nothing to turn over |
| Preset chips | 1 to 60 minutes |
| `custom` box | `90` (minutes), `5:30`, or `1:02:30`, then **set** or Enter |
| Scroll over the hourglass | Adds or removes a minute while it is stopped |
| **always on top** | Keeps the window above other apps |

When the time runs out the glass empties and the alarm starts: the chime
repeats, the clock and the status line blink, and the taskbar button flashes. It
is meant to be hard to miss, so minimising the window does not silence it — and
if the timer runs out while the window is minimised, the window comes back by
itself. **Reset** stops the alarm at once; **Restart**, a flip or picking a new
duration also stop it and start over.

### The notification area

On Windows the app lives in the notification area, so a timer is never lost to a
stray click. Closing the window with **X** only hides it: the countdown carries
on at full accuracy, and the hourglass icon stays in the tray. Click that icon to
bring the window back, or right-click it for **Show** and **Exit** — **Exit** is
what actually ends the app. A timer that runs out while the window is hidden
raises it again by itself, alarm and all.

Only one copy runs at a time. Launching the app again — a double-click on the
desktop shortcut while it is already up — does not start a second timer: the
copy already running is brought to the front instead, out of the tray if that is
where it was, and the new one exits without ever showing a window. The claim is a
named mutex held by the running process, so it is released whenever that process
ends, crash or kill included; there is no lock file to go stale.

There is no extra package behind this: a hidden window collects what the shell
sends and the timer's own frame loop reads it, so nothing runs on a second
thread. That window is also how a second launch finds the copy already running,
so in the unlikely event the tray icon cannot be registered at all, a second
launch still refuses to start a rival but cannot raise the first window either.
Elsewhere than Windows there is no notification area to hide in, and the X button
closes the app as usual.

## Requirements

* Python 3.9 or newer with Tkinter (the standard python.org installer includes it)
* `numpy` — required
* `pillow` — optional; it roughly triples the frame rate of the canvas blit and
  is used for the flip animation. Without it the app falls back to a built-in
  PNG encoder and runs at a lower frame rate.

See [Setup](#setup) for the install commands.

It is written for Windows but does run on macOS and Linux: the Windows-only
calls are all guarded, so the DPI hint becomes a no-op and the completion chime
falls back to the terminal bell. There is no tray icon, so the X button quits as
usual. You lose `run.bat`, Segoe UI and Consolas give way to Tk's default fonts,
and on Linux the scroll-to-change-duration shortcut is inert because X11 does not
send Tk's `<MouseWheel>` event.

## How the hourglass is drawn

`sand_render.py` renders every frame from scratch with numpy — there are no
image assets. Shapes are signed distance fields, so edges come out antialiased
without supersampling, and glass, wood and sand are shaded analytically:

* **Silhouette** — the bulb half-width swells out of the neck as a sine curve,
  is widest four fifths of the way up, then rolls over into the shoulder that
  meets the wooden plate.
* **Sand levels** track *volume*, not height. The bulbs are far wider than the
  neck, so a level moving at a constant speed would look wrong; a table built at
  start-up maps a fill fraction to the level that covers that much area. The
  upper surface carries a funnel-shaped crater above the neck, the lower one is
  a cone at the angle of repose.
* **The stream** is a narrow wobbling column textured with scrolling noise, plus
  a few dozen loose grains that fall under gravity and land on the cone.
* **Glass** is a faint tint that thickens toward the walls, a rim light, and
  specular streaks whose offset from the axis only partly follows the
  silhouette — a highlight that tracked it exactly would pinch shut at the waist
  and read as smoke rather than reflected light.

Everything static is baked once at start-up, and a frame is then
`static + (sand alpha x glass transmission) x (sand - backdrop)` over the
bounding box of the bulbs only. Drawing stops completely while the timer is
paused, so an idle timer costs no CPU.

## Files

```
hourglass_timer.py   timer logic and the Tk interface
sand_render.py       the renderer (standalone; no Tk dependency)
run.bat              launcher
make_shortcut.ps1    puts a shortcut to it on the desktop (Windows)
hourglass.ico        the shortcut and tray icon, rendered by sand_render.py
```
