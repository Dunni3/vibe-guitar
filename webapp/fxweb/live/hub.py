"""LiveHub: owns the live source, the history ring, the analyzer, the active
recording and the stream subscribers. Two threads, both inside the (niced)
web server process, never in the audio engine:

    source thread    source.read() -> history, recorder
    analysis thread  every 1/fps s, only while someone's watching -> JSON frame
                     -> each subscriber's asyncio queue (latest frame wins)
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .analysis import Analyzer
from .history import History
from .recordings import Recorder, RecordingStore, save_block
from .sources import Source, SourceError, make_source


class LiveError(RuntimeError):
    """A request the hub can't do right now (409 in the API)."""


class _Subscriber:
    def __init__(self, loop: asyncio.AbstractEventLoop, every: int):
        self.loop = loop
        self.every = max(1, every)
        self.n = 0
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)

    def offer(self, text: str) -> None:  # runs on the event loop
        if self.queue.full():
            try:
                self.queue.get_nowait()  # a slow phone gets the newest frame, not a backlog
            except asyncio.QueueEmpty:
                pass
        self.queue.put_nowait(text)


class LiveHub:
    def __init__(self, store: RecordingStore, *, fxrender: str | Path | None = None,
                 tap_path: Path | None = None, retro_seconds: float = 90.0, fps: float = 30.0,
                 mains_hz: float = 60.0, preset_info: Callable[[], dict | None] = lambda: None):
        self.store = store
        self.fxrender = fxrender
        self.tap_path = tap_path
        self.retro_seconds = retro_seconds
        self.fps = fps
        self.mains_hz = mains_hz
        self.preset_info = preset_info

        self._source: Source | None = None
        self._spec: dict[str, Any] = {"kind": "sim"}
        self._state = "stopped"
        self._error: str | None = None
        self._history: History | None = None
        self._gen = 0
        self._lock = threading.RLock()
        self._rec: Recorder | None = None
        self._last_take: dict | None = None
        self._auto_stopped: dict | None = None
        self._preset: dict | None = None

        self._subs: set[_Subscriber] = set()
        self._subs_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._stop_reader = threading.Event()
        self._closed = threading.Event()
        self._analysis = threading.Thread(target=self._analysis_loop, daemon=True, name="live-analysis")
        self._analysis.start()

    # ------------------------------------------------------------ source

    def start(self, spec: dict[str, Any] | str | Source) -> None:
        if isinstance(spec, Source):
            src, spec = spec, spec.describe()
        else:
            src = make_source(spec, fxrender=self.fxrender, tap_path=self.tap_path)
        with self._lock:
            if self._rec is not None:
                raise LiveError("stop the recording before switching source")
            self._stop_reader_thread()
            self._source = src
            self._spec = spec if isinstance(spec, dict) else src.describe() | {"kind": src.kind}
            if self._preset is not None:
                src.set_preset(self._preset)
            self._state, self._error = "starting", None
            self._history = None
            self._gen += 1
            self._stop_reader = threading.Event()
            self._reader = threading.Thread(target=self._reader_loop, args=(src, self._stop_reader),
                                            daemon=True, name=f"live-{src.kind}")
            self._reader.start()

    def _stop_reader_thread(self) -> None:
        if self._reader is not None:
            self._stop_reader.set()
            self._reader.join(timeout=3)
        if self._source is not None:
            self._source.close()
        self._reader = None

    def _reader_loop(self, src: Source, stop: threading.Event) -> None:
        opened = False
        while not stop.is_set():
            if not opened:
                try:
                    src.open()
                    opened = True
                    self._state, self._error = "live", None
                except SourceError as e:
                    self._state, self._error = "waiting", str(e)
                    stop.wait(1.0)
                    continue
            try:
                block = src.read(0.05)
            except SourceError as e:
                self._state, self._error = "waiting", str(e)
                src.close()
                opened = False
                stop.wait(0.5)
                continue
            except Exception as e:  # keep the thread alive whatever a source does
                self._state, self._error = "error", f"{type(e).__name__}: {e}"
                src.close()
                opened = False
                stop.wait(1.0)
                continue
            if block is None:
                continue
            self._state = "live"
            h = self._history
            if h is None or h.channels != block.shape[0]:
                h = History(block.shape[0], int(self.retro_seconds * src.sample_rate))
                with self._lock:
                    self._history = h
                    self._gen += 1
            h.write(block)
            rec = self._rec
            if rec is not None:
                rec.feed(block)
                if rec.wants_stop:
                    self._rec = None
                    threading.Thread(target=self._finish_take, args=(rec, True), daemon=True).start()
        src.close()

    def set_preset(self, preset: dict) -> None:
        """The engine loaded a preset (the simulation re-renders its "out" channel)."""
        self._preset = preset
        src = self._source
        if src is not None:
            src.set_preset(preset)

    # ------------------------------------------------------------ analysis stream

    def subscribe(self, loop: asyncio.AbstractEventLoop, fps: float | None = None) -> _Subscriber:
        every = round(self.fps / fps) if fps else 1
        s = _Subscriber(loop, every)
        with self._subs_lock:
            self._subs.add(s)
        return s

    def unsubscribe(self, s: _Subscriber) -> None:
        with self._subs_lock:
            self._subs.discard(s)

    def _analysis_loop(self) -> None:
        period = 1.0 / self.fps
        analyzer: Analyzer | None = None
        gen = -1
        next_t = time.monotonic()
        while not self._closed.is_set():
            next_t += period
            delay = next_t - time.monotonic()
            if delay < -0.5:
                next_t = time.monotonic()  # fell far behind; don't try to catch up
            elif delay > 0:
                self._closed.wait(delay)
            with self._subs_lock:
                subs = list(self._subs)
            h, src = self._history, self._source
            if not subs:
                if analyzer is not None:
                    analyzer.reset_position()
                continue
            frame: dict[str, Any]
            if h is None or src is None or h.pos == 0:
                frame = {"seq": 0, "idle": True}
            else:
                if gen != self._gen or analyzer is None:
                    gen = self._gen
                    analyzer = Analyzer(src.sample_rate, src.channel_names[: h.channels] or
                                        [f"ch{i + 1}" for i in range(h.channels)],
                                        fps=self.fps, mains_hz=self.mains_hz)
                try:
                    frame = analyzer.step(h)
                except Exception as e:  # a bug in one panel shouldn't kill the stream
                    frame = {"seq": -1, "error": f"analysis failed: {type(e).__name__}: {e}"}
            frame["source"] = self.source_status()
            frame["rec"] = self.recording_status()
            try:
                text = json.dumps(frame, separators=(",", ":"), allow_nan=False, default=_json_default)
            except (TypeError, ValueError) as e:
                text = json.dumps({"seq": -1, "error": f"frame not serializable: {e}",
                                   "source": frame["source"], "rec": frame["rec"]}, default=_json_default)
            for s in subs:
                s.n += 1
                if s.n % s.every == 0:
                    try:
                        s.loop.call_soon_threadsafe(s.offer, text)
                    except RuntimeError:  # loop closed
                        self.unsubscribe(s)

    # ------------------------------------------------------------ recording

    def _base_meta(self, rid: str, kind: str, name: str | None, notes: str | None) -> dict:
        src = self._source
        assert src is not None
        created = datetime.now().astimezone()
        default = ("Take" if kind == "take" else "Saved clip") + created.strftime(" %b %d %H:%M")
        return {"id": rid, "name": (name or "").strip() or default, "notes": (notes or "").strip(),
                "created": created.isoformat(timespec="seconds"), "kind": kind,
                "sample_rate": src.sample_rate, "channels": list(src.channel_names),
                "format": "WAV, 24-bit PCM", "source": src.describe(), "preset": self.preset_info()}

    def start_recording(self, name: str | None = None, notes: str | None = None) -> dict:
        with self._lock:
            if self._rec is not None:
                raise LiveError("already recording")
            h, src = self._history, self._source
            if h is None or src is None or self._state != "live":
                raise LiveError("no live signal to record")
            if self.store.disk()["free_bytes"] < 400 * 1024 * 1024:
                raise LiveError("less than 400 MB free on the card")
            rid = self.store.new_id()
            meta = self._base_meta(rid, "take", name, notes)
            self._rec = Recorder(self.store, meta, h.channels, src.sample_rate, h.pos, src.dropped)
            return self.recording_status()

    def stop_recording(self) -> dict:
        with self._lock:
            rec = self._rec
            if rec is None:
                auto, self._auto_stopped = self._auto_stopped, None
                if auto is not None:
                    return auto  # it stopped itself (disk full) just before the button press
                raise LiveError("not recording")
            self._rec = None
        return self._finish_take(rec)

    def _finish_take(self, rec: Recorder, auto: bool = False) -> dict:
        src = self._source
        meta = rec.finish(src.dropped if src else 0)
        self._last_take = meta
        if auto:
            self._auto_stopped = meta
        return meta

    def mark(self) -> float:
        rec = self._rec
        if rec is None:
            raise LiveError("not recording")
        return rec.mark()

    def save_retro(self, seconds: float, name: str | None = None, notes: str | None = None) -> dict:
        with self._lock:
            h, src = self._history, self._source
            if h is None or src is None or h.pos == 0:
                raise LiveError("nothing heard yet")
            pos = h.pos
            n = int(min(seconds * src.sample_rate, pos - h.oldest()))
            data = h.read(pos - n, pos)
            rid = self.store.new_id()
            meta = self._base_meta(rid, "retro", name, notes)
            meta["retro_requested_s"] = seconds
        return save_block(self.store, meta, data, src.sample_rate)

    # ------------------------------------------------------------ status

    def source_status(self) -> dict[str, Any]:
        src, h = self._source, self._history
        d: dict[str, Any] = {"state": self._state, "error": self._error, "spec": self._spec}
        if src is not None:
            d |= src.describe() | {"dropped": src.dropped}
        if h is not None and src is not None:
            d["buffered_s"] = round((h.pos - h.oldest()) / src.sample_rate, 1)
        d["retro_max_s"] = self.retro_seconds
        return d

    def recording_status(self) -> dict[str, Any] | None:
        rec = self._rec
        return rec.status() if rec is not None else None

    def status(self) -> dict[str, Any]:
        disk = self.store.disk()
        src = self._source
        bps = (src.sample_rate * len(src.channel_names) * 3) if src and src.channel_names else 288000
        return {"source": self.source_status(), "recording": self.recording_status(),
                "last_take": self._last_take,
                "disk": disk | {"free_minutes": round(disk["free_bytes"] / bps / 60, 0)},
                "fps": self.fps}

    def close(self) -> None:
        with self._lock:
            if self._rec is not None:
                rec, self._rec = self._rec, None
                self._finish_take(rec)
            self._stop_reader_thread()
        self._closed.set()


def _json_default(o: Any) -> Any:
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.bool_):
        return bool(o)
    raise TypeError(type(o).__name__)
