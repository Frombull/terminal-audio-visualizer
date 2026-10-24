"""Everything that ends up on screen: ANSI helpers, palettes, the effects layer and the renderer."""
from __future__ import annotations

import math

import numpy as np

# --------------------------------------------------------------------------- #
# ANSI helpers
# --------------------------------------------------------------------------- #

ESC = "\x1b"


def fg(c) -> str:
    return f"{ESC}[38;2;{int(c[0])};{int(c[1])};{int(c[2])}m"


def bgc(c) -> str:
    return f"{ESC}[48;2;{int(c[0])};{int(c[1])};{int(c[2])}m"


# --------------------------------------------------------------------------- #
# Palettes
# --------------------------------------------------------------------------- #

# (stops, horizontal mix, brightness at the base)
PALETTES: dict[str, tuple[list[tuple[float, tuple[int, int, int]]], float, float]] = {
    "neon": ([(0, (70, 30, 210)), (0.5, (255, 40, 190)), (1, (0, 235, 255))], 0.25, 0.6),
    "fire": ([(0, (110, 0, 0)), (0.4, (255, 60, 0)), (0.75, (255, 190, 0)), (1, (255, 255, 190))], 0.0, 0.7),
    "ocean": ([(0, (0, 25, 120)), (0.5, (0, 140, 225)), (1, (130, 255, 235))], 0.2, 0.7),
    "sunset": ([(0, (70, 20, 130)), (0.4, (225, 50, 120)), (0.75, (255, 140, 60)), (1, (255, 235, 130))], 0.2, 0.7),
    "matrix": ([(0, (0, 55, 20)), (0.6, (0, 205, 70)), (1, (200, 255, 210))], 0.0, 0.8),
    "rainbow": (
        [(0, (255, 60, 60)), (0.17, (255, 210, 40)), (0.34, (60, 255, 90)),
         (0.5, (40, 230, 255)), (0.67, (80, 90, 255)), (0.84, (230, 60, 255)), (1, (255, 60, 120))],
        0.85, 0.45,
    ),
    "ice": ([(0, (40, 60, 160)), (0.55, (120, 190, 255)), (1, (255, 255, 255))], 0.1, 0.75),
}


def gradient(stops, t: np.ndarray) -> np.ndarray:
    xs = [s[0] for s in stops]
    t = np.clip(t, 0.0, 1.0)
    return np.stack([np.interp(t, xs, [s[1][c] for s in stops]) for c in range(3)], axis=-1)


# --------------------------------------------------------------------------- #
# Effects layer (braille)
# --------------------------------------------------------------------------- #

# braille dot weights, indexed by dx * 4 + dy inside a cell
DOT_WEIGHTS = np.array([0x01, 0x02, 0x04, 0x40, 0x08, 0x10, 0x20, 0x80])
BRAILLE = [chr(0x2800 + i) for i in range(256)]


