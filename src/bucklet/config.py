"""Saved profiles and the default-profile selection.

The config is a small JSON file living in the user config directory
(``<config dir>/config.json``)::

    {
      "version": 2,
      "default": "cold",
      "profiles": {
        "cold": {"bucket": "...", "region": "...", "storage_class": "DEEP_ARCHIVE",
                 "rclone_remote": "...", "endpoint_url": null,
                 "access_key_id": null, "secret_access_key": null,
                 "multipart_chunksize": 67108864, "view": "flat"}
      }
    }

:class:`Config` is the only thing the front-ends touch. It turns the stored
dicts into fully resolved :class:`~bucklet.models.Profile` objects, with
credentials filled in from rclone or the environment, and writes changes back
atomically.

The file carries a ``version``. Older files written before versioning had no
such key; they are the original layout, which we call v1, so a missing version
*is* v1. :func:`_migrate` upgrades an older file to the current shape on load
(see its docstring for how to add the next version).
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

# Bump when the stored shape changes, and add a step to _migrate().
CONFIG_VERSION = 2

# Persisted profile keys. Everything else on Profile is derived at runtime.
_STORED_KEYS = (
    "bucket",
    "region",
    "access_key_id",
    "secret_access_key",
    "rclone_remote",
    "endpoint_url",
    "storage_class",
    "multipart_threshold",
    "multipart_chunksize",
    "upload_concurrency",
    "max_concurrency",
    "view",
)


def _migrate(data: dict) -> bool:
    """Upgrade a parsed config dict in place to :data:`CONFIG_VERSION`.

    Migrations run one version at a time, so growing the format is just adding
    the next ``if`` below. For example, to introduce v3::

        if version < 3:
            for prof in data.get("profiles", {}).values():
                prof["something"] = ...      # reshape each v2 profile
            version = 3

    A file with no ``version`` predates versioning and is treated as v1 (the
    same shape), so the only change there is stamping the version. A file from a
    *newer* bucklet is refused rather than silently downgraded. Returns True if
    anything changed, so the caller can persist the upgrade.
    """
    raw = data.get("version", 1)  # a file with no version is the original == v1
    version = raw if isinstance(raw, int) and raw >= 1 else 1
    # Anything that wasn't already a clean version int needs writing back.
    needs_persist = "version" not in data or raw != version
    if version > CONFIG_VERSION:
        raise BuckletError(
            f"config version {version} is newer than this bucklet understands "
            f"(v{CONFIG_VERSION}). Upgrade bucklet to use it"
        )
    start = version

    # --- migration steps (append the next one here) ------------------------
    if version < 2:
        # v2 remembers the TUI's flat/tree view per profile. Existing profiles
        # have never had one, so they start on the flat table.
        for prof in data.get("profiles", {}).values():
            if isinstance(prof, dict):
                prof.setdefault("view", "flat")
        version = 2
    # -----------------------------------------------------------------------

    version = CONFIG_VERSION
    data["version"] = version
    return needs_persist or (version != start)


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
        changed = _migrate(data)  # raises for a config from a newer bucklet
        profiles = data.get("profiles") or {}
        if not isinstance(profiles, dict):
            profiles = {}
        cfg = cls(path, profiles=profiles, default=data.get("default"))
        if changed:
            # Make the upgrade durable, but never fail a load over it (the dir
            # may be read-only); the next explicit save will catch up regardless.
            try:
                cfg.save()
            except OSError:
                pass
        return cfg

    def save(self):
        """Write the config back atomically, readable only by its owner.

        The file can hold secret keys, so the temp file is created 0600 from the
        start (never group- or world-readable, even briefly) and only then
        renamed into place.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"version": CONFIG_VERSION, "default": self.default, "profiles": self.profiles},
            indent=2,
        )
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
        view = stored.get("view")
        prof = Profile(
            name=name,
            bucket=stored.get("bucket"),
            region=stored.get("region"),
            access_key_id=stored.get("access_key_id"),
            secret_access_key=stored.get("secret_access_key"),
            rclone_remote=stored.get("rclone_remote"),
            endpoint_url=stored.get("endpoint_url"),
            storage_class=(stored.get("storage_class") or storage.DEFAULT_STORAGE_CLASS),
            multipart_threshold=stored.get("multipart_threshold"),
            multipart_chunksize=stored.get("multipart_chunksize"),
            upload_concurrency=stored.get("upload_concurrency"),
            max_concurrency=stored.get("max_concurrency"),
            # Anything a hand-edit left that isn't a known view falls back to flat.
            view=view if view in ("flat", "tree") else "flat",
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
