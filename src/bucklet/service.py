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
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from . import s3, storage
from .errors import BuckletError
from .models import KeyResolution, Profile

if TYPE_CHECKING:
    from botocore.client import BaseClient

# A progress callback receives the number of bytes transferred since the last call.
ProgressCB = Callable[[int], None]

# upload_many reports overall progress as (bytes_sent, bytes_total, files_done, files_total).
MultiProgressCB = Callable[[int, int, int, int], None]

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
            raise BuckletError(status.error or "could not read its status")
        if status.state == storage.AVAILABLE:
            return f"already available ({status.storage_class.lower()}), no thaw needed"
        if status.state == storage.THAWING:
            return "thaw already in progress"
        if status.state == storage.THAWED:
            until = f" (until {status.restore_expiry})" if status.restore_expiry else ""
            return f"already thawed{until}"
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

    def rename(self, old_key: str, new_key: str):
        """Rename an object — a server-side copy to ``new_key`` then a delete
        of ``old_key`` (S3 has no rename).

        It refuses rather than risk a mess: the new key must be non-empty,
        different, and not already taken (renaming over a live object would
        destroy it); the source must be readable, so an archived/cold object is
        rejected with a thaw-first message (its bytes can't be copied). Crucially
        it checks delete permission *before* copying, so it never leaves a
        duplicate it can't clean up; and if the delete fails anyway (an exact-key
        deny, object lock), it rolls the copy back and reports why. The new
        object keeps the original's storage class. Unlike ``delete`` this is
        offered in the TUI without ``--allow-deletion``, because it never loses
        data — the copy always precedes (and on failure, replaces) the delete.
        """
        new_key = new_key.strip().lstrip("/")  # S3 keys don't begin with '/'
        if not new_key:
            raise BuckletError("the new key is empty")
        if new_key == old_key:
            raise BuckletError("the new key is the same as the old one")
        if s3.object_exists(self.client, self.bucket, new_key):
            raise BuckletError(f"an object named {new_key!r} already exists")
        status = self.status(old_key)
        if status.state == storage.ERROR:
            raise BuckletError(f"couldn't read {old_key}: {status.error}")
        # The copy reads the source's bytes, so anything not downloadable can't be
        # renamed yet. Tell a still-thawing object apart from a cold one, so the
        # advice fits: "wait" vs "thaw it" (a copy of either would hit
        # InvalidObjectState with the same unhelpful message).
        if not storage.can_download(status.state):
            if status.state == storage.THAWING:
                raise BuckletError(f"{old_key} is being thawed, wait for it to finish")
            raise BuckletError(f"{old_key} is archived, you must thaw it first")
        if not s3.can_delete(self.client, self.bucket, old_key):
            raise BuckletError(
                "this profile can't delete the original, so renaming would leave "
                "a duplicate (it needs the s3:DeleteObject permission)"
            )
        s3.copy_object(self.client, self.bucket, old_key, new_key, status.storage_class)
        try:
            s3.delete_object(self.client, self.bucket, old_key)
        except BuckletError as exc:
            # The copy landed but the original won't delete after all. Undo the
            # copy so we don't strand a duplicate, then report the real cause.
            try:
                s3.delete_object(self.client, self.bucket, new_key)
            except BuckletError:
                pass  # nothing more we can do; the original message matters most
            raise BuckletError(
                f"copied the new object, but couldn't remove the original: {exc}"
            ) from exc
        return f"renamed {old_key} -> {new_key}"

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
        t = self.profile.tuning
        s3.upload_file(
            self.client,
            self.bucket,
            Path(local_path),
            key,
            resolved,
            callback=progress,
            multipart_threshold=t.multipart_threshold,
            multipart_chunksize=t.multipart_chunksize,
            max_concurrency=t.part_concurrency,
        )
        return resolved

    def upload_many(
        self,
        plan: Iterable[tuple[str | os.PathLike, str]],
        *,
        storage_class: str | None = None,
        progress: MultiProgressCB | None = None,
    ):
        """Upload many ``(local_path, key)`` pairs, several at a time.

        Concurrency is the profile's ``upload_concurrency``. boto3 already
        parallelises the *parts* of one large file; this adds file-level
        parallelism, which is the win for many small/medium files where each is
        a single round-trip. Returns a list of ``(key, error)`` in plan order,
        where ``error`` is ``None`` on success or the
        :class:`~bucklet.errors.BuckletError` that file hit — a single failure
        never aborts the others (your archive key that can't write one object
        shouldn't sink the whole batch).
        """
        plan = list(plan)
        if not plan:
            return []
        resolved = self.resolve_storage_class(storage_class)
        t = self.profile.tuning
        total_bytes = 0
        for local, _key in plan:
            try:
                total_bytes += Path(local).stat().st_size
            except OSError:
                pass
        total_bytes = max(total_bytes, 1)
        total_files = len(plan)
        lock = threading.Lock()
        state = {"sent": 0, "done": 0}

        def report():
            if progress is None:
                return
            # Snapshot under the lock, then call the (possibly slow, possibly
            # buggy) callback outside it: a progress callback must never hold up
            # other workers or, by raising, fail an upload that actually worked.
            with lock:
                sent, done = state["sent"], state["done"]
            try:
                progress(sent, total_bytes, done, total_files)
            except Exception:
                pass

        def upload_one(item: tuple[str | os.PathLike, str]):
            local, key = item

            def cb(n: int):
                with lock:
                    state["sent"] += n
                report()

            try:
                s3.upload_file(
                    self.client,
                    self.bucket,
                    Path(local),
                    key,
                    resolved,
                    callback=cb,
                    multipart_threshold=t.multipart_threshold,
                    multipart_chunksize=t.multipart_chunksize,
                    max_concurrency=t.part_concurrency,
                )
                error: BuckletError | None = None
            except BuckletError as exc:
                error = exc
            except Exception as exc:
                # Anything s3.upload_file didn't already map (a vanished file,
                # an OS error) becomes this file's error rather than crashing
                # the whole batch through pool.map.
                error = BuckletError(str(exc) or exc.__class__.__name__)
            with lock:
                state["done"] += 1
            report()
            return (key, error)

        workers = max(1, min(t.upload_concurrency, total_files))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(upload_one, plan))

    @staticmethod
    def plan_upload(
        paths: Iterable[str | os.PathLike], prefix: str = "", *, basename_key: bool = False
    ):
        """Expand paths into (local_file, key) pairs.

        By default keys mirror each file's absolute path with the leading slash
        stripped (so the bucket reflects where the files live). With
        ``basename_key`` the key is instead relative to the argument given: a
        plain file becomes just its name, and a directory keeps its own name and
        the structure under it (``mydir/sub/f.txt`` rather than the whole
        ``home/you/.../mydir/sub/f.txt``). A ``prefix`` is prepended in either
        mode. Directories are walked recursively.
        """
        prefix = prefix.strip("/")
        plan: list[tuple[Path, str]] = []
        for raw in paths:
            real = Path(os.path.realpath(raw))
            if not real.exists():
                raise BuckletError(f"not found: {raw}")
            # In basename mode keys are relative to the argument's parent, so a
            # file is its name and a dir keeps its own name as the top segment.
            base = real.parent if basename_key else None
            if real.is_dir():
                for root, _dirs, names in os.walk(real):
                    for name in sorted(names):
                        full = Path(root) / name
                        plan.append((full, _key_for(full, base, prefix)))
            else:
                plan.append((real, _key_for(real, base, prefix)))
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


def _key_for(path: Path, base: Path | None, prefix: str):
    """The S3 key for a local file.

    ``base`` None mirrors the absolute path (slash stripped); a ``base`` makes
    the key relative to it (basename mode). ``prefix`` is prepended either way.
    """
    if base is not None:
        key = path.relative_to(base).as_posix()
    else:
        key = str(path).lstrip("/")
    return f"{prefix}/{key}" if prefix else key
