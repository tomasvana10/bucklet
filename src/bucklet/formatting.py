"""Small display helpers shared by the CLI and the TUI."""

from __future__ import annotations

import re
from datetime import datetime

from . import storage
from .errors import BuckletError

# Binary (1024-based) units, matching how ``human`` labels sizes. Both "MB" and
# "MiB" are accepted and treated as 2**20, so what you type round-trips with
# what you see.
_SIZE_UNITS = {
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "KIB": 1024,
    "M": 1024**2,
    "MB": 1024**2,
    "MIB": 1024**2,
    "G": 1024**3,
    "GB": 1024**3,
    "GIB": 1024**3,
    "T": 1024**4,
    "TB": 1024**4,
    "TIB": 1024**4,
}

# Rich/Textual style names per object state (used by the TUI and rich output).
STATE_STYLE = {
    storage.AVAILABLE: "green",
    storage.COLD: "blue",
    storage.THAWING: "yellow",
    storage.THAWED: "green",
    storage.ERROR: "red",
    storage.UNKNOWN: "dim",
}


def human(num: float | int) -> str:
    """Format a byte count like ``4.2MB``."""
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024:
            return f"{int(value)}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}EB"


def parse_size(text: str) -> int:
    """Parse a human size like ``64MB`` (or plain bytes) into an int.

    Raises :class:`BuckletError` for anything unparseable or non-positive, so
    callers can surface a clean message. The inverse of :func:`human`.
    """
    m = re.fullmatch(r"\s*(\d*\.?\d+)\s*([A-Za-z]*)\s*", text or "")
    if not m:
        raise BuckletError(f"bad size: {text!r} (try e.g. 8MB, 256MiB, or a byte count)")
    unit = (m.group(2) or "B").upper()
    if unit not in _SIZE_UNITS:
        raise BuckletError(f"unknown size unit in {text!r} (use B, KB, MB, GB, TB)")
    value = int(float(m.group(1)) * _SIZE_UNITS[unit])
    if value <= 0:
        raise BuckletError(f"size must be positive: {text!r}")
    return value


def parse_count(text: str) -> int:
    """Parse a positive whole number (a concurrency/count setting)."""
    try:
        value = int((text or "").strip())
    except ValueError as exc:
        raise BuckletError(f"expected a whole number, got {text!r}") from exc
    if value <= 0:
        raise BuckletError(f"must be positive: {text!r}")
    return value


def fmt_date(dt: datetime | None) -> str:
    """Format a timestamp in local time, or ``-`` when unknown."""
    if dt is None:
        return "-"
    try:
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return str(dt)[:16]
