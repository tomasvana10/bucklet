"""Saved profiles and the default-profile selection.

The config is a small JSON file living in the user config directory
(``<config dir>/config.json``)::

    {
      "default": "cold",
      "profiles": {
        "cold": {"bucket": "...", "region": "...", "storage_class": "DEEP_ARCHIVE",
                 "rclone_remote": "...", "endpoint_url": null,
                 "access_key_id": null, "secret_access_key": null}
      }
    }

:class:`Config` is the only thing the front-ends touch. It turns the stored
dicts into fully resolved :class:`~bucklet.models.Profile` objects, with
credentials filled in from rclone or the environment, and writes changes back
atomically.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from platformdirs import user_config_dir

from . import storage
from .errors import BuckletError
from .models import Profile
from .rclone import creds_from_rclone

# Persisted profile keys. Everything else on Profile is derived at runtime.
_STORED_KEYS = (
    "bucket",
    "region",
    "access_key_id",
    "secret_access_key",
    "rclone_remote",
    "endpoint_url",
    "storage_class",
)


def default_config_dir() -> Path:
    """bucklet's config directory, overridable with ``$BUCKLET_CONFIG_DIR``.

    Without the override this is the platform's standard per-user config
    location (``~/.config/bucklet`` on Linux, the equivalent elsewhere).
    """
    override = os.environ.get("BUCKLET_CONFIG_DIR")
    return Path(override) if override else Path(user_config_dir("bucklet"))


class Config:
    """In-memory view of the config file, with profile CRUD and resolution."""

    def __init__(
        self,
        path: Path,
        profiles: dict[str, dict] | None = None,
        default: str | None = None,
    ):
        self.path = Path(path)
        self.profiles: dict[str, dict] = profiles or {}
        self.default: str | None = default

    @classmethod
    def load(cls, config_dir: Path | None = None):
        """Load the config from ``config_dir`` (or the default location)."""
        directory = Path(config_dir) if config_dir else default_config_dir()
        path = directory / "config.json"
        if path.exists():
            return cls._read(path)
        return cls(path)

    @classmethod
    def _read(cls, path: Path):
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return cls(path)
        if not isinstance(data, dict):
            return cls(path)
        profiles = data.get("profiles") or {}
        if not isinstance(profiles, dict):
            profiles = {}
        return cls(path, profiles=profiles, default=data.get("default"))

    def save(self):
        """Write the config back atomically, readable only by its owner.

        The file can hold secret keys, so the temp file is created 0600 from the
        start (never group- or world-readable, even briefly) and only then
        renamed into place.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"default": self.default, "profiles": self.profiles}, indent=2)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)  # pin 0600 regardless of umask
        except OSError:
            pass  # the O_CREAT mode already kept it owner-only at worst
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
        os.replace(tmp, self.path)

    def names(self):
        return list(self.profiles)

    def has(self, name: str):
        return name in self.profiles

    def stored(self, name: str):
        if name not in self.profiles:
            raise BuckletError(f"no such profile: {name}")
        return self.profiles[name]

    def materialize(self, name: str, stored: dict):
        """Turn a stored dict into a fully resolved Profile."""
        prof = Profile(
            name=name,
            bucket=stored.get("bucket"),
            region=stored.get("region"),
            access_key_id=stored.get("access_key_id"),
            secret_access_key=stored.get("secret_access_key"),
            rclone_remote=stored.get("rclone_remote"),
            endpoint_url=stored.get("endpoint_url"),
            storage_class=(stored.get("storage_class") or storage.DEFAULT_STORAGE_CLASS),
        )
        if not prof.has_explicit_keys and prof.rclone_remote:
            rc = creds_from_rclone(prof.rclone_remote) or {}
            prof.access_key_id = prof.access_key_id or rc.get("access_key_id")
            prof.secret_access_key = prof.secret_access_key or rc.get("secret_access_key")
            prof.region = prof.region or rc.get("region")
            prof.endpoint_url = prof.endpoint_url or rc.get("endpoint_url")
        prof.region = (
            prof.region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        )
        return prof

    def get(self, name: str):
        return self.materialize(name, self.stored(name))

    def resolve(self, profile_arg: str | None) -> Profile | None:
        """Pick the profile to operate on.

        Try an explicit ``profile_arg`` first (a saved name, otherwise treated as
        a raw bucket name using the AWS chain), then the configured default.
        Returns ``None`` when nothing is configured.
        """
        if profile_arg:
            if self.has(profile_arg):
                return self.get(profile_arg)
            # Not a saved profile, so treat it as a raw bucket name.
            return self.materialize(profile_arg, {"bucket": profile_arg})
        if self.default and self.has(self.default):
            return self.get(self.default)
        return None

    def add(self, profile: Profile, *, make_default: bool = False):
        stored = {}
        for key in _STORED_KEYS:
            value = getattr(profile, key, None)
            if value is not None:
                stored[key] = value
        self.profiles[profile.name] = stored
        if make_default or not self.default:
            self.default = profile.name

    def remove(self, name: str):
        if name not in self.profiles:
            raise BuckletError(f"no such profile: {name}")
        del self.profiles[name]
        if self.default == name:
            self.default = next(iter(self.profiles), None)

    def set_default(self, name: str):
        if name not in self.profiles:
            raise BuckletError(f"no such profile: {name}")
        self.default = name
