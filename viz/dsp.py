"""Signal analysis: FFT -> bars, smoothing and bass detection."""
from __future__ import annotations

import math

import numpy as np

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
