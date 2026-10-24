"""The application: settings, menu, keys, shake logic and the main loop."""
from __future__ import annotations

import json
import math
import os
import random
import sys
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import numpy as np

from .dsp import Analyzer, BarState, BassTracker
from .draw import ESC, PALETTES, PANEL_BG, Cosmos, Renderer, bgc, fg

if os.name == "nt":
    from .platform_win import AudioInput, WindowShaker, read_keys, setup_terminal  # noqa: F401
else:
    from .platform_linux import AudioInput, WindowShaker, read_keys, setup_terminal  # noqa: F401

# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

def _settings_path() -> Path:
    if getattr(sys, "frozen", False):  # packaged exe: the bundle dir is temporary, use the user config dir
        base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        return Path(base) / "audio-visualizer" / "settings.json"
    return Path(__file__).resolve().parent.parent / "settings.json"


SETTINGS_FILE = _settings_path()


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
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        except OSError:
            pass


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
        v = getattr(self.s, item["key"], None)
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
