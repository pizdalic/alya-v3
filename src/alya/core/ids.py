"""ULID: 48 бит времени (мс) + 80 бит случайности, Crockford Base32, сортируется по времени."""

from __future__ import annotations

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(ts_ms: int | None = None) -> str:
    ms = int(time.time() * 1000) if ts_ms is None else ts_ms
    if not 0 <= ms < 1 << 48:
        raise ValueError("время вне диапазона ULID")
    value = (ms << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))
