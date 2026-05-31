"""Storage-class vocabulary and object-state logic.

This module is deliberately free of boto3 and of any UI: it is the single
source of truth for "which classes exist", "which ones need a restore before
download", and "what state is an object in". Everything here is a pure function
so it can be unit-tested without AWS.
"""

from __future__ import annotations

import re

from .errors import ArchyError

# Canonical S3 storage classes archy can upload into, cheapest-retrieval first.
STORAGE_CLASSES: tuple[str, ...] = (
    "STANDARD",
    "REDUCED_REDUNDANCY",
    "STANDARD_IA",
    "ONEZONE_IA",
    "INTELLIGENT_TIERING",
    "GLACIER_IR",
    "GLACIER",
    "DEEP_ARCHIVE",
)

DEFAULT_STORAGE_CLASS = "STANDARD"

# Classes whose objects are offline and must be restored ("thawed") before they
# can be downloaded. GLACIER_IR is *instant* retrieval, so it is NOT here.
RESTORE_REQUIRED: frozenset[str] = frozenset({"GLACIER", "DEEP_ARCHIVE"})

# Classes whose live state cannot be known from a listing alone because the
# object may be archived/restoring/restored and only a HEAD reveals the
# ``Restore`` header. INTELLIGENT_TIERING objects can sit in an archive tier.
RESTORABLE_CLASSES: frozenset[str] = RESTORE_REQUIRED | frozenset({"INTELLIGENT_TIERING"})

# Convenient short aliases accepted on the command line, in addition to the
# canonical names (which are matched case-insensitively with - or _).
_ALIASES = {
    "RR": "REDUCED_REDUNDANCY",
    "IA": "STANDARD_IA",
    "ONEZONE": "ONEZONE_IA",
    "IT": "INTELLIGENT_TIERING",
    "INTELLIGENT": "INTELLIGENT_TIERING",
    "TIERING": "INTELLIGENT_TIERING",
    "IR": "GLACIER_IR",
    "FLEXIBLE": "GLACIER",
    "DEEP": "DEEP_ARCHIVE",
    "DA": "DEEP_ARCHIVE",
    "ARCHIVE": "DEEP_ARCHIVE",
}

# Object states (front-end-neutral identifiers).
AVAILABLE = "available"  # downloadable right now
COLD = "cold"            # archived, needs a restore first
THAWING = "thawing"      # restore in progress
THAWED = "thawed"        # restored, downloadable until it expires
ERROR = "error"          # status could not be read
UNKNOWN = "unknown"      # status not fetched yet

STATES = (AVAILABLE, COLD, THAWING, THAWED, ERROR, UNKNOWN)

# Short labels for tabular display.
STATE_LABEL = {
    AVAILABLE: "avail",
    COLD: "cold",
    THAWING: "thaw>",
    THAWED: "ready",
    ERROR: "err!",
    UNKNOWN: "?",
}


def normalize_storage_class(value: str) -> str:
    """Resolve a user-supplied class name/alias to a canonical S3 class.

    Accepts any case and either '-' or '_' separators, plus the short aliases
    in :data:`_ALIASES`. Raises :class:`ArchyError` for anything unknown.
    """
    if value is None:
        raise ArchyError("no storage class given")
    key = value.strip().upper().replace("-", "_")
    if key in STORAGE_CLASSES:
        return key
    if key in _ALIASES:
        return _ALIASES[key]
    choices = ", ".join(c.lower() for c in STORAGE_CLASSES)
    raise ArchyError(f"unknown storage class {value!r} (choose from: {choices})")


def needs_restore(storage_class: str | None) -> bool:
    """True if an object in this class must be restored before download."""
    return (storage_class or "").upper() in RESTORE_REQUIRED


def object_state(storage_class: str | None, restore_header: str | None) -> str:
    """Map an object's storage class + S3 ``Restore`` header to a state.

    The ``Restore`` header (from ``head_object``) looks like::

        ongoing-request="false", expiry-date="Fri, 21 Dec 2012 00:00:00 GMT"

    A present header always wins (an INTELLIGENT_TIERING object can be in a
    restore too), so we check it before falling back to the class.
    """
    if restore_header:
        if 'ongoing-request="true"' in restore_header:
            return THAWING
        return THAWED
    if needs_restore(storage_class):
        return COLD
    return AVAILABLE


def restore_expiry(restore_header: str | None) -> str | None:
    """Pull the ``expiry-date`` out of a ``Restore`` header, if any."""
    if not restore_header:
        return None
    m = re.search(r'expiry-date="([^"]+)"', restore_header)
    return m.group(1) if m else None


def can_download(state: str) -> bool:
    """Whether an object in this state can be downloaded right now."""
    return state in (AVAILABLE, THAWED)


def can_thaw(state: str) -> bool:
    """Whether starting a restore makes sense for an object in this state."""
    return state == COLD
