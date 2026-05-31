"""Tests for the display helpers."""

from datetime import datetime

import pytest

from archy.formatting import fmt_date, human


@pytest.mark.parametrize(
    "n,expected",
    [
        (0, "0B"),
        (5, "5B"),
        (1023, "1023B"),
        (1024, "1.0KB"),
        (2048, "2.0KB"),
        (1024 ** 2, "1.0MB"),
        (1024 ** 3, "1.0GB"),
        (1024 ** 4, "1.0TB"),
        (1024 ** 5, "1.0PB"),
        (2 * 1024 ** 6, "2.0EB"),  # overflow fallback
    ],
)
def test_human(n, expected):
    assert human(n) == expected


def test_fmt_date_none():
    assert fmt_date(None) == "-"


def test_fmt_date_value():
    # naive datetime -> treated as local wall-clock, no shift
    assert fmt_date(datetime(2020, 1, 2, 3, 4)) == "2020-01-02 03:04"
