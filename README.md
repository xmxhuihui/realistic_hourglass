# Hourglass Timer

A desktop countdown timer for Windows whose face is a hourglass: the sand
level in the upper bulb tracks the time you have left, a stream of grains falls
through the neck while it runs, a cone builds up underneath, and the whole piece
turns over when you flip it.

![the timer running](docs/screenshot.png)

## Running it

Double-click **`run.bat`**, or:

```
.venv\Scripts\pythonw.exe hourglass_timer.py
```

`run.bat` uses the bundled `.venv` if it is present and otherwise falls back to
the `pythonw` on your PATH.

## Using it

| Control | What it does |
| --- | --- |
| **Start / Pause** | Runs or holds the countdown (`space`) |
| **Reset** | Back to the full duration (`r`) |
| **Flip** | Turns the glass over: what has run out becomes the time remaining (`f`) |
| Preset chips | 1 to 60 minutes |
| `custom` box | `90` (minutes), `5:30`, or `1:02:30`, then **set** or Enter |
| Click the hourglass | Flips it |
| Scroll over the hourglass | Adds or removes a minute while it is stopped |
| **always on top** | Keeps the window above other apps |

When the time runs out the glass empties, the clock flashes and a short chime
plays. **Restart** (or a flip) starts it again.

## Requirements

* Python 3.9 or newer with Tkinter (the standard python.org installer includes it)
* `numpy` — required
* `pillow` — optional; it roughly triples the frame rate of the canvas blit and
  is used for the flip animation. Without it the app falls back to a built-in
  PNG encoder and runs at a lower frame rate.

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

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
```
