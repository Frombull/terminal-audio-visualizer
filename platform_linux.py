"""Backend do Linux.

- Áudio: `pactl` lista as fontes (inclui os "monitores" das saídas = o que você está ouvindo)
  e `parec` entrega os samples. Funciona com PulseAudio e com PipeWire (pipewire-pulse).
- Tremor: move a janela do terminal via Xlib (ctypes). Só em X11/XWayland; no Wayland puro
  nenhum programa pode mover a própria janela, então o modo automático usa o tremor de conteúdo.
- Teclado: termios em modo cbreak.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import re
import select
import subprocess
import sys
import termios
import threading
import time
import tty
from typing import Callable

import numpy as np

from audio_base import AudioBase, Device

_ENV = {**os.environ, "LC_ALL": "C"}


def _run(*cmd: str, timeout: float = 3.0) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_ENV, check=True).stdout


def _spec(spec: str) -> tuple[int, int]:
    """'float32le 2ch 48000Hz' -> (taxa, canais)."""
    m = re.search(r"(\d+)ch\s+(\d+)Hz", spec or "")
    return (int(m.group(2)), int(m.group(1))) if m else (48000, 2)


class AudioInput(AudioBase):
    def __init__(self) -> None:
        super().__init__()
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.enum_error = ""
        self.devices = self._enumerate()

    def _default_sink(self) -> str:
        try:
            return _run("pactl", "get-default-sink").strip()
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            m = re.search(r"Default Sink: (\S+)", _run("pactl", "info"))
            return m.group(1) if m else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    def _sources(self) -> list[tuple[str, str, bool, int, int]]:
        """(nome, descrição, é_monitor, taxa, canais) de cada fonte do servidor de som."""
        try:
            out = []
            for s in json.loads(_run("pactl", "--format=json", "list", "sources")):
                rate, ch = _spec(s.get("sample_specification", ""))
                monitor = (s["name"].endswith(".monitor")
                           or s.get("properties", {}).get("device.class") == "monitor")
                out.append((s["name"], s.get("description") or s["name"], monitor, rate, ch))
            return out
        except (ValueError, KeyError, subprocess.CalledProcessError):
            pass  # pactl antigo, sem --format=json
        out = []
        for line in _run("pactl", "list", "short", "sources").splitlines():
            cols = line.split("\t")
            if len(cols) >= 4:
                rate, ch = _spec(cols[3])
                out.append((cols[1], cols[1], cols[1].endswith(".monitor"), rate, ch))
        return out

    def _enumerate(self) -> list[Device]:
        try:
            sources = self._sources()
        except FileNotFoundError:
            self.enum_error = "pactl não encontrado (instale pulseaudio-utils ou pipewire-pulse)"
            return []
        except (OSError, subprocess.SubprocessError) as e:
            self.enum_error = f"não consegui falar com o servidor de som: {e}"
            return []
        default_monitor = self._default_sink() + ".monitor"
        devs = [Device(index=name, name=re.sub(r"^Monitor of ", "", desc), loopback=monitor,
                       rate=rate, channels=min(ch, 2))
                for name, desc, monitor, rate, ch in sources]
        devs.sort(key=lambda d: (d.index != default_monitor, not d.loopback))
        return devs

    def open(self, dev: Device | None) -> None:
        self.close()
        if dev is None:
            self.error = self.enum_error or "nenhum dispositivo de entrada encontrado"
            return
        cmd = ["parec", "-d", str(dev.index), "--format=float32le", f"--rate={dev.rate}",
               f"--channels={dev.channels}", "--latency-msec=20", "--client-name=audio-visualizer"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_ENV)
        except FileNotFoundError:
            self.current, self.error = None, "parec não encontrado (instale pulseaudio-utils ou pipewire-pulse)"
            return
        self.proc, self.rate, self.current, self.error = proc, dev.rate, dev, ""
        self.thread = threading.Thread(target=self._reader, args=(proc, dev), daemon=True)
        self.thread.start()

    def _reader(self, proc: subprocess.Popen, dev: Device) -> None:
        fd, ch = proc.stdout.fileno(), dev.channels
        frame, buf = 4 * ch, b""
        while True:
            try:
                data = os.read(fd, 8192)
            except OSError:
                break
            if not data:
                break
            buf += data
            k = len(buf) // frame * frame
            if k:
                x = np.frombuffer(buf[:k], np.float32)
                self._push(x.reshape(-1, ch).mean(axis=1) if ch > 1 else x)
                buf = buf[k:]
        if proc is self.proc:  # morreu sozinho (não fomos nós que fechamos)
            msg = proc.stderr.read().decode(errors="replace").strip().splitlines()
            self.current, self.error = None, f"{dev.label}: {msg[-1] if msg else 'parec terminou'}"

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
            for pipe in (proc.stdout, proc.stderr):
                pipe.close()
        if self.thread is not None:
            self.thread.join(timeout=0.5)
            self.thread = None


# --------------------------------------------------------------------------- #
# Tremor da janela do terminal (X11)
# --------------------------------------------------------------------------- #


class WindowShaker:
    def __init__(self) -> None:
        self.x = None
        self.dpy = None
        self.win = 0
        self.ok = False
        self.calibrated = False
        self.base = (0, 0)
        self.delta = (0, 0)   # diferença fixa entre onde pedimos e onde o gerenciador de janelas põe
        self.active = False
        self.t_move = self.t_check = 0.0
        try:
            self._connect()
        except Exception:  # noqa: BLE001 - qualquer falha = sem tremor de janela
            self.dpy = None

    # ----- X11 -------------------------------------------------------------
    def _connect(self) -> None:
        if not os.environ.get("DISPLAY"):
            return
        wayland = os.environ.get("XDG_SESSION_TYPE") == "wayland"
        win = int(os.environ["WINDOWID"], 0) if os.environ.get("WINDOWID") else 0
        if not win and not wayland:  # no Wayland a "janela ativa" pode ser outro app
            win = self._active_window()
        if not win:
            return
        x = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        W, D = ctypes.c_ulong, ctypes.c_void_p
        x.XOpenDisplay.restype, x.XOpenDisplay.argtypes = D, [ctypes.c_char_p]
        x.XDefaultRootWindow.restype, x.XDefaultRootWindow.argtypes = W, [D]
        x.XMoveWindow.argtypes = [D, W, ctypes.c_int, ctypes.c_int]
        x.XTranslateCoordinates.restype = ctypes.c_int
        x.XTranslateCoordinates.argtypes = [D, W, W, ctypes.c_int, ctypes.c_int,
                                            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                                            ctypes.POINTER(W)]
        x.XSync.argtypes = [D, ctypes.c_int]
        x.XFlush.argtypes = [D]
        # o handler padrão do Xlib encerra o processo se a janela não existir
        handler_t = ctypes.CFUNCTYPE(ctypes.c_int, D, D)
        self._handler = handler_t(lambda d, e: 0)
        x.XSetErrorHandler(self._handler)
        self.dpy = x.XOpenDisplay(None)
        if not self.dpy:
            return
        self.x, self.win, self.root = x, win, x.XDefaultRootWindow(self.dpy)

    @staticmethod
    def _active_window() -> int:
        for cmd in (("xdotool", "getactivewindow"), ("xprop", "-root", "_NET_ACTIVE_WINDOW")):
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=1).stdout
            except (OSError, subprocess.SubprocessError):
                continue
            m = re.search(r"0x[0-9a-fA-F]+", out) or re.search(r"^\d+$", out.strip())
            if m:
                return int(m.group(0), 0)
        return 0

    def _pos(self) -> tuple[int, int]:
        px, py, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
        if not self.x.XTranslateCoordinates(self.dpy, self.win, self.root, 0, 0,
                                            ctypes.byref(px), ctypes.byref(py), ctypes.byref(child)):
            raise OSError("janela inválida")
        return px.value, py.value

    def _request(self, x: int, y: int) -> None:
        self.x.XMoveWindow(self.dpy, self.win, int(x), int(y))
        self.x.XFlush(self.dpy)

    def _settle(self) -> tuple[int, int]:
        self.x.XSync(self.dpy, 0)
        time.sleep(0.06)  # o gerenciador de janelas aplica o movimento de forma assíncrona
        return self._pos()

    def _calibrate(self) -> None:
        """Descobre se o gerenciador de janelas deixa a janela mover e qual o desvio fixo dele."""
        self.calibrated = True
        try:
            bx, by = self._pos()
            self._request(bx + 10, by)
            p1 = self._settle()
            self._request(bx + 20, by)
            p2 = self._settle()
            self.ok = abs((p2[0] - p1[0]) - 10) <= 2  # maximizada/tiling: o movimento é ignorado
            self.delta = (p1[0] - (bx + 10), p1[1] - by)
            self._request(bx - self.delta[0], by - self.delta[1])
            self.base = self._settle()
        except OSError:
            self.ok = False

    # ----- interface comum -------------------------------------------------
    def usable(self) -> bool:
        if self.dpy is None:
            return False
        if not self.calibrated:
            self._calibrate()
        return self.ok

    def move(self, dx: int, dy: int) -> None:
        self._request(self.base[0] + dx - self.delta[0], self.base[1] + dy - self.delta[1])
        self.active, self.t_move = True, time.perf_counter()

    def reset(self) -> None:
        if not self.ok:
            return
        now = time.perf_counter()
        if self.active:
            self._request(self.base[0] - self.delta[0], self.base[1] - self.delta[1])
            self.active, self.t_move = False, now
        elif now - self.t_move > 0.5 and now - self.t_check > 0.5:
            self.t_check = now  # parado: se o usuário arrastou a janela, adota a nova posição
            try:
                pos = self._pos()
            except OSError:
                return
            if abs(pos[0] - self.base[0]) > 2 or abs(pos[1] - self.base[1]) > 2:
                self.base = pos


# --------------------------------------------------------------------------- #
# Terminal e teclado
# --------------------------------------------------------------------------- #


def setup_terminal() -> Callable[[], None] | None:
    """Põe o terminal em modo cbreak (tecla a tecla, sem eco). Devolve a função que restaura."""
    if not sys.stdin.isatty():
        return None
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    return lambda: termios.tcsetattr(fd, termios.TCSADRAIN, old)


_ARROWS = {"A": "up", "B": "down", "C": "right", "D": "left"}


def read_keys() -> list[str]:
    fd = sys.stdin.fileno()
    data = b""
    while select.select([fd], [], [], 0)[0]:
        chunk = os.read(fd, 64)
        if not chunk:
            break
        data += chunk
    text, keys, i = data.decode("utf-8", "ignore"), [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\x1b":
            if text[i + 1:i + 2] in ("[", "O"):  # seta/função: ESC [ params letra
                j = i + 2
                while j < len(text) and text[j] in "0123456789;":
                    j += 1
                keys.append(_ARROWS.get(text[j:j + 1], ""))
                i = j + 1
                continue
            keys.append("esc")
        elif ch in "\r\n":
            keys.append("enter")
        elif ch == "\t":
            keys.append("tab")
        elif ch == "\x03":
            keys.append("q")
        else:
            keys.append(ch.lower())
        i += 1
    return keys
