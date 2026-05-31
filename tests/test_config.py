"""Tests for profile config: CRUD, resolution, materialization, migration."""

import json

import pytest

from archy.config import Config
from archy.errors import ArchyError
from archy.models import Profile


def test_empty_when_no_file(config_dir):
    cfg = Config.load()
    assert cfg.profiles == {}
    assert cfg.default is None
    assert cfg.resolve(None) is None


def test_add_save_load_roundtrip(config_dir):
    cfg = Config.load()
    cfg.add(Profile(name="cold", bucket="b", region="r", storage_class="DEEP_ARCHIVE"))
    cfg.save()

    again = Config.load()
    assert again.default == "cold"  # first profile becomes default
    prof = again.get("cold")
    assert prof.bucket == "b"
    assert prof.storage_class == "DEEP_ARCHIVE"


def test_default_handling(config_dir):
    cfg = Config.load()
    cfg.add(Profile(name="a", bucket="ba"))
    cfg.add(Profile(name="b", bucket="bb"))
    assert cfg.default == "a"  # second add does not steal default
    cfg.add(Profile(name="c", bucket="bc"), make_default=True)
    assert cfg.default == "c"

    cfg.remove("c")
    assert cfg.default in ("a", "b")  # reassigned to a remaining profile

    with pytest.raises(ArchyError):
        cfg.set_default("ghost")


def test_resolve_modes(config_dir):
    cfg = Config.load()
    cfg.add(Profile(name="saved", bucket="saved-bucket"))
    # saved name
    assert cfg.resolve("saved").bucket == "saved-bucket"
    # unknown name -> treated as a raw bucket
    raw = cfg.resolve("random-bucket")
    assert raw.bucket == "random-bucket"
    assert raw.name == "random-bucket"
    # default
    assert cfg.resolve(None).name == "saved"


def test_materialize_region_from_env(config_dir, monkeypatch):
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    cfg = Config.load()
    cfg.add(Profile(name="p", bucket="b"))
    assert cfg.get("p").region == "eu-west-1"


def test_materialize_rclone_credentials(config_dir, tmp_path, monkeypatch):
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


def test_explicit_keys_override_rclone(config_dir, tmp_path, monkeypatch):
    rclone = tmp_path / "rclone.conf"
    rclone.write_text("[remote]\ntype = s3\naccess_key_id = FROM_RCLONE\nsecret_access_key = X\n")
    monkeypatch.setenv("RCLONE_CONFIG", str(rclone))
    cfg = Config.load()
    cfg.add(Profile(name="p", bucket="b", rclone_remote="remote",
                    access_key_id="EXPLICIT", secret_access_key="Y"))
    assert cfg.get("p").access_key_id == "EXPLICIT"


def test_migration_from_deeparch(config_dir, tmp_path):
    legacy = tmp_path / ".config" / "deeparch" / "config.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({
        "default": "old",
        "profiles": {"old": {"bucket": "legacy-bucket", "region": "us-east-1"}},
    }))

    cfg = Config.load()  # archy config absent -> migrates
    assert cfg.default == "old"
    prof = cfg.get("old")
    assert prof.bucket == "legacy-bucket"
    # deeparch only ever used DEEP_ARCHIVE, so migrated profiles inherit it.
    assert prof.storage_class == "DEEP_ARCHIVE"
    # and it was written to the new location.
    assert (config_dir / "config.json").exists()


def test_no_migration_when_disabled(config_dir, tmp_path):
    legacy = tmp_path / ".config" / "deeparch" / "config.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"profiles": {"old": {"bucket": "b"}}}))
    cfg = Config.load(migrate=False)
    assert cfg.profiles == {}


def test_save_permissions(config_dir):
    import os
    import stat

    cfg = Config.load()
    cfg.add(Profile(name="p", bucket="b", access_key_id="k", secret_access_key="s"))
    cfg.save()
    mode = stat.S_IMODE(os.stat(config_dir / "config.json").st_mode)
    assert mode == 0o600
