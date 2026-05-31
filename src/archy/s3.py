"""Thin boto3 wrappers.

Every function here translates the boto3/botocore exception zoo into a single
:class:`~archy.errors.ArchyError` with a readable message, and returns plain
Python data. Nothing above this layer should import boto3. boto3 itself is
imported lazily so that ``archy --help`` and profile management stay fast and
work even if boto3's import is slow.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

from . import storage
from .errors import ArchyError
from .models import ObjectInfo, ObjectStatus, Profile


def build_client(profile: Profile):
    """Create a configured boto3 S3 client for a profile."""
    import boto3
    from botocore.config import Config

    cfg = Config(
        connect_timeout=15,
        read_timeout=70,
        retries={"max_attempts": 3, "mode": "standard"},
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


def _client_error_message(exc) -> str:
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


def validate(client, bucket: str) -> None:
    """Raise :class:`ArchyError` unless the bucket is reachable and readable."""
    from botocore.exceptions import (
        ClientError,
        EndpointConnectionError,
        NoCredentialsError,
    )

    try:
        client.head_bucket(Bucket=bucket)
    except NoCredentialsError as exc:
        raise ArchyError(
            "no AWS credentials found (set keys in the profile, point it at an "
            "rclone remote, or configure the AWS environment)"
        ) from exc
    except EndpointConnectionError as exc:
        raise ArchyError("cannot reach S3 (check region / network)") from exc
    except ClientError as exc:
        raise ArchyError(_client_error_message(exc)) from exc


def list_objects(client, bucket: str, prefix: str = "") -> Iterator[ObjectInfo]:
    """Yield every object under ``prefix`` (paginated)."""
    from botocore.exceptions import ClientError

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
        raise ArchyError(_client_error_message(exc)) from exc


def head_status(client, bucket: str, key: str) -> ObjectStatus:
    """HEAD one object and return its resolved :class:`ObjectStatus`."""
    from botocore.exceptions import ClientError

    try:
        resp = client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        return ObjectStatus(key=key, state=storage.ERROR, error=_client_error_message(exc))
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


def restore_object(client, bucket: str, key: str, tier: str = "Bulk", days: int = 7) -> str:
    """Begin a restore. Returns a status message; raises on hard errors."""
    from botocore.exceptions import ClientError

    try:
        client.restore_object(
            Bucket=bucket,
            Key=key,
            RestoreRequest={"Days": days, "GlacierJobParameters": {"Tier": tier}},
        )
        return f"{tier} restore requested ({days}d)"
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code == "RestoreAlreadyInProgress":
            return "restore already in progress"
        raise ArchyError(_client_error_message(exc)) from exc


def download_file(
    client, bucket: str, key: str, dest: Path, callback: Callable[[int], None] | None = None
) -> None:
    """Download ``key`` to ``dest`` (parents created)."""
    from botocore.exceptions import ClientError

    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    try:
        client.download_file(bucket, key, str(dest), Callback=callback)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code == "InvalidObjectState":
            raise ArchyError("not restored yet — thaw it first") from exc
        raise ArchyError(_client_error_message(exc)) from exc


def upload_file(
    client,
    bucket: str,
    local_path: Path,
    key: str,
    storage_class: str,
    callback: Callable[[int], None] | None = None,
) -> None:
    """Upload a local file to ``key`` in the given storage class."""
    from boto3.s3.transfer import TransferConfig
    from botocore.exceptions import ClientError

    transfer = TransferConfig(
        multipart_threshold=8 * 1024 * 1024,
        multipart_chunksize=256 * 1024 * 1024,
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
        raise ArchyError(_client_error_message(exc)) from exc
