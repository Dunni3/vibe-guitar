"""Live signal: tap, WAV I/O, history, analysis, and the recording API."""

import json
import math
import subprocess
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from fxweb.live.analysis import Analyzer, yin
from fxweb.live.history import History
from fxweb.live.hub import LiveHub
from fxweb.live.recordings import RecordingStore
from fxweb.live.sources import Source, TapSource, make_source
from fxweb.live.tap import TapReader, TapWriterPy
from fxweb.live.wavfile import StreamingWavWriter, read_wav, wav_info, write_wav_pcm24
from fxweb.server import create_app

SR = 48000


def sine(f, dur, amp=1.0, sr=SR):
    t = np.arange(int(dur * sr)) / sr
    return (amp * np.sin(2 * np.pi * f * t)).astype(np.float32)


# ------------------------------------------------------------------ tap

def test_tap_round_trip_and_wraparound(tmp_path):
    w = TapWriterPy(tmp_path / "tap", 2, SR, ["in", "out"], capacity=1024)
    r = TapReader(tmp_path / "tap")
    r.open()
    assert (r.channels, r.sample_rate, r.capacity, r.names) == (2, SR, 1024, ["in", "out"])
    got = []
    for k in range(5):  # 5 x 300 frames: wraps the 1024-frame ring
        x = np.arange(k * 300, (k + 1) * 300, dtype=np.float32)
        w.write(np.stack([x, -x]))
        got.append(r.read())
    got = np.concatenate(got, axis=1)
    assert np.array_equal(got[0], np.arange(1500, dtype=np.float32))
    assert np.array_equal(got[1], -got[0])
    assert r.dropped == 0


def test_tap_reader_that_falls_behind_counts_drops(tmp_path):
    w = TapWriterPy(tmp_path / "tap", 1, SR, ["in"], capacity=1024)
    r = TapReader(tmp_path / "tap")
    r.open()
    w.write(np.arange(5000, dtype=np.float32)[None])  # lapped the ring several times
    got = r.read()[0]
    assert r.dropped == 5000 - len(got)
    assert np.array_equal(got, np.arange(5000 - len(got), 5000, dtype=np.float32))  # newest, in order
    assert len(got) < 1024


def test_tap_source_notices_a_restarted_engine(tmp_path):
    p = tmp_path / "tap"
    w = TapWriterPy(p, 1, SR, ["in"], capacity=1024)
    src = TapSource(p)
    src.open()
    w.write(np.ones((1, 100), np.float32))
    assert src.read(0.1).shape == (1, 100)
    TapWriterPy(p, 2, 44100, ["a", "b"], capacity=2048)  # new writer renames a new file into place
    src._last_check = 0
    with pytest.raises(Exception, match="restarted"):
        src.read(0.1)
    src.open()
    assert (src.sample_rate, src.channel_names) == (44100, ["a", "b"])


def test_tap_reads_what_the_cpp_writer_writes(tmp_path, fxrender):
    """fxtapfeed (C++ TapWriter) -> Python TapSource: the shared layout agrees."""
    feed = fxrender.parent / "fxtapfeed"
    if not feed.exists():
        pytest.skip("fxtapfeed not built")
    wav = tmp_path / "a440.wav"
    write_wav_pcm24(wav, sine(440, 2.0, 0.5), SR)
    tap = tmp_path / "tap"
    preset = tmp_path / "p.json"
    preset.write_text(json.dumps({"format": 1, "name": "Quiet", "buffer_size": 64,
                                  "chain": [{"effect": "gain", "params": {"gain_db": -6}}]}))
    p = subprocess.Popen([str(feed), "--preset", str(preset), "--in", str(wav), "--tap", str(tap), "--quiet"])
    try:
        for _ in range(100):
            if tap.exists():
                break
            time.sleep(0.02)
        src = TapSource(tap)
        src.open()
        assert src.channel_names == ["in", "out"] and src.sample_rate == SR
        blocks, t0 = [], time.monotonic()
        while sum(b.shape[1] for b in blocks) < 9600 and time.monotonic() - t0 < 3:
            b = src.read(0.1)
            if b is not None:
                blocks.append(b)
        x = np.concatenate(blocks, axis=1)
        assert x.shape[1] >= 9600
        settled = x[:, 4800:]
        ratio = np.sqrt(np.mean(settled[1] ** 2) / np.mean(settled[0] ** 2))
        assert abs(20 * np.log10(ratio) + 6) < 0.2  # "out" is "in" through -6 dB of gain
        assert abs(np.sqrt(np.mean(settled[0] ** 2)) - 0.5 / math.sqrt(2)) < 0.01
    finally:
        p.terminate()
        p.wait(5)


