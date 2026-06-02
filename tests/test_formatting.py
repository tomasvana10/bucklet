"""Tests for the display helpers."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from bucklet.errors import BuckletError
from bucklet.formatting import fmt_date, human, parse_count, parse_size, thaw_remaining


@pytest.mark.parametrize(
    "n,expected",
    [
        (0, "0B"),
        (5, "5B"),
        (1023, "1023B"),
        (1024, "1.0KB"),
        (2048, "2.0KB"),
        (1024**2, "1.0MB"),
        (1024**3, "1.0GB"),
        (1024**4, "1.0TB"),
        (1024**5, "1.0PB"),
        (2 * 1024**6, "2.0EB"),  # overflow fallback
    ],
)
def test_human(n, expected):
    assert human(n) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("512", 512),  # bare bytes
        ("8MB", 8 * 1024**2),
        ("8MiB", 8 * 1024**2),  # binary, same as MB here
        ("64mb", 64 * 1024**2),  # case-insensitive
        ("1.5GB", int(1.5 * 1024**3)),
        ("256 MB", 256 * 1024**2),  # whitespace tolerated
        ("1KB", 1024),
        ("2T", 2 * 1024**4),
    ],
)
def test_parse_size(text, expected):
    assert parse_size(text) == expected


@pytest.mark.parametrize("bad", ["", "abc", "8XB", "-5MB", "0", "0MB", "MB"])
def test_parse_size_rejects(bad):
    with pytest.raises(BuckletError):
        parse_size(bad)


def test_parse_size_human_roundtrip():
    # what `human` shows can be fed back to `parse_size`
    assert parse_size(human(64 * 1024**2)) == 64 * 1024**2


@pytest.mark.parametrize("text,expected", [("4", 4), (" 16 ", 16), ("1", 1)])
def test_parse_count(text, expected):
    assert parse_count(text) == expected


@pytest.mark.parametrize("bad", ["", "0", "-3", "4.5", "abc"])
def test_parse_count_rejects(bad):
    with pytest.raises(BuckletError):
        parse_count(bad)


def test_fmt_date_none():
    assert fmt_date(None) == "-"


def test_fmt_date_value():
    # naive datetime is treated as local wall-clock, no shift
    assert fmt_date(datetime(2020, 1, 2, 3, 4)) == "2020-01-02 03:04"


_NOW = datetime(2020, 1, 1, 0, 0, tzinfo=timezone.utc)


def _expiry(**delta):
    """An S3-style GMT expiry-date `delta` from the fixed _NOW used in tests."""
    return format_datetime(_NOW + timedelta(**delta), usegmt=True)


@pytest.mark.parametrize(
    "delta,expected",
    [
        (dict(days=2), "2d"),
        (dict(hours=50), "2d"),  # floored to the largest whole unit
        (dict(days=1), "1d"),
        (dict(hours=5), "5h"),
        (dict(minutes=50), "50m"),
        (dict(seconds=30), "<1m"),  # nearly lapsed
    ],
)
def test_thaw_remaining(delta, expected):
    assert thaw_remaining(_expiry(**delta), now=_NOW) == expected


@pytest.mark.parametrize(
    "expiry",
    [
        None,
        "",
        "not a date",
        format_datetime(_NOW - timedelta(hours=1), usegmt=True),  # already past
    ],
)
def test_thaw_remaining_none(expiry):
    assert thaw_remaining(expiry, now=_NOW) is None


def test_thaw_remaining_naive_expiry_assumed_utc():
    # A header that somehow dropped its zone is still read as UTC, not local.
    assert thaw_remaining("Thu, 02 Jan 2020 00:00:00", now=_NOW) == "1d"
