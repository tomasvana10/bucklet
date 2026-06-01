"""Thin boto3 wrappers.

Every function here turns the boto3/botocore exception zoo into a single
:class:`~bucklet.errors.BuckletError` with a readable message, and returns plain
Python data. Nothing above this layer should import boto3. boto3 is imported
lazily so that ``bucklet --help`` and profile management stay fast and keep
working even when boto3 is slow to import.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from . import storage
from .errors import BuckletError
from .models import (
    DEFAULT_MULTIPART_CHUNKSIZE,
    DEFAULT_MULTIPART_THRESHOLD,
    DEFAULT_PART_CONCURRENCY,
    ObjectInfo,
    ObjectStatus,
    Profile,
)

if TYPE_CHECKING:
    from botocore.client import BaseClient
    from botocore.exceptions import ClientError


def build_client(profile: Profile):
    """Create a configured boto3 S3 client for a profile."""
    import boto3
    from botocore.config import Config

    # Size the connection pool to the profile's concurrency so parallel uploads
    # (files × per-file parts) don't starve each other on a default pool of 10.
    # Capped so a fat-fingered tuning value can't ask urllib3 for an absurd pool.
    tuning = profile.tuning
    pool = min(128, max(10, tuning.upload_concurrency * tuning.part_concurrency))
    cfg = Config(
        connect_timeout=15,
        read_timeout=70,
        retries={"max_attempts": 3, "mode": "standard"},
        max_pool_connections=pool,
    )
    kwargs: dict = {"config": cfg}
    if profile.region:
        kwargs["region_name"] = profile.region
    if profile.endpoint_url:
        kwargs["endpoint_url"] = profile.endpoint_url
    if profile.has_explicit_keys:
        kwargs["aws_access_key_id"] = profile.access_key_id
        kwargs["aws_secret_access_key"] = profile.secret_access_key
    return boto3.client("s3", **kwargs)


def _client_error_message(exc: ClientError) -> str:
    err = getattr(exc, "response", {}).get("Error", {})
    code = str(err.get("Code", "")) or "error"
    message = err.get("Message", "")
    if code in ("404", "NoSuchBucket"):
        return "bucket not found"
    if code in ("403", "AccessDenied", "Forbidden"):
        return "access denied (check the IAM policy and keys)"
    if code in ("301", "PermanentRedirect", "AuthorizationHeaderMalformed"):
        return "wrong region for this bucket"
    return f"{code}: {message}" if message else code


def validate(client: BaseClient, bucket: str):
    """Raise :class:`BuckletError` unless the bucket is reachable and readable."""
    from botocore.exceptions import (
        ClientError,
        EndpointConnectionError,
        NoCredentialsError,
    )

    try:
        client.head_bucket(Bucket=bucket)
    except NoCredentialsError as exc:
        raise BuckletError(
            "no AWS credentials found (set keys in the profile, point it at an "
            "rclone remote, or configure the AWS environment)"
        ) from exc
    except EndpointConnectionError as exc:
        raise BuckletError("cannot reach S3 (check region / network)") from exc
    except ClientError as exc:
        raise BuckletError(_client_error_message(exc)) from exc


def list_objects(client: BaseClient, bucket: str, prefix: str = ""):
    """Yield every object under ``prefix`` (paginated)."""
    from botocore.exceptions import BotoCoreError, ClientError

    paginator = client.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix or ""):
            for obj in page.get("Contents", []):
                yield ObjectInfo(
                    key=obj["Key"],
                    size=obj["Size"],
                    last_modified=obj.get("LastModified"),
                    storage_class=obj.get("StorageClass", "STANDARD"),
                )
    except ClientError as exc:
        raise BuckletError(_client_error_message(exc)) from exc
    except BotoCoreError as exc:
        # Connection/timeout/credential failures (the non-ClientError half of
        # the botocore zoo) must surface as a clean error, not a raw traceback.
        raise BuckletError(str(exc) or "could not list objects") from exc


def head_status(client: BaseClient, bucket: str, key: str):
    """HEAD one object and return its resolved :class:`ObjectStatus`.

    This never raises: a failed HEAD (denied, missing, or a network/credential
    error) comes back as an ``ERROR`` state. Callers poll it from worker threads,
    so a transient failure must degrade to a marked row, not a crash.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        resp = client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        return ObjectStatus(key=key, state=storage.ERROR, error=_client_error_message(exc))
    except BotoCoreError as exc:
        return ObjectStatus(key=key, state=storage.ERROR, error=str(exc) or "status unavailable")
    sc = resp.get("StorageClass", "STANDARD")
    restore = resp.get("Restore")
    return ObjectStatus(
        key=key,
        state=storage.object_state(sc, restore),
        storage_class=sc,
        size=resp.get("ContentLength"),
        last_modified=resp.get("LastModified"),
        restore_expiry=storage.restore_expiry(restore),
    )


def restore_object(client: BaseClient, bucket: str, key: str, tier: str = "Bulk", days: int = 7):
    """Begin a restore. Returns a status message; raises on hard errors."""
    from botocore.exceptions import ClientError

    try:
        client.restore_object(
            Bucket=bucket,
            Key=key,
            RestoreRequest={"Days": days, "GlacierJobParameters": {"Tier": tier}},
        )
        return f"{tier} thaw requested ({days}d)"
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code == "RestoreAlreadyInProgress":
            return "thaw already in progress"
        raise BuckletError(_client_error_message(exc)) from exc