# ------------------------------------------------------------------ wav + history

def test_wav24_round_trip(tmp_path):
    x = np.stack([sine(100, 0.1, 0.9), sine(200, 0.1, -0.5)])
    write_wav_pcm24(tmp_path / "a.wav", x, SR)
    sr, y = read_wav(tmp_path / "a.wav")
    assert sr == SR and y.shape == x.shape
    assert np.max(np.abs(x - y)) < 2 ** -22
    import wave
    with wave.open(str(tmp_path / "a.wav")) as w:  # standard readers accept it
        assert (w.getnchannels(), w.getsampwidth(), w.getnframes()) == (2, 3, x.shape[1])


def test_streaming_wav_survives_a_crash(tmp_path):
    w = StreamingWavWriter(tmp_path / "c.wav", 1, SR)
    w.write(np.zeros((1, 1000), np.float32))
    w.patch_header()
    w.write(np.zeros((1, 500), np.float32))
    w._f.flush()  # ...and the power goes out: no close(), header still says 1000
    assert wav_info(tmp_path / "c.wav")["frames"] == 1000  # the header, as written
    assert wav_info(tmp_path / "c.wav", trust_file_size=True)["frames"] == 1500  # what's on disk


def test_history_ranges():
    h = History(1, 1000)
    h.write(np.arange(700, dtype=np.float32)[None])
    assert np.array_equal(h.latest(800)[0][:100], np.zeros(100))  # before the start: silence
    h.write(np.arange(700, 1500, dtype=np.float32)[None])
    assert np.array_equal(h.read(600, 1500)[0], np.arange(600, 1500))
    with pytest.raises(ValueError):
        h.read(400, 600)  # overwritten


# ------------------------------------------------------------------ analysis

def run_analyzer(x, names=("in",), fps=30):
    x = np.atleast_2d(x)
    h = History(x.shape[0], SR * 30)
    a = Analyzer(SR, list(names), fps=fps)
    step = SR // fps
    frames = []
    for i in range(0, x.shape[1], step):
        h.write(x[:, i:i + step])
        frames.append(a.step(h))
    return frames


@pytest.mark.parametrize("hz", [82.41, 110.0, 196.0, 440.0, 1046.5])
def test_yin_is_within_a_cent(hz):
    f, conf = yin(sine(hz, 0.2)[::2], SR / 2)
    assert abs(1200 * math.log2(f / hz)) < 1.0 and conf > 0.9


def test_levels_tuner_and_spectrum_on_a_sine():
    frames = run_analyzer(sine(440, 1.0, 0.5))
    f = frames[-1]
    lv = f["levels"][0]
    assert abs(lv["peak_db"] - (-6.02)) < 0.1
    assert abs(lv["rms_db"] - (-9.03)) < 0.1
    assert not lv["clip"] and lv["clips"] == 0
    p = f["pitch"]
    assert (p["note"], p["octave"]) == ("A", 4) and abs(p["cents"]) < 1
    assert f["chord"] is None
    import base64
    spec = np.frombuffer(base64.b64decode(f["spectrum"]["data"][0]), np.uint8) / 2 + f["spectrum"]["floor"]
    centers = np.geomspace(30, f["spectrum"]["fmax"], 241)
    centers = np.sqrt(centers[:-1] * centers[1:])
    peak = int(np.argmax(spec))
    assert abs(spec[peak] - (-6.02)) < 0.6  # amplitude 0.5 -> -6 dBFS in the spectrum too
    assert abs(math.log2(centers[peak] / 440)) < 0.05


