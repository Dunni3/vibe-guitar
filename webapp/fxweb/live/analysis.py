"""Live analysis: one JSON-ready frame ~30 times a second from the History ring.

What's computed, on which signal, and why it's cheap enough (numbers for one
Pi 5 core, numpy, 48 kHz):

    levels      every channel, the samples since the last frame: peak, RMS, clips
    spectrum    every channel, 8192-point FFT (5.9 Hz resolution), Hann window,
                shown on 240 log-spaced bins 30 Hz-16 kHz
    scope       2048 samples from a rising zero crossing, so a held note stands still
    pitch       YIN on the "in" channel (decimated to 24 kHz): note, cents, Hz
    harmonics   levels of partials 1-10 relative to the fundamental, when pitched
    chroma      energy per pitch class (C..B) from spectral peaks 60 Hz-2 kHz,
                matched against chord templates for a chord guess
    onsets      spectral flux at a 10.7 ms hop; tempo from its autocorrelation
    noise/hum   noise floor (5th percentile of frame RMS over 60 s), mains hum
                at 60/120/180 Hz measured while the guitar is quiet
    response    with 2+ channels: H1 transfer function out/in, coherence, and the
                delay between them (GCC-PHAT), averaged while there's signal

All of it is a few FFTs per frame: about 1-2 ms of one core at 30 frames/s.
"""

from __future__ import annotations

import base64
import math
from collections import deque
from typing import Any

import numpy as np

from .history import History

NOTE_NAMES = ["C", "C♯", "D", "D♯", "E", "F", "F♯", "G", "G♯", "A", "A♯", "B"]
EPS = 1e-12

SPEC_N = 8192
SPEC_BINS = 240
SPEC_FMIN, SPEC_FMAX = 30.0, 16000.0
SPEC_DB_FLOOR = -120.0  # byte = (dB - floor) * 2, so 0..255 covers -120..+7.5 dBFS

SCOPE_N = 2048
SCOPE_POINTS = 1024

YIN_DECIM = 2
YIN_W = 1024  # at 24 kHz: 43 ms
YIN_FMIN, YIN_FMAX = 60.0, 1400.0
YIN_THRESH = 0.15
GATE_DB = -52.0  # below this the guitar is "quiet": no pitch, chords or onsets

ONSET_HOP = 512
ONSET_N = 1024

XFER_N = 4096
XFER_BINS = 96
XFER_EVERY = 4  # frames
XFER_MAX_DELAY_MS = 20.0

HARMONIC_PROFILE = [(round(12 * math.log2(k)) % 12, 0.6 ** (k - 1)) for k in range(1, 7)]
CHORD_PENALTY = {"": 0.0, "m": 0.0, "5": 0.04, "7": 0.035, "maj7": 0.035, "m7": 0.035,
                 "sus2": 0.03, "sus4": 0.03}  # a 7th must clearly beat the plain triad
CHORD_TYPES = {  # intervals with weights: root and fifth carry it, the third decides major/minor
    "": {0: 1.0, 4: 1.0, 7: 1.0},
    "m": {0: 1.0, 3: 1.0, 7: 1.0},
    "7": {0: 1.0, 4: 1.0, 7: 1.0, 10: 0.8},
    "maj7": {0: 1.0, 4: 1.0, 7: 1.0, 11: 0.8},
    "m7": {0: 1.0, 3: 1.0, 7: 1.0, 10: 0.8},
    "sus2": {0: 1.0, 2: 1.0, 7: 1.0},
    "sus4": {0: 1.0, 5: 1.0, 7: 1.0},
    "5": {0: 1.0, 7: 1.0},
}


def db(x: float | np.ndarray) -> Any:
    return 20.0 * np.log10(np.maximum(x, EPS))


