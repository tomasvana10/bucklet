"""Tests for reading credentials out of an rclone config."""

from pathlib import Path

from bucklet.rclone import creds_from_rclone

SAMPLE = """\
[backup]
type = s3
provider = AWS
access_key_id = AKIAEXAMPLE
secret_access_key = supersecret
region = ap-southeast-2
endpoint = https://s3.example.com

[other]
type = drive
"""


def _write(tmp_path: Path, text: str = SAMPLE) -> Path:
    path = tmp_path / "rclone.conf"
    path.write_text(text)
    return path


def test_reads_all_fields(tmp_path):
    creds = creds_from_rclone("backup", _write(tmp_path))
    assert creds == {
        "access_key_id": "AKIAEXAMPLE",
        "secret_access_key": "supersecret",
        "region": "ap-southeast-2",
        "endpoint_url": "https://s3.example.com",
    }


def test_partial_remote(tmp_path):
    text = "[mini]\ntype = s3\naccess_key_id = ONLYKEY\n"
    creds = creds_from_rclone("mini", _write(tmp_path, text))
    assert creds == {"access_key_id": "ONLYKEY"}


def test_unknown_remote(tmp_path):
    assert creds_from_rclone("nope", _write(tmp_path)) is None


def test_no_remote_arg(tmp_path):
    assert creds_from_rclone(None, _write(tmp_path)) is None


def test_missing_file(tmp_path):
    assert creds_from_rclone("backup", tmp_path / "absent.conf") is None
