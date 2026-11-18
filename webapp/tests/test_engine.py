from fxweb.engine import ENGINES, Engine, FakeEngine, estimated_latency_ms, make_engine
from fxweb.system import cpu_temp_c

P128 = {"format": 1, "name": "A", "buffer_size": 128, "chain": [{"effect": "gain"}]}
P64 = {"format": 1, "name": "B", "buffer_size": 64, "chain": []}


def test_fake_engine_is_an_engine():
    assert isinstance(make_engine("fake"), Engine)
    assert set(ENGINES) >= {"fake"}


def test_nothing_loaded():
    h = FakeEngine(seed=1).health()
    assert not h.running and h.buffer_size is None and h.latency_ms is None and h.dsp_load_pct is None


def test_load_and_health():
    e = FakeEngine(seed=1, xrun_chance=0)
    lp = e.load(P128, "a")
    assert e.loaded() == lp and lp.id == "a" and not lp.modified
    h = e.health()
    assert h.running and h.buffer_size == 128 and h.xruns == 0
    assert h.latency_ms == round(estimated_latency_ms(128), 2)


def test_buffer_switch_counts_one_glitch():
    e = FakeEngine(seed=1, xrun_chance=0)
    e.load(P128, "a")
    e.load(P128, "a")
    assert e.health().xruns == 0
    e.load(P64, "b")
    assert e.health().xruns == 1


def test_latency_model():
    assert abs(estimated_latency_ms(128) - 6.333) < 0.01


def test_cpu_temp_reads_sysfs(tmp_path):
    f = tmp_path / "temp"
    f.write_text("54321\n")
    assert cpu_temp_c(f) == 54.321
    assert cpu_temp_c(tmp_path / "missing") is None
