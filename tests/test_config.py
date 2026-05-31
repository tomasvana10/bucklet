"""Tests for profile config: CRUD, resolution, materialization."""

import json

import pytest

from bucklet.config import CONFIG_VERSION, Config
from bucklet.errors import BuckletError
from bucklet.models import Profile


@pytest.mark.usefixtures("config_dir")
def test_empty_when_no_file():
    cfg = Config.load()
    assert cfg.profiles == {}
    assert cfg.default is None
    assert cfg.resolve(None) is None


@pytest.mark.usefixtures("config_dir")
def test_add_save_load_roundtrip():
    cfg = Config.load()
    cfg.add(Profile(name="cold", bucket="b", region="r", storage_class="DEEP_ARCHIVE"))
    cfg.save()

    again = Config.load()
    assert again.default == "cold"  # first profile becomes default
    prof = again.get("cold")
    assert prof.bucket == "b"
    assert prof.storage_class == "DEEP_ARCHIVE"


@pytest.mark.usefixtures("config_dir")
def test_default_handling():
    cfg = Config.load()
    cfg.add(Profile(name="a", bucket="ba"))
    cfg.add(Profile(name="b", bucket="bb"))
    assert cfg.default == "a"  # second add does not steal default
    cfg.add(Profile(name="c", bucket="bc"), make_default=True)
    assert cfg.default == "c"

    cfg.remove("c")
    assert cfg.default in ("a", "b")  # reassigned to a remaining profile

    with pytest.raises(BuckletError):
        cfg.set_default("ghost")


@pytest.mark.usefixtures("config_dir")
def test_resolve_modes():
    cfg = Config.load()
    cfg.add(Profile(name="saved", bucket="saved-bucket"))
    # saved name
    assert cfg.resolve("saved").bucket == "saved-bucket"
    # unknown name is treated as a raw bucket
    raw = cfg.resolve("random-bucket")
    assert raw.bucket == "random-bucket"
    assert raw.name == "random-bucket"
    # default
    assert cfg.resolve(None).name == "saved"


@pytest.mark.usefixtures("config_dir")
def test_materialize_region_from_env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    cfg = Config.load()
    cfg.add(Profile(name="p", bucket="b"))
    assert cfg.get("p").region == "eu-west-1"


@pytest.mark.usefixtures("config_dir")
def test_materialize_rclone_credentials(tmp_path, monkeypatch):
    rclone = tmp_path / "rclone.conf"
    rclone.write_text(
        "[remote]\ntype = s3\naccess_key_id = AK\nsecret_access_key = SK\nregion = ap-southeast-2\n"
    )
    monkeypatch.setenv("RCLONE_CONFIG", str(rclone))
    cfg = Config.load()
    cfg.add(Profile(name="p", bucket="b", rclone_remote="remote"))
    prof = cfg.get("p")
    assert prof.access_key_id == "AK"
    assert prof.secret_access_key == "SK"
    assert prof.region == "ap-southeast-2"


@pytest.mark.usefixtures("config_dir")
def test_explicit_keys_override_rclone(tmp_path, monkeypatch):
    rclone = tmp_path / "rclone.conf"
    rclone.write_text("[remote]\ntype = s3\naccess_key_id = FROM_RCLONE\nsecret_access_key = X\n")
    monkeypatch.setenv("RCLONE_CONFIG", str(rclone))
    cfg = Config.load()
    cfg.add(
        Profile(
            name="p",
            bucket="b",
            rclone_remote="remote",
            access_key_id="EXPLICIT",
            secret_access_key="Y",
        )
    )
    assert cfg.get("p").access_key_id == "EXPLICIT"


def test_save_permissions(config_dir):
    import os
    import stat

    cfg = Config.load()
    cfg.add(Profile(name="p", bucket="b", access_key_id="k", secret_access_key="s"))
    cfg.save()
    mode = stat.S_IMODE(os.stat(config_dir / "config.json").st_mode)
    assert mode == 0o600


def test_save_writes_version(config_dir):
    cfg = Config.load()
    cfg.add(Profile(name="p", bucket="b"))
    cfg.save()
    data = json.loads((config_dir / "config.json").read_text())
    assert data["version"] == CONFIG_VERSION


@pytest.mark.usefixtures("config_dir")
def test_legacy_config_is_migrated_and_persisted(config_dir):
    # a pre-versioning file (no "version") is the original layout == v1
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config.json"
    path.write_text(json.dumps({"default": "old", "profiles": {"old": {"bucket": "b"}}}))

    cfg = Config.load()
    assert cfg.get("old").bucket == "b"
    # the upgrade is stamped and written back to disk
    on_disk = json.loads(path.read_text())
    assert on_disk["version"] == CONFIG_VERSION
    assert on_disk["profiles"]["old"]["bucket"] == "b"


@pytest.mark.usefixtures("config_dir")
def test_config_from_newer_bucklet_is_refused(config_dir):
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config.json"
    path.write_text(json.dumps({"version": CONFIG_VERSION + 1, "profiles": {}}))
    with pytest.raises(BuckletError, match="newer"):
        Config.load()


@pytest.mark.usefixtures("config_dir")
def test_malformed_version_is_normalized_and_persisted(config_dir):
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config.json"
    # a hand-corrupted version (0) should be normalised to a valid one and saved
    path.write_text(json.dumps({"version": 0, "profiles": {"p": {"bucket": "b"}}}))
    Config.load()
    assert json.loads(path.read_text())["version"] == CONFIG_VERSION


@pytest.mark.usefixtures("config_dir")
def test_legacy_config_without_tuning_uses_defaults():
    # a v1 profile predating the tuning knobs still resolves to sane defaults
    cfg = Config.load()
    cfg.add(Profile(name="p", bucket="b"))
    cfg.save()
    prof = Config.load().get("p")
    assert prof.upload_concurrency is None  # not stored
    assert prof.tuning.upload_concurrency > 0  # but resolves to a default


@pytest.mark.usefixtures("config_dir")
def test_tuning_fields_round_trip():
    cfg = Config.load()
    cfg.add(
        Profile(
            name="p",
            bucket="b",
            multipart_chunksize=64 * 1024 * 1024,
            upload_concurrency=8,
        )
    )
    cfg.save()

    again = Config.load()
    prof = again.get("p")
    assert prof.multipart_chunksize == 64 * 1024 * 1024
    assert prof.upload_concurrency == 8
    # unset knobs stay None (so they resolve to the default)
    assert prof.multipart_threshold is None
    assert prof.tuning.upload_concurrency == 8