class Cosmos:
    """Braille effects layer (2x4 dots per cell): warp stars, sparks flying off the
    bars and shockwaves on every bass hit."""

    GRAVITY = 140.0  # dots/s^2

    def __init__(self) -> None:
        self.rng = np.random.default_rng()
        self.C = self.R = self.W = self.H = 0
        self.aspect = 2.0
        self.x = self.y = self.z = np.zeros(0)
        self.warp = 0.0
        self.sparks = np.zeros((0, 7))  # x, y, vx, vy, age, life, palette position
        self.rings: list[list[float]] = []  # [age 0..1, strength]
        self.last_spawn = 0.0
        self.occ = np.zeros(0, bool)  # one flag per braille dot
        self.col = np.zeros((0, 3))   # color per cell
        self._seqs: dict[int, str] = {}

    def _spawn_stars(self, k: int, anywhere: bool) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        z = self.rng.uniform(0.05, 1.0, k) if anywhere else self.rng.uniform(0.7, 1.0, k)
        x = self.rng.uniform(-1, 1, k) * self.aspect * 0.95 * z
        y = self.rng.uniform(-1, 1, k) * 0.95 * z
        return x, y, z

    def resize(self, C: int, R: int) -> None:
        if (C, R) == (self.C, self.R):
            return
        self.C, self.R, self.W, self.H = C, R, C * 2, R * 4
        self.aspect = self.W / self.H
        n = int(np.clip(self.W * self.H / 140, 40, 700))
        if n > len(self.z):
            x, y, z = self._spawn_stars(n - len(self.z), True)
            self.x, self.y, self.z = np.append(self.x, x), np.append(self.y, y), np.append(self.z, z)
        else:
            self.x, self.y, self.z = self.x[:n], self.y[:n], self.z[:n]
        self.sparks = np.zeros((0, 7))

    def update(self, dt: float, env: float, energy: float) -> None:
        self.warp = max(self.warp * math.exp(-dt / 0.35), env)
        self.z -= (0.05 + 0.25 * energy + 1.9 * self.warp ** 1.5) * dt
        gone = ((self.z < 0.04) | (np.abs(self.x / self.z) > self.aspect * 1.05)
                | (np.abs(self.y / self.z) > 1.05))
        k = int(gone.sum())
        if k:
            self.x[gone], self.y[gone], self.z[gone] = self._spawn_stars(k, False)
        if len(self.sparks):
            s = self.sparks
            s[:, 4] += dt
            s[:, 3] += self.GRAVITY * dt
            s[:, 0] += s[:, 2] * dt
            s[:, 1] += s[:, 3] * dt
            self.sparks = s[s[:, 4] < s[:, 5]]
        self.rings = [[a + dt / 0.9, st] for a, st in self.rings if a + dt / 0.9 < 1.0]

    def burst(self, xs: np.ndarray, ys: np.ndarray, ts: np.ndarray, strength: float, ring: bool) -> None:
        w = ts ** 2 + 1e-6
        k = int(14 + 46 * strength)
        idx = self.rng.choice(len(xs), k, p=w / w.sum())
        a = np.zeros((k, 7))
        a[:, 0] = xs[idx] + self.rng.uniform(-1, 1, k) * 2
        a[:, 1] = ys[idx]
        a[:, 2] = self.rng.normal(0, 28, k)
        a[:, 3] = -self.rng.uniform(40, 120, k) * (0.6 + 0.6 * strength)
        a[:, 5] = self.rng.uniform(0.6, 1.3, k)
        a[:, 6] = ts[idx]
        self.sparks = np.vstack((self.sparks, a))[-600:]
        if ring:
            self.rings = (self.rings + [[0.0, strength]])[-4:]

    def _plot(self, px: np.ndarray, py: np.ndarray, rgb: np.ndarray) -> None:
        """Marks dots on the braille grid; where dots share a cell the brightest color wins."""
        ok = (px >= 0) & (px < self.W) & (py >= 0) & (py < self.H)
        px, py, rgb = px[ok], py[ok], rgb[ok]
        cell = (py >> 2) * self.C + (px >> 1)
        self.occ[cell * 8 + (px & 1) * 4 + (py & 3)] = True
        order = np.argsort(rgb.sum(axis=1))  # dimmest first: the last write to a cell is the brightest
        self.col[cell[order]] = rgb[order]

    @staticmethod
    def _make_seq(key: int) -> str:
        return f"{ESC}[38;2;{((key >> 10) & 31) << 3 | 4};{((key >> 5) & 31) << 3 | 4};{(key & 31) << 3 | 4}m"

    def render(self, palette: str, flash: float, stars_on: bool, bursts_on: bool,
               axis_y: float, mirror: bool) -> dict[int, tuple[list[int], list[str]]]:
        """Returns {row: (columns, ready-to-print texts)}, columns ascending."""
        W, H, C = self.W, self.H, self.C
        n_cells = self.R * C
        if len(self.occ) != n_cells * 8:
            self.occ = np.zeros(n_cells * 8, bool)
            self.col = np.zeros((n_cells, 3))
        else:
            self.occ[:] = False
        stops = PALETTES[palette][0]
        top = gradient(stops, np.array(0.9))
        xs: list[np.ndarray] = []
        ys: list[np.ndarray] = []
        cs: list[np.ndarray] = []
        if stars_on and len(self.z):
            # the streak only needs many samples when it is long, i.e. while warping
            steps = 2 + int(4 * min(self.warp, 1.0))
            f = np.linspace(0, 1, steps)[:, None]
            zt = self.z + 0.004 + 0.075 * self.warp  # streak tail: farther away = closer to the center
            zz = self.z[None, :] + (zt - self.z)[None, :] * f
            xs.append(np.floor(W / 2 + self.x[None, :] / zz * (H / 2)).astype(int).ravel())
            ys.append(np.floor(H / 2 + self.y[None, :] / zz * (H / 2)).astype(int).ravel())
            b = ((1 - self.z) ** 0.8 * 0.8 + 0.2)[None, :] * (1 - 0.65 * f) * (0.55 + 0.45 * min(flash, 1.0))
            cs.append((b[..., None] * (top * 0.5 + 255 * 0.5)).reshape(-1, 3))
        if bursts_on:
            s = self.sparks
            if len(s):
                a = (s[:, 4] / s[:, 5])[:, None]
                base = gradient(stops, s[:, 6])
                rgb = (base + (255 - base) * (1 - a) * 0.6) * (1 - a) ** 0.8
                for back in (0.0, 0.03):
                    xs.append(np.floor(s[:, 0] - s[:, 2] * back).astype(int))
                    ys.append(np.floor(s[:, 1] - s[:, 3] * back).astype(int))
                    cs.append(rgb)
            for age, st in self.rings:
                r = (1 - (1 - age) ** 2) * W * 0.5
                m = int(np.clip(r * 1.6, 24, 600))
                th = np.linspace(0 if mirror else math.pi, 2 * math.pi, m)
                inten = (1 - age) ** 1.6 * (0.45 + 0.55 * st)
                xs.append(np.floor(W / 2 + r * np.cos(th)).astype(int))
                ys.append(np.floor(axis_y + r * np.sin(th) * 0.85).astype(int))
                cs.append(np.tile((top * 0.4 + 255 * 0.6) * inten, (m, 1)))
        if not xs:
            return {}
        self._plot(np.concatenate(xs), np.concatenate(ys), np.concatenate(cs))

        bits = self.occ.reshape(-1, 8) @ DOT_WEIGHTS
        nz = np.flatnonzero(bits)
        if not len(nz):
            return {}
        q = np.clip(self.col[nz], 0, 255).astype(np.int32) >> 3  # 32 levels per channel: few distinct strings
        keys = ((q[:, 0] << 10) | (q[:, 1] << 5) | q[:, 2]).tolist()
        seqs = self._seqs
        for k in set(keys):
            if k not in seqs:
                seqs[k] = self._make_seq(k)
        texts = [seqs[k] + BRAILLE[b] for k, b in zip(keys, bits[nz].tolist())]
        rows = nz // C
        cols = (nz - rows * C).tolist()
        cuts = (np.flatnonzero(np.diff(rows)) + 1).tolist()
        starts, ends = [0] + cuts, cuts + [len(nz)]
        first = rows[starts].tolist()
        return {r: (cols[a:b], texts[a:b]) for r, a, b in zip(first, starts, ends)}


