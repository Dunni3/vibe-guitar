"""Preset validation and storage.

`validate_preset` mirrors fx::parsePreset in engine/src/preset.cpp rule for rule
(including nlohmann::json's notion of "integer" and the C++ float comparison),
so the UI gets precise errors without a subprocess per edit. The test
`test_validation.py` checks both validators agree on every file in presets/
and on a corpus of broken presets. Before writing a file, PresetStore also runs
the C++ check, so nothing reaches disk that the engine would refuse.
"""

from __future__ import annotations

import json
import math
import os
import re
import struct
from pathlib import Path
from typing import Any, Callable

PRESET_FORMAT = 1
MIN_BUFFER = 16
MAX_BUFFER = 2048
KNOWN_KEYS = ("format", "name", "description", "buffer_size", "chain")
PRESET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class PresetError(ValueError):
    pass


def _reject_constant(name: str) -> Any:
    raise PresetError(f"invalid JSON: {name} is not allowed")


def parse_json(text: str) -> Any:
    """Parse strictly like nlohmann::json: no NaN/Infinity literals."""
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as e:
        raise PresetError(f"invalid JSON: {e}") from e


def _is_int(v: Any) -> bool:  # nlohmann is_number_integer()
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v: Any) -> bool:  # nlohmann is_number()
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _as_float32(v: float) -> float:
    """What `value.get<float>()` yields in C++ (inf on overflow)."""
    try:
        return struct.unpack("f", struct.pack("f", v))[0]
    except OverflowError:
        return math.copysign(math.inf, v)


def _is_power_of_two(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def validate_preset(j: Any, catalog: dict) -> dict:
    """Raise PresetError on the first problem, exactly where the C++ side would."""
    if not isinstance(j, dict):
        raise PresetError("preset must be a JSON object")
    for key in j:
        if key not in KNOWN_KEYS:
            raise PresetError(f"unknown preset key '{key}'")

    if not _is_int(j.get("format")) or j["format"] != PRESET_FORMAT:
        raise PresetError(f"'format' must be {PRESET_FORMAT}")
    if not isinstance(j.get("name"), str) or not j["name"]:
        raise PresetError("'name' must be a non-empty string")
    if "description" in j and not isinstance(j["description"], str):
        raise PresetError("'description' must be a string")

    buf = 128
    if "buffer_size" in j:
        if not _is_int(j["buffer_size"]):
            raise PresetError("'buffer_size' must be an integer")
        buf = j["buffer_size"]
    if not _is_power_of_two(buf) or buf < MIN_BUFFER or buf > MAX_BUFFER:
        raise PresetError(
            f"'buffer_size' must be a power of two in [{MIN_BUFFER}, {MAX_BUFFER}], got {buf}"
        )

    if not isinstance(j.get("chain"), list):
        raise PresetError("'chain' must be an array")
    effects = {e["id"]: e for e in catalog["effects"]}
    for i, slot in enumerate(j["chain"]):
        where = f"chain[{i}]"
        if not isinstance(slot, dict):
            raise PresetError(f"{where} must be an object")
        for key in slot:
            if key not in ("effect", "params"):
                raise PresetError(f"{where}: unknown key '{key}'")
        if not isinstance(slot.get("effect"), str):
            raise PresetError(f"{where}: 'effect' must be a string")
        info = effects.get(slot["effect"])
        if info is None:
            raise PresetError(f"{where}: unknown effect '{slot['effect']}'")
        if "params" in slot:
            if not isinstance(slot["params"], dict):
                raise PresetError(f"{where}: 'params' must be an object")
            specs = {p["id"]: p for p in info["params"]}
            for pid, value in slot["params"].items():
                spec = specs.get(pid)
                if spec is None:
                    raise PresetError(f"{where}: effect '{slot['effect']}' has no param '{pid}'")
                if not _is_number(value):
                    raise PresetError(f"{where}.{pid} must be a number")
                v = _as_float32(value)
                if not (spec["min"] <= v <= spec["max"]):
                    raise PresetError(
                        f"{where}.{pid} = {value:g} is outside [{spec['min']:g}, {spec['max']:g}]"
                    )
    return j


def _fmt_number(v: float | int) -> str:
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    return json.dumps(v)


def dump_preset(p: dict) -> str:
    """Serialize in the same hand-written style as the files in presets/."""
    lines = ["{", f'  "format": {PRESET_FORMAT},', f'  "name": {json.dumps(p["name"])},']
    if "description" in p:
        lines.append(f'  "description": {json.dumps(p["description"])},')
    lines.append(f'  "buffer_size": {p.get("buffer_size", 128)},')
    slots = []
    for s in p["chain"]:
        params = ", ".join(
            f"{json.dumps(k)}: {_fmt_number(v)}" for k, v in s.get("params", {}).items()
        )
        slots.append(f'    {{"effect": {json.dumps(s["effect"])}, "params": {{{params}}}}}')
    if slots:
        lines.append('  "chain": [')
        lines.append(",\n".join(slots))
        lines.append("  ]")
    else:
        lines.append('  "chain": []')
    lines.append("}")
    return "\n".join(lines) + "\n"


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:64]
    return s or "preset"


class PresetStore:
    """Preset files in one directory; the id is the filename without `.json`."""

    def __init__(
        self,
        directory: Path,
        catalog: dict,
        cpp_check: Callable[[str], str | None] | None = None,
    ):
        self.dir = Path(directory)
        self.catalog = catalog
        self.cpp_check = cpp_check

    def path(self, preset_id: str) -> Path:
        if not PRESET_ID_RE.match(preset_id):
            raise KeyError(preset_id)
        return self.dir / f"{preset_id}.json"

    def exists(self, preset_id: str) -> bool:
        try:
            return self.path(preset_id).is_file()
        except KeyError:
            return False

    def list(self) -> list[dict]:
        out = []
        for f in sorted(self.dir.glob("*.json")):
            if not PRESET_ID_RE.match(f.stem):
                continue
            entry: dict[str, Any] = {"id": f.stem}
            try:
                p = self.get(f.stem)
                entry |= {
                    "name": p["name"],
                    "description": p.get("description", ""),
                    "buffer_size": p.get("buffer_size", 128),
                    "effects": [s["effect"] for s in p["chain"]],
                    "valid": True,
                }
            except PresetError as e:
                entry |= {"name": f.stem, "valid": False, "error": str(e)}
            out.append(entry)
        return sorted(out, key=lambda e: e["name"].lower())

    def get(self, preset_id: str) -> dict:
        path = self.path(preset_id)
        if not path.is_file():
            raise KeyError(preset_id)
        return self.parse(path.read_text(encoding="utf-8-sig"))

    def parse(self, text: str) -> dict:
        return validate_preset(parse_json(text), self.catalog)

    def parse_obj(self, obj: Any) -> dict:
        return validate_preset(obj, self.catalog)

    def save(self, preset_id: str, preset: dict) -> dict:
        validate_preset(preset, self.catalog)
        text = dump_preset(preset)
        if self.cpp_check is not None:
            err = self.cpp_check(text)
            if err:
                raise PresetError(err)
        path = self.path(preset_id)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)  # atomic: a crash never leaves a half-written preset
        return self.parse(text)

    def new_id(self, name: str) -> str:
        base = slugify(name)
        if not self.exists(base):
            return base
        n = 2
        while self.exists(f"{base[:60]}_{n}"):
            n += 1
        return f"{base[:60]}_{n}"

    def delete(self, preset_id: str) -> None:
        path = self.path(preset_id)
        if not path.is_file():
            raise KeyError(preset_id)
        path.unlink()
