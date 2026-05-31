"""Tests for the UI-agnostic service facade, against moto."""

import pytest

from bucklet import storage
from bucklet.errors import BuckletError
from bucklet.models import ObjectStatus, Profile
from bucklet.service import Service


def test_open_validates_bucket(s3_client):
    profile = Profile(
        name="t",
        bucket="ghost",
        region="us-east-1",
        access_key_id="testing",
        secret_access_key="testing",
    )
    with pytest.raises(BuckletError):
        Service.open(profile)


def test_open_requires_bucket():
    with pytest.raises(BuckletError):
        Service.open(Profile(name="t", bucket=None), validate=False)


def test_upload_uses_profile_default_class(make_service, tmp_path):
    svc = make_service(storage_class="DEEP_ARCHIVE")
    f = tmp_path / "a.txt"
    f.write_text("x")
    used = svc.upload(f, "a.txt")
    assert used == "DEEP_ARCHIVE"
    assert svc.status("a.txt").storage_class == "DEEP_ARCHIVE"


def test_upload_class_override(make_service, tmp_path):
    svc = make_service(storage_class="DEEP_ARCHIVE")
    f = tmp_path / "a.txt"
    f.write_text("x")
    # Override the deep-archive default with standard for this upload.
    used = svc.upload(f, "a.txt", storage_class="standard")
    assert used == "STANDARD"
    assert svc.status("a.txt").state == storage.AVAILABLE


def test_list_objects_sorted(make_service, tmp_path):
    svc = make_service()
    for name in ("c", "a", "b"):
        f = tmp_path / name
        f.write_text("x")
        svc.upload(f, name)
    assert [o.key for o in svc.list_objects()] == ["a", "b", "c"]


def test_restore_on_available_is_noop(make_service, tmp_path):
    svc = make_service()
    f = tmp_path / "f"
    f.write_text("x")
    svc.upload(f, "f", storage_class="standard")
    message = svc.restore("f")
    assert "no thaw needed" in message


def test_restore_cold_object(make_service, tmp_path):
    svc = make_service(storage_class="DEEP_ARCHIVE")
    f = tmp_path / "f"
    f.write_text("x")
    svc.upload(f, "f")
    assert svc.status("f").state == storage.COLD
    svc.restore("f")
    assert svc.status("f").state != storage.COLD


def test_plan_upload_file_and_dir(tmp_path):
    d = tmp_path / "dir"
    d.mkdir()
    (d / "x.txt").write_text("1")
    (d / "y.txt").write_text("2")
    single = tmp_path / "solo.txt"
    single.write_text("3")

    plan = Service.plan_upload([single, d])
    keys = sorted(key for _, key in plan)
    # keys mirror the absolute path with the leading slash stripped
    assert all(not k.startswith("/") for k in keys)
    assert any(k.endswith("dir/x.txt") for k in keys)
    assert any(k.endswith("solo.txt") for k in keys)


def test_plan_upload_prefix(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x")
    plan = Service.plan_upload([f], prefix="backups")
    _, key = plan[0]
    assert key.startswith("backups/")


def test_plan_upload_missing_path(tmp_path):
    with pytest.raises(BuckletError):
        Service.plan_upload([tmp_path / "nope"])


def test_resolve_keys(make_service, tmp_path):
    svc = make_service()
    for name in ("a.txt", "b.txt", "c.log"):
        f = tmp_path / name
        f.write_text("x")
        svc.upload(f, name)

    res = svc.resolve_keys(["a.txt", "*.log", "missing.dat"])
    assert "a.txt" in res.matched
    assert "c.log" in res.matched
    assert "b.txt" not in res.matched
    assert "missing.dat" in res.missing


def test_resolve_keys_literal_glob_chars(make_service, tmp_path):
    """An exact key containing * ? [ ] must resolve to itself, not a glob."""
    svc = make_service()
    f = tmp_path / "x"
    f.write_text("x")
    for name in ("file[1].txt", "report?.pdf", "reportX.pdf"):
        svc.upload(f, name)

    # exact key with brackets resolves to itself (would be 'missing' before the fix)
    res = svc.resolve_keys(["file[1].txt"])
    assert res.matched == ["file[1].txt"]
    assert res.missing == []

    # exact key with '?' resolves to exactly itself, not reportX.pdf (over-match)
    res2 = svc.resolve_keys(["report?.pdf"])
    assert res2.matched == ["report?.pdf"]

    # a genuine glob still expands
    res3 = svc.resolve_keys(["report*"])
    assert set(res3.matched) == {"report?.pdf", "reportX.pdf"}


def _force_status(svc: Service, status: ObjectStatus, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(svc, "status", lambda key: status)


def test_restore_thawing_message(make_service, monkeypatch):
    svc = make_service()
    _force_status(svc, ObjectStatus("k", storage.THAWING, "DEEP_ARCHIVE"), monkeypatch)
    assert svc.restore("k") == "restore already in progress"


def test_restore_thawed_message_with_expiry(make_service, monkeypatch):
    svc = make_service()
    _force_status(
        svc,
        ObjectStatus(
            "k", storage.THAWED, "DEEP_ARCHIVE", restore_expiry="Fri, 21 Dec 2012 00:00:00 GMT"
        ),
        monkeypatch,
    )
    assert "already restored (until Fri, 21 Dec 2012 00:00:00 GMT)" in svc.restore("k")


def test_restore_thawed_message_without_expiry(make_service, monkeypatch):
    svc = make_service()
    _force_status(svc, ObjectStatus("k", storage.THAWED, "DEEP_ARCHIVE"), monkeypatch)
    message = svc.restore("k")
    assert "already restored" in message and "until" not in message


def test_restore_error_raises(make_service, monkeypatch):
    svc = make_service()
    _force_status(svc, ObjectStatus("k", storage.ERROR, error="boom"), monkeypatch)
    with pytest.raises(BuckletError, match="boom"):
        svc.restore("k")