def b64(a: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode("ascii")


def note_of(hz: float, a4: float = 440.0) -> dict[str, Any]:
    midi = 69 + 12 * math.log2(hz / a4)
    nearest = int(round(midi))
    return {"hz": round(hz, 2), "midi": nearest, "note": NOTE_NAMES[nearest % 12],
            "octave": nearest // 12 - 1, "cents": round(100 * (midi - nearest), 1)}


def _log_bin_map(n_fft: int, sr: int, n_bins: int, fmin: float, fmax: float):
    """Map rFFT bins to log-spaced display bins. Wide bins take the max of the FFT
    bins inside them; narrow ones (low end) interpolate between FFT bins."""
    fmax = min(fmax, sr / 2 * 0.98)
    edges = np.geomspace(fmin, fmax, n_bins + 1)
    centers = np.sqrt(edges[:-1] * edges[1:])
    df = sr / n_fft
    lo = np.ceil(edges[:-1] / df).astype(int)
    hi = np.floor(edges[1:] / df).astype(int) + 1  # exclusive
    wide = hi > lo
    return {"centers": centers, "edges": edges, "lo": lo, "hi": hi, "wide": wide,
            "interp_at": centers / df}


def _apply_bin_map(m: dict, spec_db: np.ndarray) -> np.ndarray:
    """spec_db: (..., n_fft/2+1) -> (..., n_bins)."""
    out = np.interp(m["interp_at"], np.arange(spec_db.shape[-1]), spec_db) if spec_db.ndim == 1 else \
        np.stack([np.interp(m["interp_at"], np.arange(s.shape[-1]), s) for s in spec_db])
    w = m["wide"]
    if w.any():
        idx = np.flatnonzero(w)
        starts = m["lo"][idx]
        # reduceat needs increasing starts; it reduces [start_i, start_{i+1}), so append ends.
        bounds = np.empty(2 * len(idx), int)
        bounds[0::2] = starts
        bounds[1::2] = m["hi"][idx]
        bounds = np.minimum(bounds, spec_db.shape[-1] - 1)
        red = np.maximum.reduceat(spec_db, bounds, axis=-1)[..., 0::2]
        out[..., idx] = red
    return out


def yin(x: np.ndarray, sr: float, fmin: float = YIN_FMIN, fmax: float = YIN_FMAX,
        w: int = YIN_W, thresh: float = YIN_THRESH) -> tuple[float, float] | None:
    """YIN pitch estimate on the last w + tau_max samples of x. Returns (Hz, confidence 0..1)."""
    tau_min = max(2, int(sr / fmax))
    tau_max = int(sr / fmin) + 1
    need = w + tau_max
    if len(x) < need:
        return None
    x = x[-need:].astype(np.float64)
    x = x - x.mean()
    nfft = 1 << int(np.ceil(np.log2(need + w)))
    fx = np.fft.rfft(x, nfft)
    fw = np.fft.rfft(x[:w], nfft)
    r = np.fft.irfft(np.conj(fw) * fx, nfft)[: tau_max + 1]  # r[tau] = sum_j x[j] x[j+tau], j < w
    cs = np.concatenate([[0.0], np.cumsum(x * x)])
    e0 = cs[w]
    taus = np.arange(tau_max + 1)
    etau = cs[taus + w] - cs[taus]
    d = np.maximum(e0 + etau - 2 * r, 0.0)
    cm = np.ones_like(d)
    running = np.cumsum(d[1:])
    cm[1:] = d[1:] * taus[1:] / np.maximum(running, EPS)
    seg = cm[tau_min: tau_max]
    below = np.flatnonzero(seg < thresh)
    if below.size:
        i = below[0]
        while i + 1 < len(seg) and seg[i + 1] < seg[i]:
            i += 1
    else:
        return None  # no clear period (a chord, noise): better no reading than a wrong one
    tau = i + tau_min
    # Parabolic interpolation on the raw difference function (as in the YIN paper):
    # the normalized one is skewed at short periods, i.e. high notes.
    if 1 <= tau < len(d) - 1:
        a, b, c = d[tau - 1], d[tau], d[tau + 1]
        den = a - 2 * b + c
        shift = 0.5 * (a - c) / den if abs(den) > EPS else 0.0
        shift = max(-1.0, min(1.0, shift))
    else:
        shift = 0.0
    return sr / (tau + shift), float(max(0.0, 1.0 - cm[tau]))


def chord_templates() -> tuple[list[str], np.ndarray, np.ndarray]:
    names, rows, pen = [], [], []
    for root in range(12):
        for suffix, iv in CHORD_TYPES.items():
            # Each chord tone brings its harmonics, as a real string does: the 3rd
            # harmonic of E is a B, so a plain C chord "contains" B and must not
            # read as Cmaj7 (Gomez 2006-style harmonic templates).
            v = np.zeros(12)
            for k, wgt in iv.items():
                for h, hw in HARMONIC_PROFILE:
                    v[(root + k + h) % 12] += wgt * hw
            names.append(NOTE_NAMES[root] + suffix)
            rows.append(v / np.linalg.norm(v))
            pen.append(CHORD_PENALTY[suffix])
    return names, np.array(rows), np.array(pen)


class Analyzer:
    def __init__(self, sample_rate: int, channel_names: list[str], fps: float = 30.0,
                 mains_hz: float = 60.0, a4: float = 440.0):
        self.sr = sample_rate
        self.names = list(channel_names)
        self.nch = len(channel_names)
        self.fps = fps
        self.mains = mains_hz
        self.a4 = a4
        self.seq = 0

        self._win = np.hanning(SPEC_N).astype(np.float32)
        self._win_norm = 2.0 / self._win.sum()  # full-scale sine -> 0 dBFS
        self._freqs = np.fft.rfftfreq(SPEC_N, 1 / self.sr)
        self._map = _log_bin_map(SPEC_N, self.sr, SPEC_BINS, SPEC_FMIN, SPEC_FMAX)

        # chroma: pitch class of every FFT bin in 60 Hz-2 kHz
        band = (self._freqs >= 60) & (self._freqs <= 2000)
        self._chroma_bins = np.flatnonzero(band)
        midi = 69 + 12 * np.log2(self._freqs[self._chroma_bins] / a4)
        self._chroma_pc = np.mod(np.rint(midi).astype(int), 12)
        self._chroma = np.zeros(12)
        self._raw_chroma = np.zeros(12)
        self._note_tpl = np.zeros((12, 12))  # one plucked note per pitch class, with its harmonics
        for pc in range(12):
            for h, hw in HARMONIC_PROFILE:
                self._note_tpl[pc, (pc + h) % 12] += hw
        self._note_tpl /= np.linalg.norm(self._note_tpl, axis=1, keepdims=True)
        self._chord_names, self._chord_tpl, self._chord_pen = chord_templates()
        self._chord: str | None = None

        # levels
        self._last_pos: int | None = None
        self._clips = [0] * self.nch
        self._clipping = [False] * self.nch
        self._peak_ring = np.zeros((int(10 * fps) + 1, self.nch))  # last 10 s of frame peaks
        self._peak_i = 0

        # pitch smoothing
        self._f0_hist: deque[float] = deque(maxlen=3)

        # onsets
        self._on_win = np.hanning(ONSET_N)
        self._on_pos: int | None = None
        self._on_prev: np.ndarray | None = None
        self._flux: deque[float] = deque(maxlen=int(8 * self.sr / ONSET_HOP))
        self._flux_recent: deque[float] = deque(maxlen=24)  # ~0.25 s, for the adaptive threshold
        self._flux_t: deque[float] = deque(maxlen=self._flux.maxlen)
        self._last_onset_t = -1.0
        self._onset_times: deque[float] = deque()
        self._tempo: float | None = None
        self._tempo_frame = 0

        # noise floor / hum
        self._rms_hist: deque[float] = deque(maxlen=int(60 * fps))  # quietest moments of the last minute
        self._hum_db: float | None = None
        self._hum_prominent = False

        # transfer function
        self._xwin = np.hanning(XFER_N)
        self._sxx = self._syy = None
        self._sxy = None
        self._xmap = _log_bin_map(XFER_N, self.sr, XFER_BINS, 40.0, 16000.0)
        self._xfer: dict | None = None

    def reset_position(self) -> None:
        """Call after a pause (nobody was watching) so we don't analyse a backlog."""
        self._last_pos = None
        self._on_pos = None
        self._on_prev = None

    # ------------------------------------------------------------ frame

    def step(self, hist: History, now_pos: int | None = None) -> dict[str, Any]:
        pos = hist.pos if now_pos is None else now_pos
        frame_len = int(self.sr / self.fps)
        if self._last_pos is None or pos - self._last_pos > self.sr // 2:
            self._last_pos = max(0, pos - frame_len)
        new = hist.read(self._last_pos, pos)
        self._last_pos = pos
        t = pos / self.sr
        self.seq += 1

        win = hist.latest(SPEC_N)  # (ch, SPEC_N)
        out: dict[str, Any] = {"seq": self.seq, "t": round(t, 3), "sr": self.sr, "channels": self.names}

        out["levels"] = self._levels(new, t)
        spec_mag = np.abs(np.fft.rfft(win * self._win, axis=-1)) * self._win_norm  # (ch, bins)
        spec_db = db(spec_mag)
        disp = _apply_bin_map(self._map, spec_db)
        out["spectrum"] = {
            "fmin": SPEC_FMIN, "fmax": float(self._map["edges"][-1]), "bins": SPEC_BINS, "floor": SPEC_DB_FLOOR,
            "data": [b64(np.clip(np.rint((d - SPEC_DB_FLOOR) * 2), 0, 255).astype(np.uint8)) for d in disp],
        }

        x0 = win[0]
        rms0 = float(np.sqrt(np.mean(x0[-frame_len:] ** 2)))
        rms0_db = float(db(rms0))
        active = rms0_db > GATE_DB
        self._rms_hist.append(rms0_db)

        out["chroma"], out["chord"] = self._chroma_chord(spec_mag[0], active)
        pitch = self._pitch(x0) if active else None
        if pitch is not None:
            pc = pitch["midi"] % 12
            if self._raw_chroma[pc] < 0.4:
                pitch = None  # a period the ear doesn't hear (e.g. the common period of a chord)
            elif out["chord"] is not None:
                # One string's overtones (octave, fifth, third) look like a chord. Whichever
                # explains the pitch classes better wins: one note, or the best chord.
                v = self._chroma / (np.linalg.norm(self._chroma) + EPS)
                if float(self._note_tpl[pc] @ v) >= out["chord"]["score"] - 0.02:
                    out["chord"] = None
                    self._chord = None
                else:
                    pitch = None
        if pitch is None:
            self._f0_hist.clear()
        out["pitch"] = pitch
        out["scope"] = self._scope(hist, pitch)
        out["harmonics"] = self._harmonics(spec_mag[0], pitch["hz"]) if pitch and pitch["conf"] > 0.8 else None
        onsets = self._onsets(hist, pos)
        out["onsets"] = onsets
        out["features"] = self._features(x0[-frame_len * 3:], spec_mag[0], spec_db[0], rms0_db, active, t)
        if self.nch >= 2:
            self._update_xfer(win, active)
            if self.seq % XFER_EVERY == 0 or self._xfer is None:
                self._xfer = self._xfer_frame()
            out["response"] = self._xfer
        return out

    # ------------------------------------------------------------ pieces

    def _levels(self, new: np.ndarray, t: float) -> list[dict[str, Any]]:
        res = []
        if new.shape[1] == 0:
            new = np.zeros((self.nch, 1), np.float32)
        a = np.abs(new)
        peaks = a.max(axis=1)
        rms = np.sqrt(np.mean(new.astype(np.float64) ** 2, axis=1))
        self._peak_ring[self._peak_i] = peaks
        self._peak_i = (self._peak_i + 1) % len(self._peak_ring)
        max10 = self._peak_ring.max(axis=0)
        for c in range(self.nch):
            clipping = bool(peaks[c] >= 0.999)
            if clipping and not self._clipping[c]:
                self._clips[c] += 1
            self._clipping[c] = clipping
            res.append({"peak_db": round(float(db(peaks[c])), 1), "rms_db": round(float(db(rms[c])), 1),
                        "max10_db": round(float(db(max10[c])), 1), "clip": clipping, "clips": self._clips[c]})
        return res

    def _pitch(self, x: np.ndarray) -> dict[str, Any] | None:
        d = YIN_DECIM
        n = (len(x) // d) * d
        xd = x[-n:].reshape(-1, d).mean(axis=1)
        est = yin(xd, self.sr / d)
        if est is None:
            return None
        hz, conf = est
        if self._f0_hist and abs(12 * math.log2(hz / np.median(self._f0_hist))) > 0.7:
            self._f0_hist.clear()  # a new note: respond at once instead of smoothing across it
        self._f0_hist.append(hz)
        p = note_of(float(np.median(self._f0_hist)), self.a4)
        p["conf"] = round(conf, 2)
        return p

    def _scope(self, hist: History, pitch: dict | None) -> dict[str, Any]:
        search = SCOPE_N // 2
        seg = hist.latest(SCOPE_N + search)
        # Trigger on the fundamental: lowpass the in channel, find the latest
        # rising zero crossing that still leaves a full window after it.
        x = seg[0].astype(np.float64)
        if pitch:
            k = max(1, int(self.sr / pitch["hz"] / 8))
            x = np.convolve(x, np.ones(k) / k, "same")
        head = x[: search + 1]
        rising = np.flatnonzero((head[:-1] < 0) & (head[1:] >= 0))
        start = int(rising[-1]) + 1 if rising.size else search
        w = seg[:, start: start + SCOPE_N]
        pts = w.reshape(self.nch, SCOPE_POINTS, -1).mean(axis=2)
        scale = float(np.max(np.abs(pts))) or 1.0
        return {"n": SCOPE_POINTS, "ms": round(SCOPE_N / self.sr * 1000, 2), "scale": round(scale, 5),
                "triggered": bool(rising.size),
                "data": [b64(np.clip(np.rint(p / scale * 127), -127, 127).astype(np.int8)) for p in pts]}

    def _harmonics(self, mag: np.ndarray, f0: float) -> list[float | None]:
        df = self.sr / SPEC_N
        levels = []
        for k in range(1, 11):
            f = k * f0
            if f > self.sr / 2 * 0.95:
                break
            c = f / df
            r = max(2.0, 0.03 * c)
            lo, hi = int(max(0, c - r)), int(min(len(mag) - 1, c + r)) + 1
            levels.append(float(np.max(mag[lo:hi])))
        if not levels:
            return []
        ref = max(levels[0], EPS)
        return [round(float(db(v / ref)), 1) for v in levels]

    def _chroma_chord(self, mag: np.ndarray, active: bool) -> tuple[list[float], dict | None]:
        if active:
            m = mag[self._chroma_bins]
            # Only spectral peaks within 40 dB of the loudest: keeps leakage and noise out.
            peak = np.zeros_like(m, dtype=bool)
            peak[1:-1] = (m[1:-1] > m[:-2]) & (m[1:-1] >= m[2:])
            peak &= m > m.max() * 0.01
            c = np.bincount(self._chroma_pc[peak], weights=np.sqrt(m[peak]), minlength=12)
            if c.max() > 0:
                c = c / c.max()
            self._raw_chroma = c
            self._chroma = 0.7 * self._chroma + 0.3 * c
        else:
            self._chroma *= 0.85
        ch = self._chroma
        chord = None
        if active and ch.max() > 0.2:
            v = ch / (np.linalg.norm(ch) + EPS)
            scores = self._chord_tpl @ v - self._chord_pen
            order = np.argsort(scores)[::-1]
            best = int(order[0])
            # Single notes (one pitch class far above the rest) aren't chords.
            second = np.sort(ch)[-2] / (ch.max() + EPS)
            if scores[best] > 0.8 and second > 0.3:
                name = self._chord_names[best]
                if self._chord in self._chord_names:
                    prev = self._chord_names.index(self._chord)
                    if scores[prev] > scores[best] - 0.03:  # hysteresis
                        name, best = self._chord, prev
                self._chord = name
                chord = {"name": name, "score": round(float(scores[best]), 2)}
            else:
                self._chord = None
        else:
            self._chord = None
        return [round(float(v), 2) for v in ch], chord

    def _onsets(self, hist: History, pos: int) -> list[dict[str, float]]:
        found = []
        if self._on_pos is None or pos - self._on_pos > self.sr:
            self._on_pos = pos
            self._on_prev = None
            return found
        while self._on_pos + ONSET_HOP <= pos:
            self._on_pos += ONSET_HOP
            x = hist.read(self._on_pos - ONSET_N, self._on_pos)[0]
            if len(x) < ONSET_N:
                continue
            spec = np.log1p(100 * np.abs(np.fft.rfft(x * self._on_win)))
            quiet = db(np.sqrt(np.mean(x ** 2))) < GATE_DB
            flux = 0.0 if self._on_prev is None or quiet else float(np.sum(np.maximum(spec - self._on_prev, 0)))
            self._on_prev = spec
            t = self._on_pos / self.sr
            self._flux.append(flux)
            self._flux_recent.append(flux)
            self._flux_t.append(t)
            # Peak-pick the previous hop: local max, above an adaptive threshold.
            if len(self._flux) >= 3:
                a, b, c = self._flux[-3], self._flux[-2], self._flux[-1]
                recent = np.fromiter(self._flux_recent, float, len(self._flux_recent))
                thresh = 1.5 * float(np.median(recent)) + 0.1 * float(recent.mean()) + 4.0
                tb = self._flux_t[-2]
                if b > a and b >= c and b > thresh and tb - self._last_onset_t > 0.06:
                    self._last_onset_t = tb
                    self._onset_times.append(tb)
                    found.append({"t": round(tb, 3), "strength": round(b, 1)})
        while self._onset_times and self._onset_times[0] < pos / self.sr - 8:
            self._onset_times.popleft()
        return found

    def _tempo_estimate(self) -> float | None:
        if len(self._onset_times) < 8 or len(self._flux) < self._flux.maxlen // 2:
            return None
        f = np.array(self._flux)
        f = f - f.mean()
        rate = self.sr / ONSET_HOP
        ac = np.correlate(f, f, "full")[len(f) - 1:]
        if ac[0] <= 0:
            return None
        lags = np.arange(len(ac))
        bpm = 60 * rate / np.maximum(lags, 1)
        ok = (bpm >= 50) & (bpm <= 220)
        # Prefer tempos near 110 BPM to avoid half/double-time picks.
        weight = np.exp(-0.5 * (np.log2(np.maximum(bpm, 1) / 110) / 0.9) ** 2)
        score = np.where(ok, ac / ac[0] * weight, -np.inf)
        i = int(np.argmax(score))
        if not np.isfinite(score[i]) or ac[i] / ac[0] < 0.2:
            return None
        if 1 <= i < len(ac) - 1:
            a, b, c = ac[i - 1], ac[i], ac[i + 1]
            den = a - 2 * b + c
            i = i + (0.5 * (a - c) / den if abs(den) > EPS else 0.0)
        return round(float(60 * rate / i), 1)

    def _features(self, x: np.ndarray, mag: np.ndarray, spec_db: np.ndarray, rms_db: float,
                  active: bool, t: float) -> dict[str, Any]:
        f: dict[str, Any] = {}
        band = self._freqs >= 50
        m = mag[band]  # magnitude-weighted centroid, the usual "brightness" measure
        f["centroid_hz"] = round(float(np.sum(self._freqs[band] * m) / (np.sum(m) + EPS)), 0) if active else None
        if active and len(x):
            pk = float(np.max(np.abs(x)))
            r = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
            f["crest_db"] = round(float(db(pk / max(r, EPS))), 1)
        else:
            f["crest_db"] = None
        f["noise_floor_db"] = round(float(np.percentile(self._rms_hist, 5)), 1) if len(self._rms_hist) > 10 else None
        # Hum: measure only while it's quiet, or the guitar's own partials near 60/120 Hz count as hum.
        floor = f["noise_floor_db"]
        if floor is not None and rms_db < floor + 6:
            df = self.sr / SPEC_N
            lv = []
            for h in (1, 2, 3):
                c = int(round(h * self.mains / df))
                lv.append(float(np.max(spec_db[c - 1: c + 2])))
            ref = float(np.median(spec_db[int(40 / df): int(400 / df)]))
            self._hum_db = round(max(lv), 1)
            self._hum_prominent = max(lv) - ref > 12
        f["hum_db"] = self._hum_db
        f["hum_prominent"] = self._hum_prominent
        f["mains_hz"] = self.mains
        self._tempo_frame += 1
        if self._tempo_frame % int(self.fps) == 0:
            self._tempo = self._tempo_estimate()
        f["tempo_bpm"] = self._tempo
        f["onsets_8s"] = len(self._onset_times)
        f["active"] = active
        return f

    def _update_xfer(self, win: np.ndarray, active: bool) -> None:
        if not active:
            return
        x = win[0, -XFER_N:] * self._xwin
        y = win[1, -XFER_N:] * self._xwin
        X, Y = np.fft.rfft(x), np.fft.rfft(y)
        a = 0.93
        if self._sxx is None:
            self._sxx, self._syy, self._sxy = np.abs(X) ** 2, np.abs(Y) ** 2, np.conj(X) * Y
        else:
            self._sxx = a * self._sxx + (1 - a) * np.abs(X) ** 2
            self._syy = a * self._syy + (1 - a) * np.abs(Y) ** 2
            self._sxy = a * self._sxy + (1 - a) * np.conj(X) * Y

    def _xfer_frame(self) -> dict | None:
        if self._sxx is None:
            return None
        h_db = db(np.abs(self._sxy) / np.maximum(self._sxx, EPS))
        coh = np.abs(self._sxy) ** 2 / np.maximum(self._sxx * self._syy, EPS)
        # Bins where the input has no energy say nothing; mark them so the UI greys them out.
        valid = self._sxx > self._sxx.max() * 1e-7
        h_db = np.where(valid, h_db, np.nan)
        coh = np.where(valid, coh, 0.0)
        m = self._xmap
        centers = m["interp_at"]
        hb = np.interp(centers, np.arange(len(h_db)), np.nan_to_num(h_db, nan=-200.0))
        cb = np.interp(centers, np.arange(len(coh)), coh)
        wide = np.flatnonzero(m["wide"])
        if wide.size:  # average power and coherence over the FFT bins inside each wide bin
            n = len(h_db)
            bounds = np.empty(2 * wide.size, int)
            bounds[0::2] = m["lo"][wide]
            bounds[1::2] = np.minimum(m["hi"][wide], n - 1)
            pw = np.where(valid, 10 ** (np.nan_to_num(h_db, nan=-200.0) / 10), 0.0)
            cnt = np.add.reduceat(valid.astype(float), bounds)[0::2]
            psum = np.add.reduceat(pw, bounds)[0::2]
            csum = np.add.reduceat(coh, bounds)[0::2]
            width = (bounds[1::2] - bounds[0::2]).clip(min=1)
            hb[wide] = np.where(cnt > 0, 10 * np.log10(np.maximum(psum / np.maximum(cnt, 1), EPS)), -200.0)
            cb[wide] = csum / width
        # Delay: generalized cross-correlation with phase transform.
        g = self._sxy / np.maximum(np.abs(self._sxy), EPS)
        cc = np.fft.irfft(g * valid, XFER_N)
        maxlag = int(XFER_MAX_DELAY_MS / 1000 * self.sr)
        lags = np.concatenate([np.arange(0, maxlag + 1), np.arange(-maxlag, 0)])
        vals = np.concatenate([cc[: maxlag + 1], cc[-maxlag:]])
        i = int(np.argmax(vals))
        lag = float(lags[i])
        j = i if i <= maxlag else len(cc) - (len(vals) - i)
        a, b, c = cc[(j - 1) % XFER_N], cc[j], cc[(j + 1) % XFER_N]
        den = a - 2 * b + c
        if abs(den) > EPS:
            lag += 0.5 * (a - c) / den
        return {
            "freqs": [round(float(f), 1) for f in m["centers"]],
            "mag_db": [None if v < -150 else round(float(v), 1) for v in hb],
            "coherence": [round(float(v), 2) for v in cb],
            "delay_ms": round(lag / self.sr * 1000, 3),
            # How far the correlation peak stands above the rest: below ~8 the delay is a guess.
            "delay_sharpness": round(float(vals[i] / (np.mean(np.abs(vals)) + EPS)), 1),
            "names": self.names[:2],
        }
