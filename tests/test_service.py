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


def test_plan_upload_basename_file(tmp_path):
    f = tmp_path / "deep" / "nested" / "f.txt"
    f.parent.mkdir(parents=True)
    f.write_text("x")
    # basename mode keys a single file by just its name, not the absolute path
    ((_, key),) = Service.plan_upload([f], basename_key=True)
    assert key == "f.txt"


def test_plan_upload_basename_dir_keeps_own_name(tmp_path):
    d = tmp_path / "mydir"
    (d / "sub").mkdir(parents=True)
    (d / "a.txt").write_text("1")
    (d / "sub" / "b.txt").write_text("2")
    keys = sorted(key for _, key in Service.plan_upload([d], basename_key=True))
    # keys are relative to the dir's parent: the dir's own name leads, structure kept
    assert keys == ["mydir/a.txt", "mydir/sub/b.txt"]


def test_plan_upload_basename_with_prefix(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x")
    ((_, key),) = Service.plan_upload([f], prefix="backups", basename_key=True)
    assert key == "backups/f.txt"


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
    assert svc.restore("k") == "thaw already in progress"


def test_restore_thawed_message_with_expiry(make_service, monkeypatch):
    svc = make_service()
    _force_status(
        svc,
        ObjectStatus(
            "k", storage.THAWED, "DEEP_ARCHIVE", restore_expiry="Fri, 21 Dec 2012 00:00:00 GMT"
        ),
        monkeypatch,
    )
    assert "already thawed (until Fri, 21 Dec 2012 00:00:00 GMT)" in svc.restore("k")


def test_restore_thawed_message_without_expiry(make_service, monkeypatch):
    svc = make_service()
    _force_status(svc, ObjectStatus("k", storage.THAWED, "DEEP_ARCHIVE"), monkeypatch)
    message = svc.restore("k")
    assert "already thawed" in message and "until" not in message


def test_restore_error_raises(make_service, monkeypatch):
    svc = make_service()
    _force_status(svc, ObjectStatus("k", storage.ERROR, error="boom"), monkeypatch)
    with pytest.raises(BuckletError, match="boom"):
        svc.restore("k")


def test_delete_removes_object(make_service, tmp_path):
    svc = make_service()
    f = tmp_path / "f"
    f.write_text("x")
    svc.upload(f, "f", storage_class="standard")
    assert [o.key for o in svc.list_objects()] == ["f"]
    message = svc.delete("f")
    assert "deleted" in message and "f" in message
    assert svc.list_objects() == []


def test_rename_moves_object(make_service, tmp_path):
    svc = make_service()
    f = tmp_path / "f"
    f.write_text("payload")
    svc.upload(f, "old/name.txt", storage_class="standard")
    message = svc.rename("old/name.txt", "new/name.txt")
    assert "renamed" in message
    keys = [o.key for o in svc.list_objects()]
    assert "new/name.txt" in keys and "old/name.txt" not in keys


def test_rename_preserves_storage_class(make_service, tmp_path):
    svc = make_service()
    f = tmp_path / "f"
    f.write_text("x")
    svc.upload(f, "a.bin", storage_class="standard_ia")
    svc.rename("a.bin", "b.bin")
    assert svc.status("b.bin").storage_class == "STANDARD_IA"


def test_rename_rejects_existing_target(make_service, tmp_path):
    svc = make_service()
    for name in ("a.txt", "b.txt"):
        f = tmp_path / name
        f.write_text("x")
        svc.upload(f, name, storage_class="standard")
    with pytest.raises(BuckletError, match="already exists"):
        svc.rename("a.txt", "b.txt")
    # both untouched
    assert {o.key for o in svc.list_objects()} == {"a.txt", "b.txt"}


def test_rename_rejects_empty_and_same(make_service, tmp_path):
    svc = make_service()
    f = tmp_path / "f"
    f.write_text("x")
    svc.upload(f, "k", storage_class="standard")
    with pytest.raises(BuckletError, match="empty"):
        svc.rename("k", "   ")
    with pytest.raises(BuckletError, match="same"):
        svc.rename("k", "k")


def test_rename_rejects_archived(make_service, tmp_path):
    svc = make_service(storage_class="DEEP_ARCHIVE")
    f = tmp_path / "f"
    f.write_text("x")
    svc.upload(f, "cold.bin")
    assert svc.status("cold.bin").state == storage.COLD
    with pytest.raises(BuckletError, match="archived"):
        svc.rename("cold.bin", "warm.bin")


def test_rename_rejects_thawing_with_wait_message(make_service, tmp_path, monkeypatch):
    svc = make_service()
    f = tmp_path / "f"
    f.write_text("x")
    svc.upload(f, "k", storage_class="standard")
    # A restore in progress: bytes aren't readable yet, so the copy would fail.
    # The advice must be "wait", not the misleading "thaw it".
    _force_status(svc, ObjectStatus("k", storage.THAWING, "DEEP_ARCHIVE"), monkeypatch)
    from bucklet import s3

    def no_copy(*_a, **_k):
        pytest.fail("copy_object must not run for a thawing object")

    monkeypatch.setattr(s3, "copy_object", no_copy)
    with pytest.raises(BuckletError, match="being thawed"):
        svc.rename("k", "k2")


def test_rename_refuses_without_delete_permission(make_service, tmp_path, monkeypatch):
    svc = make_service()
    f = tmp_path / "f"
    f.write_text("x")
    svc.upload(f, "a.txt", storage_class="standard")
    from bucklet import s3

    monkeypatch.setattr(s3, "can_delete", lambda *a, **k: False)
    with pytest.raises(BuckletError, match="can't delete the original"):
        svc.rename("a.txt", "b.txt")
    # it refused *before* copying, so no duplicate was made
    assert [o.key for o in svc.list_objects()] == ["a.txt"]


def test_rename_rolls_back_copy_when_delete_fails(make_service, tmp_path, monkeypatch):
    svc = make_service()
    f = tmp_path / "f"
    f.write_text("x")
    svc.upload(f, "a.txt", storage_class="standard")
    from bucklet import s3

    real_delete = s3.delete_object

    def selective(client, bucket, key):
        # the permission probe and the rollback go through; the real original
        # "can't" be removed (an exact-key deny that the pre-check can't see).
        if key == "a.txt":
            raise BuckletError("access denied (check the IAM policy and keys)")
        return real_delete(client, bucket, key)

    monkeypatch.setattr(s3, "delete_object", selective)
    with pytest.raises(BuckletError, match="couldn't remove the original"):
        svc.rename("a.txt", "b.txt")
    # the copy was rolled back, so neither a duplicate nor a lost original
    assert [o.key for o in svc.list_objects()] == ["a.txt"]


def test_delete_propagates_access_denied(make_service, monkeypatch):
    svc = make_service()
    from bucklet import s3

    def denied(client, bucket, key):
        raise BuckletError("access denied (check the IAM policy and keys)")

    monkeypatch.setattr(s3, "delete_object", denied)
    with pytest.raises(BuckletError, match="access denied"):
        svc.delete("anything")


def test_upload_many_uploads_all(make_service, tmp_path):
    svc = make_service()
    plan = []
    for name in ("a.txt", "b.txt", "c.txt"):
        f = tmp_path / name
        f.write_text(name * 10)
        plan.append((f, name))

    results = svc.upload_many(plan, storage_class="standard")
    assert {key for key, err in results} == {"a.txt", "b.txt", "c.txt"}
    assert all(err is None for _key, err in results)
    assert [o.key for o in svc.list_objects()] == ["a.txt", "b.txt", "c.txt"]


def test_upload_many_reports_aggregate_progress(make_service, tmp_path):
    svc = make_service()
    plan = []
    for name in ("a", "b"):
        f = tmp_path / name
        f.write_text("x" * 100)
        plan.append((f, name))

    seen = []
    svc.upload_many(plan, storage_class="standard", progress=lambda *a: seen.append(a))
    assert seen, "progress should be reported"
    last_sent, total, done, total_files = seen[-1]
    assert done == total_files == 2
    assert last_sent == total  # all bytes accounted for at the end


def test_upload_many_partial_failure_does_not_abort(make_service, tmp_path, monkeypatch):
    svc = make_service()
    plan = []
    for name in ("ok1", "bad", "ok2"):
        f = tmp_path / name
        f.write_text("data")
        plan.append((f, name))

    from bucklet import s3

    real = s3.upload_file

    def flaky(client, bucket, local, key, sc, **kw):
        if key == "bad":
            raise BuckletError("denied")
        return real(client, bucket, local, key, sc, **kw)

    monkeypatch.setattr(s3, "upload_file", flaky)
    results = dict(svc.upload_many(plan, storage_class="standard"))
    assert results["ok1"] is None and results["ok2"] is None
    assert isinstance(results["bad"], BuckletError)
    # the two good files still made it despite the middle failure
    assert set(o.key for o in svc.list_objects()) == {"ok1", "ok2"}


def test_upload_many_empty_plan(make_service):
    assert make_service().upload_many([]) == []


def test_upload_many_survives_unexpected_error(make_service, tmp_path, monkeypatch):
    """A non-BuckletError on one file must not crash the whole batch."""
    svc = make_service()
    plan = []
    for name in ("ok1", "boom", "ok2"):
        f = tmp_path / name
        f.write_text("data")
        plan.append((f, name))

    from bucklet import s3

    real = s3.upload_file

    def explode(client, bucket, local, key, sc, **kw):
        if key == "boom":
            raise RuntimeError("kaboom")  # not a BuckletError
        return real(client, bucket, local, key, sc, **kw)

    monkeypatch.setattr(s3, "upload_file", explode)
    results = dict(svc.upload_many(plan, storage_class="standard"))
    assert results["ok1"] is None and results["ok2"] is None
    assert isinstance(results["boom"], BuckletError)  # wrapped, not raised
    assert set(o.key for o in svc.list_objects()) == {"ok1", "ok2"}


def test_upload_many_survives_buggy_progress_callback(make_service, tmp_path):
    """A progress callback that raises must not fail the uploads."""
    svc = make_service()
    f = tmp_path / "a"
    f.write_text("x" * 50)

    def bad_progress(*_a):
        raise ValueError("bad callback")

    results = svc.upload_many([(f, "a")], storage_class="standard", progress=bad_progress)
    assert results == [("a", None)]  # uploaded fine despite the callback
    assert [o.key for o in svc.list_objects()] == ["a"]
