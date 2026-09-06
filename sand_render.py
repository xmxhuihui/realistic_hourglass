"""Procedural hourglass renderer.

Everything is drawn per pixel with numpy: shapes come from signed distance
fields, so edges are antialiased without supersampling, and glass, wood and sand
can all be shaded analytically.  The static parts of the scene (backdrop, glass,
wooden frame) are baked once at start-up; only the sand is recomputed per frame,
and only inside the bounding box of the two bulbs.

Layers are kept with premultiplied alpha, so compositing is a plain lerp and the
whole hourglass can be rotated in one piece for the flip animation.
"""

from __future__ import annotations

import math

import numpy as np

try:                       # optional, only used to speed up the flip animation
    from PIL import Image as _PIL
except Exception:          # pragma: no cover - numpy fallback below
    _PIL = None

F = np.float32


# ---------------------------------------------------------------------------
# small numeric helpers
# ---------------------------------------------------------------------------
def cov(d):
    """Antialiased coverage of a signed distance (positive = inside), 1px wide."""
    return np.clip(d + 0.5, 0.0, 1.0)


def smoothstep(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def value_noise(rng, h, w, cell_y, cell_x):
    """Smooth bilinear value noise on a cell_y x cell_x lattice."""
    gh = max(2, int(h / max(cell_y, 0.5)) + 2)
    gw = max(2, int(w / max(cell_x, 0.5)) + 2)
    g = rng.random((gh, gw)).astype(F)
    ys = np.linspace(0, gh - 1.001, h).astype(F)
    xs = np.linspace(0, gw - 1.001, w).astype(F)
    y0 = ys.astype(np.intp)
    x0 = xs.astype(np.intp)
    fy = (ys - y0)[:, None]
    fx = (xs - x0)[None, :]
    fy = fy * fy * (3 - 2 * fy)
    fx = fx * fx * (3 - 2 * fx)
    a = g[np.ix_(y0, x0)]
    b = g[np.ix_(y0, x0 + 1)]
    c = g[np.ix_(y0 + 1, x0)]
    d = g[np.ix_(y0 + 1, x0 + 1)]
    return (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy


def fbm(rng, h, w, cell, octaves=4, gain=0.5):
    total = np.zeros((h, w), F)
    amp, norm = 1.0, 0.0
    for i in range(octaves):
        scale = cell / (2 ** i)
        total += amp * value_noise(rng, h, w, scale, scale)
        norm += amp
        amp *= gain
    return total / norm


def rounded_rect(X, Y, x0, y0, x1, y1, r):
    """Signed distance to a rounded rectangle, positive inside."""
    mx, my = (x0 + x1) * 0.5, (y0 + y1) * 0.5
    hx, hy = (x1 - x0) * 0.5 - r, (y1 - y0) * 0.5 - r
    qx = np.abs(X - mx) - hx
    qy = np.abs(Y - my) - hy
    outside = np.hypot(np.maximum(qx, 0.0), np.maximum(qy, 0.0))
    inside = np.minimum(np.maximum(qx, qy), 0.0)
    return r - (outside + inside)


def over_(dstP, dstA, srcP, srcA):
    """Composite premultiplied src over premultiplied dst, in place."""
    inv = 1.0 - srcA
    dstP *= inv
    dstP += srcP
    dstA *= inv
    dstA += srcA


def over_opaque(dst, srcP, srcA):
    dst *= (1.0 - srcA)
    dst += srcP
    return dst


def rgb(*c):
    return np.array(c, F).reshape(1, 1, 3)


# ---------------------------------------------------------------------------
# palette
# ---------------------------------------------------------------------------
BG_CORE = rgb(0.247, 0.267, 0.310)
BG_EDGE = rgb(0.067, 0.075, 0.098)

WOOD_DARK = rgb(0.180, 0.098, 0.051)
WOOD_LIGHT = rgb(0.690, 0.459, 0.243)

SAND_DARK = rgb(0.400, 0.247, 0.086)
SAND_MID = rgb(0.855, 0.643, 0.325)
SAND_LIGHT = rgb(0.996, 0.906, 0.706)

GLASS_TINT = rgb(0.620, 0.760, 0.820)
GLASS_SHEEN = rgb(0.906, 0.969, 1.000)


class HourglassRenderer:
    """Draws the hourglass at a given fill level.  All units are pixels."""

    SHADE_MAX = 1.6

    def __init__(self, width=420, height=560, seed=11):
        self.W = int(width)
        self.H = int(height)
        self.rng = np.random.default_rng(seed)
        self._layout()
        self._grids()
        self._bake()
        self._levels()
        self.particles = []
        self.phase = 0.0
        self.time = 0.0
        self.top_y = self.inner_top
        self.bot_y = self.inner_bot
        self.flowing = False

    # -- geometry ----------------------------------------------------------
    def _layout(self):
        W, H = self.W, self.H
        self.cx = W * 0.5
        self.plate_h = H * 0.050
        self.glass_top = H * 0.092
        self.glass_bot = H - H * 0.108
        self.ymid = (self.glass_top + self.glass_bot) * 0.5
        self.hh = (self.glass_bot - self.glass_top) * 0.5
        self.R = W * 0.283             # widest outer half-width of the glass
        self.neck = max(6.0, W * 0.026)
        self.wall = max(2.2, W * 0.0078)
        self.t_neck = 0.030            # length of the straight neck tube
        self.t_belly = 0.80            # where the bulb is at its widest
        self.post_x = self.R + W * 0.036
        self.post_w = W * 0.019
        self.repose = 0.62             # tan(angle of repose) of the poured cone
        self.inner_top = self.glass_top + self.wall + 1.0
        self.inner_bot = self.glass_bot - self.wall - 1.0
        # extent of the whole piece, used to keep a flip inside the canvas
        self.piece_w = 2.0 * (self.R + W * 0.062)
        self.piece_h = (self.glass_bot - self.glass_top) + 2.0 * (self.plate_h - 13.0)

    def half_width(self, y):
        """Outer half-width of the glass silhouette at height y.

        The bulb swells gently out of the neck, reaches its widest four fifths
        of the way up, then rolls back over into the shoulder that meets the
        wooden plate.
        """
        t = np.clip(np.abs((y - self.ymid) / self.hh), 0.0, 1.0)
        t = np.clip((t - self.t_neck) / (1.0 - self.t_neck), 0.0, 1.0)
        belly = np.sin(np.minimum(t / self.t_belly, 1.0) * (math.pi * 0.5)) ** 1.25
        s = np.clip((t - self.t_belly) / (1.0 - self.t_belly), 0.0, 1.0)
        shoulder = np.sqrt(np.clip(1.0 - 0.62 * s * s, 0.0, 1.0))
        return self.neck + (self.R - self.neck) * belly * shoulder

    def inner_width(self, y):
        return np.maximum(self.half_width(y) - self.wall, 0.55)

    def _grids(self):
        H, W = self.H, self.W
        self.Y = np.arange(H, dtype=F)[:, None] + 0.5
        self.X = np.arange(W, dtype=F)[None, :] + 0.5
        self.dx = self.X - self.cx
        self.adx = np.abs(self.dx)
        self.wo = self.half_width(self.Y)
        self.wi = self.inner_width(self.Y)
        self.d_out = np.minimum(self.wo - self.adx,
                                np.minimum(self.Y - self.glass_top,
                                           self.glass_bot - self.Y))
        self.d_in = np.minimum(self.wi - self.adx,
                               np.minimum(self.Y - self.inner_top,
                                          self.inner_bot - self.Y))
        self.a_glass = cov(self.d_out)
        self.a_cavity = cov(self.d_in)
        self.u = np.clip(self.dx / np.maximum(self.wo, 1.0), -1.6, 1.6)

        # the slice that contains the whole cavity: sand work happens in here
        x0 = max(0, int(self.cx - self.R - 3))
        x1 = min(W, int(self.cx + self.R + 4))
        y0 = max(0, int(self.glass_top - 2))
        y1 = min(H, int(self.glass_bot + 3))
        self.box = (slice(y0, y1), slice(x0, x1))
        self.bY = self.Y[self.box[0]]
        self.bX = self.X[:, self.box[1]]
        self.bdx = self.bX - self.cx
        self.badx = np.abs(self.bdx)
        self.bwi = self.wi[self.box[0]]
        self.b_cavity = self.a_cavity[self.box]
        self.b_din = self.d_in[self.box]
        self.b_u = np.clip(self.bdx / np.maximum(self.bwi, 1.0), -1.4, 1.4)

    # -- static layers -----------------------------------------------------
    def _bake(self):
        H, W = self.H, self.W
        rng = self.rng
        X, Y = self.X, self.Y

        # backdrop: a soft pool of light, vignette, and a contact shadow
        r = np.hypot((X - self.cx) / (W * 0.62), (Y - H * 0.46) / (H * 0.62))
        g = (np.clip(1.0 - r, 0.0, 1.0) ** 1.5)[:, :, None]
        back = BG_EDGE + (BG_CORE - BG_EDGE) * g
        back = back + (fbm(rng, H, W, 3, 2)[:, :, None] - 0.5) * 0.014
        sh = np.exp(-(((X - self.cx) / (self.R * 1.55)) ** 2)
                    - (((Y - (self.glass_bot + self.plate_h * 1.35)) / (self.plate_h * 0.8)) ** 2))
        back = back * (1.0 - 0.5 * sh)[:, :, None]
        self.backdrop = np.clip(back, 0, 1).astype(F)

        # wood texture shared by the plates and posts
        grain = fbm(rng, H, W, 26, 4)
        rings = 0.5 + 0.5 * np.sin(X * 0.9 + grain * 9.0 + Y * 0.05)
        self.wood_tex = np.clip(0.62 * grain + 0.38 * rings, 0, 1).astype(F)

        underP = np.zeros((H, W, 3), F)
        underA = np.zeros((H, W, 1), F)
        topP = np.zeros((H, W, 3), F)
        topA = np.zeros((H, W, 1), F)

        # glass interior: a faint tint that thickens towards the walls
        wall_shade = smoothstep(0.0, 11.0, self.d_in)
        interior = GLASS_TINT * (0.30 + 0.34 * (1.0 - wall_shade))[:, :, None]
        ia = (self.a_cavity * (0.05 + 0.17 * (1.0 - wall_shade)))[:, :, None]
        over_(underP, underA, interior * ia, ia)
        tuck = (np.exp(-((Y - self.inner_top) / 16.0) ** 2)
                + np.exp(-((Y - self.inner_bot) / 16.0) ** 2))
        tuck = (np.clip(tuck, 0, 1) * self.a_cavity * 0.55)[:, :, None]
        over_(underP, underA, np.zeros_like(tuck), tuck)
        self.underP, self.underA = underP, underA

        # glass walls, rim light and specular streaks, drawn over the sand
        au = np.abs(self.u)
        wall_band = self.a_glass * (1.0 - self.a_cavity)
        edge = np.exp(-((au - 1.0) / 0.055) ** 2)
        rim = np.clip(wall_band * (0.16 + 0.64 * edge), 0, 1)[:, :, None]
        rimc = GLASS_SHEEN * (0.50 + 0.50 * np.clip(1.2 - au, 0, 1))[:, :, None]
        over_(topP, topA, rimc * rim, rim)

        # Specular streaks.  Their offset from the axis only partly follows the
        # silhouette: a highlight that tracked it exactly would pinch shut at
        # the waist and read as a swirl of smoke rather than reflected light.
        env = (smoothstep(self.glass_top, self.glass_top + 40, Y)
               * smoothstep(self.glass_bot, self.glass_bot - 40, Y))
        env = env * (1.0 - 0.72 * np.exp(-((Y - self.ymid) / (self.hh * 0.22)) ** 2))
        left = self.cx - (0.54 * self.wo + 0.20 * self.R)
        right = self.cx + (0.62 * self.wo + 0.17 * self.R)
        hi = (np.exp(-((X - left) / 5.5) ** 2) * 0.30
              + np.exp(-((X - (left - 5.0)) / 1.8) ** 2) * 0.42
              + np.exp(-((X - right) / 2.6) ** 2) * 0.26)
        sheen = np.clip(hi * env * self.a_glass, 0, 1)[:, :, None]
        over_(topP, topA, GLASS_SHEEN * sheen, sheen)

        band = (np.exp(-((Y - (self.ymid - self.hh * 0.55)) / 7.0) ** 2)
                + np.exp(-((Y - (self.ymid + self.hh * 0.58)) / 9.0) ** 2))
        band = (band * np.clip(1.0 - au * 1.15, 0, 1) * self.a_cavity * 0.10)[:, :, None]
        over_(topP, topA, GLASS_SHEEN * band, band)

        # wooden stand
        self._add_post(topP, topA, self.cx - self.post_x, self.post_w)
        self._add_post(topP, topA, self.cx + self.post_x, self.post_w)
        self._add_plate(topP, topA, self.glass_top + 13, -1)
        self._add_plate(topP, topA, self.glass_bot - 13, +1)
        self.topP, self.topA = topP, topA

        # fast path: backdrop with everything that sits behind the sand
        self.base_rgb = over_opaque(self.backdrop.copy(), underP, underA)

        # static sand fields, in cavity-box coordinates
        bh, bw = self.b_cavity.shape
        self.sand_grain = (0.80 + 0.32 * fbm(rng, bh, bw, 2.4, 3)
                           + 0.12 * value_noise(rng, bh, bw, 13, 13)).astype(F)
        self.stream_noise = fbm(rng, 256, bw, 2.0, 3).astype(F)
        light = np.clip(0.56 + 0.54 * np.cos((self.b_u - 0.32) * 1.25), 0.25, 1.25)
        wall_dark = 0.72 + 0.28 * smoothstep(0.0, 6.0, self.b_din)
        # everything about the sand's shading that never moves, folded into one
        # field so that only the surface highlights are computed per frame
        self.sand_shade = (self.sand_grain * light * wall_dark).astype(F)
        self.m_upper = cov(self.ymid + 1.0 - self.bY).astype(F)
        self.m_lower = cov(self.bY - self.ymid).astype(F)
        self.left_flank = np.clip(0.5 - self.bdx / 26.0, 0.0, 1.0).astype(F)

        # colour ramp for the sand, sampled through a lookup table: cheaper per
        # frame than evaluating the two-segment gradient over every pixel
        n = 192
        s = np.linspace(0.0, self.SHADE_MAX, n, dtype=F).reshape(n, 1, 1)
        lut = np.where(s < 1.0,
                       SAND_DARK + (SAND_MID - SAND_DARK) * s,
                       SAND_MID + (SAND_LIGHT - SAND_MID) * (s - 1.0) / 0.6)
        self.sand_lut = (np.clip(lut.reshape(n, 3), 0, 1) * 255.0).astype(F)
        self.lut_scale = F((n - 1) / self.SHADE_MAX)

        # per-frame work is confined to the cavity box; the rest of the canvas
        # is baked once into the output buffer
        base_box = np.ascontiguousarray(self.base_rgb[self.box])
        topP_box = np.ascontiguousarray(self.topP[self.box])
        self.top_inv_box = np.ascontiguousarray(1.0 - self.topA[self.box])
        # A frame is  static + (sand alpha * glass transmission) * (sand - base):
        # folding the two composites into a single lerp, and premultiplying the
        # sand palette by 255, cuts the per-frame work to a few passes.
        self.base255 = base_box * 255.0
        self.static255 = (base_box * self.top_inv_box + topP_box) * 255.0 + 0.5
        self._buf = np.empty_like(base_box)
        self._u8 = ((np.clip(over_opaque(self.base_rgb.copy(), self.topP, self.topA),
                             0, 1) * 255.0 + 0.5).astype(np.uint8))
        # the stream is a narrow column: shade it in its own slim slice
        sx = int(self.cx - self.box[1].start)
        self.scol = slice(max(0, sx - 9), min(base_box.shape[1], sx + 10))
        self.sdx = np.ascontiguousarray(self.bdx[:, self.scol])
        self.stream_noise = np.ascontiguousarray(self.stream_noise[:, self.scol])

    def _add_post(self, P, A, px, pw):
        """A turned wooden column running between the two plates."""
        X, Y = self.X, self.Y
        top = self.glass_top - self.plate_h * 0.30
        bot = self.glass_bot + self.plate_h * 0.30
        rings = np.zeros_like(Y)
        for ry, rs in ((top + 11, 6.0), (bot - 11, 6.0), (self.ymid, 10.0)):
            rings = rings + np.exp(-((Y - ry) / rs) ** 2)
        w = pw * (1.0 + 0.45 * np.clip(rings, 0, 1))
        d = np.minimum(w - np.abs(X - px), np.minimum(Y - top, bot - Y))
        a = cov(d)
        u = np.clip((X - px) / np.maximum(w, 1.0), -1, 1)
        cyl = np.sqrt(np.clip(1.0 - u * u, 0, 1))
        shade = 0.26 + 0.64 * cyl + 0.30 * np.exp(-((u + 0.42) / 0.30) ** 2)
        shade = np.clip(shade * (0.80 + 0.30 * self.wood_tex), 0, 1)
        col = WOOD_DARK + (WOOD_LIGHT - WOOD_DARK) * shade[:, :, None]
        a = a[:, :, None]
        over_(P, A, col * a, a)

    def _add_plate(self, P, A, y_edge, direction):
        """Top (direction=-1) or base (direction=+1) plate of the stand."""
        X, Y = self.X, self.Y
        h = self.plate_h
        y0, y1 = (y_edge, y_edge + h) if direction > 0 else (y_edge - h, y_edge)
        hw = self.R + self.W * 0.062
        d = rounded_rect(X, Y, self.cx - hw, y0, self.cx + hw, y1, h * 0.32)
        a = cov(d)
        v = np.clip((Y - y0) / h, 0, 1)
        prof = (0.76 - 0.52 * v
                + 0.46 * np.exp(-(v / 0.16) ** 2)
                + 0.22 * np.exp(-((v - 1.0) / 0.14) ** 2))
        side = 1.0 - 0.40 * np.clip((np.abs(X - self.cx) / hw) ** 3, 0, 1)
        shade = np.clip(prof * side * (0.82 + 0.28 * self.wood_tex), 0, 1)
        col = WOOD_DARK + (WOOD_LIGHT - WOOD_DARK) * shade[:, :, None]
        a = a[:, :, None]
        over_(P, A, col * a, a)

    # -- fill levels -------------------------------------------------------
    def _levels(self):
        """Tables mapping a sand level to the area it covers, so the level
        tracks volume rather than height (the bulbs are much wider than the
        neck, so a linear level would drain far too fast at the start)."""
        step = 2
        ys = self.bY[::step]
        xs = self.badx[:, ::step]
        cavity = self.b_cavity[::step, ::step] > 0.5
        cell = float(step * step)

        tl = np.linspace(self.inner_top, self.ymid, 200, dtype=F)
        ta = np.empty_like(tl)
        top_half = ys <= self.ymid
        for i, L in enumerate(tl):
            surf = self._top_surface(ys, xs, float(L))
            ta[i] = np.count_nonzero(cavity & top_half & (ys >= surf)) * cell
        self.top_levels, self.top_areas = tl, ta
        self.sand_area = float(ta[0])

        bl = np.linspace(self.inner_bot, self.ymid, 200, dtype=F)
        ba = np.empty_like(bl)
        bot_half = ys >= self.ymid
        for i, P in enumerate(bl):
            surf = float(P) + self.repose * (np.hypot(xs, 7.0) - 7.0)
            ba[i] = np.count_nonzero(cavity & bot_half & (ys >= surf)) * cell
        self.bot_levels, self.bot_areas = bl, ba

    def _top_surface(self, ys, xs, L):
        """Surface of the sand still in the upper bulb: a shallow crater that
        funnels towards the neck."""
        w = float(self.inner_width(np.array(L, F)))
        rad = min(w, self.R * 0.34)
        depth = min(0.45 * rad, 15.0)
        t = np.clip(xs / max(rad, 1.0), 0, 1)
        return L + depth * (1.0 - t * t)

    def _top_level(self, frac):
        target = frac * self.sand_area
        return float(np.interp(target, self.top_areas[::-1], self.top_levels[::-1]))

    def _bot_level(self, frac):
        target = frac * self.sand_area
        return float(np.interp(target, self.bot_areas, self.bot_levels))

    # -- simulation --------------------------------------------------------
    def step(self, dt, top_frac, flowing):
        """Advance loose grains and move the two sand surfaces."""
        dt = float(np.clip(dt, 0.0, 0.1))
        self.time += dt
        self.phase += dt * 230.0
        top_frac = float(np.clip(top_frac, 0.0, 1.0))
        self.top_y = self._top_level(top_frac) if top_frac > 1e-4 else self.ymid
        self.bot_y = (self._bot_level(1.0 - top_frac) if top_frac < 1.0 - 1e-4
                      else self.inner_bot)
        self.flowing = bool(flowing) and top_frac > 1e-4
        impact = min(self.bot_y, self.inner_bot)
        rng = self.rng

        alive = []
        for p in self.particles:
            p[3] += 1500.0 * dt
            p[0] += p[2] * dt
            p[1] += p[3] * dt
            p[4] -= dt
            floor = impact + self.repose * abs(p[0] - self.cx)
            if (p[4] > 0.0 and p[1] < floor
                    and abs(p[0] - self.cx) < float(self.inner_width(np.array(p[1], F)))):
                alive.append(p)
        self.particles = alive

        if self.flowing and len(self.particles) < 110:
            for _ in range(int(rng.integers(2, 5))):
                self.particles.append([self.cx + rng.normal(0, 1.7),
                                       self.ymid + rng.random() * 6.0,
                                       rng.normal(0, 6.0), rng.random() * 45.0, 2.6])
            if impact - self.ymid > 14:            # splash off the growing cone
                for _ in range(int(rng.integers(0, 3))):
                    self.particles.append([self.cx + rng.normal(0, 2.2), impact - 2.0,
                                           rng.normal(0, 60.0),
                                           -rng.random() * 95.0 - 30.0, 0.9])

    # -- sand --------------------------------------------------------------
    def _sand(self):
        ys, xs = self.bY, self.badx
        shade = self.sand_shade.copy()

        # sand resting in the upper bulb, under its funnel-shaped surface
        if self.top_y < self.ymid - 0.5:
            d = ys - self._top_surface(ys, xs, self.top_y)
            a = cov(d) * self.m_upper
            np.clip(d, 0.0, 24.0, out=d)
            d *= -0.20
            np.exp(d, out=d)
            shade += 0.42 * d
        else:
            a = np.zeros_like(shade)

        # the cone poured into the lower bulb
        if self.bot_y < self.inner_bot - 0.5:
            d = ys - (self.bot_y + self.repose * (np.hypot(xs, 7.0) - 7.0))
            np.maximum(a, cov(d) * self.m_lower, out=a)
            np.clip(d, 0.0, 24.0, out=d)
            d *= -0.25
            np.exp(d, out=d)
            d *= self.m_lower
            shade += 0.38 * d
            shade += (0.20 * self.left_flank) * d      # lit flank of the cone

        np.clip(shade, 0.0, self.SHADE_MAX, out=shade)
        shade *= self.lut_scale
        col = self.sand_lut[shade.astype(np.intp)]

        # the falling stream, drawn in its own narrow column
        if self.flowing:
            sy = ys
            wob = (1.1 * np.sin(sy * 0.055 + self.time * 5.3)
                   + 0.7 * np.sin(sy * 0.130 - self.time * 8.1))
            sw = 2.6 - 1.2 * smoothstep(self.ymid, self.inner_bot, sy)
            body = (cov(sw - np.abs(self.sdx - wob))
                    * cov(sy - (self.ymid - 3.0))
                    * cov(self.bot_y + 1.5 - sy))
            n = self.stream_noise
            off = int(self.phase) % n.shape[0]
            n = np.roll(n, -off, axis=0)
            reps = -(-sy.shape[0] // n.shape[0])
            n = np.tile(n, (reps, 1))[:sy.shape[0]]
            st = np.clip(body * (0.50 + 0.80 * n), 0, 1)
            sub = a[:, self.scol]
            np.maximum(sub, st, out=sub)
            lit = np.clip(st * 1.2, 0, 1)[:, :, None]
            csub = col[:, self.scol]
            col[:, self.scol] = csub * (1.0 - lit) + (SAND_LIGHT * (0.94 * 255.0)) * lit

        a *= self.b_cavity

        if self.particles:                      # loose grains in the air
            y0, x0 = self.box[0].start, self.box[1].start
            h, w = a.shape
            grain = SAND_LIGHT.reshape(3) * 255.0
            for p in self.particles:
                ix, iy = int(p[0] - x0), int(p[1] - y0)
                if 1 <= ix < w - 1 and 1 <= iy < h - 1:
                    a[iy:iy + 2, ix:ix + 2] = 1.0
                    col[iy:iy + 2, ix:ix + 2] = grain
        return col, a[:, :, None]

    # -- frames ------------------------------------------------------------
    def frame(self):
        """Composite one frame; returns an (H, W, 3) uint8 array.

        The buffer is reused between frames: outside the bulbs nothing ever
        changes, so only the cavity box is redrawn.
        """
        col, a = self._sand()
        buf = self._buf
        np.subtract(col, self.base255, out=buf)
        buf *= a * self.top_inv_box
        buf += self.static255
        self._u8[self.box] = buf.astype(np.uint8)
        return self._u8

    def flip_scale(self, angle_deg):
        """How much to shrink the piece at this angle so a turn stays in frame."""
        th = math.radians(angle_deg)
        c, sn = abs(math.cos(th)), abs(math.sin(th))
        span_w = self.piece_w * c + self.piece_h * sn
        span_h = self.piece_w * sn + self.piece_h * c
        return min(1.0, 0.98 * self.W / span_w, 0.98 * self.H / span_h)

    def frame_rotated(self, angle_deg):
        """The same scene with the whole hourglass turned about its waist."""
        if abs(angle_deg) < 0.05:
            return self.frame()
        zoom = self.flip_scale(angle_deg)
        P = self.underP.copy()
        A = self.underA.copy()
        col, a = self._sand()
        sP = np.zeros_like(P)
        sA = np.zeros_like(A)
        sP[self.box] = col * (a * F(1.0 / 255.0))
        sA[self.box] = a
        over_(P, A, sP, sA)
        over_(P, A, self.topP, self.topA)

        if _PIL is not None:
            # Pillow resamples an 8-bit premultiplied RGBA far faster than the
            # numpy fallback below, and the flip is the one place it matters.
            rgba = np.empty((self.H, self.W, 4), np.uint8)
            rgba[:, :, :3] = (np.clip(P, 0, 1) * 255.0 + 0.5).astype(np.uint8)
            rgba[:, :, 3] = (np.clip(A[:, :, 0], 0, 1) * 255.0 + 0.5).astype(np.uint8)
            th = math.radians(angle_deg)
            a, b = math.cos(th) / zoom, math.sin(th) / zoom
            spun = _PIL.fromarray(rgba, "RGBA").transform(
                (self.W, self.H), _PIL.AFFINE,
                (a, b, self.cx - a * self.cx - b * self.ymid,
                 -b, a, self.ymid + b * self.cx - a * self.ymid),
                resample=_PIL.BILINEAR)
            spun = np.asarray(spun, dtype=F) / 255.0
            out = over_opaque(self.backdrop.copy(), spun[:, :, :3], spun[:, :, 3:4])
            return (np.clip(out, 0, 1) * 255.0 + 0.5).astype(np.uint8)

        th = math.radians(angle_deg)
        cs, sn = math.cos(th) / zoom, math.sin(th) / zoom
        xr = self.X - self.cx
        yr = self.Y - self.ymid
        sx = np.clip(cs * xr + sn * yr + self.cx, 0, self.W - 1.001)
        sy = np.clip(-sn * xr + cs * yr + self.ymid, 0, self.H - 1.001)
        x0 = sx.astype(np.intp)
        y0 = sy.astype(np.intp)
        fx = (sx - x0)[:, :, None]
        fy = (sy - y0)[:, :, None]
        x1 = np.minimum(x0 + 1, self.W - 1)
        y1 = np.minimum(y0 + 1, self.H - 1)

        def sample(arr):
            return ((arr[y0, x0] * (1 - fx) + arr[y0, x1] * fx) * (1 - fy)
                    + (arr[y1, x0] * (1 - fx) + arr[y1, x1] * fx) * fy)

        out = over_opaque(self.backdrop.copy(), sample(P), sample(A))
        return (np.clip(out, 0, 1) * 255.0 + 0.5).astype(np.uint8)
