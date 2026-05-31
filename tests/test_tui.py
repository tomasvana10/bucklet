"""Smoke tests for the Textual TUI, driven with a fake (no-network) service.

The app is UI-only: it calls Service methods. A fake service lets us exercise
the app's wiring (loading, filtering, thaw gating, detail) without AWS.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from textual.widgets import DataTable

from bucklet import storage
from bucklet.config import Config
from bucklet.errors import BuckletError
from bucklet.models import ObjectInfo, ObjectStatus, Profile
from bucklet.tui.app import BuckletApp


class _FakeClient:
    """Stands in for a boto3 client; only needs to be closeable."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeService:
    def __init__(self):
        self.profile = Profile(
            name="t", bucket="b", region="us-east-1", storage_class="DEEP_ARCHIVE"
        )
        self.client = _FakeClient()
        self.objects = [
            ObjectInfo("cold.bin", 100, None, "DEEP_ARCHIVE"),
            ObjectInfo("doc.txt", 50, None, "STANDARD"),
        ]
        self._statuses = {
            "cold.bin": ObjectStatus("cold.bin", storage.COLD, "DEEP_ARCHIVE", 100),
        }
        self.restored: list[tuple[str, str]] = []
        self.downloaded: list[str] = []
        self.deleted: list[str] = []
        self.uploaded: list[str] = []
        # The (local, key) plan plan_upload hands back, and per-key upload errors.
        self.plan: list[tuple[Path, str]] = []
        self.upload_errors: dict[str, BuckletError] = {}
        # When set, the matching operation raises BuckletError to simulate a
        # denied/broken bucket (e.g. an archive-only key that cannot delete).
        self.list_error: str | None = None
        self.delete_error: str | None = None

    def list_objects(self, prefix: str = ""):
        if self.list_error:
            raise BuckletError(self.list_error)
        return list(self.objects)

    def status(self, key: str):
        return self._statuses.get(key) or ObjectStatus(key, storage.AVAILABLE, "STANDARD")

    def restore(self, key: str, *, tier: str = "Bulk", days: int = 7):
        self.restored.append((key, tier))
        return "restore requested"

    def download(self, key: str, dest: Path, progress: Callable[[int], None] | None = None):
        self.downloaded.append(key)
        return dest

    def delete(self, key: str):
        if self.delete_error:
            raise BuckletError(self.delete_error)
        self.deleted.append(key)
        # Mirror S3: a successful delete is reflected in subsequent listings.
        self.objects = [o for o in self.objects if o.key != key]
        return f"deleted {key}"

    def plan_upload(self, paths, prefix: str = ""):
        return list(self.plan)

    def upload_many(self, plan, *, storage_class=None, progress=None):
        plan = list(plan)
        total = len(plan)
        results = []
        for i, (_local, key) in enumerate(plan, 1):
            if progress is not None:
                progress(i, total or 1, i, total)
            err = self.upload_errors.get(key)
            if err is None:
                self.uploaded.append(key)
                # mirror S3: a successful upload shows up in the next listing
                self.objects.append(ObjectInfo(key, 1, None, "STANDARD"))
            results.append((key, err))
        return results


def _app(tmp_path: Path, fake: FakeService, *, allow_deletion: bool = False):
    return BuckletApp(
        config=Config(tmp_path / "config.json"), service=fake, allow_deletion=allow_deletion
    )


def _messages(app):
    """(text, severity) pairs currently shown in the message stack."""
    from bucklet.tui.app import MessageStack

    return app.query_one(MessageStack).messages


def _has_message(app, substring: str, severity: str | None = None) -> bool:
    return any(
        substring in text and (severity is None or sev == severity)
        for text, sev in _messages(app)
    )


async def test_table_populates(tmp_path):
    app = _app(tmp_path, FakeService())
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        table = app.query_one(DataTable)
        assert table.row_count == 2


async def test_filter_cycles_to_cold(tmp_path):
    fake = FakeService()
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("f")
        await pilot.pause()
        assert app.state_filter == storage.COLD
        # only the cold object remains displayed
        assert [o.key for o in app.displayed] == ["cold.bin"]


async def test_thaw_cold_object_calls_restore(tmp_path):
    fake = FakeService()
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("t")  # cursor is on row 0 == cold.bin
        await app.workers.wait_for_complete()
        assert ("cold.bin", "Bulk") in fake.restored


