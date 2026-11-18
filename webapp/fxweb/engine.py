"""The web app's view of the audio engine.

The UI and HTTP API only talk to `Engine`. Today that is `FakeEngine`, which
remembers the loaded preset and makes up plausible health numbers. On the Pi
it will be replaced by a backend that talks to the real JACK client (a
separate real-time process; the web server never runs audio itself). Such a
backend implements the same three methods and registers in `make_engine`.

Control latency doesn't matter (presets change between songs), so `load` may
block for a second while the engine swaps chains or restarts at a new buffer
size.
"""

from __future__ import annotations

import abc
import math
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .system import cpu_temp_c

SAMPLE_RATE = 48000
CONVERTER_MS = 1.0  # rough ADC + DAC delay, see CLAUDE.md latency model


def estimated_latency_ms(buffer_size: int, sample_rate: int = SAMPLE_RATE) -> float:
    """Round trip ≈ 2 × buffer ÷ rate + converters. M2 replaces this with a measurement."""
    return 2 * buffer_size / sample_rate * 1000 + CONVERTER_MS


@dataclass
class LoadedPreset:
    id: str | None  # None for an unsaved preset being auditioned
    name: str
    buffer_size: int
    modified: bool  # True when the engine runs edits that aren't saved to the file
    preset: dict = field(repr=False)


@dataclass
class Health:
    engine: str  # backend name, e.g. "fake" or "jack"
    running: bool
    sample_rate: int
    buffer_size: int | None
    latency_ms: float | None  # round trip, guitar in -> amp out
    latency_measured: bool  # False = estimated from the buffer size
    dsp_load_pct: float | None
    xruns: int
    cpu_temp_c: float | None
    cpu_temp_simulated: bool
    uptime_s: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Engine(abc.ABC):
    name = "abstract"

    @abc.abstractmethod
    def load(self, preset: dict, preset_id: str | None, modified: bool = False) -> LoadedPreset:
        """Switch the engine to a validated preset. May glitch once if the buffer size changes."""

    @abc.abstractmethod
    def loaded(self) -> LoadedPreset | None:
        """The preset currently running, if any."""

    @abc.abstractmethod
    def health(self) -> Health:
        """Cheap snapshot for the health panel; polled every couple of seconds."""


class FakeEngine(Engine):
    """Stands in for the audio engine until the Pisound is installed."""

    name = "fake"

    def __init__(self, seed: int | None = None, xrun_chance: float = 0.003):
        self._lock = threading.Lock()
        self._loaded: LoadedPreset | None = None
        self._started = time.monotonic()
        self._rng = random.Random(seed)
        self._xruns = 0
        self._xrun_chance = xrun_chance
        self._load_jitter = 0.0

    def load(self, preset: dict, preset_id: str | None, modified: bool = False) -> LoadedPreset:
        with self._lock:
            old = self._loaded
            buf = preset.get("buffer_size", 128)
            self._loaded = LoadedPreset(
                id=preset_id, name=preset["name"], buffer_size=buf, modified=modified, preset=preset
            )
            if old is not None and old.buffer_size != buf:
                self._xruns += 1  # a buffer-size switch restarts the server: one glitch
            return self._loaded

    def loaded(self) -> LoadedPreset | None:
        with self._lock:
            return self._loaded

    def _dsp_load(self) -> float | None:
        if self._loaded is None:
            return None
        # Cost grows with chain length; smaller buffers have more per-block overhead.
        base = 1.5 + 2.5 * len(self._loaded.preset["chain"])
        base *= 1 + 32 / self._loaded.buffer_size
        self._load_jitter = 0.8 * self._load_jitter + self._rng.gauss(0, 0.4)
        return round(max(0.3, base + self._load_jitter), 1)

    def health(self) -> Health:
        with self._lock:
            loaded = self._loaded
            if loaded is not None and self._rng.random() < self._xrun_chance:
                self._xruns += 1
            temp = cpu_temp_c()
            simulated = temp is None
            if simulated:
                t = time.monotonic() - self._started
                temp = 52 + 3 * math.sin(t / 60) + self._rng.uniform(-0.5, 0.5)
            buf = loaded.buffer_size if loaded else None
            return Health(
                engine=self.name,
                running=loaded is not None,
                sample_rate=SAMPLE_RATE,
                buffer_size=buf,
                latency_ms=round(estimated_latency_ms(buf), 2) if buf else None,
                latency_measured=False,
                dsp_load_pct=self._dsp_load(),
                xruns=self._xruns,
                cpu_temp_c=round(temp, 1),
                cpu_temp_simulated=simulated,
                uptime_s=round(time.monotonic() - self._started, 1),
            )


ENGINES: dict[str, type[Engine]] = {"fake": FakeEngine}


def make_engine(name: str) -> Engine:
    try:
        return ENGINES[name]()
    except KeyError:
        raise ValueError(f"unknown engine '{name}'; choose from {sorted(ENGINES)}") from None
