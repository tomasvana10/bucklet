"""Shared fixtures.

Two kinds of isolation:
  * ``config_dir`` points HOME and $ARCHY_CONFIG_DIR at a tmp dir, so config,
    rclone and legacy-deeparch lookups never touch the real machine.
  * ``s3_client`` / ``make_service`` run inside moto's ``mock_aws`` with fake
    credentials, so nothing hits real AWS.
"""

from __future__ import annotations

import pytest

from archy.models import Profile
from archy.service import Service


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Isolate HOME + the archy config dir under tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("RCLONE_CONFIG", raising=False)
    cfg = tmp_path / ".config" / "archy"
    monkeypatch.setenv("ARCHY_CONFIG_DIR", str(cfg))
    return cfg


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def s3_client(aws_env):
    """A moto-backed S3 client; the mock stays active for the whole test."""
    import boto3
    from moto import mock_aws

    with mock_aws():
        yield boto3.client("s3", region_name="us-east-1")


@pytest.fixture
def make_service(s3_client):
    def _make(bucket="test-bucket", *, storage_class="STANDARD", create=True) -> Service:
        if create:
            s3_client.create_bucket(Bucket=bucket)
        profile = Profile(
            name="t",
            bucket=bucket,
            region="us-east-1",
            access_key_id="testing",
            secret_access_key="testing",
            storage_class=storage_class,
        )
        return Service.open(profile)

    return _make
