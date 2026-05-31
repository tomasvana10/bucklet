"""Tests for the display helpers."""

from datetime import datetime

import pytest

from bucklet.errors import BuckletError
from bucklet.formatting import fmt_date, human, parse_count, parse_size


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
