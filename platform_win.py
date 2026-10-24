"""Windows backend: loopback/microphone through WASAPI, shake through user32, keys through msvcrt."""
from __future__ import annotations

import ctypes
import msvcrt
import sys
import time
from ctypes import wintypes
from typing import Callable

import numpy as np

from audio_base import AudioBase, Device

try:
    import pyaudiowpatch as pyaudio
except ImportError:  # pragma: no cover
    sys.exit("Missing dependencies. Run:  pip install -r requirements.txt")


class AudioInput(AudioBase):
    def __init__(self) -> None:
        super().__init__()
        self.pa = pyaudio.PyAudio()
        self.stream = None
        self.devices = self._enumerate()

    def _enumerate(self) -> list[Device]:
        try:
            api = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)["index"]
        except OSError:
            return []
        default_loop = None
        try:
            default_loop = self.pa.get_default_wasapi_loopback()["index"]
        except Exception:
            pass
        found = []
        for i in range(self.pa.get_device_count()):
            d = self.pa.get_device_info_by_index(i)
            if d["hostApi"] != api or d["maxInputChannels"] < 1:
                continue
            found.append(Device(
                index=i,
                name=d["name"].replace(" [Loopback]", ""),
                loopback=bool(d.get("isLoopbackDevice")),
                rate=int(d["defaultSampleRate"]),
                channels=int(d["maxInputChannels"]),
            ))
        found.sort(key=lambda dv: (dv.index != default_loop, not dv.loopback, dv.index))
        return found

    def open(self, dev: Device | None) -> None:
        self.close()
        if dev is None:
            self.error = "no input device found"
            return
        ch = dev.channels

        def callback(data, frames, tinfo, status):
            x = np.frombuffer(data, np.float32)
            if ch > 1:
                x = x.reshape(-1, ch).mean(axis=1)
            self._push(x)
            return (None, pyaudio.paContinue)

        try:
            self.stream = self.pa.open(
                format=pyaudio.paFloat32, channels=ch, rate=dev.rate, input=True,
                input_device_index=dev.index, frames_per_buffer=1024, stream_callback=callback,
            )
            self.rate = dev.rate
            self.current = dev
            self.error = ""
        except Exception as e:  # noqa: BLE001 - the error is shown in the UI
            self.stream = None
            self.current = None
            self.error = f"{dev.label}: {e}"

    def close(self) -> None:
        if self.stream is not None:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except Exception:  # noqa: BLE001
                pass
            self.stream = None

    def shutdown(self) -> None:
        self.close()
        self.pa.terminate()


# --------------------------------------------------------------------------- #
# Terminal window shake
# --------------------------------------------------------------------------- #


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class WindowShaker:
    SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x4, 0x10

    def __init__(self) -> None:
        self.u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        try:
            self.u.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass
        H = wintypes.HWND
        k.GetConsoleWindow.restype = H
        self.u.GetWindow.restype = H
        self.u.GetWindow.argtypes = [H, wintypes.UINT]
        self.u.GetWindowRect.argtypes = [H, ctypes.POINTER(RECT)]
        self.u.SetWindowPos.argtypes = [H, H, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, wintypes.UINT]
        for fn in (self.u.IsWindowVisible, self.u.IsZoomed, self.u.IsIconic):
            fn.argtypes = [H]
        hwnd = k.GetConsoleWindow()
        if hwnd:
            owner = self.u.GetWindow(hwnd, 4)  # GW_OWNER: in Windows Terminal this is the real window
            hwnd = owner or hwnd
        self.hwnd = hwnd
        self.base: tuple[int, int] | None = None
        self.last: tuple[int, int] | None = None
        self._checked = 0.0
        self._ok = False

    def usable(self) -> bool:
        now = time.perf_counter()
        if now - self._checked > 0.5:
            self._checked = now
            h = self.hwnd
            self._ok = bool(h and self.u.IsWindowVisible(h) and not self.u.IsZoomed(h)
                            and not self.u.IsIconic(h))
        return self._ok

    def _pos(self) -> tuple[int, int]:
        r = RECT()
        self.u.GetWindowRect(self.hwnd, ctypes.byref(r))
        return r.left, r.top

    def move(self, dx: int, dy: int) -> None:
        cur = self._pos()
        if self.last is None or cur != self.last:
            self.base = cur  # first time, or the user dragged the window
        bx, by = self.base
        self.u.SetWindowPos(self.hwnd, None, bx + dx, by + dy, 0, 0,
                            self.SWP_NOSIZE | self.SWP_NOZORDER | self.SWP_NOACTIVATE)
        self.last = self._pos()

    def reset(self) -> None:
        if self.base is not None and self.last != self.base:
            self.move(0, 0)


# --------------------------------------------------------------------------- #
# Terminal and keyboard
# --------------------------------------------------------------------------- #


def setup_terminal() -> Callable[[], None] | None:
    """Turns on VT sequence processing. Returns the restore function, or None if it fails."""
    k = ctypes.windll.kernel32
    h = k.GetStdHandle(-11)
    mode = wintypes.DWORD()
    if not k.GetConsoleMode(h, ctypes.byref(mode)):
        return None
    if not k.SetConsoleMode(h, mode.value | 0x0004):  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return None
    return lambda: None


def read_keys() -> list[str]:
    keys = []
    while msvcrt.kbhit():
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            keys.append({"H": "up", "P": "down", "K": "left", "M": "right"}.get(msvcrt.getwch(), ""))
        elif ch == "\r":
            keys.append("enter")
        elif ch == "\t":
            keys.append("tab")
        elif ch == "\x1b":
            keys.append("esc")
        elif ch == "\x03":
            keys.append("q")
        else:
            keys.append(ch.lower())
    return keys
