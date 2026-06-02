"""Small display helpers shared by the CLI and the TUI."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

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


def thaw_remaining(expiry: str | None, *, now: datetime | None = None) -> str | None:
    """How long a thawed copy has left before S3 lets it lapse back to cold.

    Parses the S3 ``expiry-date`` (an HTTP date such as
    ``Fri, 21 Dec 2012 00:00:00 GMT``) and returns the gap from now to it as a
    single largest unit: ``2d``, ``5h``, ``50m``, or ``<1m`` when it's nearly up.
    Returns None when there's no expiry, it can't be parsed, or it has already
    passed. ``now`` is injectable so the conversion is testable.
    """
    if not expiry:
        return None
    try:
        when = parsedate_to_datetime(expiry)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    # S3 stamps the expiry in GMT; a header missing the zone is still UTC.
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    seconds = (when - current).total_seconds()
    if seconds <= 0:
        return None
    if seconds >= 86400:
        return f"{int(seconds // 86400)}d"
    if seconds >= 3600:
        return f"{int(seconds // 3600)}h"
    if seconds >= 60:
        return f"{int(seconds // 60)}m"
    return "<1m"