def delete_object(client: BaseClient, bucket: str, key: str):
    """Delete one object. Raises :class:`BuckletError` on failure.

    S3 deletion is idempotent (deleting a missing key still succeeds), so the
    only failures here are real ones: most often ``AccessDenied`` when the
    credentials lack ``s3:DeleteObject`` (common for archive-only keys).
    """
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        client.delete_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        raise BuckletError(_client_error_message(exc)) from exc
    except BotoCoreError as exc:
        raise BuckletError(str(exc) or "could not delete object") from exc


def can_delete(client: BaseClient, bucket: str, key: str) -> bool:
    """Best-effort probe of whether we may delete objects at ``key``'s prefix.

    S3 has no dry run, but ``DeleteObject`` is idempotent: deleting a key that
    does not exist *succeeds* when you hold ``s3:DeleteObject`` and returns
    ``AccessDenied`` when you don't. So we delete a sentinel key that shares
    ``key``'s prefix (so a resource-scoped IAM policy evaluates the same way)
    and never names a real object — learning the permission without touching
    any data. ``rename`` uses this to refuse *before* it copies, so a rename
    can't strand a duplicate.

    Returns True/False for the permission-decisive answers and re-raises
    anything else (network, wrong region) as :class:`BuckletError`. It is only
    a best guess: a policy scoped to the exact key, or an object-lock/retention
    rule, can still block the real delete — which is why ``rename`` also rolls
    the copy back if the delete ultimately fails.

    On a versioning-enabled bucket the probe delete creates a delete marker even
    for a key that never existed; we remove that marker afterwards (best-effort,
    needs ``s3:DeleteObjectVersion``) so the check really does leave no trace.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    prefix = f"{key.rsplit('/', 1)[0]}/" if "/" in key else ""
    probe = f"{prefix}.bucklet-delete-permission-check"
    try:
        resp = client.delete_object(Bucket=bucket, Key=probe)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in ("403", "AccessDenied", "Forbidden"):
            return False
        raise BuckletError(_client_error_message(exc)) from exc
    except BotoCoreError as exc:
        raise BuckletError(str(exc) or "could not check delete permission") from exc
    if resp.get("DeleteMarker") and resp.get("VersionId"):
        try:
            client.delete_object(Bucket=bucket, Key=probe, VersionId=resp["VersionId"])
        except Exception:
            pass  # a stray marker for a non-existent key is harmless
    return True


def object_exists(client: BaseClient, bucket: str, key: str) -> bool:
    """True if ``key`` exists. Raises :class:`BuckletError` on a real error.

    A 404/NoSuchKey is the clean "no"; anything else (denied, wrong region) is
    surfaced so a caller doesn't mistake a permission failure for "absent".
    """
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise BuckletError(_client_error_message(exc)) from exc
    except BotoCoreError as exc:
        raise BuckletError(str(exc) or "could not check object") from exc


def copy_object(client: BaseClient, bucket: str, src_key: str, dst_key: str, storage_class: str):
    """Server-side copy ``src_key`` to ``dst_key``, keeping the storage class.

    Used by ``rename`` (copy then delete the original). ``MetadataDirective``
    is ``COPY`` so user metadata and content type ride along. An archived
    source can't be read, so S3 returns ``InvalidObjectState``; a single copy
    tops out at 5 GB (``EntityTooLarge``). Both get a readable message.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        client.copy_object(
            Bucket=bucket,
            Key=dst_key,
            CopySource={"Bucket": bucket, "Key": src_key},
            StorageClass=storage_class,
            MetadataDirective="COPY",
        )
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code == "InvalidObjectState":
            raise BuckletError("the object is archived, you must thaw it first") from exc
        if code == "EntityTooLarge":
            raise BuckletError(
                "the object is larger than 5GB, so it can't be renamed by copy"
            ) from exc
        raise BuckletError(_client_error_message(exc)) from exc
    except BotoCoreError as exc:
        raise BuckletError(str(exc) or "could not copy object") from exc


def download_file(
    client: BaseClient,
    bucket: str,
    key: str,
    dest: Path,
    callback: Callable[[int], None] | None = None,
):
    """Download ``key`` to ``dest`` (parent directories created)."""
    from botocore.exceptions import ClientError

    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    try:
        client.download_file(bucket, key, str(dest), Callback=callback)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code == "InvalidObjectState":
            raise BuckletError("not restored yet, thaw it first") from exc
        raise BuckletError(_client_error_message(exc)) from exc


def upload_file(
    client: BaseClient,
    bucket: str,
    local_path: Path,
    key: str,
    storage_class: str,
    callback: Callable[[int], None] | None = None,
    *,
    multipart_threshold: int = DEFAULT_MULTIPART_THRESHOLD,
    multipart_chunksize: int = DEFAULT_MULTIPART_CHUNKSIZE,
    max_concurrency: int = DEFAULT_PART_CONCURRENCY,
):
    """Upload a local file to ``key`` in the given storage class.

    The multipart knobs come from the profile's :class:`~bucklet.models.Tuning`;
    ``max_concurrency`` is how many parts of *this* file upload at once.
    """
    from boto3.s3.transfer import TransferConfig
    from botocore.exceptions import BotoCoreError, ClientError

    transfer = TransferConfig(
        multipart_threshold=multipart_threshold,
        multipart_chunksize=multipart_chunksize,
        max_concurrency=max_concurrency,
    )
    try:
        client.upload_file(
            str(local_path),
            bucket,
            key,
            ExtraArgs={"StorageClass": storage_class},
            Config=transfer,
            Callback=callback,
        )
    except ClientError as exc:
        raise BuckletError(_client_error_message(exc)) from exc
    except BotoCoreError as exc:
        raise BuckletError(str(exc) or "upload failed") from exc
