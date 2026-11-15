"""Generate synthetic test signals for rendering and profiling.

These stand in for real recordings until the Pisound arrives. Plucked
strings use Karplus-Strong synthesis, which is close enough to a guitar to
exercise envelopes, delay tails and reverbs. Drop real recordings (48 kHz WAV)
into test_audio/recordings/ and the profiler picks them up too.

    uv run fx-signals            # writes test_audio/*.wav
"""

import argparse
from pathlib import Path

import numpy as np

from .wavio import write_wav_float

SR = 48000

# Open-position E major, low to high (Hz).
E_MAJOR = [82.41, 123.47, 164.81, 207.65, 246.94, 329.63]


def pluck(freq: float, dur: float, rng: np.random.Generator, *, decay: float = 0.999,
          brightness: float = 0.6) -> np.ndarray:
    """One Karplus-Strong string. brightness in (0, 1]: lower = darker attack."""
    n = max(2, int(round(SR / freq)))
    total = int(dur * SR)
    exc = rng.uniform(-1.0, 1.0, n)
    # Darken the excitation with a one-pole lowpass (a softer pick).
    for i in range(1, n):
        exc[i] = brightness * exc[i] + (1.0 - brightness) * exc[i - 1]
    exc -= exc.mean()
    # y[k] = decay * (y[k-n] + y[k-n-1]) / 2, computed one period at a time;
    # every sample a period needs comes from earlier periods. y[0] is y[-1].
    y = np.zeros(total + n + 1)
    y[1:n + 1] = exc
    for start in range(n + 1, len(y), n):
        end = min(start + n, len(y))
        y[start:end] = decay * 0.5 * (y[start - n:end - n] + y[start - n - 1:end - n - 1])
    return y[1:total + 1]


def place(out: np.ndarray, sound: np.ndarray, t: float, gain: float = 1.0) -> None:
    start = int(t * SR)
    end = min(len(out), start + len(sound))
    if start < end:
        out[start:end] += gain * sound[:end - start]


def strum(out: np.ndarray, t: float, rng: np.random.Generator, *, down: bool = True,
          spread: float = 0.012, dur: float = 3.0, **kw) -> None:
    strings = E_MAJOR if down else E_MAJOR[::-1]
    for i, f in enumerate(strings):
        place(out, pluck(f, dur, rng, **kw), t + i * spread, gain=rng.uniform(0.7, 1.0))


def normalize(x: np.ndarray, peak_dbfs: float) -> np.ndarray:
    peak = np.max(np.abs(x))
    return x if peak == 0 else x * (10 ** (peak_dbfs / 20) / peak)


def fade(x: np.ndarray, ms: float = 10.0) -> np.ndarray:
    n = int(ms * 1e-3 * SR)
    ramp = np.linspace(0.0, 1.0, n)
    x = x.copy()
    x[:n] *= ramp
    x[-n:] *= ramp[::-1]
    return x


def make_signals(seed: int = 0) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    sig: dict[str, np.ndarray] = {}

    sig["silence"] = np.zeros(5 * SR)

    imp = np.zeros(3 * SR)
    imp[int(0.1 * SR)] = 1.0
    sig["impulse"] = imp

    # Exponential sine sweep 20 Hz to 20 kHz.
    dur, f0, f1 = 5.0, 20.0, 20000.0
    t = np.arange(int(dur * SR)) / SR
    k = np.log(f1 / f0)
    sig["sweep"] = fade(0.5 * np.sin(2 * np.pi * f0 * dur / k * (np.exp(t * k / dur) - 1)))

    sig["noise"] = fade(normalize(rng.standard_normal(5 * SR), -12.0))

    # Alternating down/up strums, two per second.
    x = np.zeros(6 * SR)
    for i in range(10):
        strum(x, 0.1 + 0.5 * i, rng, down=(i % 2 == 0), dur=2.0)
    sig["strum"] = normalize(x, -6.0)

    # Hard, bright sixteenth notes at 120 bpm on the top strings, near full scale.
    x = np.zeros(5 * SR)
    notes = [329.63, 392.00, 440.00, 493.88, 440.00, 392.00]
    for i in range(32):
        place(x, pluck(notes[i % len(notes)], 0.6, rng, brightness=1.0, decay=0.998), 0.1 + i * 0.125)
    sig["hard_pick"] = normalize(x, -1.0)

    # One chord left ringing.
    x = np.zeros(10 * SR)
    strum(x, 0.1, rng, dur=9.9, decay=0.9995)
    sig["sustain"] = normalize(x, -6.0)

    # A hard strum, then exact digital silence: delay/reverb tails decay toward
    # zero here, which is where denormals cause CPU spikes.
    x = np.zeros(2 * SR)
    strum(x, 0.1, rng, dur=1.9, brightness=1.0)
    x = normalize(x, -1.0)
    x[-int(0.01 * SR):] *= np.linspace(1.0, 0.0, int(0.01 * SR))
    sig["tail_to_silence"] = np.concatenate([x, np.zeros(8 * SR)])

    return sig


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", type=Path, default=Path("test_audio"))
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "recordings").mkdir(exist_ok=True)
    for name, x in make_signals(args.seed).items():
        path = args.out_dir / f"{name}.wav"
        write_wav_float(path, x, SR)
        print(f"wrote {path}  ({len(x) / SR:.1f} s)")


if __name__ == "__main__":
    main()
