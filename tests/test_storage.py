"""Tests for the pure storage-class and state logic."""

import pytest

from bucklet import storage
from bucklet.errors import BuckletError


@pytest.mark.parametrize(
    "value,expected",
    [
        ("standard", "STANDARD"),
        ("STANDARD", "STANDARD"),
        ("deep_archive", "DEEP_ARCHIVE"),
        ("deep-archive", "DEEP_ARCHIVE"),
        ("deep", "DEEP_ARCHIVE"),
        ("da", "DEEP_ARCHIVE"),
        ("ia", "STANDARD_IA"),
        ("ir", "GLACIER_IR"),
        ("glacier", "GLACIER"),
        ("  Glacier_IR ", "GLACIER_IR"),
    ],
)
def test_normalize_storage_class(value, expected):
    assert storage.normalize_storage_class(value) == expected


def test_normalize_unknown_raises():
    with pytest.raises(BuckletError) as exc:
        storage.normalize_storage_class("frostbite")
    assert "frostbite" in str(exc.value)


@pytest.mark.parametrize(
    "cls,expected",
    [
        ("GLACIER", True),
        ("DEEP_ARCHIVE", True),
        ("STANDARD", False),
        ("GLACIER_IR", False),  # instant retrieval, no thaw
        ("INTELLIGENT_TIERING", False),
        (None, False),
    ],
)
def test_needs_restore(cls, expected):
    assert storage.needs_restore(cls) is expected


@pytest.mark.parametrize(
    "cls,restore,expected",
    [
        ("STANDARD", None, storage.AVAILABLE),
        ("GLACIER_IR", None, storage.AVAILABLE),
        ("DEEP_ARCHIVE", None, storage.COLD),
        ("GLACIER", None, storage.COLD),
        ("GLACIER", 'ongoing-request="true"', storage.THAWING),
        ("DEEP_ARCHIVE", 'ongoing-request="false", expiry-date="x"', storage.THAWED),
        # A present Restore header wins over the class.
        ("STANDARD", 'ongoing-request="true"', storage.THAWING),
    ],
)
def test_object_state(cls, restore, expected):
    assert storage.object_state(cls, restore) == expected


def test_restore_expiry():
    header = 'ongoing-request="false", expiry-date="Fri, 21 Dec 2012 00:00:00 GMT"'
    assert storage.restore_expiry(header) == "Fri, 21 Dec 2012 00:00:00 GMT"
    assert storage.restore_expiry('ongoing-request="true"') is None
    assert storage.restore_expiry(None) is None


def test_can_download_and_thaw():
    assert storage.can_download(storage.AVAILABLE)
    assert storage.can_download(storage.THAWED)
    assert not storage.can_download(storage.COLD)
    assert not storage.can_download(storage.THAWING)

    assert storage.can_thaw(storage.COLD)
    assert not storage.can_thaw(storage.AVAILABLE)
    assert not storage.can_thaw(storage.THAWED)
