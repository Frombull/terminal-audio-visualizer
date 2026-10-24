#!/usr/bin/env python3
"""Terminal audio visualizer (Windows and Linux).

Captures what is playing on your PC (WASAPI loopback on Windows, PulseAudio/PipeWire
monitor on Linux) or a microphone and draws color-faded bars. When a strong bass hit
lands, the terminal window shakes for real (or, if the window can't be moved, the
content shakes instead).

    python visualizer.py            # default output (what you are listening to)
    python visualizer.py --demo     # synthetic signal, nothing needs to play
    python visualizer.py --list-devices

Keys:  M = menu   Up/Down = sensitivity   Left/Right = bar count   P = palette
       S = shake   F = effects   D = switch input   Q = quit
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import numpy as np

from audio_base import Device

if os.name == "nt":
    from platform_win import AudioInput, WindowShaker, read_keys, setup_terminal
else:
    from platform_linux import AudioInput, WindowShaker, read_keys, setup_terminal

SETTINGS_FILE = Path(__file__).with_name("settings.json")
ESC = "\x1b"

# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


@dataclass
class Settings:
    device: str = ""            # device label ("" = default output)
    sensitivity: float = 1.0
    auto_gain: bool = True
    bars: int = 48
    smoothing: int = 5
    palette: str = "neon"
    style: str = "bars"         # bars | mirror
    reflection: bool = True
    peaks: bool = True
    fx: str = "all"             # all | stars | bursts | off
    shake: str = "auto"         # auto | window | content | off
    shake_strength: int = 5
    bass_sensitivity: float = 1.0
    fps: int = 60

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            names = {f.name for f in fields(cls)}
            for k, v in data.items():
                if k in names and type(v) is type(getattr(s, k)):
                    setattr(s, k, v)
        except (OSError, ValueError):
            pass
        return s

    def save(self) -> None:
        try:
            SETTINGS_FILE.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        except OSError:
            pass


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
# Audio input
# --------------------------------------------------------------------------- #

class DemoSource:
    """Synthetic signal (kick + hi-hat + chords) to try it without playing anything."""

    rate = 48000
    devices: list[Device] = []
    error = ""
    label = "Demo"
    current = None

    def __init__(self, clock=time.perf_counter) -> None:
        self.clock = clock
        self.t0 = clock()

    def find(self, label):  # noqa: D401
        return None

    def open(self, dev) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def latest(self, n: int) -> np.ndarray:
        t = (self.clock() - self.t0) - (n - 1 - np.arange(n)) / self.rate
        beat = 60.0 / 124.0
        p = t % beat
        kick = 0.85 * np.exp(-p * 9) * np.sin(2 * np.pi * (46 * p + 3 * (1 - np.exp(-30 * p))))
        ph = (t + beat / 4) % (beat / 2)
        hat = np.random.randn(n) * np.exp(-ph * 70) * 0.12
        swell = 0.55 + 0.45 * np.sin(2 * np.pi * 0.25 * t)
        pad = swell * (0.07 * np.sin(2 * np.pi * 220 * t) + 0.06 * np.sin(2 * np.pi * 330 * t)
                       + 0.05 * np.sin(2 * np.pi * 659 * t) + 0.03 * np.sin(2 * np.pi * 1760 * t))
        return (kick + hat + pad).astype(np.float32)


# --------------------------------------------------------------------------- #
# Analysis (FFT -> bars) and bass detection
# --------------------------------------------------------------------------- #

DB_RANGE = 50.0       # displayed dynamic range
MANUAL_REF_DB = -14.0  # top reference when auto gain is off


class Analyzer:
    def __init__(self) -> None:
        self.rate = 0
        self.bars = 0
        self.N = 4096
        self.ref_db = -30.0

    def configure(self, rate: int, bars: int) -> None:
        self.rate, self.bars = rate, bars
        self.N = 1 << int(round(math.log2(rate * 0.085)))
        self.win = np.hanning(self.N).astype(np.float32)
        self.wsum = float(self.win.sum())
        self.freqs = np.fft.rfftfreq(self.N, 1.0 / rate)
        bin_hz = rate / self.N
        top = len(self.freqs) - 1
        edges = np.geomspace(30.0, min(16000.0, rate / 2 * 0.92), bars + 1)
        klo, khi = edges[:-1] / bin_hz, edges[1:] / bin_hz
        s = np.minimum(np.floor(klo).astype(int), top - 1)
        e = np.minimum(np.maximum(np.ceil(khi).astype(int), s + 1), top)
        self.red = np.empty(2 * bars, int)
        self.red[0::2], self.red[1::2] = s, e
        self.narrow = (khi - klo) < 1.5
        self.centers = np.sqrt(edges[:-1] * edges[1:])
        self.tilt = 3.0 * np.log2(self.centers / 60.0)  # compensates the natural treble roll-off
        self.b0 = max(1, int(30 / bin_hz))
        self.b1 = max(self.b0 + 2, int(150 / bin_hz))

    def process(self, x: np.ndarray, sens_db: float, auto: bool, dt: float):
        spec = np.abs(np.fft.rfft(x[-self.N:] * self.win)) * (2.0 / self.wsum)
        vals = np.maximum.reduceat(spec, self.red)[0::2]
        vals = np.where(self.narrow, np.interp(self.centers, self.freqs, spec), vals)
        db = 20.0 * np.log10(vals + 1e-9) + self.tilt
        bass_db = 20.0 * np.log10(spec[self.b0:self.b1].max() + 1e-9)
        if auto:
            peak = float(db.max())
            if peak > self.ref_db:
                self.ref_db += (peak - self.ref_db) * min(1.0, dt / 0.08)
            else:
                self.ref_db -= 4.0 * dt
            self.ref_db = max(self.ref_db, -55.0)
            ref = self.ref_db
        else:
            ref = MANUAL_REF_DB
        bass = max(0.0, (bass_db - ref + DB_RANGE) / DB_RANGE)  # the shake ignores the bar sensitivity
        ref -= sens_db
        lv = np.clip((db - ref + DB_RANGE) / DB_RANGE, 0.0, 1.0)
        return lv, bass


class BarState:
    """Smoothing (fast attack / slow release) and floating peaks."""

    def __init__(self, n: int) -> None:
        self.v = np.zeros(n)
        self.cap = np.zeros(n)
        self.vel = np.zeros(n)
        self.hold = np.zeros(n)

    def update(self, target: np.ndarray, smoothing: int, dt: float) -> None:
        if smoothing > 0 and len(target) > 2:
            t = np.pad(target, 1, mode="edge")
            target = 0.2 * t[:-2] + 0.6 * t[1:-1] + 0.2 * t[2:]
        ka = 1 - math.exp(-dt / (0.002 + 0.006 * smoothing))
        kr = 1 - math.exp(-dt / (0.02 + 0.045 * smoothing))
        self.v += (target - self.v) * np.where(target > self.v, ka, kr)
        up = self.v >= self.cap
        self.cap[up] = self.v[up]
        self.hold[up] = 0.4
        self.vel[up] = 0.0
        self.hold[~up] -= dt
        fall = ~up & (self.hold <= 0)
        self.vel[fall] += 2.2 * dt
        self.cap[fall] = np.maximum(self.cap[fall] - self.vel[fall] * dt, self.v[fall])


class BassTracker:
    """Detects bass hits (energy rising above the recent average)."""

    def __init__(self) -> None:
        self.avg = 0.0
        self.env = 0.0    # shake intensity (0..1)
        self.flash = 0.0  # extra brightness on the bars
        self.level = 0.0

    def update(self, bass: float, sens: float, dt: float) -> None:
        # fast average: only a sudden rise (attack) stays above it
        excess = bass - self.avg
        self.avg += (bass - self.avg) * (1 - math.exp(-dt / 0.15))
        hit = 0.0
        if bass > 0.4 and excess > 0.06 / sens:
            hit = min(1.0, 0.45 + 2.5 * excess)
        self.env = max(self.env * math.exp(-dt / 0.10), hit)
        self.flash = max(self.flash * math.exp(-dt / 0.18), hit)
        self.level += (min(bass, 1.0) - self.level) * (1 - math.exp(-dt / 0.06))


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

BG = np.array([9, 9, 16])
BG_PULSE = np.array([34, 10, 44])
PANEL_BG = (18, 18, 30)
CH = " ▁▂▃▄▅▆▇█"
FLASH_LEVELS = 5


def fg(c) -> str:
    return f"{ESC}[38;2;{int(c[0])};{int(c[1])};{int(c[2])}m"


def bgc(c) -> str:
    return f"{ESC}[48;2;{int(c[0])};{int(c[1])};{int(c[2])}m"


BIT = np.array([[0x01, 0x02, 0x04, 0x40], [0x08, 0x10, 0x20, 0x80]], np.uint8)  # [dx][dy] -> braille dot


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

    def _plot(self, bits, col, px, py, rgb) -> None:
        ok = (px >= 0) & (px < self.W) & (py >= 0) & (py < self.H)
        px, py, rgb = px[ok], py[ok], rgb[ok]
        cy, cx = py >> 2, px >> 1
        np.bitwise_or.at(bits, (cy, cx), BIT[px & 1, py & 3])
        for c in range(3):
            np.maximum.at(col[:, :, c], (cy, cx), rgb[:, c])

    def render(self, palette: str, flash: float, stars_on: bool, bursts_on: bool,
               axis_y: float, mirror: bool) -> dict[int, list[tuple[int, str, str]]]:
        W, H = self.W, self.H
        bits = np.zeros((self.R, self.C), np.uint8)
        col = np.zeros((self.R, self.C, 3))
        stops = PALETTES[palette][0]
        top = gradient(stops, np.array(0.9))
        if stars_on and len(self.z):
            f = np.linspace(0, 1, 5)[:, None]
            zt = self.z + 0.004 + 0.075 * self.warp  # streak tail: farther away = closer to the center
            zz = self.z[None, :] + (zt - self.z)[None, :] * f
            px = np.floor(W / 2 + self.x[None, :] / zz * (H / 2)).astype(int)
            py = np.floor(H / 2 + self.y[None, :] / zz * (H / 2)).astype(int)
            b = ((1 - self.z) ** 0.8 * 0.8 + 0.2)[None, :] * (1 - 0.65 * f) * (0.55 + 0.45 * min(flash, 1.0))
            tint = top * 0.5 + 255 * 0.5
            self._plot(bits, col, px.ravel(), py.ravel(), (b[..., None] * tint).reshape(-1, 3))
        if bursts_on:
            s = self.sparks
            if len(s):
                a = (s[:, 4] / s[:, 5])[:, None]
                base = gradient(stops, s[:, 6])
                rgb = (base + (255 - base) * (1 - a) * 0.6) * (1 - a) ** 0.8
                for back in (0.0, 0.03):
                    self._plot(bits, col, np.floor(s[:, 0] - s[:, 2] * back).astype(int),
                               np.floor(s[:, 1] - s[:, 3] * back).astype(int), rgb)
            for age, st in self.rings:
                r = (1 - (1 - age) ** 2) * W * 0.5
                m = int(np.clip(r * 1.6, 24, 600))
                th = np.linspace(0 if mirror else math.pi, 2 * math.pi, m)
                inten = (1 - age) ** 1.6 * (0.45 + 0.55 * st)
                rgb = np.tile((top * 0.4 + 255 * 0.6) * inten, (m, 1))
                self._plot(bits, col, np.floor(W / 2 + r * np.cos(th)).astype(int),
                           np.floor(axis_y + r * np.sin(th) * 0.85).astype(int), rgb)
        fx: dict[int, list[tuple[int, str, str]]] = {}
        for r, c in np.argwhere(bits > 0).tolist():
            cr, cg, cb = np.clip(col[r, c], 0, 255).astype(int).tolist()
            fx.setdefault(r, []).append((c, chr(0x2800 + int(bits[r, c])), f"{ESC}[38;2;{cr};{cg};{cb}m"))
        return fx


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
        norm = [[f"{ESC}[38;2;{r};{g};{bl};48;2;{b}m" for r, g, bl in row] for row in col.tolist()]
        flip = [[f"{ESC}[38;2;{b};48;2;{r};{g};{bl}m" for r, g, bl in row] for row in col.tolist()]
        out = (norm, flip, f"{ESC}[48;2;{b}m")
        self.luts[lvl] = out
        return out

    def title_color(self, t: float):
        stops, _, _ = PALETTES[self.palette]
        return gradient(stops, np.array(t))

    def visual(self, h: np.ndarray, caps: np.ndarray | None, flash: float, dx: int, dy: int,
               fx: dict[int, list[tuple[int, str, str]]] | None = None) -> list[str]:
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
        grid = np.vstack(parts).tolist()

        blank_line = bgseq + " " * cols
        pad = min(max(self.pad + dx, 0), cols - self.used)
        bw, gap = self.bw, self.gap

        def blank_run(out: list[str], li: int, x0: int, w: int) -> None:
            """Empty space [x0, x0+w) on line li, with the effect dots on top."""
            cur, end = x0, x0 + w
            for c, glyph, seq in fx.get(li, ()):
                if c < cur or c >= end:
                    continue
                if c > cur:
                    out.append(bgseq + " " * (c - cur))
                out.append(seq + bgseq + glyph)
                cur = c + 1
            if cur < end:
                out.append(bgseq + " " * (end - cur))

        zero = [0] * n
        lines = []
        for li in range(self.vis_rows):
            r = li - self.top_pad
            crow = grid[r] if r >= 0 else zero
            if not any(crow):
                if li in fx:
                    out = []
                    blank_run(out, li, 0, cols)
                    lines.append("".join(out))
                else:
                    lines.append(blank_line)
                continue
            flipped = r >= Ru
            nrow, frow = norm[r], flip[r]
            out = []
            blank, x = pad, pad
            for b, c in enumerate(crow):
                last = b == n - 1
                if c == 0:
                    w = bw if last else bw + gap
                    blank += w
                    x += w
                    continue
                if blank:
                    blank_run(out, li, x - blank, blank)
                    blank = 0
                if c == 8:
                    out.append(nrow[b] + "█" * bw)
                elif c == 9:
                    out.append(nrow[b] + "▁" * bw)
                elif flipped:
                    out.append(frow[b] + CH[8 - c] * bw)
                else:
                    out.append(nrow[b] + CH[c] * bw)
                x += bw
                if not last and gap:
                    blank = gap
                    x += gap
            blank += cols - x
            if blank > 0:
                blank_run(out, li, cols - blank, blank)
            lines.append("".join(out))
        if dy > 0:
            lines = [blank_line] * dy + lines[:-dy]
        elif dy < 0:
            lines = lines[-dy:] + [blank_line] * (-dy)
        return lines


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #

ITEMS = [
    dict(key="device", label="Audio input", kind="device"),
    dict(key="sensitivity", label="Sensitivity", kind="num", lo=0.02, hi=8.0, mult=1.25, fmt="x{:.2f}"),
    dict(key="auto_gain", label="Auto gain", kind="bool"),
    dict(key="bars", label="Bar count", kind="num", lo=4, hi=256, step=2, fmt="{:d}"),
    dict(key="smoothing", label="Smooth motion", kind="num", lo=0, hi=10, step=1, fmt="{:d}"),
    dict(key="palette", label="Colors (fade)", kind="choice", opts=list(PALETTES)),
    dict(key="style", label="Style", kind="choice", opts=["bars", "mirror"],
         names={"bars": "Bars", "mirror": "Mirrored"}),
    dict(key="reflection", label="Reflection", kind="bool"),
    dict(key="peaks", label="Floating peaks", kind="bool"),
    dict(key="fx", label="Cosmic effects", kind="choice", opts=["all", "stars", "bursts", "off"],
         names={"all": "All", "stars": "Stars only", "bursts": "Sparks/rings only", "off": "Off"}),
    dict(key="shake", label="Screen shake", kind="choice", opts=["auto", "window", "content", "off"],
         names={"auto": "Automatic", "window": "Window", "content": "Content", "off": "Off"}),
    dict(key="shake_strength", label="Shake strength", kind="num", lo=0, hi=10, step=1, fmt="{:d}"),
    dict(key="bass_sensitivity", label="Bass sensitivity", kind="num", lo=0.3, hi=3.0, step=0.1, fmt="x{:.1f}"),
    dict(key="fps", label="Frames per second", kind="num", lo=15, hi=120, step=5, fmt="{:d}"),
]

HINTS = " M menu  Up/Down sensitivity  Left/Right bars  P palette  S shake  F effects  D input  Q quit "
MENU_HINTS = " Up/Down item  Left/Right adjust  R reset defaults  M/Enter/Esc close "


def term_size() -> tuple[int, int]:
    try:
        s = os.get_terminal_size()
        return s.columns, s.lines
    except OSError:
        return 100, 30


class App:
    def __init__(self, settings: Settings, audio) -> None:
        self.s = settings
        self.audio = audio
        self.an = Analyzer()
        self.bars = BarState(1)
        self.bass = BassTracker()
        self.rend = Renderer()
        self.cosmos = Cosmos()
        self.prev_env = 0.0
        self.shaker = WindowShaker()
        self.running = True
        self.menu_open = False
        self.sel = 0
        self.toast = ("", 0.0)
        self.dirty_at = 0.0
        self.size = (0, 0)
        self.fps = float(settings.fps)
        self.shake_state = "off"
        self.audio.open(self.audio.find(settings.device))

    # ----- settings ---------------------------------------------------------
    def say(self, text: str) -> None:
        self.toast = (text, time.perf_counter() + 1.6)

    def touch(self) -> None:
        self.dirty_at = time.perf_counter()

    def display(self, item) -> str:
        k, v = item["key"], getattr(self.s, item["key"], None)
        if item["kind"] == "device":
            return self.audio.label
        if item["kind"] == "bool":
            return "on" if v else "off"
        if item["kind"] == "choice":
            return item.get("names", {}).get(v, v)
        return item["fmt"].format(v)

    def adjust(self, item, direction: int) -> None:
        k, s = item["key"], self.s
        if item["kind"] == "bool":
            setattr(s, k, not getattr(s, k))
        elif item["kind"] == "choice":
            opts = item["opts"]
            setattr(s, k, opts[(opts.index(getattr(s, k)) + direction) % len(opts)])
        elif item["kind"] == "num":
            cur = getattr(s, k)
            v = cur * item["mult"] ** direction if "mult" in item else cur + direction * item["step"]
            v = min(max(v, item["lo"]), item["hi"])
            setattr(s, k, int(round(v)) if isinstance(cur, int) else round(v, 3))
        elif item["kind"] == "device":
            devs = self.audio.devices
            if devs:
                cur = self.audio.current
                i = devs.index(cur) if cur in devs else -1
                dev = devs[(i + direction) % len(devs)]
                s.device = dev.label
                self.audio.open(dev)
        self.touch()
        if item["kind"] != "device" or self.audio.devices:
            self.say(f"{item['label']}: {self.display(item)}")

    def item(self, key: str):
        return next(i for i in ITEMS if i["key"] == key)

    def handle_keys(self) -> None:
        for k in read_keys():
            if k == "q" or (k == "esc" and not self.menu_open):
                self.running = False
            elif k in ("m", "tab", "enter") or (k == "esc" and self.menu_open):
                self.menu_open = not self.menu_open
            elif self.menu_open:
                if k == "up":
                    self.sel = (self.sel - 1) % len(ITEMS)
                elif k == "down":
                    self.sel = (self.sel + 1) % len(ITEMS)
                elif k in ("left", "right"):
                    self.adjust(ITEMS[self.sel], 1 if k == "right" else -1)
                elif k == "r":
                    dev = self.s.device
                    self.s.__dict__.update(asdict(Settings(device=dev)))
                    self.touch()
                    self.say("Defaults restored")
            elif k in ("up", "+", "="):
                self.adjust(self.item("sensitivity"), 1)
            elif k in ("down", "-", "_"):
                self.adjust(self.item("sensitivity"), -1)
            elif k == "right":
                self.adjust(self.item("bars"), 1)
            elif k == "left":
                self.adjust(self.item("bars"), -1)
            elif k == "p":
                self.adjust(self.item("palette"), 1)
            elif k == "s":
                self.adjust(self.item("shake"), 1)
            elif k == "f":
                self.adjust(self.item("fx"), 1)
            elif k == "d":
                self.adjust(self.item("device"), 1)

    # ----- shake ----------------------------------------------------------
    def apply_shake(self) -> tuple[int, int]:
        s, env = self.s, self.bass.env
        amp = (env ** 1.3) * s.shake_strength / 10.0 if s.shake != "off" else 0.0
        use_window = s.shake in ("auto", "window") and self.shaker.usable()
        self.shake_state = "window" if use_window else (
            "content" if s.shake in ("auto", "content") else ("no window" if s.shake == "window" else "off"))
        if use_window:
            if amp > 0.02:
                px = amp * 40
                self.shaker.move(round(random.uniform(-1, 1) * px), round(random.uniform(-1, 1) * px * 0.7))
            else:
                self.shaker.reset()
            return 0, 0
        self.shaker.reset()
        if s.shake in ("auto", "content") and amp > 0.04:
            return round(random.uniform(-1, 1) * amp * 2.5), round(random.uniform(-1, 1) * amp * 1.4)
        return 0, 0

    # ----- drawing ---------------------------------------------------------
    def title_bar(self, cols: int) -> str:
        title = " ♪ AUDIO VISUALIZER"
        left = "".join(fg(self.rend.title_color(i / (len(title) - 1))) + ch for i, ch in enumerate(title))
        meter_n = 10
        filled = int(round(min(1.0, self.bass.level) * meter_n))
        right = f"bass {'▮' * filled}{'▯' * (meter_n - filled)}  {self.fps:3.0f} fps  shake: {self.shake_state} "
        room = cols - len(title) - len(right) - 3
        if room < 8:  # narrow terminal: essentials only
            right = f"{self.fps:3.0f} fps "
            room = max(0, cols - len(title) - len(right) - 3)
        dev = self.audio.label
        dev = dev if len(dev) <= room else (dev[:room - 1] + "~" if room > 0 else "")
        gap = max(0, room - len(dev))
        return (bgc(PANEL_BG) + left + fg((120, 120, 150)) + "  " + dev + " " * (gap + 1)
                + fg((170, 170, 200)) + right + " " * 0)

    def bottom_bar(self, cols: int) -> str:
        text, until = self.toast
        if text and time.perf_counter() < until:
            text, color = f" {text}", (255, 255, 255)
        else:
            text, color = (MENU_HINTS if self.menu_open else HINTS), (120, 120, 150)
        return bgc(PANEL_BG) + fg(color) + text[:cols].ljust(cols)

    def menu_lines(self, cols: int, avail: int) -> list[str]:
        n_vis = min(len(ITEMS), avail)
        top = min(max(0, self.sel - n_vis // 2), len(ITEMS) - n_vis)
        accent = self.rend.title_color(0.5)
        out = []
        for i in range(top, top + n_vis):
            it = ITEMS[i]
            val = self.display(it)
            if it["kind"] == "num":
                v = getattr(self.s, it["key"])
                if "mult" in it:  # logarithmic scale
                    frac = math.log(v / it["lo"]) / math.log(it["hi"] / it["lo"])
                else:
                    frac = (v - it["lo"]) / (it["hi"] - it["lo"])
                f = int(round(min(max(frac, 0.0), 1.0) * 12))
                val = "█" * f + "░" * (12 - f) + "  " + val
            selected = i == self.sel
            text = f" {'>' if selected else ' '} {it['label']:<20}{'< ' if selected else '  '}{val}{' >' if selected else ''}"
            text = text[:cols].ljust(cols)
            if selected:
                out.append(bgc(tuple(c * 0.35 for c in accent)) + fg((255, 255, 255)) + text)
            else:
                out.append(bgc(PANEL_BG) + fg((170, 170, 200)) + text)
        return out

    def menu_height(self, rows: int) -> int:
        return min(len(ITEMS), max(3, rows - 2 - 8))

    def fx_frame(self, dt: float):
        """Advances and draws the cosmic layer; fires sparks/rings on bass hits."""
        mode, c, r, env = self.s.fx, self.cosmos, self.rend, self.bass.env
        if mode == "off":
            return None
        c.resize(r.cols, r.vis_rows)
        c.update(dt, env, float(self.bars.v.mean()))
        now = time.perf_counter()
        if mode in ("all", "bursts") and env - self.prev_env > 0.25 and now - c.last_spawn > 0.12 \
                and self.bars.v.max() > 0.15:
            c.last_spawn = now
            xs = (r.pad + np.arange(r.n) * r.unit + r.bw / 2) * 2
            ys = (r.top_pad + r.Ru * (1 - self.bars.v)) * 4
            c.burst(xs, ys, np.clip(self.bars.v, 0, 1), env, ring=env > 0.6)
        self.prev_env = env
        return c.render(self.s.palette, self.bass.flash, mode in ("all", "stars"), mode in ("all", "bursts"),
                        (r.top_pad + r.Ru) * 4, self.s.style == "mirror")

    def draw(self, cols: int, rows: int, n: int, dx: int, dy: int, dt: float) -> str:
        s = self.s
        menu_h = self.menu_height(rows) if self.menu_open else 0
        vis_rows = max(4, rows - 2 - menu_h)
        self.rend.setup(cols, vis_rows, n, s.palette, s.style, s.reflection)
        vis = self.rend.visual(self.bars.v, self.bars.cap if s.peaks and s.style == "bars" else None,
                               self.bass.flash, dx, dy, self.fx_frame(dt))
        lines = [self.title_bar(cols)] + vis
        if self.menu_open:
            lines += self.menu_lines(cols, menu_h)
        lines.append(self.bottom_bar(cols))
        body = "".join(f"{ESC}[{i};1H{ln}" for i, ln in enumerate(lines, 1))
        return f"{ESC}[?2026h{body}{ESC}[0m{ESC}[?2026l"

    # ----- main loop --------------------------------------------------
    def run(self) -> None:
        out = sys.stdout
        last = time.perf_counter()
        while self.running:
            now = time.perf_counter()
            dt = min(max(now - last, 1e-3), 0.1)
            last = now
            self.fps += (1.0 / dt - self.fps) * 0.1
            self.handle_keys()
            if self.dirty_at and now - self.dirty_at > 1.0:
                self.s.save()
                self.dirty_at = 0.0

            cols, rows = term_size()
            if (cols, rows) != self.size:
                self.size = (cols, rows)
                out.write(f"{ESC}[0m{ESC}[2J")
            if cols < 30 or rows < 10:
                out.write(f"{ESC}[2J{ESC}[Hterminal too small")
                out.flush()
                time.sleep(0.1)
                continue

            n = max(4, min(self.s.bars, cols - 4))
            if self.an.rate != self.audio.rate or self.an.bars != n:
                if self.an.bars != n:
                    self.bars = BarState(n)
                self.an.configure(self.audio.rate, n)
            lv, bass = self.an.process(self.audio.latest(self.an.N),
                                       20 * math.log10(self.s.sensitivity), self.s.auto_gain, dt)
            self.bars.update(lv, self.s.smoothing, dt)
            self.bass.update(bass, self.s.bass_sensitivity, dt)
            dx, dy = self.apply_shake()

            out.write(self.draw(cols, rows, n, dx, dy, dt))
            out.flush()
            spare = 1.0 / self.s.fps - (time.perf_counter() - now)
            if spare > 0:
                time.sleep(spare)


# --------------------------------------------------------------------------- #
# Terminal / program entry
# --------------------------------------------------------------------------- #


def main() -> None:
    ap = argparse.ArgumentParser(description="Terminal audio visualizer")
    ap.add_argument("--demo", action="store_true", help="use a synthetic signal instead of capturing audio")
    ap.add_argument("--list-devices", action="store_true", help="list the available inputs")
    ap.add_argument("--device", help="part of the input name (e.g. 'Speakers' or 'Mic')")
    ap.add_argument("--reset", action="store_true", help="ignore the saved settings")
    args = ap.parse_args()

    settings = Settings() if args.reset else Settings.load()
    audio = DemoSource() if args.demo else AudioInput()

    if args.list_devices:
        for dv in audio.devices:
            print(f"{dv.label}  ({dv.rate} Hz, {dv.channels} channels)")
        audio.shutdown()
        return
    if args.device and audio.devices:
        match = next((d for d in audio.devices if args.device.lower() in d.label.lower()), None)
        if match:
            settings.device = match.label

    restore = setup_terminal() if sys.stdout.isatty() else None
    if restore is None:
        audio.shutdown()
        sys.exit("Run this inside a terminal (on Windows, preferably Windows Terminal).")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    app = None
    sys.stdout.write(f"{ESC}[?1049h{ESC}[?25l{ESC}[2J")
    try:
        app = App(settings, audio)
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        if app is not None:
            app.shaker.reset()
        settings.save()
        audio.shutdown()
        sys.stdout.write(f"{ESC}[0m{ESC}[?25h{ESC}[?1049l")
        sys.stdout.flush()
        restore()


if __name__ == "__main__":
    main()
