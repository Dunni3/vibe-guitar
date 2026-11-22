"""Recordings on disk: `recordings/<id>.wav` (24-bit PCM, every source channel)
plus `recordings/<id>.json` (name, notes, preset, levels, markers, overview).

A take is written while it's recorded, by its own thread, from a queue the
source thread fills: a slow SD card can't stall the source, and the header is
patched every second so a power cut leaves a playable file. On startup, takes
still marked "recording" are recovered from whatever reached the disk.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from .wavfile import StreamingWavWriter, read_wav, wav_info, write_wav_pcm24

ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}(-\d+)?$")
OVERVIEW_POINTS = 400
MIN_FREE_BYTES = 300 * 1024 * 1024  # stop a take before the SD card fills


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _db(x: float) -> float:
    return round(20 * np.log10(max(x, 1e-12)), 1)


class Stats:
    """Running per-channel peak, clip count and a coarse peak overview."""

    def __init__(self, channels: int, sample_rate: int):
        self.peak = np.zeros(channels)
        self.clipped = np.zeros(channels, dtype=np.int64)
        self.chunk = max(1, sample_rate // 20)  # 50 ms overview columns
        self._acc = np.zeros(channels)
        self._acc_n = 0
        self.cols: list[np.ndarray] = []

    def add(self, block: np.ndarray) -> None:
        a = np.abs(block)
        self.peak = np.maximum(self.peak, a.max(axis=1) if a.shape[1] else 0)
        self.clipped += (a >= 0.999).sum(axis=1)
        i = 0
        n = a.shape[1]
        while i < n:
            take = min(self.chunk - self._acc_n, n - i)
            self._acc = np.maximum(self._acc, a[:, i:i + take].max(axis=1))
            self._acc_n += take
            i += take
            if self._acc_n == self.chunk:
                self.cols.append(self._acc)
                self._acc = np.zeros_like(self._acc)
                self._acc_n = 0

    def overview(self) -> list[list[float]]:
        cols = self.cols + ([self._acc] if self._acc_n else [])
        if not cols:
            return []
        m = np.stack(cols, axis=1)  # (ch, ncols)
        if m.shape[1] > OVERVIEW_POINTS:
            edges = np.linspace(0, m.shape[1], OVERVIEW_POINTS + 1).astype(int)
            m = np.maximum.reduceat(m, edges[:-1], axis=1)
        return [[round(float(v), 3) for v in row] for row in m]

    def summary(self) -> dict[str, Any]:
        return {"peak_dbfs": [_db(p) for p in self.peak], "clipped_samples": [int(c) for c in self.clipped],
                "overview": self.overview()}


class RecordingStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache = self.root / ".cache"
        self._lock = threading.Lock()
        self.recover()

    # --- paths & ids ---

    def wav(self, rid: str) -> Path:
        return self.root / f"{self._check(rid)}.wav"

    def meta_path(self, rid: str) -> Path:
        return self.root / f"{self._check(rid)}.json"

    @staticmethod
    def _check(rid: str) -> str:
        if not ID_RE.match(rid):
            raise KeyError(rid)
        return rid

    def new_id(self) -> str:
        with self._lock:
            base = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            rid, k = base, 1
            while (self.root / f"{rid}.json").exists() or (self.root / f"{rid}.wav").exists():
                k += 1
                rid = f"{base}-{k}"
            (self.root / f"{rid}.json").write_text("{}")  # reserve
            return rid

    # --- metadata ---

    def write_meta(self, rid: str, meta: dict) -> None:
        p = self.meta_path(rid)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, indent=1))
        os.replace(tmp, p)

    def get(self, rid: str) -> dict:
        try:
            return json.loads(self.meta_path(rid).read_text())
        except FileNotFoundError:
            raise KeyError(rid) from None

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.root.glob("*.json"), reverse=True):
            if not ID_RE.match(p.stem):
                continue
            try:
                m = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if m.get("id"):
                out.append({k: v for k, v in m.items() if k != "overview"} | {"overview": _thin(m.get("overview"), 120)})
        return out

    def update(self, rid: str, name: str | None = None, notes: str | None = None) -> dict:
        m = self.get(rid)
        if name is not None:
            m["name"] = name
        if notes is not None:
            m["notes"] = notes
        self.write_meta(rid, m)
        return m

    def delete(self, rid: str) -> None:
        m = self.get(rid)
        if m.get("status") == "recording":
            raise ValueError("stop the recording first")
        for p in [self.wav(rid), self.meta_path(rid), *self.cache.glob(f"{rid}.*")]:
            p.unlink(missing_ok=True)

    def channel_wav(self, rid: str, ch: int) -> Path:
        """A mono WAV of one channel, cached, for playback in the browser."""
        m = self.get(rid)
        if not 0 <= ch < len(m.get("channels", [])):
            raise KeyError(f"{rid} ch{ch}")
        src = self.wav(rid)
        out = self.cache / f"{rid}.ch{ch}.wav"
        if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
            self.cache.mkdir(exist_ok=True)
            sr, data = read_wav(src)
            tmp = out.with_suffix(".tmp")
            write_wav_pcm24(tmp, data[ch], sr)
            os.replace(tmp, out)
        return out

    def disk(self) -> dict[str, int]:
        u = shutil.disk_usage(self.root)
        return {"free_bytes": u.free, "total_bytes": u.total}

    def recover(self) -> None:
        """Finish the metadata of takes that were cut off by a crash or power loss."""
        for p in self.root.glob("*.json"):
            if not ID_RE.match(p.stem):
                continue
            try:
                m = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if m == {}:
                p.unlink(missing_ok=True)  # a reserved id that never got a file
                continue
            if m.get("status") != "recording":
                continue
            w = self.root / f"{p.stem}.wav"
            try:
                info = wav_info(w, trust_file_size=True)
                frames = info["frames"]
                _patch_wav_sizes(w, info)
            except (OSError, ValueError):
                frames = 0
            m.update(status="interrupted", frames=frames,
                     duration_s=round(frames / m.get("sample_rate", 48000), 2),
                     size_bytes=w.stat().st_size if w.exists() else 0)
            self.write_meta(p.stem, m)


def _thin(ov: list[list[float]] | None, n: int) -> list[list[float]]:
    if not ov:
        return []
    out = []
    for row in ov:
        if len(row) <= n:
            out.append(row)
        else:
            a = np.array(row)
            edges = np.linspace(0, len(a), n + 1).astype(int)
            out.append([round(float(v), 3) for v in np.maximum.reduceat(a, edges[:-1])])
    return out


def _patch_wav_sizes(path: Path, info: dict) -> None:
    """Rewrite the RIFF and data sizes from what's actually on disk."""
    data_bytes = info["frames"] * info["block_align"]
    with open(path, "r+b") as f:
        f.seek(4)
        f.write(int(info["data_offset"] - 8 + data_bytes).to_bytes(4, "little"))
        f.seek(info["data_offset"] - 4)
        f.write(int(data_bytes).to_bytes(4, "little"))


