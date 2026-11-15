"""32-bit float WAV writing (the stdlib wave module only does integer PCM)."""

import struct
from pathlib import Path

import numpy as np


def write_wav_float(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    """Write mono (n,) or planar (channels, n) float audio as 32-bit float WAV."""
    a = np.atleast_2d(np.asarray(audio, dtype=np.float32))
    channels, frames = a.shape
    data = a.T.astype("<f4").tobytes()  # interleave
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    fmt = struct.pack("<HHIIHH", 3, channels, sample_rate, sample_rate * channels * 4, channels * 4, 32)
    header += b"fmt " + struct.pack("<I", len(fmt)) + fmt
    header += b"data" + struct.pack("<I", len(data))
    Path(path).write_bytes(header + data)
