"""Plain data types passed between the core and the front-ends."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import storage


@dataclass
class Profile:
    """A resolved bucket profile: everything needed to talk to one bucket.

    ``storage_class`` is only the *default* class used for uploads from this
    profile; any upload may override it. Credentials may be left blank, in
    which case boto3 falls back to its standard chain (env / ~/.aws / role).
    """

    name: str
    bucket: str | None = None
    region: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    rclone_remote: str | None = None
    endpoint_url: str | None = None
    storage_class: str = storage.DEFAULT_STORAGE_CLASS

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
