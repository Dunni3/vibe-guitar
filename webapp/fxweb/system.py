"""Host metrics that don't depend on the engine backend."""

from __future__ import annotations

from pathlib import Path

THERMAL_ZONE = Path("/sys/class/thermal/thermal_zone0/temp")  # Pi 5: the SoC sensor


def cpu_temp_c(path: Path = THERMAL_ZONE) -> float | None:
    """SoC temperature in °C, or None where there's no sensor (e.g. the Mac)."""
    try:
        return int(path.read_text().strip()) / 1000.0
    except (OSError, ValueError):
        return None