async def test_thaw_available_object_does_nothing(tmp_path):
    fake = FakeService()
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("down")  # move to doc.txt (available)
        await pilot.press("t")
        await app.workers.wait_for_complete()
        assert fake.restored == []  # thaw not offered for available objects


async def test_thaw_standard_tier(tmp_path):
    fake = FakeService()
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("T")  # shift+t -> Standard tier, on the cold row
        await app.workers.wait_for_complete()
        assert ("cold.bin", "Standard") in fake.restored


async def test_download_available_object(tmp_path):
    fake = FakeService()
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("down")  # move to doc.txt (available)
        await pilot.press("g")
        await app.workers.wait_for_complete()
        assert "doc.txt" in fake.downloaded


async def test_download_cold_object_is_blocked(tmp_path):
    fake = FakeService()
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("g")  # cursor on cold.bin
        await app.workers.wait_for_complete()
        assert fake.downloaded == []


async def test_detail_screen_opens(tmp_path):
    app = _app(tmp_path, FakeService())
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("i")
        await pilot.pause()
        assert len(app.screen_stack) > 1


async def test_ctrl_c_quits(tmp_path):
    app = _app(tmp_path, FakeService())
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert app._exit is True


async def test_initial_error_is_shown(tmp_path):
    app = BuckletApp(
        config=Config(tmp_path / "config.json"), service=None, error="cannot open 'b': denied"
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert _has_message(app, "denied", "error")


async def test_initial_profile_opens_in_background(tmp_path, monkeypatch):
    from bucklet.tui import app as app_mod

    fake = FakeService()
    monkeypatch.setattr(app_mod.Service, "open", lambda profile, validate=True: fake)
    app = BuckletApp(
        config=Config(tmp_path / "config.json"),
        initial_profile=Profile(name="p", bucket="b"),
    )
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.service is fake
        assert app.query_one(DataTable).row_count == 2


def test_run_tui_passes_initial_profile(tmp_path, monkeypatch):
    from bucklet.tui import app as app_mod

    cfg = Config(tmp_path / "config.json")
    cfg.add(Profile(name="p", bucket="bkt"))
    captured = {}
    monkeypatch.setattr(app_mod.BuckletApp, "run", lambda self, *a, **k: captured.update(app=self))
    app_mod.run_tui(cfg, "p")
    app = captured["app"]
    # The profile opens lazily in a worker, so it is not yet attached.
    assert app.service is None
    assert app._initial_profile is not None and app._initial_profile.bucket == "bkt"
    assert app._initial_error == ""


def test_run_tui_without_profile(tmp_path, monkeypatch):
    from bucklet.tui import app as app_mod

    cfg = Config(tmp_path / "config.json")  # nothing configured
    captured = {}
    monkeypatch.setattr(app_mod.BuckletApp, "run", lambda self, *a, **k: captured.update(app=self))
    app_mod.run_tui(cfg, None)
    app = captured["app"]
    assert app.service is None
    assert app._initial_profile is None
    assert app._initial_error == ""


# --- deletion (gated behind --allow-deletion) -------------------------------


async def test_delete_binding_hidden_without_flag(tmp_path):
    app = _app(tmp_path, FakeService())  # allow_deletion defaults False
    async with app.run_test():
        await app.workers.wait_for_complete()
        # False (not None) is what removes the key from the footer entirely in
        # Textual; None would only grey it out. We want it gone.
        assert app.check_action("delete", ()) is False
        assert "d" not in app.screen.active_bindings  # really absent from the footer


async def test_delete_binding_enabled_with_flag(tmp_path):
    app = _app(tmp_path, FakeService(), allow_deletion=True)
    async with app.run_test():
        await app.workers.wait_for_complete()
        assert app.check_action("delete", ()) is True
        assert "d" in app.screen.active_bindings


async def test_delete_does_nothing_without_flag(tmp_path):
    fake = FakeService()
    app = _app(tmp_path, fake)  # deletion not allowed
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("d")  # cursor on cold.bin
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert fake.deleted == []
        assert len(app.screen_stack) == 1  # no confirm dialog appeared


async def test_delete_confirmed_removes_object(tmp_path):
    fake = FakeService()
    app = _app(tmp_path, fake, allow_deletion=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("d")  # cursor on cold.bin -> confirm dialog
        await pilot.pause()
        assert len(app.screen_stack) > 1  # confirmation is required
        await pilot.press("y")  # confirm
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert fake.deleted == ["cold.bin"]
        assert "cold.bin" not in [o.key for o in app.objects]
        assert app.query_one(DataTable).row_count == 1
        # the user is told it worked
        assert _has_message(app, "deleted")


async def test_delete_then_reload_does_not_resurrect(tmp_path):
    """A locally-removed row must not reappear when the bucket is re-listed."""
    fake = FakeService()
    app = _app(tmp_path, fake, allow_deletion=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("y")
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.reload()  # 'r' would do the same; re-lists from the (fake) bucket
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "cold.bin" not in [o.key for o in app.objects]
        assert app.query_one(DataTable).row_count == 1


async def test_delete_cancelled_keeps_object(tmp_path):
    fake = FakeService()
    app = _app(tmp_path, fake, allow_deletion=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("n")  # decline
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert fake.deleted == []
        assert "cold.bin" in [o.key for o in app.objects]
        assert app.query_one(DataTable).row_count == 2


async def test_delete_cancelled_with_escape_keeps_object(tmp_path):
    fake = FakeService()
    app = _app(tmp_path, fake, allow_deletion=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("escape")  # esc is a distinct cancel path for the dialog
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert fake.deleted == []
        assert len(app.screen_stack) == 1  # dialog dismissed
        assert "cold.bin" in [o.key for o in app.objects]


async def test_delete_while_filtered_removes_from_view(tmp_path):
    fake = FakeService()
    app = _app(tmp_path, fake, allow_deletion=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("f")  # filter -> COLD; only cold.bin shows
        await pilot.pause()
        assert [o.key for o in app.displayed] == ["cold.bin"]
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("y")
        await app.workers.wait_for_complete()
        await pilot.pause()
        # gone from both the model and the (still-filtered) view
        assert "cold.bin" not in [o.key for o in app.objects]
        assert app.displayed == []


async def test_failed_delete_keeps_object_and_warns(tmp_path):
    fake = FakeService()
    fake.delete_error = "access denied (check the IAM policy and keys)"
    app = _app(tmp_path, fake, allow_deletion=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("y")  # confirm; the delete then fails
        await app.workers.wait_for_complete()
        await pilot.pause()
        # the object must stay put when S3 refuses the delete
        assert "cold.bin" in [o.key for o in app.objects]
        assert app.query_one(DataTable).row_count == 2
        assert _has_message(app, "access denied", "error")


# --- graceful degradation when the bucket can't be listed -------------------


async def test_list_failure_shows_empty_table(tmp_path):
    fake = FakeService()
    fake.list_error = "access denied (check the IAM policy and keys)"
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        # no crash; empty table and a surfaced error
        assert app.query_one(DataTable).row_count == 0
        assert app.objects == []
        assert _has_message(app, "access denied", "error")


async def test_actions_safe_on_empty_listing(tmp_path):
    """With nothing listed, the row-actions must no-op rather than crash."""
    fake = FakeService()
    fake.list_error = "boom"
    app = _app(tmp_path, fake, allow_deletion=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        for key in ("i", "t", "g", "d", "r"):
            await pilot.press(key)
            await pilot.pause()
        await app.workers.wait_for_complete()
        assert fake.deleted == []
        assert fake.downloaded == []
        assert fake.restored == []
        # no modal (info/confirm) was pushed on an empty listing
        assert len(app.screen_stack) == 1


# --- footer: divider + greying object actions -------------------------------


async def test_footer_has_divider(tmp_path):
    from textual.widgets import Static

    from bucklet.tui.app import BuckletFooter

    app = _app(tmp_path, FakeService())
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        footer = app.query_one(BuckletFooter)
        dividers = [w for w in footer.query(Static) if "footer-divider" in w.classes]
        assert len(dividers) == 1


async def test_object_actions_greyed_when_empty(tmp_path):
    # with objects listed, object actions are live
    app = _app(tmp_path, FakeService())
    async with app.run_test():
        await app.workers.wait_for_complete()
        for action in ("detail", "thaw", "download"):
            assert app.check_action(action, ()) is True

    # with nothing listed, they grey out (None) rather than vanish (False)
    fake = FakeService()
    fake.list_error = "boom"
    app2 = _app(tmp_path, fake)
    async with app2.run_test():
        await app2.workers.wait_for_complete()
        assert app2.displayed == []
        for action in ("detail", "thaw", "download"):
            assert app2.check_action(action, ()) is None
        # bucket-wide actions stay available
        assert app2.check_action("upload", ()) is True
        assert app2.check_action("settings", ()) is True


# --- filter no longer toasts ------------------------------------------------


async def test_filter_does_not_toast(tmp_path):
    fake = FakeService()
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        calls = []
        app.notify = lambda *a, **k: calls.append((a, k))  # type: ignore[method-assign]
        await pilot.press("f")
        await pilot.pause()
        assert app.state_filter == storage.COLD  # still cycles
        assert calls == []  # but no toast


# --- errors go to the message line, not a toast -----------------------------


async def test_errors_show_in_message_not_toast(tmp_path):
    fake = FakeService()
    fake.delete_error = "access denied"
    app = _app(tmp_path, fake, allow_deletion=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        calls = []
        app.notify = lambda *a, **k: calls.append((a, k))  # type: ignore[method-assign]
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("y")  # confirm; delete fails
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert _has_message(app, "access denied", "error")
        assert calls == []  # surfaced once, in the message stack — no toast


# --- the message stack itself (stacking, keys, severity, trim, expiry) ------


def _item(app, text):
    from bucklet.tui.app import MessageStack

    return next(i for i in app.query_one(MessageStack)._items if i.message_text == text)


async def test_messages_stack(tmp_path):
    app = _app(tmp_path, FakeService())
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.flash("first", timeout=30.0)
        app.flash("second", severity="warning", timeout=30.0)
        await pilot.pause()
        texts = [t for t, _ in _messages(app)]
        assert "first" in texts and "second" in texts  # both visible, stacked
        assert dict(_messages(app))["second"] == "warning"  # severity carried
        # severity drives the actual colour class, not just stored state
        assert _item(app, "second").has_class("-warning")
        assert _item(app, "first").has_class("-info")


async def test_keyed_message_updates_in_place(tmp_path):
    app = _app(tmp_path, FakeService())
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.flash("uploading 1/3", key="op", timeout=30.0)
        app.flash("uploading 2/3", key="op", timeout=30.0)
        await pilot.pause()
        texts = [t for t, _ in _messages(app)]
        # one keyed line, updated — not two stacked
        assert "uploading 1/3" not in texts
        assert texts.count("uploading 2/3") == 1


async def test_message_stack_trims_to_max(tmp_path):
    from bucklet.tui.app import MessageStack

    app = _app(tmp_path, FakeService())
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        for i in range(MessageStack.MAX + 3):
            app.flash(f"msg {i}", timeout=30.0)
        await pilot.pause()
        texts = [t for t, _ in _messages(app)]
        assert len(texts) <= MessageStack.MAX
        assert "msg 0" not in texts  # oldest trimmed
        assert f"msg {MessageStack.MAX + 2}" in texts  # newest kept


async def test_messages_expire(tmp_path):
    import asyncio

    app = _app(tmp_path, FakeService())
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.flash("blip", timeout=0.05)
        await pilot.pause()
        assert _has_message(app, "blip")
        await asyncio.sleep(0.15)  # let the expiry timer fire
        await pilot.pause()
        assert not _has_message(app, "blip")


async def test_empty_text_clears_keyed_message(tmp_path):
    app = _app(tmp_path, FakeService())
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.flash("working…", key="op", timeout=30.0)
        await pilot.pause()
        assert _has_message(app, "working…")
        app.flash("", key="op")  # empty text clears the keyed line
        await pilot.pause()
        assert not _has_message(app, "working…")


async def test_keyed_update_restarts_expiry_timer(tmp_path):
    """A keyed line being updated must not expire on its original timer —
    otherwise a long progress stream would vanish mid-transfer."""
    import asyncio

    app = _app(tmp_path, FakeService())
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.flash("progress 10%", key="op", timeout=0.3)
        await asyncio.sleep(0.2)  # most of the original timeout elapses
        app.flash("progress 90%", key="op", timeout=0.3)  # update restarts it
        await asyncio.sleep(0.2)  # past the ORIGINAL deadline, within the new one
        await pilot.pause()
        assert _has_message(app, "progress 90%")  # still here -> timer restarted


async def test_keyed_severity_change(tmp_path):
    """A keyed line can flip information -> error (progress that then fails)."""
    app = _app(tmp_path, FakeService())
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.flash("uploading…", key="op", timeout=30.0)
        await pilot.pause()
        assert _item(app, "uploading…").has_class("-info")
        app.flash("upload failed", severity="error", key="op", timeout=30.0)
        await pilot.pause()
        item = _item(app, "upload failed")
        assert item.has_class("-error") and not item.has_class("-info")


async def test_loading_cleared_on_success(tmp_path):
    fake = FakeService()
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        # the transient "loading…" status is gone once the listing succeeds
        assert not _has_message(app, "loading")


async def test_error_replaces_loading_in_place(tmp_path):
    fake = FakeService()
    fake.list_error = "access denied"
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        # the error took over the "loading…" line (same key) rather than stacking
        assert not _has_message(app, "loading")
        assert _has_message(app, "access denied", "error")
        assert len(_messages(app)) == 1


# --- header / bar don't duplicate the bucket --------------------------------


async def test_header_and_bar_do_not_duplicate(tmp_path):
    from textual.widgets import Static

    fake = FakeService()  # profile 't', bucket 'b', region 'us-east-1'
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        # header carries profile + region
        assert app.title == "bucklet · profile 't' · region us-east-1"
        assert app.sub_title == ""
        # bar carries the bucket, without the region-in-brackets
        bar = str(app.query_one("#bar", Static).render())
        assert "us-east-1" not in bar
        assert "[" not in bar


# --- dialogs scroll when they overflow --------------------------------------


async def test_upload_dialog_is_scrollable(tmp_path):
    from textual.containers import VerticalScroll

    fake = FakeService()
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("u")
        await pilot.pause()
        assert isinstance(app.screen.query_one("#dialog"), VerticalScroll)


# --- per-profile settings ---------------------------------------------------


def _saved_config(tmp_path):
    cfg = Config(tmp_path / "config.json")
    cfg.add(Profile(name="t", bucket="b", region="us-east-1", storage_class="DEEP_ARCHIVE"))
    cfg.save()
    return cfg


async def test_settings_applies_and_persists(tmp_path):
    from textual.widgets import Button, Input

    cfg = _saved_config(tmp_path)
    fake = FakeService()  # fake.profile.name == "t", matching the saved profile
    app = BuckletApp(config=cfg, service=fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("s")
        await pilot.pause()
        old_client = app.service.client
        screen = app.screen
        screen.query_one("#tune-multipart-chunksize", Input).value = "64MB"
        screen.query_one("#tune-upload-concurrency", Input).value = "8"
        screen.query_one("#ok", Button).press()  # robust vs. a scrolled-off button
        await pilot.pause()
        # applied to the live profile and written to the saved config
        assert app.service.profile.multipart_chunksize == 64 * 1024**2
        reloaded = Config.load(tmp_path).get("t")
        assert reloaded.multipart_chunksize == 64 * 1024**2
        assert reloaded.upload_concurrency == 8
        # the client was rebuilt (new pool) and the old one released
        assert old_client.closed is True
        assert app.service.client is not old_client


async def test_settings_blank_field_resets(tmp_path):
    from textual.widgets import Button, Input

    cfg = Config(tmp_path / "config.json")
    cfg.add(Profile(name="t", bucket="b", region="us-east-1", multipart_chunksize=64 * 1024**2))
    cfg.save()
    fake = FakeService()
    # the open profile reflects the stored value, as Service.open would materialize it
    fake.profile.multipart_chunksize = 64 * 1024**2
    app = BuckletApp(config=cfg, service=fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("s")
        await pilot.pause()
        field = app.screen.query_one("#tune-multipart-chunksize", Input)
        assert field.value == "64.0MB"  # prefilled with the current value
        field.value = ""  # clear it -> reset to default
        app.screen.query_one("#ok", Button).press()
        await pilot.pause()
        reloaded = Config.load(tmp_path).get("t")
        assert reloaded.multipart_chunksize is None


# --- upload goes through upload_many ----------------------------------------


async def test_upload_uses_upload_many(tmp_path):
    fake = FakeService()
    fake.plan = [(Path("a"), "a"), (Path("b"), "b")]
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        app._upload_worker("ignored", "STANDARD", "")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert fake.uploaded == ["a", "b"]
        # the in-worker re-list refreshed the view with the new objects
        keys = [o.key for o in app.objects]
        assert "a" in keys and "b" in keys


async def test_upload_reports_partial_failure(tmp_path):
    fake = FakeService()
    fake.plan = [(Path("a"), "a"), (Path("b"), "b")]
    fake.upload_errors = {"b": BuckletError("denied")}
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        app._upload_worker("ignored", "STANDARD", "")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert fake.uploaded == ["a"]  # the good one still uploaded
        assert _has_message(app, "failed", "error")
