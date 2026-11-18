"""The effect catalog (ids, params, ranges, defaults) comes from the C++ side.

`fxrender --list-effects` is the single source of truth, so adding an effect in
C++ and rebuilding is all the web app needs. We also use `fxrender --check` as
the authoritative validator before writing a preset to disk.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FXRENDER = REPO_ROOT / "build" / "fxrender"


class CatalogError(RuntimeError):
    pass


def find_fxrender(path: str | os.PathLike | None = None) -> Path:
    p = Path(path or os.environ.get("FX_FXRENDER") or DEFAULT_FXRENDER)
    if not p.is_file() or not os.access(p, os.X_OK):
        raise CatalogError(f"fxrender not found at {p}; run `make build` first (or pass --fxrender)")
    return p


def load_catalog(fxrender: Path) -> dict:
    try:
        out = subprocess.run(
            [str(fxrender), "--list-effects"], capture_output=True, text=True, check=True, timeout=10
        ).stdout
        catalog = json.loads(out)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as e:
        raise CatalogError(f"could not read the effect catalog from {fxrender}: {e}") from e
    if not isinstance(catalog.get("effects"), list):
        raise CatalogError(f"{fxrender} --list-effects returned no 'effects' list")
    return catalog


def cpp_check_text(fxrender: Path, text: str) -> str | None:
    """Run `fxrender --check` on preset text. Returns None if OK, else the error."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write(text)
        tmp = f.name
    try:
        r = subprocess.run(
            [str(fxrender), "--check", tmp], capture_output=True, text=True, timeout=10
        )
    finally:
        os.unlink(tmp)
    if r.returncode == 0:
        return None
    msg = (r.stdout + r.stderr).strip().removeprefix("FAIL").strip()
    return msg.replace(tmp + ": ", "") or "rejected by fxrender --check"