# --------------------------------------------------------------------------- #
# Renderer
# --------------------------------------------------------------------------- #

BG = np.array([9, 9, 16])
BG_PULSE = np.array([34, 10, 44])
PANEL_BG = (18, 18, 30)
CH = " ▁▂▃▄▅▆▇█"
FLASH_LEVELS = 5


class Renderer:
    def __init__(self) -> None:
        self.key = None

    def setup(self, cols: int, vis_rows: int, n: int, palette: str, style: str, reflection: bool) -> None:
        key = (cols, vis_rows, n, palette, style, reflection)
        if key == self.key:
            return
        self.key = key
        self.cols, self.vis_rows, self.n, self.palette = cols, vis_rows, n, palette
        if style == "mirror":
            self.Ru = vis_rows // 2
            self.Rl = self.Ru
            self.top_pad = vis_rows - 2 * self.Ru
            self.dim, self.fade = 0.88, 0.0
        else:
            self.Rl = max(2, vis_rows // 5) if reflection and vis_rows >= 10 else 0
            self.Ru = vis_rows - self.Rl
            self.top_pad = 0
            self.dim, self.fade = 0.34, 0.7
        unit = max(1, (cols - 4) // n)
        self.gap = 1 if unit >= 2 else 0
        self.bw = unit - self.gap
        self.unit = unit
        self.used = n * unit - self.gap
        self.pad = (cols - self.used) // 2
        self.luts: dict[int, tuple[list, list, str]] = {}

    def _lut(self, lvl: int):
        if lvl in self.luts:
            return self.luts[lvl]
        Ru, Rl, n = self.Ru, self.Rl, self.n
        stops, hx, vf = PALETTES[self.palette]
        xf = np.linspace(0, 1, n)[None, :]
        d = np.concatenate((np.arange(Ru - 1, -1, -1), np.arange(Rl)))[:, None].astype(float)
        tv = (d + 0.5) / Ru
        col = gradient(stops, tv * (1 - hx) + xf * hx) * (vf + (1 - vf) * tv)[..., None]
        f = lvl / (FLASH_LEVELS - 1)
        bg = (BG + BG_PULSE * f).astype(int)
        if Rl:
            fade = self.dim * (1 - self.fade * np.arange(Rl) / Rl)
            col[Ru:] = col[Ru:] * fade[:, None, None] + bg * (1 - fade)[:, None, None]
        col = np.clip(col + (255 - col) * 0.28 * f, 0, 255).astype(int)
        b = f"{bg[0]};{bg[1]};{bg[2]}"
        rows = col.tolist()
        norm = [[f"{ESC}[38;2;{r};{g};{bl}m" for r, g, bl in row] for row in rows]
        flip = [[f"{ESC}[38;2;{b};48;2;{r};{g};{bl}m" for r, g, bl in row] for row in rows]
        out = (norm, flip, f"{ESC}[48;2;{b}m")
        self.luts[lvl] = out
        return out

    def title_color(self, t: float):
        stops, _, _ = PALETTES[self.palette]
        return gradient(stops, np.array(t))

    def visual(self, h: np.ndarray, caps: np.ndarray | None, flash: float, dx: int, dy: int,
               fx: dict[int, tuple[list[int], list[str]]] | None = None) -> list[str]:
        fx = fx or {}
        lvl = int(round(min(1.0, flash) * (FLASH_LEVELS - 1)))
        norm, flip, bgseq = self._lut(lvl)
        Ru, Rl, n, cols = self.Ru, self.Rl, self.n, self.cols
        Hu = np.clip(np.rint(h * Ru * 8).astype(int), 1, Ru * 8)
        d = np.arange(Ru - 1, -1, -1)[:, None]
        up = np.clip(Hu[None, :] - d * 8, 0, 8)
        if caps is not None:
            capH = np.rint(caps * Ru * 8).astype(int)
            dcap = np.minimum(capH // 8, Ru - 1)
            show = (dcap * 8 >= Hu) & (capH > Hu)
            cols_i = np.nonzero(show)[0]
            up[Ru - 1 - dcap[cols_i], cols_i] = 9
        parts = [up]
        if Rl:
            parts.append(np.clip(Hu[None, :] - np.arange(Rl)[:, None] * 8, 0, 8))
        grid = np.vstack(parts)
        has_cells = grid.any(axis=1).tolist()
        grid = grid.tolist()

        blank_line = bgseq + " " * cols
        pad = min(max(self.pad + dx, 0), cols - self.used)
        bw, gap = self.bw, self.gap
        glyph_up = [c * bw for c in CH]            # bottom-aligned fill
        glyph_dn = [CH[8 - c] * bw for c in range(9)]  # top-aligned fill (drawn inverted)
        cap_glyph, full = "▁" * bw, "█" * bw
        last_b = n - 1

        lines = []
        for li in range(self.vis_rows):
            r = li - self.top_pad
            fxrow = fx.get(li)
            if r < 0 or not has_cells[r]:
                lines.append(blank_line if fxrow is None else "".join(flush_fx(fxrow, 0, 0, cols, bgseq)[0]))
                continue
            crow, nrow, frow = grid[r], norm[r], flip[r]
            flipped = r >= Ru
            out = [bgseq]
            blank, x, ptr, dirty = pad, pad, 0, False
            cl = fxrow[0] if fxrow else ()
            nfx = len(cl)
            for b, c in enumerate(crow):
                if c == 0:
                    w = bw if b == last_b else bw + gap
                    blank += w
                    x += w
                    continue
                if blank:
                    if ptr >= nfx or cl[ptr] >= x:  # no effect dot inside this span
                        out.append(bgseq + " " * blank)
                    else:
                        parts_, ptr = flush_fx(fxrow, ptr, x - blank, blank, bgseq)
                        out += parts_
                    blank = 0
                    dirty = False
                elif dirty:
                    out.append(bgseq)
                    dirty = False
                if c == 8:
                    out.append(nrow[b] + full)
                elif c == 9:
                    out.append(nrow[b] + cap_glyph)
                elif flipped:
                    out.append(frow[b] + glyph_dn[c])
                    dirty = True
                else:
                    out.append(nrow[b] + glyph_up[c])
                x += bw
                if b != last_b and gap:
                    blank = gap
                    x += gap
            blank += cols - x
            if blank > 0:
                if ptr >= nfx:
                    out.append(bgseq + " " * blank)
                else:
                    out += flush_fx(fxrow, ptr, cols - blank, blank, bgseq)[0]
            lines.append("".join(out))
        if dy > 0:
            lines = [blank_line] * dy + lines[:-dy]
        elif dy < 0:
            lines = lines[-dy:] + [blank_line] * (-dy)
        return lines


def flush_fx(fxrow: tuple[list[int], list[str]], ptr: int, x0: int, w: int, bgseq: str) -> tuple[list[str], int]:
    """Empty space [x0, x0+w) with the effect dots drawn on top.

    `fxrow` holds the dots of the line sorted by column; `ptr` is the first one not consumed yet,
    so a whole line costs one pass over its dots instead of one pass per empty span.
    """
    cl, tl = fxrow
    end, n = x0 + w, len(cl)
    while ptr < n and cl[ptr] < x0:  # hidden under a bar
        ptr += 1
    if ptr >= n or cl[ptr] >= end:
        return [bgseq + " " * w], ptr
    out = [bgseq]
    cur = x0
    while ptr < n and cl[ptr] < end:
        c = cl[ptr]
        if c > cur:
            out.append(" " * (c - cur))
        out.append(tl[ptr])
        cur = c + 1
        ptr += 1
    if cur < end:
        out.append(" " * (end - cur))
    return out, ptr
