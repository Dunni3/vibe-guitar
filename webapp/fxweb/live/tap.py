"""Reader for the engine's shared-memory signal tap. Layout: engine/include/fx/tap.h.

The writer (the audio thread) never waits for us. We poll `write_pos`, copy the
new frames, then re-read `write_pos`: any frame the writer may have overwritten
while we copied is thrown away and counted as dropped. Frame addresses are
computed from the `write_pos` we read, so on aarch64 the data loads are ordered
after it (address dependency), matching the writer's release store.
"""

from __future__ import annotations

import mmap
import os
import struct
import sys
from pathlib import Path

import numpy as np

MAGIC = b"FXTAP001"
VERSION = 1
HEADER_BYTES = 128
MAX_CHANNELS = 4


def default_tap_path() -> Path:
    if sys.platform.startswith("linux"):
        return Path("/dev/shm/fxbox-tap")
    return Path(os.environ.get("TMPDIR") or "/tmp") / "fxbox-tap"


class TapError(RuntimeError):
    pass


class TapReader:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._mm: mmap.mmap | None = None
        self._ino: int | None = None
        self.channels = 0
        self.sample_rate = 0
        self.capacity = 0
        self.session = 0
        self.names: list[str] = []
        self._data: np.ndarray | None = None
        self._pos_view: np.ndarray | None = None
        self.read_pos = 0
        self.dropped = 0

    # --- mapping ---

    def open(self) -> None:
        self.close()
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            raise TapError(f"no tap at {self.path} (is the engine running?)") from None
        try:
            st = os.fstat(fd)
            if st.st_size < HEADER_BYTES:
                raise TapError(f"{self.path} is too small to be a tap")
            mm = mmap.mmap(fd, st.st_size, prot=mmap.PROT_READ)
        finally:
            os.close(fd)
        if mm[:8] != MAGIC:
            mm.close()
            raise TapError(f"{self.path} is not a tap (bad magic)")
        version, ch, sr, cap = struct.unpack_from("<IIII", mm, 8)
        (session,) = struct.unpack_from("<Q", mm, 24)
        if version != VERSION or not (1 <= ch <= MAX_CHANNELS) or cap & (cap - 1):
            mm.close()
            raise TapError(f"{self.path}: unsupported tap (version {version}, {ch} ch, cap {cap})")
        if st.st_size < HEADER_BYTES + cap * ch * 4:
            mm.close()
            raise TapError(f"{self.path} is truncated")
        self._mm = mm
        self._ino = st.st_ino
        self.channels, self.sample_rate, self.capacity, self.session = ch, sr, cap, session
        self.names = [
            mm[64 + 16 * c: 80 + 16 * c].split(b"\0", 1)[0].decode("utf-8", "replace") or f"ch{c + 1}"
            for c in range(ch)
        ]
        self._pos_view = np.frombuffer(mm, dtype="<u8", count=1, offset=32)
        self._data = np.frombuffer(mm, dtype="<f4", count=cap * ch, offset=HEADER_BYTES).reshape(cap, ch)
        self.read_pos = self.write_pos()  # start live, not from the old contents

    def close(self) -> None:
        self._data = None
        self._pos_view = None
        if self._mm is not None:
            try:
                self._mm.close()
            except BufferError:
                pass  # a numpy view is still alive somewhere; the GC will unmap it
        self._mm = None
        self._ino = None

    @property
    def is_open(self) -> bool:
        return self._mm is not None

    def replaced(self) -> bool:
        """True if a new writer has put a new file at the path (it renames into place)."""
        try:
            return os.stat(self.path).st_ino != self._ino
        except FileNotFoundError:
            return False  # writer gone; keep the old mapping, it just stops advancing

    def write_pos(self) -> int:
        assert self._pos_view is not None
        return int(self._pos_view[0])

    # --- reading ---

    def read(self, max_frames: int | None = None) -> np.ndarray:
        """New frames since the last call, as planar float32 (channels, n). May be empty."""
        assert self._data is not None
        cap = self.capacity
        w = self.write_pos()
        if w < self.read_pos:  # writer restarted in place
            self.read_pos = w
        margin = cap // 8  # don't read right behind the writer's tail
        if w - self.read_pos > cap - margin:
            new_start = w - (cap - margin)
            self.dropped += new_start - self.read_pos
            self.read_pos = new_start
        end = w if max_frames is None else min(w, self.read_pos + max_frames)
        n = end - self.read_pos
        if n <= 0:
            return np.zeros((self.channels, 0), np.float32)
        i0 = self.read_pos & (cap - 1)
        i1 = i0 + n
        if i1 <= cap:
            out = self._data[i0:i1].copy()
        else:
            out = np.concatenate([self._data[i0:], self._data[: i1 - cap]])
        # Anything the writer lapped while we copied is garbage.
        w2 = self.write_pos()
        oldest_valid = w2 - cap
        if oldest_valid > self.read_pos:
            lost = min(n, oldest_valid - self.read_pos)
            self.dropped += lost
            out = out[lost:]
        self.read_pos = end
        return np.ascontiguousarray(out.T)


class TapWriterPy:
    """Python writer with the same layout, for tests and simulations only (not real-time safe)."""

    def __init__(self, path: Path, channels: int, sample_rate: int, names: list[str], capacity: int = 1 << 14):
        assert capacity & (capacity - 1) == 0
        self.path = Path(path)
        self.channels, self.capacity = channels, capacity
        size = HEADER_BYTES + capacity * channels * 4
        tmp = self.path.with_name(self.path.name + f".new.{os.getpid()}")
        with open(tmp, "wb") as f:
            f.truncate(size)
        fd = os.open(tmp, os.O_RDWR)
        self._mm = mmap.mmap(fd, size)
        os.close(fd)
        self._mm[:8] = MAGIC
        struct.pack_into("<IIIIQ", self._mm, 8, VERSION, channels, sample_rate, capacity, os.getpid())
        for c, name in enumerate(names[:MAX_CHANNELS]):
            b = name.encode()[:15]
            self._mm[64 + 16 * c: 64 + 16 * c + len(b)] = b
        self._data = np.frombuffer(self._mm, dtype="<f4", count=capacity * channels, offset=HEADER_BYTES).reshape(capacity, channels)
        self.pos = 0
        os.replace(tmp, self.path)

    def write(self, block: np.ndarray) -> None:
        block = np.atleast_2d(block)
        n = block.shape[1]
        idx = (self.pos + np.arange(n)) & (self.capacity - 1)
        self._data[idx] = block.T
        self.pos += n
        struct.pack_into("<Q", self._mm, 32, self.pos)

    def close(self) -> None:
        self._data = None
        self._mm.close()
