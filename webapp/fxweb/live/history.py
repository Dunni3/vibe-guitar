"""The last N seconds of the live signal, in memory.

Serves three readers: the analysis thread (latest few thousand frames), the
"save the last 30 s" button, and nothing else. Positions are absolute frame
counts since the source started, so readers can ask for exact ranges.
"""

from __future__ import annotations

import threading

import numpy as np


class History:
    def __init__(self, channels: int, capacity: int):
        self.channels = channels
        self.capacity = capacity
        self._buf = np.zeros((channels, capacity), np.float32)
        self._pos = 0
        self._lock = threading.Lock()

    @property
    def pos(self) -> int:
        return self._pos

    def write(self, block: np.ndarray) -> None:
        n = block.shape[1]
        if n == 0:
            return
        if n > self.capacity:
            block = block[:, -self.capacity:]
            skip = n - self.capacity
        else:
            skip = 0
        with self._lock:
            start = (self._pos + skip) % self.capacity
            m = block.shape[1]
            first = min(m, self.capacity - start)
            self._buf[:, start:start + first] = block[:, :first]
            if first < m:
                self._buf[:, : m - first] = block[:, first:]
            self._pos += n

    def oldest(self) -> int:
        return max(0, self._pos - self.capacity)

    def read(self, start: int, end: int) -> np.ndarray:
        """Frames [start, end). Frames before the start of the stream read as silence."""
        with self._lock:
            end = min(end, self._pos)
            if start >= end:
                return np.zeros((self.channels, 0), np.float32)
            if self._pos > self.capacity and start < self._pos - self.capacity:
                raise ValueError("range is older than the history")
            lo = max(start, 0)
            i0, n = lo % self.capacity, end - lo
            if i0 + n <= self.capacity:
                data = self._buf[:, i0:i0 + n].copy()
            else:
                data = np.concatenate([self._buf[:, i0:], self._buf[:, : i0 + n - self.capacity]], axis=1)
        if lo == start:
            return data
        out = np.zeros((self.channels, end - start), np.float32)
        out[:, lo - start:] = data
        return out

    def latest(self, n: int) -> np.ndarray:
        p = self._pos
        return self.read(p - n, p)
