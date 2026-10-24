"""Parte da entrada de áudio que independe do sistema operacional."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import numpy as np

RING = 1 << 15


@dataclass
class Device:
    index: int | str  # índice (Windows) ou nome da fonte (Linux/PulseAudio)
    name: str
    loopback: bool
    rate: int
    channels: int

    @property
    def label(self) -> str:
        return ("Saída: " if self.loopback else "Mic: ") + self.name


class AudioBase:
    """Guarda os últimos samples (mono) num ring buffer preenchido por uma thread de captura.

    As subclasses preenchem `devices` e implementam `open`/`close`; a captura chama `_push`.
    """

    def __init__(self) -> None:
        self.ring = np.zeros(RING, np.float32)
        self.widx = 0
        self.lock = threading.Lock()
        self.last_data = 0.0
        self.rate = 48000
        self.error = ""
        self.current: Device | None = None
        self.devices: list[Device] = []

    def find(self, label: str) -> Device | None:
        for dv in self.devices:
            if dv.label == label:
                return dv
        return self.devices[0] if self.devices else None

    def open(self, dev: Device | None) -> None:  # pragma: no cover - implementado nas subclasses
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def shutdown(self) -> None:
        self.close()

    def _push(self, x: np.ndarray) -> None:
        n = len(x)
        with self.lock:
            if n >= RING:
                self.ring[:] = x[-RING:]
                self.widx = 0
            else:
                end = self.widx + n
                if end <= RING:
                    self.ring[self.widx:end] = x
                else:
                    k = RING - self.widx
                    self.ring[self.widx:] = x[:k]
                    self.ring[:n - k] = x[k:]
                self.widx = end % RING
            self.last_data = time.perf_counter()

    def latest(self, n: int) -> np.ndarray:
        with self.lock:
            # sem callbacks (ex.: loopback do Windows em silêncio) = sem sinal
            if time.perf_counter() - self.last_data > 0.12:
                return np.zeros(n, np.float32)
            return self.ring.take(np.arange(self.widx - n, self.widx), mode="wrap")

    @property
    def label(self) -> str:
        return self.current.label if self.current else (self.error or "sem entrada")
