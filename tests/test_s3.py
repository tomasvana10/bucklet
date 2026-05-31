"""Tests for the thin boto3 layer, against moto."""

import pytest

from bucklet import s3, storage
from bucklet.errors import BuckletError
from bucklet.models import Profile


def test_validate_ok(s3_client):
    s3_client.create_bucket(Bucket="the-bucket")
    profile = Profile(
        name="t",
        bucket="the-bucket",
        region="us-east-1",
        access_key_id="testing",
        secret_access_key="testing",
    )
    client = s3.build_client(profile)
    s3.validate(client, "the-bucket")  # should not raise


def test_validate_missing_bucket(s3_client):
    profile = Profile(
        name="t",
        bucket="ghost",
        region="us-east-1",
        access_key_id="testing",
        secret_access_key="testing",
    )
    client = s3.build_client(profile)
    with pytest.raises(BuckletError) as exc:
        s3.validate(client, "ghost")
    assert "not found" in str(exc.value)


def test_upload_list_head_roundtrip(s3_client, tmp_path):
    s3_client.create_bucket(Bucket="the-bucket")
    profile = Profile(
        name="t",
        bucket="the-bucket",
        region="us-east-1",
        access_key_id="testing",
        secret_access_key="testing",
    )
    client = s3.build_client(profile)

    src = tmp_path / "hello.txt"
    src.write_text("hi")
    s3.upload_file(client, "the-bucket", src, "data/hello.txt", "DEEP_ARCHIVE")

    listed = list(s3.list_objects(client, "the-bucket"))
    assert [o.key for o in listed] == ["data/hello.txt"]
    assert listed[0].storage_class == "DEEP_ARCHIVE"

    status = s3.head_status(client, "the-bucket", "data/hello.txt")
    assert status.storage_class == "DEEP_ARCHIVE"
    assert status.state == storage.COLD


def test_head_standard_is_available(s3_client, tmp_path):
    s3_client.create_bucket(Bucket="the-bucket")
    client = s3.build_client(
        Profile(
            name="t",
            bucket="the-bucket",
            region="us-east-1",
            access_key_id="testing",
            secret_access_key="testing",
        )
    )
    src = tmp_path / "f"
    src.write_text("x")
    s3.upload_file(client, "the-bucket", src, "f", "STANDARD")
    assert s3.head_status(client, "the-bucket", "f").state == storage.AVAILABLE


def test_download_roundtrip(s3_client, tmp_path):
    s3_client.create_bucket(Bucket="the-bucket")
    client = s3.build_client(
        Profile(
            name="t",
            bucket="the-bucket",
            region="us-east-1",
            access_key_id="testing",
            secret_access_key="testing",
        )
    )
    src = tmp_path / "f"
    src.write_text("payload")
    s3.upload_file(client, "the-bucket", src, "k", "STANDARD")

    dest = tmp_path / "out" / "f"
    s3.download_file(client, "the-bucket", "k", dest)
    assert dest.read_text() == "payload"


def test_restore_moves_off_cold(s3_client, tmp_path):
    s3_client.create_bucket(Bucket="the-bucket")
    client = s3.build_client(
        Profile(
            name="t",
            bucket="the-bucket",
            region="us-east-1",
            access_key_id="testing",
            secret_access_key="testing",
        )
    )
    src = tmp_path / "f"
    src.write_text("x")
    s3.upload_file(client, "the-bucket", src, "k", "DEEP_ARCHIVE")
    assert s3.head_status(client, "the-bucket", "k").state == storage.COLD

    message = s3.restore_object(client, "the-bucket", "k", tier="Bulk", days=3)
    assert "restore" in message.lower()
    assert s3.head_status(client, "the-bucket", "k").state != storage.COLD


def _client_error(code: str, message: str = ""):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": message}}, "Op")


