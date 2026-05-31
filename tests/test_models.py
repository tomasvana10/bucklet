"""Tests for the data types."""

from datetime import datetime

from bucklet import storage
from bucklet.models import ObjectInfo, ObjectStatus, Profile


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