class Recorder:
    """One take in progress. feed() is called from the source thread and only queues."""

    def __init__(self, store: RecordingStore, meta: dict, channels: int, sample_rate: int,
                 start_pos: int, dropped_at_start: int):
        self.store = store
        self.id = meta["id"]
        self.meta = meta
        self.channels, self.sample_rate = channels, sample_rate
        self.start_pos = start_pos
        self.dropped_at_start = dropped_at_start
        self.started = time.monotonic()
        self.frames = 0
        self.markers: list[float] = []
        self.error: str | None = None
        self.stop_reason: str | None = None
        self._q: queue.Queue[np.ndarray | None] = queue.Queue()
        self._writer = StreamingWavWriter(store.wav(self.id), channels, sample_rate)
        self._stats = Stats(channels, sample_rate)
        self._done = threading.Event()
        self._stopping = False
        store.write_meta(self.id, meta | {"status": "recording"})
        self._t = threading.Thread(target=self._run, daemon=True, name=f"rec-{self.id}")
        self._t.start()

    def feed(self, block: np.ndarray) -> None:
        if not self._stopping:
            self.frames += block.shape[1]
            self._q.put(block)

    def mark(self) -> float:
        t = round(self.frames / self.sample_rate, 2)
        self.markers.append(t)
        return t

    @property
    def elapsed_s(self) -> float:
        return self.frames / self.sample_rate

    def status(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.meta.get("name"), "elapsed_s": round(self.elapsed_s, 2),
                "bytes": self.frames * self.channels * 3 + 44, "markers": self.markers,
                "peak_dbfs": [_db(p) for p in self._stats.peak], "error": self.error,
                "backlog_s": round(self._q.qsize() * 0.01, 2)}

    def _run(self) -> None:
        last_patch = time.monotonic()
        try:
            while True:
                block = self._q.get()
                if block is None:
                    break
                self._writer.write(block)
                self._stats.add(block)
                now = time.monotonic()
                if now - last_patch > 1.0:
                    last_patch = now
                    self._writer.patch_header()
                    if self.store.disk()["free_bytes"] < MIN_FREE_BYTES:
                        self.stop_reason = "disk almost full"
                        self._stopping = True
        except OSError as e:
            self.error = f"write failed: {e}"
            self._stopping = True
        finally:
            try:
                self._writer.close()
            except OSError:
                pass
            self._done.set()

    @property
    def wants_stop(self) -> bool:
        return self._stopping

    def finish(self, dropped_now: int) -> dict:
        self._stopping = True
        self._q.put(None)
        self._done.wait(timeout=30)
        frames = self._writer.frames
        meta = self.meta | self._stats.summary() | {
            "status": "error" if self.error else "done",
            "frames": frames, "duration_s": round(frames / self.sample_rate, 2),
            "markers": self.markers, "dropped_frames": max(0, dropped_now - self.dropped_at_start),
            "size_bytes": self.store.wav(self.id).stat().st_size,
            "error": self.error, "stop_reason": self.stop_reason,
        }
        self.store.write_meta(self.id, meta)
        return meta


def save_block(store: RecordingStore, meta: dict, data: np.ndarray, sample_rate: int) -> dict:
    """Write a finished chunk of audio (the "save last N s" button)."""
    rid = meta["id"]
    write_wav_pcm24(store.wav(rid), data, sample_rate)
    st = Stats(data.shape[0], sample_rate)
    st.add(data)
    meta = meta | st.summary() | {
        "status": "done", "frames": data.shape[1], "duration_s": round(data.shape[1] / sample_rate, 2),
        "markers": [], "size_bytes": store.wav(rid).stat().st_size,
    }
    store.write_meta(rid, meta)
    return meta
