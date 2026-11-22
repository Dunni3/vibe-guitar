"""WAV I/O for recordings: 24-bit PCM out (plays in every browser, lossless for
the Pisound's 24-bit converters), and a reader for 16/24/32-bit PCM and float.

`StreamingWavWriter` rewrites the header sizes about once a second, so a crash
or power cut mid-take leaves a playable file with everything up to then.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import BinaryIO

import numpy as np

PCM24_MAX = 8388607
FRAME_HEADER = 44


def _header(channels: int, sample_rate: int, data_bytes: int, bits: int = 24, fmt_tag: int = 1) -> bytes:
    block_align = channels * bits // 8
    fmt = struct.pack("<HHIIHH", fmt_tag, channels, sample_rate, sample_rate * block_align, block_align, bits)
    return (b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVE"
            + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", data_bytes))


def to_pcm24(block: np.ndarray) -> bytes:
    """Planar float (channels, n) -> interleaved little-endian 24-bit bytes. Clips at full scale."""
    x = np.asarray(block, dtype=np.float32).T  # (n, ch)
    ints = np.clip(np.rint(x * 8388608.0), -8388608, PCM24_MAX).astype("<i4")
    return ints.reshape(-1, 1).view(np.uint8)[:, :3].tobytes()


class StreamingWavWriter:
    def __init__(self, path: Path, channels: int, sample_rate: int):
        self.path = Path(path)
        self.channels, self.sample_rate = channels, sample_rate
        self.frames = 0
        self._f: BinaryIO = open(self.path, "wb")
        self._f.write(_header(channels, sample_rate, 0))

    @property
    def data_bytes(self) -> int:
        return self.frames * self.channels * 3

    def write(self, block: np.ndarray) -> None:
        self._f.write(to_pcm24(block))
        self.frames += block.shape[1]

    def patch_header(self) -> None:
        self._f.flush()
        pos = self._f.tell()
        self._f.seek(0)
        self._f.write(_header(self.channels, self.sample_rate, self.data_bytes))
        self._f.seek(pos)
        self._f.flush()

    def close(self) -> None:
        if self._f.closed:
            return
        self.patch_header()
        self._f.close()


def write_wav_pcm24(path: Path, data: np.ndarray, sample_rate: int) -> None:
    data = np.atleast_2d(data)
    w = StreamingWavWriter(path, data.shape[0], sample_rate)
    w.write(data)
    w.close()


def wav_info(path: Path, trust_file_size: bool = False) -> dict:
    """Format and frame count. An unpatched (zero) or impossible data size falls back to
    the file size; trust_file_size=True always does (recovering our own cut-off takes,
    whose header may be a second stale)."""
    with open(path, "rb") as f:
        head = f.read(12)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError(f"{path} is not a WAV file")
        fmt = None
        while True:
            ck = f.read(8)
            if len(ck) < 8:
                raise ValueError(f"{path}: no data chunk")
            cid, size = ck[:4], struct.unpack("<I", ck[4:])[0]
            if cid == b"fmt ":
                raw = f.read(size)
                tag, ch, sr, _, align, bits = struct.unpack("<HHIIHH", raw[:16])
                if tag == 0xFFFE and len(raw) >= 26:
                    tag = struct.unpack("<H", raw[24:26])[0]
                fmt = {"tag": tag, "channels": ch, "sample_rate": sr, "block_align": align, "bits": bits}
                if size % 2:
                    f.read(1)
            elif cid == b"data":
                if fmt is None:
                    raise ValueError(f"{path}: data before fmt")
                offset = f.tell()
                f.seek(0, 2)
                avail = f.tell() - offset
                if trust_file_size or size == 0 or size > avail:
                    size = avail
                fmt["data_offset"] = offset
                fmt["frames"] = size // fmt["block_align"]
                return fmt
            else:
                f.seek(size + (size % 2), 1)


def read_wav(path: Path) -> tuple[int, np.ndarray]:
    """(sample_rate, planar float32 (channels, n))."""
    info = wav_info(path)
    ch, bits, tag, n = info["channels"], info["bits"], info["tag"], info["frames"]
    with open(path, "rb") as f:
        f.seek(info["data_offset"])
        raw = f.read(n * info["block_align"])
    if tag == 3 and bits == 32:
        x = np.frombuffer(raw, "<f4").astype(np.float32)
    elif tag == 1 and bits == 16:
        x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    elif tag == 1 and bits == 24:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3)
        i = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
        i = np.where(i & 0x800000, i - 0x1000000, i)
        x = i.astype(np.float32) / 8388608.0
    elif tag == 1 and bits == 32:
        x = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"{path}: unsupported WAV format (tag {tag}, {bits} bit)")
    return info["sample_rate"], np.ascontiguousarray(x.reshape(-1, ch).T)
