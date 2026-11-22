"""Where the live signal comes from.

    sim   Synthetic guitar on a loop (known notes, chords, a clipped strum, hum
          and silence), with "out" rendered through the loaded preset by
          fxrender. Runs anywhere, so the Live page can be developed now.
    tap   The engine's shared-memory tap (engine/include/fx/tap.h): what the
          Pisound hears ("in") and plays ("out"). `fxtapfeed` stands in for the
          engine until the JACK client exists.
    alsa  A capture device via `arecord`, e.g. the USB interface (pickup + mic)
          for the body-IR recordings. arecord does the device I/O in its own
          process; we read its stdout pipe, so a slow Python moment can't
          cause a device overrun. Not for the Pisound once JACK owns it.

A source's read() blocks for at most ~`timeout` and returns planar float32
(channels, n), or None when there's nothing new (e.g. the engine stopped).
"""

from __future__ import annotations

import abc
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from .tap import TapError, TapReader, default_tap_path
from .wavfile import read_wav, write_wav_pcm24

SAMPLE_RATE = 48000


class SourceError(RuntimeError):
    """The source can't deliver right now (shown in the UI; the hub retries)."""


class Source(abc.ABC):
    kind = "abstract"

    def __init__(self) -> None:
        self.sample_rate = SAMPLE_RATE
        self.channel_names: list[str] = []
        self.label = self.kind

    @abc.abstractmethod
    def open(self) -> None: ...

    @abc.abstractmethod
    def read(self, timeout: float = 0.05) -> np.ndarray | None: ...

    def close(self) -> None:
        pass

    @property
    def dropped(self) -> int:
        """Frames lost before they reached us (the reader fell behind)."""
        return 0

    def set_preset(self, preset: dict) -> None:
        """The loaded preset changed. Only the simulation cares."""

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "label": self.label, "channels": self.channel_names,
                "sample_rate": self.sample_rate}


# ---------------------------------------------------------------- sim