def test_clips_are_counted_once_per_event():
    x = sine(200, 1.0, 0.3)
    x[SR // 4: SR // 4 + 200] = 1.0
    x[SR // 2: SR // 2 + 200] = -1.0
    assert run_analyzer(x)[-1]["levels"][0]["clips"] == 2


def test_chord_guess():
    def tone(m, dur=1.5):
        f0 = 440 * 2 ** ((m - 69) / 12)
        return sum((0.6 ** (k - 1)) * sine(k * f0, dur, 0.1) for k in range(1, 6))
    for name, notes in {"G": [43, 47, 50, 55, 59, 67], "Am": [45, 52, 57, 60, 64], "D": [50, 57, 62, 66]}.items():
        frames = run_analyzer(sum(tone(m) for m in notes))
        assert frames[-1]["chord"]["name"] == name
        assert frames[-1]["pitch"] is None  # the tuner doesn't pretend a chord is one note


def test_single_note_is_not_a_chord():
    f0 = 110.0
    x = sum((0.6 ** (k - 1)) * sine(k * f0, 1.0, 0.2) for k in range(1, 8))
    f = run_analyzer(x)[-1]
    assert f["chord"] is None and f["pitch"]["note"] == "A"
    assert f["harmonics"][0] == 0.0 and abs(f["harmonics"][1] - 20 * math.log10(0.6)) < 1.0


def test_response_gain_and_delay():
    rng = np.random.default_rng(1)
    x = rng.normal(0, 0.1, SR * 3).astype(np.float32)
    d = int(0.002 * SR)
    y = np.concatenate([np.zeros(d, np.float32), x[:-d]]) * 0.5
    f = run_analyzer(np.stack([x, y]), names=("in", "out"))[-1]
    r = f["response"]
    assert abs(r["delay_ms"] - 2.0) < 0.05 and r["delay_sharpness"] > 8
    mags = [m for m, fr in zip(r["mag_db"], r["freqs"]) if 200 < fr < 10000]
    assert all(abs(m + 6.02) < 0.5 for m in mags)


def test_onsets_and_tempo():
    beat = 60 / 100
    x = np.zeros(SR * 10, np.float32)
    rng = np.random.default_rng(0)
    for k in range(int(10 / beat)):
        i = int(k * beat * SR)
        n = min(int(beat * SR), len(x) - i)  # ring until the next pick
        t = np.arange(n) / SR
        x[i:i + n] += (0.3 * np.exp(-12 * t) * np.sin(2 * np.pi * 196 * t)
                       + 0.05 * np.exp(-200 * t) * rng.normal(0, 1, n)).astype(np.float32)
    frames = run_analyzer(x)
    onsets = [o["t"] for fr in frames for o in fr["onsets"]]
    assert 14 <= len(onsets) <= 18
    assert abs(np.median(np.diff(onsets)) - beat) < 0.02
    assert abs(frames[-1]["features"]["tempo_bpm"] - 100) < 2


def test_quiet_noise_floor_and_hum():
    t = np.arange(SR * 3) / SR
    rng = np.random.default_rng(2)
    x = (10 ** (-60 / 20) * np.sin(2 * np.pi * 60 * t) + rng.normal(0, 10 ** (-80 / 20), len(t))).astype(np.float32)
    ft = run_analyzer(x)[-1]["features"]
    assert ft["hum_prominent"] and abs(ft["hum_db"] - (-60)) < 1.5
    assert ft["noise_floor_db"] < -60 and not ft["active"]


# ------------------------------------------------------------------ hub + API

class FastSource(Source):
    """A real-time 220 Hz tone on two channels, without the sim's synthesis cost."""

    kind = "test"

    def __init__(self):
        super().__init__()
        self.channel_names = ["in", "out"]
        self.label = "test tone"

    def open(self):
        self._t0, self._sent = time.monotonic(), 0

    def read(self, timeout=0.05):
        due = int((time.monotonic() - self._t0) * SR)
        if due - self._sent < 480:
            time.sleep(0.005)
            return None
        n = due - self._sent
        t = (self._sent + np.arange(n)) / SR
        self._sent = due
        x = (0.25 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        return np.stack([x, 0.5 * x])


@pytest.fixture
def live_client(store, engine, tmp_path):
    hub = LiveHub(RecordingStore(tmp_path / "rec"), retro_seconds=5)
    hub.start(FastSource())
    for _ in range(100):
        if hub.source_status()["state"] == "live" and hub._history and hub._history.pos > 1.3 * SR:
            break
        time.sleep(0.02)
    yield TestClient(create_app(store, engine, live=hub)), hub
    hub.close()


def test_record_mark_stop(live_client):
    c, hub = live_client
    r = c.post("/api/live/record/start", json={"name": "Body IR take 1", "notes": "mic 30 cm"})
    assert r.status_code == 200, r.text
    assert c.post("/api/live/record/start").status_code == 409  # one take at a time
    assert c.post("/api/live/source", json={"kind": "sim"}).status_code == 409  # not while recording
    time.sleep(0.3)
    assert c.post("/api/live/record/mark").json()["t"] > 0
    time.sleep(0.3)
    m = c.post("/api/live/record/stop").json()
    assert m["name"] == "Body IR take 1" and m["notes"] == "mic 30 cm" and m["status"] == "done"
    assert m["channels"] == ["in", "out"] and m["kind"] == "take"
    assert 0.5 < m["duration_s"] < 2 and len(m["markers"]) == 1
    assert m["preset"]["id"] == "clean"  # what was loaded when it was played
    assert abs(m["peak_dbfs"][0] - 20 * math.log10(0.25)) < 0.1
    assert abs(m["peak_dbfs"][1] - 20 * math.log10(0.125)) < 0.1
    sr, data = read_wav(hub.store.wav(m["id"]))
    assert sr == SR and data.shape == (2, m["frames"])
    assert np.allclose(data[1], 0.5 * data[0], atol=1e-6)  # channels stay aligned
    assert c.post("/api/live/record/stop").status_code == 409


def test_save_last_seconds(live_client):
    c, hub = live_client
    time.sleep(0.2)
    r = c.post("/api/live/record/retro", json={"seconds": 1})
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["kind"] == "retro" and abs(m["duration_s"] - 1.0) < 0.01
    m2 = c.post("/api/live/record/retro", json={"seconds": 60}).json()  # more than we keep
    assert m2["duration_s"] <= 5.0
    assert c.post("/api/live/record/retro", json={"seconds": "lots"}).status_code == 422
    assert c.post("/api/live/record/retro", json={"seconds": 0.5}).status_code == 422


def test_recordings_list_edit_play_delete(live_client):
    c, hub = live_client
    m = c.post("/api/live/record/retro", json={"seconds": 1}).json()
    rid = m["id"]
    lst = c.get("/api/recordings").json()["recordings"]
    assert [x["id"] for x in lst] == [rid] and lst[0]["overview"]
    assert c.patch(f"/api/recordings/{rid}", json={"name": "Keeper"}).json()["name"] == "Keeper"
    full = c.get(f"/api/recordings/{rid}/audio")
    assert full.status_code == 200 and full.content[:4] == b"RIFF"
    assert "Keeper.wav" in full.headers["content-disposition"]
    mono = c.get(f"/api/recordings/{rid}/audio?ch=1", headers={"Range": "bytes=0-99"})
    assert mono.status_code == 206 and len(mono.content) == 100  # Safari's <audio> needs ranges
    assert c.get(f"/api/recordings/{rid}/audio?ch=5").status_code == 404
    assert c.get("/api/recordings/..%2F..%2Fetc").status_code == 404
    assert c.delete(f"/api/recordings/{rid}").status_code == 200
    assert c.get("/api/recordings").json()["recordings"] == []
    assert not list(hub.store.root.glob(f"{rid}*"))
    assert not list(hub.store.cache.glob(f"{rid}*"))


def test_stream_sends_analysis_frames(live_client):
    c, _ = live_client
    with c.stream("GET", "/api/live/stream?fps=30&limit=3") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        frames = [json.loads(line[5:]) for line in r.iter_lines() if line.startswith("data:")]
    assert len(frames) == 3
    f = frames[-1]
    assert f["channels"] == ["in", "out"] and f["source"]["state"] == "live"
    assert f["pitch"]["note"] == "A" and f["pitch"]["octave"] == 3
    assert abs(f["response"]["mag_db"][40] + 6.0) < 0.5 if f["response"]["mag_db"][40] is not None else True


def test_interrupted_take_is_recovered(tmp_path):
    root = tmp_path / "rec"
    store = RecordingStore(root)
    rid = store.new_id()
    store.write_meta(rid, {"id": rid, "name": "x", "status": "recording", "sample_rate": SR, "channels": ["in"]})
    w = StreamingWavWriter(store.wav(rid), 1, SR)
    w.write(np.zeros((1, SR // 2), np.float32))
    w.patch_header()
    w.write(np.zeros((1, SR // 2), np.float32))
    w._f.flush()  # crash: the header says half a second, the disk has a second
    m = RecordingStore(root).get(rid)  # next start-up
    assert m["status"] == "interrupted" and m["duration_s"] == 1.0
    assert wav_info(store.wav(rid))["frames"] == SR  # header repaired


def test_source_specs():
    assert make_source("tap:/tmp/x").path.name == "x"
    a = make_source("alsa:plughw:CARD=USB,DEV=0:2")
    assert (a.device, a.nch) == ("plughw:CARD=USB,DEV=0", 2)
    with pytest.raises(ValueError):
        make_source({"kind": "alsa", "device": "hw:0; rm -rf /"})
    with pytest.raises(ValueError):
        make_source({"kind": "nope"})
