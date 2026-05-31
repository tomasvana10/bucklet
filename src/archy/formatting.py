"""Small display helpers shared by the CLI and the TUI."""

from __future__ import annotations

from datetime import datetime

from . import storage

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


def fmt_date(dt: datetime | None) -> str:
    """Format a timestamp in local time, or ``-`` when unknown."""
    if dt is None:
        return "-"
    try:
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return str(dt)[:16]
