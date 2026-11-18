"""The Python validator must agree with the C++ one (`fxrender --check`)."""

import json
import subprocess

import pytest

from conftest import PRESETS_DIR
from fxweb.presets import PresetError, dump_preset, parse_json, validate_preset

BASE = {
    "format": 1,
    "name": "Test",
    "description": "d",
    "buffer_size": 128,
    "chain": [
        {"effect": "gain", "params": {"gain_db": -2}},
        {"effect": "delay", "params": {"time_ms": 110, "feedback": 10, "mix": 30}},
    ],
}


def variant(**changes):
    p = json.loads(json.dumps(BASE))
    for k, v in changes.items():
        if v is DROP:
            p.pop(k, None)
        else:
            p[k] = v
    return json.dumps(p)


def with_param(slot, pid, value):
    p = json.loads(json.dumps(BASE))
    p["chain"][slot]["params"][pid] = value
    return json.dumps(p)


DROP = object()

# Raw JSON texts, valid and not. Values like 24.0000001 test the float32
# comparison (C++ reads params as float, so this rounds to exactly 24).
CORPUS = {
    "base": json.dumps(BASE),
    "minimal": '{"format": 1, "name": "x", "chain": []}',
    "no_params": '{"format": 1, "name": "x", "chain": [{"effect": "delay"}]}',
    "empty_params": '{"format": 1, "name": "x", "chain": [{"effect": "delay", "params": {}}]}',
    "not_object": "[1, 2]",
    "invalid_json": '{"format": 1,',
    "trailing_comma": '{"format": 1, "name": "x", "chain": [],}',
    "nan": '{"format": 1, "name": "x", "chain": [{"effect": "gain", "params": {"gain_db": NaN}}]}',
    "unknown_key": variant(author="me"),
    "format_2": variant(format=2),
    "format_str": variant(format="1"),
    "format_float": variant(format=1.0),
    "format_bool": variant(format=True),
    "format_missing": variant(format=DROP),
    "name_empty": variant(name=""),
    "name_missing": variant(name=DROP),
    "name_number": variant(name=3),
    "desc_number": variant(description=3),
    "desc_missing": variant(description=DROP),
    "buf_missing": variant(buffer_size=DROP),
    "buf_100": variant(buffer_size=100),
    "buf_8": variant(buffer_size=8),
    "buf_16": variant(buffer_size=16),
    "buf_2048": variant(buffer_size=2048),
    "buf_4096": variant(buffer_size=4096),
    "buf_float": variant(buffer_size=128.0),
    "buf_neg": variant(buffer_size=-128),
    "buf_str": variant(buffer_size="128"),
    "chain_missing": variant(chain=DROP),
    "chain_object": variant(chain={}),
    "slot_not_object": variant(chain=["gain"]),
    "slot_unknown_key": variant(chain=[{"effect": "gain", "bypass": True}]),
    "slot_no_effect": variant(chain=[{"params": {}}]),
    "slot_effect_number": variant(chain=[{"effect": 1}]),
    "unknown_effect": variant(chain=[{"effect": "fuzz"}]),
    "params_array": variant(chain=[{"effect": "gain", "params": []}]),
    "unknown_param": with_param(0, "drive", 1),
    "param_str": with_param(0, "gain_db", "3"),
    "param_bool": with_param(0, "gain_db", True),
    "param_null": with_param(0, "gain_db", None),
    "param_at_min": with_param(0, "gain_db", -60),
    "param_at_max": with_param(0, "gain_db", 24),
    "param_below": with_param(0, "gain_db", -60.5),
    "param_above": with_param(0, "gain_db", 24.5),
    "param_f32_rounds_in": with_param(0, "gain_db", 24.0000001),
    "param_f32_out": with_param(0, "gain_db", 24.00001),
    "param_huge": with_param(0, "gain_db", 1e300),
    "param_fraction": with_param(1, "mix", 33.3),
    "time_min": with_param(1, "time_ms", 1),
    "time_below": with_param(1, "time_ms", 0.5),
    "feedback_above": with_param(1, "feedback", 96),
}


def python_ok(text: str, catalog: dict) -> bool:
    try:
        validate_preset(parse_json(text), catalog)
        return True
    except PresetError:
        return False


def cpp_ok(fxrender, path) -> bool:
    r = subprocess.run([str(fxrender), "--check", str(path)], capture_output=True, text=True)
    return r.returncode == 0


@pytest.mark.parametrize("path", sorted(PRESETS_DIR.glob("*.json")), ids=lambda p: p.name)
def test_repo_presets_agree(path, fxrender, catalog):
    assert python_ok(path.read_text(), catalog) == cpp_ok(fxrender, path)
    assert python_ok(path.read_text(), catalog), f"{path.name} should be valid"


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_corpus_agrees(name, tmp_path, fxrender, catalog):
    f = tmp_path / f"{name}.json"
    f.write_text(CORPUS[name])
    assert python_ok(CORPUS[name], catalog) == cpp_ok(fxrender, f)


def test_corpus_has_both_outcomes(catalog):
    results = {python_ok(t, catalog) for t in CORPUS.values()}
    assert results == {True, False}


@pytest.mark.parametrize("path", sorted(PRESETS_DIR.glob("*.json")), ids=lambda p: p.name)
def test_dump_round_trips(path, catalog):
    p = validate_preset(json.loads(path.read_text()), catalog)
    assert json.loads(dump_preset(p)) == p
    assert dump_preset(p) == path.read_text(), "dump_preset should match the hand-written style"


def test_error_messages_are_specific(catalog):
    with pytest.raises(PresetError, match=r"chain\[0\]\.gain_db = 30 is outside \[-60, 24\]"):
        validate_preset(json.loads(with_param(0, "gain_db", 30)), catalog)
    with pytest.raises(PresetError, match="power of two"):
        validate_preset(json.loads(variant(buffer_size=100)), catalog)
