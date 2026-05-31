"""Plain data types passed between the core and the front-ends."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import storage

# Defaults for the S3 transfer knobs a profile may override. A profile leaves a
# knob as ``None`` to mean "use the default", which is what lets each one be
# reset individually. ``part_concurrency`` is boto3's own per-file part pool;
# ``upload_concurrency`` is how many files bucklet uploads at once.
DEFAULT_MULTIPART_THRESHOLD = 8 * 1024 * 1024  # 8 MiB: above this, multipart
DEFAULT_MULTIPART_CHUNKSIZE = 256 * 1024 * 1024  # 256 MiB per part
DEFAULT_UPLOAD_CONCURRENCY = 4  # files uploaded in parallel
DEFAULT_PART_CONCURRENCY = 10  # boto3's TransferConfig.max_concurrency default


@dataclass(frozen=True)
class Tunable:
    """Metadata for one tunable transfer setting (drives the CLI and TUI)."""

    key: str  # the Profile attribute / stored-config key
    label: str  # human description
    default: int  # value used when the profile leaves it unset
    is_size: bool  # bytes (shown/parsed as a size) vs a plain count


# Single source of truth for the tunable settings. Both `profile tune` and the
# TUI settings screen iterate this, so adding a knob here surfaces it in both.
TUNABLES: tuple[Tunable, ...] = (
    Tunable("multipart_threshold", "multipart threshold", DEFAULT_MULTIPART_THRESHOLD, True),
    Tunable("multipart_chunksize", "multipart chunk size", DEFAULT_MULTIPART_CHUNKSIZE, True),
    Tunable("upload_concurrency", "parallel uploads", DEFAULT_UPLOAD_CONCURRENCY, False),
    Tunable("max_concurrency", "parts per file", DEFAULT_PART_CONCURRENCY, False),
)


@dataclass(frozen=True)
class Tuning:
    """A profile's transfer settings with every default filled in."""

    multipart_threshold: int
    multipart_chunksize: int
    upload_concurrency: int
    part_concurrency: int


@dataclass
class Profile:
    """A resolved bucket profile: everything needed to talk to one bucket.

    ``storage_class`` is only the *default* class used for uploads from this
    profile; any upload may override it. Credentials may be left blank, in
    which case boto3 falls back to its standard chain (env / ~/.aws / role).
    The ``multipart_*``/``*_concurrency`` knobs are per-profile transfer tuning;
    each is ``None`` to use the shared default (see :data:`TUNABLES`).
    """

    name: str
    bucket: str | None = None
    region: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    rclone_remote: str | None = None
    endpoint_url: str | None = None
    storage_class: str = storage.DEFAULT_STORAGE_CLASS
    multipart_threshold: int | None = None
    multipart_chunksize: int | None = None
    upload_concurrency: int | None = None
    max_concurrency: int | None = None

    @property
    def credential_source(self) -> str:
        """Human label for where this profile's credentials come from."""
        if self.access_key_id and self.secret_access_key:
            return "keys"
        if self.rclone_remote:
            return f"rclone:{self.rclone_remote}"
        return "aws-chain"

    @property
    def has_explicit_keys(self) -> bool:
        return bool(self.access_key_id and self.secret_access_key)

    @property
    def tuning(self) -> Tuning:
        """Transfer settings with defaults applied for any unset knob.

        A knob counts as "set" only when it's a positive int. None (the normal
        unset case) and any out-of-range value that slipped into the config by
        hand (0, negative, non-int) fall back to the default, so .tuning is
        always sane to hand to boto3.
        """

        def positive(value: int | None, default: int) -> int:
            return value if isinstance(value, int) and value > 0 else default

        return Tuning(
            multipart_threshold=positive(self.multipart_threshold, DEFAULT_MULTIPART_THRESHOLD),
            multipart_chunksize=positive(self.multipart_chunksize, DEFAULT_MULTIPART_CHUNKSIZE),
            upload_concurrency=positive(self.upload_concurrency, DEFAULT_UPLOAD_CONCURRENCY),
            part_concurrency=positive(self.max_concurrency, DEFAULT_PART_CONCURRENCY),
        )


@dataclass
class ObjectInfo:
    """A bucket object as returned by a listing (no per-object HEAD)."""

    key: str
    size: int
    last_modified: datetime | None
    storage_class: str = "STANDARD"

    @property
    def baseline_state(self) -> str:
        """State inferable from the listing alone (no ``Restore`` header)."""
        return storage.object_state(self.storage_class, None)


@dataclass
class ObjectStatus:
    """The detailed status of a single object, from ``head_object``."""

    key: str
    state: str
    storage_class: str = "STANDARD"
    size: int | None = None
    last_modified: datetime | None = None
    restore_expiry: str | None = None
    error: str | None = None

    @property
    def label(self) -> str:
        return storage.STATE_LABEL.get(self.state, "?")

    @property
    def can_download(self) -> bool:
        return storage.can_download(self.state)

    @property
    def can_thaw(self) -> bool:
        return storage.can_thaw(self.state)


@dataclass
class KeyResolution:
    """Result of expanding key patterns/globs against a bucket listing."""

    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