def _midi_hz(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def _pluck(freq: float, dur: float, sr: int, rng: np.random.Generator, *, bright: float = 1.0) -> np.ndarray:
    """Additive plucked string: exact pitch (unlike Karplus-Strong's integer delay),
    slightly stretched partials, highs dying first, and a short pick-noise attack."""
    n = int(dur * sr)
    t = np.arange(n) / sr
    out = np.zeros(n)
    pluck_pos = 0.17
    inharm = 1.2e-4
    for k in range(1, 40):
        fk = k * freq * np.sqrt(1 + inharm * k * k)
        if fk > 0.45 * sr:
            break
        amp = abs(np.sin(np.pi * k * pluck_pos)) / k ** (1.25 - 0.25 * bright)
        decay = 0.9 + 0.55 * k ** 1.1 * (freq / 200) ** 0.35
        m = min(n, int(9.2 / decay * sr))  # stop once this partial is 80 dB down
        tk = t[:m]
        out[:m] += amp * np.exp(-decay * tk) * np.sin(2 * np.pi * fk * tk + rng.uniform(0, 2 * np.pi))
    att = int(0.012 * sr)
    noise = rng.normal(0, 1, att) * np.linspace(1, 0, att) ** 2
    out[:att] += 0.25 * bright * np.convolve(noise, np.ones(4) / 4, "same")
    fade = min(n, int(0.02 * sr))
    out[-fade:] *= np.linspace(1, 0, fade)
    return out


def synth_performance(sr: int = SAMPLE_RATE, seed: int = 7) -> np.ndarray:
    """~32 s loop exercising every panel. Mono float32."""
    rng = np.random.default_rng(seed)
    total = int(32 * sr)
    x = np.zeros(total)

    def place(sound: np.ndarray, t: float, gain: float) -> None:
        i = int(t * sr)
        j = min(total, i + len(sound))
        x[i:j] += gain * sound[: j - i]

    # 0-8 s: open strings one by one, the G a little flat and the B a little sharp (tuner).
    t = 0.3
    for m, cents in [(40, 0), (45, 0), (50, 0), (55, -12), (59, 9), (64, 0)]:
        place(_pluck(_midi_hz(m + cents / 100), 1.6, sr, rng), t, 0.32)
        t += 1.25
    # 8.5-18.5 s: G C D Em, four strums each at 96 BPM (tempo, chords).
    beat = 60 / 96
    t = 8.5
    voicings = {"G": [43, 47, 50, 55, 59, 67], "C": [48, 52, 55, 60, 64], "D": [50, 57, 62, 66],
                "Em": [40, 47, 52, 55, 59, 64]}
    for name in ["G", "C", "D", "Em"]:
        for s in range(4):
            down = s % 2 == 0
            notes = voicings[name] if down else voicings[name][::-1][:4]
            g = (0.16 if down else 0.1) * rng.uniform(0.85, 1.1)
            for i, m in enumerate(notes):
                place(_pluck(_midi_hz(m), 2.2, sr, rng, bright=0.9), t + i * 0.011, g)
            t += beat
    # 19-23.5 s: Am arpeggio in eighths, quieter (dynamics, onsets).
    t = 19.0
    for m in [45, 52, 57, 60, 64, 60, 57, 52] * 2:
        place(_pluck(_midi_hz(m), 1.4, sr, rng, bright=0.7), t, 0.22)
        t += beat / 2
    # 24.5 s: one hard strum that clips (clip indicator, headroom).
    for i, m in enumerate(voicings["Em"]):
        place(_pluck(_midi_hz(m), 3.0, sr, rng, bright=1.0), 24.5 + i * 0.008, 0.55)
    # 28-32 s: silence. Throughout: preamp hiss and 60 Hz mains hum (noise floor, hum).
    tt = np.arange(total) / sr
    x += 10 ** (-66 / 20) * np.sin(2 * np.pi * 60 * tt) + 10 ** (-76 / 20) * np.sin(2 * np.pi * 180 * tt)
    x += rng.normal(0, 10 ** (-74 / 20), total)
    fade = int(0.05 * sr)
    x[:fade] *= np.linspace(0, 1, fade)
    x[-fade:] *= np.linspace(1, 0, fade)
    return np.clip(x, -1.0, 1.0).astype(np.float32)


class SimSource(Source):
    """Plays the synthetic loop in real time, paced by the wall clock."""

    kind = "sim"
    BLOCK = 480  # 10 ms

    def __init__(self, fxrender: str | Path | None = None, seed: int = 7):
        super().__init__()
        self.label = "Simulated guitar (test loop)"
        self.channel_names = ["in", "out"]
        self._fxrender = str(fxrender) if fxrender else None
        self._seed = seed
        self._dry: np.ndarray | None = None
        self._wet: np.ndarray | None = None
        self._pending_preset: dict | None = None
        self._render_lock = threading.Lock()
        self._t0 = 0.0
        self._sent = 0

    def open(self) -> None:
        if self._dry is None:
            self._dry = synth_performance(self.sample_rate, self._seed)
            self._wet = self._dry.copy()
            if self._pending_preset is not None:
                self._render(self._pending_preset)
        self._t0 = time.monotonic()
        self._sent = 0

    def set_preset(self, preset: dict) -> None:
        self._pending_preset = preset
        if self._dry is not None:
            threading.Thread(target=self._render, args=(preset,), daemon=True, name="sim-render").start()

    def _render(self, preset: dict) -> None:
        """Run the dry loop through the real effect chain, offline, with fxrender."""
        if not self._fxrender or self._dry is None:
            return
        with self._render_lock, tempfile.TemporaryDirectory(prefix="fxsim-") as td:
            d = Path(td)
            write_wav_pcm24(d / "dry.wav", self._dry, self.sample_rate)
            (d / "preset.json").write_text(json.dumps(preset))
            try:
                subprocess.run(
                    [self._fxrender, "--preset", str(d / "preset.json"), "--in", str(d / "dry.wav"),
                     "--out", str(d / "wet.wav"), "--warmup", "0", "--repeat", "1", "--quiet"],
                    check=True, capture_output=True, timeout=30)
                _, wet = read_wav(d / "wet.wav")
            except (subprocess.SubprocessError, OSError, ValueError):
                return  # keep the previous wet signal
            if preset is self._pending_preset:
                self._wet = np.clip(wet[0], -1, 1).astype(np.float32)

    def read(self, timeout: float = 0.05) -> np.ndarray | None:
        assert self._dry is not None and self._wet is not None
        due = int((time.monotonic() - self._t0) * self.sample_rate)
        if due - self._sent > self.sample_rate:  # we were paused for a second+: skip, don't burst
            self._sent = due - self.BLOCK
        wait = (self._sent + self.BLOCK - due) / self.sample_rate
        if wait > 0:
            time.sleep(min(wait, timeout))
            due = int((time.monotonic() - self._t0) * self.sample_rate)
        n = due - self._sent
        if n < self.BLOCK:
            return None
        n = (n // self.BLOCK) * self.BLOCK
        L = len(self._dry)
        idx = (self._sent + np.arange(n)) % L
        self._sent += n
        return np.stack([self._dry[idx], self._wet[idx % len(self._wet)]])


# ---------------------------------------------------------------- tap

class TapSource(Source):
    kind = "tap"

    def __init__(self, path: str | Path | None = None):
        super().__init__()
        self.path = Path(path) if path else default_tap_path()
        self.label = "Engine tap (Pisound in/out)"
        self._r = TapReader(self.path)
        self._last_advance = 0.0
        self._last_check = 0.0
        self._dropped_before = 0

    def open(self) -> None:
        try:
            self._r.open()
        except TapError as e:
            raise SourceError(str(e)) from None
        self.sample_rate = self._r.sample_rate
        self.channel_names = self._r.names
        self._last_advance = time.monotonic()

    def read(self, timeout: float = 0.05) -> np.ndarray | None:
        deadline = time.monotonic() + timeout
        while True:
            now = time.monotonic()
            if now - self._last_check > 1.0:
                self._last_check = now
                if self._r.replaced():  # the engine restarted: new file, maybe a new format
                    self._dropped_before += self._r.dropped
                    raise SourceError("engine restarted; reopening the tap")
            block = self._r.read(max_frames=self.sample_rate)
            if block.shape[1]:
                self._last_advance = now
                return block
            if now - self._last_advance > 1.0:
                raise SourceError("the engine isn't writing to the tap (stopped?)")
            if now >= deadline:
                return None
            time.sleep(0.004)

    def close(self) -> None:
        self._r.close()

    @property
    def dropped(self) -> int:
        return self._dropped_before + self._r.dropped


# ---------------------------------------------------------------- alsa

class AlsaSource(Source):
    kind = "alsa"
    BLOCK = 480

    def __init__(self, device: str = "default", channels: int = 2, names: list[str] | None = None,
                 label: str | None = None):
        super().__init__()
        self.device = device
        self.nch = channels
        self.channel_names = names or [f"ch{i + 1}" for i in range(channels)]
        self.label = label or f"ALSA {device}"
        self._p: subprocess.Popen | None = None

    def open(self) -> None:
        exe = shutil.which("arecord")
        if not exe:
            raise SourceError("arecord not found (Linux: sudo apt install alsa-utils)")
        self._p = subprocess.Popen(
            [exe, "-q", "-D", self.device, "-f", "S32_LE", "-c", str(self.nch), "-r", str(self.sample_rate),
             "-t", "raw", "--buffer-time=200000"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

    def read(self, timeout: float = 0.05) -> np.ndarray | None:
        assert self._p is not None and self._p.stdout is not None
        want = self.BLOCK * self.nch * 4
        buf = bytearray()
        while len(buf) < want:
            chunk = self._p.stdout.read(want - len(buf))
            if not chunk:
                err = (self._p.stderr.read() if self._p.stderr else b"").decode(errors="replace").strip()
                self.close()
                raise SourceError(f"arecord stopped: {err or 'device closed'}")
            buf += chunk
        x = np.frombuffer(bytes(buf), "<i4").astype(np.float32) / 2147483648.0
        return np.ascontiguousarray(x.reshape(-1, self.nch).T)

    def close(self) -> None:
        if self._p is not None:
            self._p.terminate()
            try:
                self._p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._p.kill()
        self._p = None

    def describe(self) -> dict[str, Any]:
        return super().describe() | {"device": self.device}


def alsa_devices() -> list[dict[str, Any]]:
    """Capture devices from `arecord -l`, as source specs."""
    exe = shutil.which("arecord")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "-l"], capture_output=True, text=True, timeout=3).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    devs = []
    for m in re.finditer(r"^card (\d+): (\S+) \[(.*?)\], device (\d+): .*?\[(.*?)\]", out, re.M):
        card, cid, cname, dev, _ = m.groups()
        devs.append({"kind": "alsa", "device": f"plughw:CARD={cid},DEV={dev}", "channels": 2,
                     "label": f"{cname} (card {card})"})
    return devs


# ---------------------------------------------------------------- factory

def available_sources(tap_path: Path | None = None) -> list[dict[str, Any]]:
    tp = Path(tap_path) if tap_path else default_tap_path()
    return [
        {"kind": "tap", "label": "Engine tap (Pisound in/out)", "path": str(tp), "present": tp.exists()},
        *alsa_devices(),
        {"kind": "sim", "label": "Simulated guitar (test loop)"},
    ]


def make_source(spec: dict[str, Any] | str, *, fxrender: str | Path | None = None,
                tap_path: Path | None = None) -> Source:
    """spec: {"kind": "sim"|"tap"|"alsa", ...} or a CLI string: sim, tap[:PATH], alsa:DEVICE[:CHANNELS]."""
    if isinstance(spec, str):
        kind, _, rest = spec.partition(":")
        if kind == "tap":
            spec = {"kind": "tap", **({"path": rest} if rest else {})}
        elif kind == "alsa":
            dev, _, ch = rest.rpartition(":") if re.search(r":\d+$", rest) else (rest, "", "")
            spec = {"kind": "alsa", "device": dev or "default", **({"channels": int(ch)} if ch else {})}
        else:
            spec = {"kind": kind}
    kind = spec.get("kind")
    if kind == "sim":
        return SimSource(fxrender=fxrender)
    if kind == "tap":
        return TapSource(spec.get("path") or tap_path)
    if kind == "alsa":
        ch = spec.get("channels", 2)
        if not isinstance(ch, int) or not 1 <= ch <= 8:
            raise ValueError("alsa channels must be 1-8")
        dev = spec.get("device", "default")
        if not isinstance(dev, str) or not re.fullmatch(r"[\w:=,.\-]+", dev):
            raise ValueError("bad ALSA device name")
        names = spec.get("names")
        if names is not None and not (isinstance(names, list) and len(names) == ch
                                      and all(isinstance(n, str) and 0 < len(n) <= 15 for n in names)):
            raise ValueError("names must be one short string per channel")
        return AlsaSource(dev, ch, names, spec.get("label") if isinstance(spec.get("label"), str) else None)
    raise ValueError(f"unknown source kind {kind!r}; use sim, tap or alsa")