def test_client_error_message_mapping():
    assert s3._client_error_message(_client_error("404")) == "bucket not found"
    assert s3._client_error_message(_client_error("NoSuchBucket")) == "bucket not found"
    assert (
        s3._client_error_message(_client_error("403"))
        == "access denied (check the IAM policy and keys)"
    )
    assert (
        s3._client_error_message(_client_error("AccessDenied"))
        == "access denied (check the IAM policy and keys)"
    )
    assert s3._client_error_message(_client_error("301")) == "wrong region for this bucket"
    assert (
        s3._client_error_message(_client_error("PermanentRedirect"))
        == "wrong region for this bucket"
    )
    assert (
        s3._client_error_message(_client_error("AuthorizationHeaderMalformed"))
        == "wrong region for this bucket"
    )
    # an unrelated message mentioning 'endpoint' must NOT be mistaken for a region error
    msg = s3._client_error_message(_client_error("InvalidRequest", "bad endpoint configuration"))
    assert msg == "InvalidRequest: bad endpoint configuration"


def test_download_invalid_object_state_raises(tmp_path):
    class FakeClient:
        def download_file(self, *args, **kwargs):
            raise _client_error("InvalidObjectState")

    with pytest.raises(BuckletError, match="not restored yet"):
        s3.download_file(FakeClient(), "the-bucket", "k", tmp_path / "out" / "f")


def test_delete_object_roundtrip(s3_client, tmp_path):
    s3_client.create_bucket(Bucket="the-bucket")
    client = s3.build_client(
        Profile(
            name="t",
            bucket="the-bucket",
            region="us-east-1",
            access_key_id="testing",
            secret_access_key="testing",
        )
    )
    src = tmp_path / "f"
    src.write_text("x")
    s3.upload_file(client, "the-bucket", src, "k", "STANDARD")
    assert [o.key for o in s3.list_objects(client, "the-bucket")] == ["k"]

    s3.delete_object(client, "the-bucket", "k")
    assert list(s3.list_objects(client, "the-bucket")) == []


def test_delete_object_access_denied_raises():
    class FakeClient:
        def delete_object(self, *args, **kwargs):
            raise _client_error("AccessDenied")

    # An archive-only key that lacks s3:DeleteObject must surface as a clean
    # BuckletError, not a raw botocore traceback.
    with pytest.raises(BuckletError, match="access denied"):
        s3.delete_object(FakeClient(), "the-bucket", "k")


def test_head_status_survives_network_error():
    """A non-ClientError botocore failure (e.g. a timeout while polling a
    thawing object) must degrade to an ERROR state, never raise."""
    from botocore.exceptions import EndpointConnectionError

    class FakeClient:
        def head_object(self, *args, **kwargs):
            raise EndpointConnectionError(endpoint_url="https://s3.example")

    status = s3.head_status(FakeClient(), "the-bucket", "k")
    assert status.state == storage.ERROR
    assert status.error  # carries a message rather than crashing


def test_list_objects_network_error_raises_buckleterror():
    from botocore.exceptions import EndpointConnectionError

    class FakePaginator:
        def paginate(self, **_kwargs):
            # the network call happens as the pages are fetched
            raise EndpointConnectionError(endpoint_url="https://s3.example")

    class FakeClient:
        def get_paginator(self, _name):
            return FakePaginator()

    with pytest.raises(BuckletError):
        list(s3.list_objects(FakeClient(), "the-bucket"))


def test_delete_object_network_error_raises_buckleterror():
    from botocore.exceptions import ConnectTimeoutError

    class FakeClient:
        def delete_object(self, *args, **kwargs):
            raise ConnectTimeoutError(endpoint_url="https://s3.example")

    with pytest.raises(BuckletError):
        s3.delete_object(FakeClient(), "the-bucket", "k")


def test_upload_file_network_error_raises_buckleterror(tmp_path):
    from botocore.exceptions import EndpointConnectionError

    class FakeClient:
        def upload_file(self, *args, **kwargs):
            raise EndpointConnectionError(endpoint_url="https://s3.example")

    src = tmp_path / "f"
    src.write_text("x")
    with pytest.raises(BuckletError):
        s3.upload_file(FakeClient(), "the-bucket", src, "k", "STANDARD")


def test_build_client_pool_scales_with_tuning():
    def prof(**kw):
        return Profile(name="t", bucket="b", region="us-east-1", **kw)

    def pool(p):
        return s3.build_client(p).meta.config.max_pool_connections

    assert pool(prof()) == 40  # default 4 x 10
    assert pool(prof(upload_concurrency=6, max_concurrency=4)) == 24  # 6 x 4
    assert pool(prof(upload_concurrency=1000, max_concurrency=1000)) == 128  # capped
