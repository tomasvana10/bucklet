"""The UI-agnostic core.

A :class:`Service` binds one resolved :class:`~bucklet.models.Profile` to a boto3
client and exposes every operation bucklet can do as plain method calls that
return plain data or raise :class:`~bucklet.errors.BuckletError`. The CLI and the
TUI are both thin layers over this. Whatever one front-end can do, the other can
too, because the capability lives here.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from . import s3, storage
from .errors import BuckletError
from .models import KeyResolution, Profile

if TYPE_CHECKING:
    from botocore.client import BaseClient

# A progress callback receives the number of bytes transferred since the last call.
ProgressCB = Callable[[int], None]

_GLOB_CHARS = set("*?[]")


class Service:
    """High-level operations against one bucket/profile."""

    def __init__(self, profile: Profile, client: BaseClient):
        if not profile.bucket:
            raise BuckletError(f"profile {profile.name!r} has no bucket")
        self.profile = profile
        self.client = client
        self.bucket = profile.bucket

    @classmethod
    def open(cls, profile: Profile, *, validate: bool = True):
        """Build a client for ``profile`` and (optionally) check the bucket."""
        if not profile.bucket:
            raise BuckletError(f"profile {profile.name!r} has no bucket")
        client = s3.build_client(profile)
        if validate:
            s3.validate(client, profile.bucket)
        return cls(profile, client)

    def list_objects(self, prefix: str = ""):
        objs = list(s3.list_objects(self.client, self.bucket, prefix))
        objs.sort(key=lambda o: o.key)
        return objs

    def status(self, key: str):
        return s3.head_status(self.client, self.bucket, key)

    def restore(self, key: str, *, tier: str = "Bulk", days: int = 7):
        """Begin a restore for ``key``.

        Restore only applies to objects whose live class needs it. For anything
        already available this is a no-op that reports why.
        """
        status = self.status(key)
        if status.state == storage.ERROR:
            raise BuckletError(f"{key}: {status.error}")
        if status.state == storage.AVAILABLE:
            return f"already available ({status.storage_class.lower()}), no thaw needed"
        if status.state == storage.THAWING:
            return "restore already in progress"
        if status.state == storage.THAWED:
            until = f" (until {status.restore_expiry})" if status.restore_expiry else ""
            return f"already restored{until}"
        return s3.restore_object(self.client, self.bucket, key, tier=tier, days=days)

    def download(self, key: str, dest: Path, progress: ProgressCB | None = None):
        dest = Path(dest)
        s3.download_file(self.client, self.bucket, key, dest, callback=progress)
        return dest

    def delete(self, key: str):
        """Permanently delete one object.

        Raises :class:`~bucklet.errors.BuckletError` if the object cannot be
        deleted (e.g. the credentials lack ``s3:DeleteObject``). Deletion is
        intentionally not exposed by the CLI; only the TUI offers it, and only
        when launched with ``--allow-deletion``.
        """
        s3.delete_object(self.client, self.bucket, key)
        return f"deleted {key}"

    def resolve_storage_class(self, storage_class: str | None):
        """The class to upload with: an explicit override or the profile default."""
        if storage_class:
            return storage.normalize_storage_class(storage_class)
        return storage.normalize_storage_class(self.profile.storage_class)

    def upload(
        self,
        local_path: str | os.PathLike,
        key: str,
        *,
        storage_class: str | None = None,
        progress: ProgressCB | None = None,
    ):
        """Upload one file, returning the class it was stored in."""
        resolved = self.resolve_storage_class(storage_class)
        s3.upload_file(self.client, self.bucket, Path(local_path), key, resolved, callback=progress)
        return resolved

    @staticmethod
    def plan_upload(paths: Iterable[str | os.PathLike], prefix: str = ""):
        """Expand paths into (local_file, key) pairs.

        Keys mirror each file's absolute path with the leading slash stripped,
        optionally under ``prefix``. Directories are walked recursively.
        """
        prefix = prefix.strip("/")
        plan: list[tuple[Path, str]] = []
        for raw in paths:
            real = Path(os.path.realpath(raw))
            if not real.exists():
                raise BuckletError(f"not found: {raw}")
            if real.is_dir():
                for root, _dirs, names in os.walk(real):
                    for name in sorted(names):
                        full = Path(root) / name
                        plan.append((full, _mirror_key(full, prefix)))
            else:
                plan.append((real, _mirror_key(real, prefix)))
        return plan

    def resolve_keys(self, patterns: Iterable[str]):
        """Expand exact keys and globs against the current listing."""
        keys = [o.key for o in self.list_objects()]
        keyset = set(keys)
        result = KeyResolution()
        seen: set[str] = set()
        for raw in patterns:
            pat = raw.lstrip("/")
            if pat in keyset:
                # An exact key wins even when it contains glob metacharacters
                # (S3 keys may legitimately contain * ? [ ]).
                candidates = [pat]
            elif _GLOB_CHARS & set(pat):
                hits = [k for k in keys if fnmatch.fnmatch(k, pat)]
                if not hits:
                    result.missing.append(raw)
                candidates = hits
            else:
                candidates = []
                result.missing.append(raw)
            for k in candidates:
                if k not in seen:
                    seen.add(k)
                    result.matched.append(k)
        return result


def _mirror_key(path: Path, prefix: str):
    key = str(path).lstrip("/")
    return f"{prefix}/{key}" if prefix else key
