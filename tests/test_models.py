"""Tests for the data types."""

from datetime import datetime

from bucklet import storage
from bucklet.models import (
    DEFAULT_MULTIPART_CHUNKSIZE,
    DEFAULT_MULTIPART_THRESHOLD,
    DEFAULT_PART_CONCURRENCY,
    DEFAULT_UPLOAD_CONCURRENCY,
    ObjectInfo,
    ObjectStatus,
    Profile,
)


def test_credential_source():
    keys = Profile(name="a", bucket="b", access_key_id="k", secret_access_key="s")
    assert keys.credential_source == "keys"
    assert keys.has_explicit_keys

    rc = Profile(name="a", bucket="b", rclone_remote="remote")
    assert rc.credential_source == "rclone:remote"
    assert not rc.has_explicit_keys

    chain = Profile(name="a", bucket="b")
    assert chain.credential_source == "aws-chain"


def test_profile_default_class():
    assert Profile(name="a").storage_class == storage.DEFAULT_STORAGE_CLASS


def test_tuning_defaults_when_unset():
    t = Profile(name="a").tuning
    assert t.multipart_threshold == DEFAULT_MULTIPART_THRESHOLD
    assert t.multipart_chunksize == DEFAULT_MULTIPART_CHUNKSIZE
    assert t.upload_concurrency == DEFAULT_UPLOAD_CONCURRENCY
    assert t.part_concurrency == DEFAULT_PART_CONCURRENCY


def test_tuning_treats_bad_values_as_default():
    # 0, negative, and non-int knobs that slipped into a config are not "set":
    # they fall back to the default rather than poisoning a transfer.
    bad = Profile(name="a", upload_concurrency=0, max_concurrency=-5, multipart_chunksize=0)
    assert bad.tuning.upload_concurrency == DEFAULT_UPLOAD_CONCURRENCY
    assert bad.tuning.part_concurrency == DEFAULT_PART_CONCURRENCY
    assert bad.tuning.multipart_chunksize == DEFAULT_MULTIPART_CHUNKSIZE


def test_tuning_uses_overrides():
    t = Profile(
        name="a",
        multipart_threshold=16 * 1024 * 1024,
        multipart_chunksize=64 * 1024 * 1024,
        upload_concurrency=8,
        max_concurrency=2,
    ).tuning
    assert t.multipart_threshold == 16 * 1024 * 1024
    assert t.multipart_chunksize == 64 * 1024 * 1024
    assert t.upload_concurrency == 8
    assert t.part_concurrency == 2  # max_concurrency maps to part_concurrency


def test_object_info_baseline_state():
    assert ObjectInfo("k", 1, None, "STANDARD").baseline_state == storage.AVAILABLE
    assert ObjectInfo("k", 1, None, "DEEP_ARCHIVE").baseline_state == storage.COLD


def test_object_status_helpers():
    cold = ObjectStatus(key="k", state=storage.COLD, storage_class="DEEP_ARCHIVE")
    assert cold.label == "cold"
    assert cold.can_thaw
    assert not cold.can_download

    ready = ObjectStatus(
        key="k",
        state=storage.THAWED,
        storage_class="DEEP_ARCHIVE",
        size=10,
        last_modified=datetime.now(),
    )
    assert ready.can_download
    assert not ready.can_thaw
