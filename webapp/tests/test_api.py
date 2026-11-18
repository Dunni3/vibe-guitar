import json
import re

from fxweb.server import STATIC_DIR

SLAP = {
    "format": 1,
    "name": "Slapback",
    "description": "edited",
    "buffer_size": 64,
    "chain": [
        {"effect": "gain", "params": {"gain_db": -4}},
        {"effect": "delay", "params": {"time_ms": 120, "feedback": 10, "mix": 30}},
    ],
}


def test_catalog(client):
    c = client.get("/api/catalog").json()
    ids = {e["id"] for e in c["effects"]}
    assert {"gain", "delay"} <= ids
    gain = next(e for e in c["effects"] if e["id"] == "gain")
    assert gain["params"][0] | {} == gain["params"][0]
    assert {"id", "min", "max", "default", "unit", "name"} <= set(gain["params"][0])


def test_list_presets(client):
    data = client.get("/api/presets").json()
    ids = [p["id"] for p in data["presets"]]
    assert set(ids) == {"clean", "long_echo", "slapback"}
    assert all(p["valid"] for p in data["presets"])
    assert data["loaded"]["id"] == "clean"  # loaded at startup


def test_invalid_file_is_listed_with_error(client, presets_dir):
    (presets_dir / "broken.json").write_text('{"format": 1, "name": "Broken", "chain": [{"effect": "fuzz"}]}')
    p = next(p for p in client.get("/api/presets").json()["presets"] if p["id"] == "broken")
    assert p["valid"] is False and "fuzz" in p["error"]


def test_get_preset(client):
    r = client.get("/api/presets/slapback")
    assert r.status_code == 200
    assert r.json()["preset"]["chain"][1]["effect"] == "delay"


def test_get_missing_and_bad_ids(client):
    assert client.get("/api/presets/nope").status_code == 404
    assert client.get("/api/presets/..%2Fpyproject").status_code == 404
    assert client.get("/api/presets/UPPER").status_code == 404


def test_load_by_id_updates_engine_and_health(client):
    r = client.post("/api/engine/load", json={"preset_id": "long_echo"})
    assert r.status_code == 200
    assert r.json()["loaded"] == {"id": "long_echo", "name": "Long echo", "buffer_size": 128, "modified": False}
    h = client.get("/api/health").json()
    assert h["engine"] == "fake" and h["running"]
    assert h["buffer_size"] == 128
    assert abs(h["latency_ms"] - (2 * 128 / 48000 * 1000 + 1)) < 0.01
    assert 0 < h["dsp_load_pct"] < 100
    assert h["cpu_temp_c"] is not None
    assert client.get("/api/engine").json()["loaded"]["id"] == "long_echo"


def test_load_unsaved_edits(client, presets_dir):
    before = (presets_dir / "slapback.json").read_text()
    r = client.post("/api/engine/load", json={"preset": SLAP, "preset_id": "slapback"})
    assert r.status_code == 200
    assert r.json()["loaded"]["modified"] is True
    assert client.get("/api/health").json()["buffer_size"] == 64
    assert (presets_dir / "slapback.json").read_text() == before  # auditioning never writes


def test_load_rejects_invalid(client):
    bad = SLAP | {"buffer_size": 100}
    r = client.post("/api/engine/load", json={"preset": bad})
    assert r.status_code == 422 and "power of two" in r.json()["detail"]
    assert client.post("/api/engine/load", json={"preset_id": "nope"}).status_code == 404
    assert client.post("/api/engine/load", json={}).status_code == 422
    assert client.post("/api/engine/load", content="not json").status_code == 422
    assert client.get("/api/engine").json()["loaded"]["id"] == "clean"  # unchanged


def test_save_writes_file_in_house_style(client, presets_dir):
    r = client.put("/api/presets/slapback", json={"preset": SLAP})
    assert r.status_code == 200 and r.json()["reloaded"] is False
    text = (presets_dir / "slapback.json").read_text()
    assert json.loads(text) == SLAP
    assert '    {"effect": "gain", "params": {"gain_db": -4}},\n' in text
    assert not list(presets_dir.glob(".*.tmp"))


def test_save_reloads_running_preset(client):
    client.post("/api/engine/load", json={"preset": SLAP, "preset_id": "slapback"})
    r = client.put("/api/presets/slapback", json={"preset": SLAP})
    assert r.json()["reloaded"] is True
    assert client.get("/api/engine").json()["loaded"] == {
        "id": "slapback", "name": "Slapback", "buffer_size": 64, "modified": False
    }


def test_save_rejects_invalid_and_keeps_file(client, presets_dir):
    before = (presets_dir / "slapback.json").read_text()
    bad = json.loads(json.dumps(SLAP))
    bad["chain"][1]["params"]["feedback"] = 120
    r = client.put("/api/presets/slapback", json={"preset": bad})
    assert r.status_code == 422
    assert "feedback = 120 is outside [0, 95]" in r.json()["detail"]
    assert (presets_dir / "slapback.json").read_text() == before


def test_save_rejects_float_ints_and_nan(client):
    r = client.put("/api/presets/slapback", json={"preset": SLAP | {"buffer_size": 64.0}})
    assert r.status_code == 422
    body = json.dumps({"preset": SLAP}).replace('"gain_db": -4', '"gain_db": NaN')
    r = client.put("/api/presets/slapback", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 422


def test_put_does_not_create(client, presets_dir):
    assert client.put("/api/presets/brand_new", json={"preset": SLAP}).status_code == 404
    assert not (presets_dir / "brand_new.json").exists()


def test_save_as_creates_unique_ids(client, presets_dir):
    p = SLAP | {"name": "Big Room!"}
    r1 = client.post("/api/presets", json={"preset": p})
    assert r1.status_code == 201 and r1.json()["id"] == "big_room"
    r2 = client.post("/api/presets", json={"preset": p})
    assert r2.json()["id"] == "big_room_2"
    assert (presets_dir / "big_room.json").exists() and (presets_dir / "big_room_2.json").exists()
    r3 = client.post("/api/presets", json={"preset": SLAP})  # same name as an existing file
    assert r3.json()["id"] == "slapback_2"


def test_saved_files_pass_cpp_check(client, presets_dir, fxrender):
    import subprocess

    client.post("/api/presets", json={"preset": SLAP | {"name": "Check me"}})
    r = subprocess.run([str(fxrender), "--check", str(presets_dir / "check_me.json")])
    assert r.returncode == 0


def test_delete(client, presets_dir):
    assert client.delete("/api/presets/long_echo").status_code == 200
    assert not (presets_dir / "long_echo.json").exists()
    assert client.delete("/api/presets/long_echo").status_code == 404


def test_ui_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "FX Box" in r.text
    assert client.get("/app.js").status_code == 200
    assert client.get("/style.css").status_code == 200


def test_ui_has_no_external_resources():
    """The box runs as its own hotspot with no internet: nothing may load from elsewhere."""
    pattern = re.compile(r"""(https?:)?//[a-z0-9.-]+\.[a-z]{2,}|@import|fonts\.g""", re.I)
    for f in STATIC_DIR.rglob("*"):
        if f.is_file():
            text = f.read_text()
            # The SVG namespace inside the inline data: favicon isn't a fetch.
            text = text.replace("http://www.w3.org/2000/svg", "")
            assert not pattern.search(text), f"{f.name} references an external resource"


def test_responses_are_not_cached(client):
    assert client.get("/app.js").headers["cache-control"] == "no-cache"
    assert client.get("/api/health").headers["cache-control"] == "no-cache"
