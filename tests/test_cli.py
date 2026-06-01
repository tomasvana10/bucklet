"""End-to-end CLI tests (config CRUD without AWS, object ops against moto)."""

from bucklet.cli import main


def test_profile_add_and_ls(config_dir, capsys):
    assert (
        main(
            ["profile", "add", "p", "--bucket", "b", "--region", "us-east-1", "--class", "standard"]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["profile", "ls"]) == 0
    out = capsys.readouterr().out
    assert "p" in out and "b" in out and "standard" in out


def test_unknown_class_returns_error(config_dir, capsys):
    assert main(["profile", "add", "p", "--bucket", "b", "--class", "frostbite"]) == 1
    assert "frostbite" in capsys.readouterr().err


def test_ls_without_profile_errors(config_dir, capsys):
    assert main(["ls"]) == 1
    assert "no profile" in capsys.readouterr().err


def test_profile_show_archival_note(config_dir, capsys):
    main(["profile", "add", "cold", "--bucket", "b", "--class", "deep_archive"])
    capsys.readouterr()
    assert main(["profile", "show", "cold"]) == 0
    out = capsys.readouterr().out
    assert "DEEP_ARCHIVE" in out and "thaw" in out


def _add_profile(bucket: str, cls: str = "standard"):
    main(
        [
            "profile",
            "add",
            "p",
            "--bucket",
            bucket,
            "--region",
            "us-east-1",
            "--class",
            cls,
            "--access-key",
            "testing",
            "--secret",
            "testing",
        ]
    )


def test_up_ls_stat_cycle(config_dir, s3_client, capsys, tmp_path):
    s3_client.create_bucket(Bucket="cli-bucket")
    _add_profile("cli-bucket")
    f = tmp_path / "doc.txt"
    f.write_text("hello")
    key = str(f.resolve()).lstrip("/")
    capsys.readouterr()

    assert main(["up", str(f), "--profile", "p"]) == 0
    assert main(["ls", "--profile", "p"]) == 0
    assert key in capsys.readouterr().out

    assert main(["stat", key, "--profile", "p"]) == 0
    assert "STANDARD" in capsys.readouterr().out


def test_global_profile_before_subcommand(config_dir, s3_client, capsys):
    s3_client.create_bucket(Bucket="cli-bucket")
    _add_profile("cli-bucket")
    capsys.readouterr()
    # --profile given before the subcommand must work too
    assert main(["--profile", "p", "ls"]) == 0


def test_up_with_class_override(config_dir, s3_client, capsys, tmp_path):
    s3_client.create_bucket(Bucket="cli-bucket")
    _add_profile("cli-bucket", cls="standard")  # profile default standard
    f = tmp_path / "a.txt"
    f.write_text("x")
    key = str(f.resolve()).lstrip("/")
    capsys.readouterr()
    # override to deep_archive on this upload only
    assert main(["up", str(f), "--class", "deep_archive", "--profile", "p"]) == 0
    main(["stat", key, "--profile", "p"])
    assert "DEEP_ARCHIVE" in capsys.readouterr().out


def test_thaw_standard_object_is_noop(config_dir, s3_client, capsys, tmp_path):
    s3_client.create_bucket(Bucket="cli-bucket")
    _add_profile("cli-bucket")
    f = tmp_path / "f"
    f.write_text("x")
    main(["up", str(f), "--profile", "p"])
    key = str(f.resolve()).lstrip("/")
    capsys.readouterr()
    assert main(["thaw", key, "--profile", "p"]) == 0
    assert "no thaw needed" in capsys.readouterr().out


def test_get_downloads(config_dir, s3_client, capsys, tmp_path):
    s3_client.create_bucket(Bucket="cli-bucket")
    _add_profile("cli-bucket")
    f = tmp_path / "src.txt"
    f.write_text("payload")
    main(["up", str(f), "--profile", "p"])
    key = str(f.resolve()).lstrip("/")

    outdir = tmp_path / "downloads"
    capsys.readouterr()
    assert main(["get", key, "-o", str(outdir), "--profile", "p"]) == 0
    assert (outdir / key).read_text() == "payload"


def test_get_missing_key_exit_1(config_dir, s3_client, capsys):
    s3_client.create_bucket(Bucket="cli-bucket")
    _add_profile("cli-bucket")
    capsys.readouterr()
    assert main(["get", "no-such-key", "--profile", "p"]) == 1
    assert "no match" in capsys.readouterr().err


def test_thaw_glob_no_match_exit_1(config_dir, s3_client, capsys):
    s3_client.create_bucket(Bucket="cli-bucket")
    _add_profile("cli-bucket")
    capsys.readouterr()
    assert main(["thaw", "nothing-*", "--profile", "p"]) == 1
    assert "no match" in capsys.readouterr().err


def test_stat_missing_exit_1(config_dir, s3_client, capsys):
    s3_client.create_bucket(Bucket="cli-bucket")
    _add_profile("cli-bucket")
    capsys.readouterr()
    assert main(["stat", "ghost", "--profile", "p"]) == 1
    assert "no match" in capsys.readouterr().err


def test_get_download_error_exit_1(config_dir, s3_client, capsys, tmp_path, monkeypatch):
    s3_client.create_bucket(Bucket="cli-bucket")
    _add_profile("cli-bucket")
    f = tmp_path / "f"
    f.write_text("x")
    main(["up", str(f), "--profile", "p"])
    key = str(f.resolve()).lstrip("/")

    from bucklet.errors import BuckletError
    from bucklet.service import Service

    def boom(self, *args, **kwargs):
        raise BuckletError("not restored yet, thaw it first")

    monkeypatch.setattr(Service, "download", boom)
    capsys.readouterr()
    assert main(["get", key, "-o", str(tmp_path / "out"), "--profile", "p"]) == 1
    out = capsys.readouterr().out
    assert "ERR" in out and "not restored" in out


def test_ls_long_search_and_state(config_dir, s3_client, capsys, tmp_path):
    s3_client.create_bucket(Bucket="cli-bucket")
    _add_profile("cli-bucket")  # standard default
    warm = tmp_path / "warm.txt"
    warm.write_text("1")
    cold = tmp_path / "cold.bin"
    cold.write_text("2")
    main(["up", str(warm), "--profile", "p"])
    main(["up", str(cold), "--class", "deep_archive", "--profile", "p"])
    kw = str(warm.resolve()).lstrip("/")
    kc = str(cold.resolve()).lstrip("/")

    capsys.readouterr()
    assert main(["ls", "-l", "--profile", "p"]) == 0
    long_out = capsys.readouterr().out
    assert "STANDARD" in long_out and "DEEP_ARCHIVE" in long_out and "cold" in long_out

    assert main(["ls", "--search", "cold.bin", "--profile", "p"]) == 0
    search_out = capsys.readouterr().out
    assert kc in search_out and kw not in search_out

    assert main(["ls", "--state", "cold", "--profile", "p"]) == 0
    cold_out = capsys.readouterr().out
    assert kc in cold_out and kw not in cold_out

    assert main(["ls", "--state", "available", "--profile", "p"]) == 0
    avail_out = capsys.readouterr().out
    assert kw in avail_out and kc not in avail_out


def test_thaw_standard_tier_is_passed(config_dir, s3_client, capsys, tmp_path, monkeypatch):
    s3_client.create_bucket(Bucket="cli-bucket")
    _add_profile("cli-bucket", cls="deep_archive")
    f = tmp_path / "f"
    f.write_text("x")
    main(["up", str(f), "--profile", "p"])
    key = str(f.resolve()).lstrip("/")

    from bucklet.service import Service

    captured = {}

    def spy(self, _key, *, tier="Bulk", **_):
        captured["tier"] = tier
        return "ok"

    monkeypatch.setattr(Service, "restore", spy)
    capsys.readouterr()
    assert main(["thaw", key, "--standard", "--profile", "p"]) == 0
    assert captured["tier"] == "Standard"


def test_no_subcommand_launches_tui(config_dir, monkeypatch):
    from bucklet.tui import app as app_mod

    called = {}

    def fake_run_tui(_config, profile_arg=None, *, allow_deletion=False):
        called["profile"] = profile_arg
        called["allow_deletion"] = allow_deletion

    monkeypatch.setattr(app_mod, "run_tui", fake_run_tui)
    assert main([]) == 0
    # deletion is off unless explicitly requested
    assert called == {"profile": None, "allow_deletion": False}


def test_allow_deletion_flag_reaches_tui(config_dir, monkeypatch):
    from bucklet.tui import app as app_mod

    called = {}

    def fake_run_tui(_config, profile_arg=None, *, allow_deletion=False):
        called["allow_deletion"] = allow_deletion

    monkeypatch.setattr(app_mod, "run_tui", fake_run_tui)
    assert main(["--allow-deletion"]) == 0
    assert called["allow_deletion"] is True


def test_no_delete_subcommand_exists(config_dir, capsys):
    # Deletion is intentionally TUI-only: there must be no `delete`/`rm` object
    # subcommand on the CLI. argparse exits 2 on an unknown subcommand.
    import pytest

    for bad in ("delete", "rm"):
        with pytest.raises(SystemExit) as exc:
            main([bad, "some-key"])
        assert exc.value.code == 2


def test_profile_flag_after_profile_subcommand(config_dir, capsys):
    # the documented "before or after" contract must hold for profile commands too
    main(["profile", "add", "p", "--bucket", "b"])
    capsys.readouterr()
    assert main(["profile", "ls", "--profile", "p"]) == 0  # must not argparse-exit(2)


def test_class_completer_includes_aliases():
    from bucklet.cli import _class_completer

    out = _class_completer()
    assert "deep_archive" in out  # canonical
    assert "da" in out  # alias


def test_profile_completer_lists_saved(config_dir):
    from bucklet.cli import _profile_completer

    main(["profile", "add", "p", "--bucket", "b"])
    assert "p" in _profile_completer()


def test_profile_tune_set_and_reset(config_dir, capsys):
    main(["profile", "add", "p", "--bucket", "b"])
    capsys.readouterr()

    # set a size and a count
    rc = main(
        ["profile", "tune", "p", "--multipart-chunksize", "64MB", "--upload-concurrency", "8"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "64.0MB" in out and "8" in out

    # the values persist
    from bucklet.config import Config

    prof = Config.load().get("p")
    assert prof.multipart_chunksize == 64 * 1024**2
    assert prof.upload_concurrency == 8

    # reset one of them back to default
    assert main(["profile", "tune", "p", "--reset", "multipart-chunksize"]) == 0
    capsys.readouterr()
    prof = Config.load().get("p")
    assert prof.multipart_chunksize is None  # reset -> default
    assert prof.upload_concurrency == 8  # untouched


def test_profile_tune_reset_all(config_dir, capsys):
    main(["profile", "add", "p", "--bucket", "b"])
    main(["profile", "tune", "p", "--multipart-chunksize", "64MB", "--max-concurrency", "2"])
    capsys.readouterr()
    assert main(["profile", "tune", "p", "--reset", "all"]) == 0
    from bucklet.config import Config

    prof = Config.load().get("p")
    assert prof.multipart_chunksize is None
    assert prof.max_concurrency is None


def test_profile_tune_rejects_bad_value(config_dir, capsys):
    main(["profile", "add", "p", "--bucket", "b"])
    capsys.readouterr()
    assert main(["profile", "tune", "p", "--multipart-chunksize", "lots"]) == 1
    assert "size" in capsys.readouterr().err


def test_profile_add_preserves_existing_tuning(config_dir, capsys):
    main(["profile", "add", "p", "--bucket", "b"])
    main(["profile", "tune", "p", "--upload-concurrency", "8"])
    capsys.readouterr()
    # re-adding to change the bucket must not wipe the tuning
    assert main(["profile", "add", "p", "--bucket", "b2", "--region", "eu-west-1"]) == 0
    from bucklet.config import Config

    prof = Config.load().get("p")
    assert prof.bucket == "b2"  # connection settings updated
    assert prof.upload_concurrency == 8  # tuning preserved


def test_profile_show_includes_tuning(config_dir, capsys):
    main(["profile", "add", "p", "--bucket", "b"])
    capsys.readouterr()
    assert main(["profile", "show", "p"]) == 0
    out = capsys.readouterr().out
    assert "tuning" in out and "parallel uploads" in out and "(default)" in out


def test_up_basename_key(config_dir, s3_client, capsys, tmp_path):
    s3_client.create_bucket(Bucket="cli-bucket")
    _add_profile("cli-bucket")
    f = tmp_path / "deep" / "nested" / "doc.txt"
    f.parent.mkdir(parents=True)
    f.write_text("hello")
    capsys.readouterr()
    # --basename-key stores the object under just its name, not the abs path
    assert main(["up", str(f), "--basename-key", "--profile", "p"]) == 0
    assert main(["ls", "--profile", "p"]) == 0
    out = capsys.readouterr().out
    assert "doc.txt" in out
    assert str(f.resolve()).lstrip("/") not in out  # not the mirrored absolute path


def test_up_multiple_files(config_dir, s3_client, capsys, tmp_path):
    s3_client.create_bucket(Bucket="cli-bucket")
    _add_profile("cli-bucket")
    paths = []
    for name in ("one.txt", "two.txt", "three.txt"):
        f = tmp_path / name
        f.write_text(name)
        paths.append(str(f))
    capsys.readouterr()
    assert main(["up", *paths, "--profile", "p"]) == 0
    err = capsys.readouterr().err
    assert "3/3 uploaded" in err
