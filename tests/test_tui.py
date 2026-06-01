"""Smoke tests for the Textual TUI, driven with a fake (no-network) service.

The app is UI-only: it calls Service methods. A fake service lets us exercise
the app's wiring (loading, filtering, thaw gating, detail) without AWS.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from textual.widgets import Button, Checkbox, DataTable, Input, RadioButton, RadioSet, Tree

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
        self.restored: list[tuple[str, str, int]] = []
        self.downloaded: list[str] = []
        self.deleted: list[str] = []
        self.uploaded: list[str] = []
        self.renamed: list[tuple[str, str]] = []
        # The (local, key) plan plan_upload hands back, and per-key upload errors.
        self.plan: list[tuple[Path, str]] = []
        self.upload_errors: dict[str, BuckletError] = {}
        # Records of how the last plan_upload was called, for basename tests.
        self.plan_calls: list[dict] = []
        # When set, the matching operation raises BuckletError to simulate a
        # denied/broken bucket (e.g. an archive-only key that cannot delete).
        self.list_error: str | None = None
        self.delete_error: str | None = None
        self.rename_error: str | None = None

    def list_objects(self, prefix: str = ""):
        if self.list_error:
            raise BuckletError(self.list_error)
        return list(self.objects)

    def status(self, key: str):
        return self._statuses.get(key) or ObjectStatus(key, storage.AVAILABLE, "STANDARD")

    def restore(self, key: str, *, tier: str = "Bulk", days: int = 7):
        self.restored.append((key, tier, days))
        return "thaw requested"

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

    def rename(self, old_key: str, new_key: str):
        if self.rename_error:
            raise BuckletError(self.rename_error)
        self.renamed.append((old_key, new_key))
        # Mirror S3: the object now lives under the new key.
        self.objects = [
            ObjectInfo(new_key, o.size, o.last_modified, o.storage_class) if o.key == old_key else o
            for o in self.objects
        ]
        return f"renamed {old_key} -> {new_key}"

    def plan_upload(self, paths, prefix: str = "", *, basename_key: bool = False):
        self.plan_calls.append(
            {"paths": list(paths), "prefix": prefix, "basename_key": basename_key}
        )
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
        substring in text and (severity is None or sev == severity) for text, sev in _messages(app)
    )


def _select_radio(screen, set_id: str, index: int):
    """Pick option ``index`` of a RadioSet (its pressed_index has no setter)."""
    buttons = list(screen.query_one(f"#{set_id}", RadioSet).query(RadioButton))
    buttons[index].value = True


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
        assert ("cold.bin", "Bulk", 7) in fake.restored


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


async def test_advanced_thaw_picks_tier_and_days(tmp_path):
    from bucklet.tui.screens import AdvancedThawScreen

    fake = FakeService()
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("T")  # shift+t -> advanced thaw dialog, on the cold row
        await pilot.pause()
        assert isinstance(app.screen, AdvancedThawScreen)
        screen = app.screen
        _select_radio(screen, "tier", 1)  # Standard
        await pilot.pause()
        screen.query_one("#days", Input).value = "3"
        screen.query_one("#ok", Button).press()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert ("cold.bin", "Standard", 3) in fake.restored


async def test_large_object_thaw_asks_confirmation(tmp_path):
    from bucklet.tui.app import THAW_CONFIRM_BYTES

    fake = FakeService()
    fake.objects = [ObjectInfo("big.bin", THAW_CONFIRM_BYTES + 1, None, "DEEP_ARCHIVE")]
    fake._statuses = {"big.bin": ObjectStatus("big.bin", storage.COLD, "DEEP_ARCHIVE", 0)}
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("t")  # quick thaw on the big cold object
        await pilot.pause()
        # a confirmation must stand between the keypress and the restore
        assert len(app.screen_stack) > 1
        assert fake.restored == []
        await pilot.press("y")  # confirm
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert ("big.bin", "Bulk", 7) in fake.restored


async def test_advanced_thaw_enter_submits(tmp_path):
    # The dialog's "enter to thaw" hint must be true: Enter in the days field
    # confirms (the days Input wires Input.Submitted to the same handler).
    fake = FakeService()
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("T")
        await pilot.pause()
        app.screen.query_one("#days", Input).focus()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert ("cold.bin", "Bulk", 7) in fake.restored  # default tier + window


async def test_large_object_thaw_can_be_declined(tmp_path):
    from bucklet.tui.app import THAW_CONFIRM_BYTES

    fake = FakeService()
    fake.objects = [ObjectInfo("big.bin", THAW_CONFIRM_BYTES + 1, None, "DEEP_ARCHIVE")]
    fake._statuses = {"big.bin": ObjectStatus("big.bin", storage.COLD, "DEEP_ARCHIVE", 0)}
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        await pilot.press("n")  # decline
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert fake.restored == []  # nothing thawed


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


async def test_footer_has_dividers_and_profile_label(tmp_path):
    from textual.widgets import Static

    from bucklet.tui.app import BuckletFooter

    app = _app(tmp_path, FakeService())
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        footer = app.query_one(BuckletFooter)
        # Four sections (objects │ view │ bucket-wide │ profile) → three dividers.
        dividers = [w for w in footer.query(Static) if "footer-divider" in w.classes]
        assert len(dividers) == 3
        # The profile section is prefixed with a muted "Profile" label.
        labels = [w for w in footer.query(Static) if "footer-group-label" in w.classes]
        assert any("Profile" in str(w.render()) for w in labels)


async def test_footer_view_key_shows_active_view_icon(tmp_path):
    from textual.widgets._footer import FooterKey

    from bucklet.tui.app import BuckletFooter

    app = _app(tmp_path, FakeService())
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        footer = app.query_one(BuckletFooter)

        def view_desc():
            return next(k.description for k in footer.query(FooterKey) if k.action == "view")

        assert "≡" in view_desc()  # flat view is the default
        await pilot.press("v")
        await pilot.pause()
        await pilot.pause()
        assert app.view_mode == "tree"
        assert "├" in view_desc()


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


async def test_keyed_line_survives_trim(tmp_path):
    """A keyed progress line must not be dropped by trimming — otherwise its next
    update would stack a new line instead of replacing it."""
    from bucklet.tui.app import MessageStack

    app = _app(tmp_path, FakeService())
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.flash("progress 1", key="op", timeout=30.0)  # oldest line, keyed
        for i in range(MessageStack.MAX):  # enough unkeyed noise to force a trim
            app.flash(f"noise {i}", timeout=30.0)
        await pilot.pause()
        app.flash("progress 2", key="op", timeout=30.0)  # updates in place
        await pilot.pause()
        texts = [t for t, _ in _messages(app)]
        assert texts.count("progress 2") == 1  # one keyed line, updated
        assert "progress 1" not in texts  # replaced, not stacked


async def test_messages_expire(tmp_path):
    import asyncio

    app = _app(tmp_path, FakeService())
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.flash("blip", timeout=0.1)
        # present synchronously — assert before any await so the timer (which
        # only fires on the event loop) can't race us on a slow CI runner.
        assert _has_message(app, "blip")
        await asyncio.sleep(0.5)  # comfortably past the timeout
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
    """A keyed update must restart the line's expiry timer — otherwise a long
    progress stream would vanish mid-transfer. Checked deterministically (the
    timer object is replaced) rather than by racing the clock."""
    from bucklet.tui.app import MessageStack

    app = _app(tmp_path, FakeService())
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        stack = app.query_one(MessageStack)
        app.flash("progress 10%", key="op", timeout=30.0)
        item = stack._keyed["op"]
        first_timer = stack._expiry[item]
        app.flash("progress 90%", key="op", timeout=30.0)  # update -> re-arm
        assert stack._keyed["op"] is item  # same line, updated in place
        assert stack._expiry[item] is not first_timer  # timer was restarted


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


async def test_upload_basename_checkbox_passes_through(tmp_path):
    fake = FakeService()
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("u")
        await pilot.pause()
        app.screen.query_one("#path", Input).value = "/some/dir"
        app.screen.query_one("#basename", Checkbox).value = True
        app.screen.query_one("#ok", Button).press()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert fake.plan_calls[-1]["basename_key"] is True


# --- rename (copy + delete; TUI-only, ungated) ------------------------------


async def test_rename_renames_object(tmp_path):
    fake = FakeService()
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("down")  # move to doc.txt (available, renamable)
        await pilot.press("e")
        await pilot.pause()
        field = app.screen.query_one("#value", Input)
        assert field.value == "doc.txt"  # prefilled with the current key
        field.value = "notes.txt"
        app.screen.query_one("#ok", Button).press()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert ("doc.txt", "notes.txt") in fake.renamed
        keys = [o.key for o in app.objects]
        assert "notes.txt" in keys and "doc.txt" not in keys
        assert _has_message(app, "renamed")


async def test_rename_error_keeps_object(tmp_path):
    fake = FakeService()
    fake.rename_error = "an object named 'notes.txt' already exists"
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("e")
        await pilot.pause()
        app.screen.query_one("#value", Input).value = "notes.txt"
        app.screen.query_one("#ok", Button).press()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert fake.renamed == []
        assert "doc.txt" in [o.key for o in app.objects]  # untouched
        assert _has_message(app, "already exists", "error")


async def test_rename_error_does_not_double_the_key(tmp_path):
    # Service.rename messages already name the object, so the worker shows them
    # as-is. The old code prepended the key, doubling it into the eyesore
    # "doc.txt: doc.txt is archived…". Drive a key-bearing error and check once.
    fake = FakeService()
    fake.rename_error = "doc.txt is archived, you must thaw it first"
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("down")  # doc.txt
        await pilot.press("e")
        await pilot.pause()
        app.screen.query_one("#value", Input).value = "warm.txt"
        app.screen.query_one("#ok", Button).press()
        await app.workers.wait_for_complete()
        await pilot.pause()
        msg = next(text for text, _ in _messages(app) if "archived" in text)
        assert msg == "doc.txt is archived, you must thaw it first"
        assert msg.count("doc.txt") == 1


# --- tree (folder) view -----------------------------------------------------


def _nested_fake():
    fake = FakeService()
    fake.objects = [
        ObjectInfo("docs/2024/report.txt", 10, None, "STANDARD"),
        ObjectInfo("docs/2024/notes.txt", 20, None, "STANDARD"),
        ObjectInfo("images/logo.png", 30, None, "STANDARD"),
    ]
    fake._statuses = {}
    return fake


def _tree_labels(node):
    return [str(child.label) for child in node.children]


async def test_view_toggle_shows_tree(tmp_path):
    fake = _nested_fake()
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one(DataTable).display is True
        await pilot.press("v")
        await pilot.pause()
        assert app.view_mode == "tree"
        assert app.query_one(Tree).display is True
        assert app.query_one(DataTable).display is False
        # single-child chain "docs/2024" is collapsed into one folder node
        labels = _tree_labels(app.query_one(Tree).root)
        assert "docs/2024/" in labels and "images/" in labels


async def test_tree_search_expands_to_matches(tmp_path):
    fake = _nested_fake()
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("v")
        await pilot.pause()
        app.search_term = "logo"
        app.refresh_view()
        await pilot.pause()
        tree = app.query_one(Tree)
        images = next(c for c in tree.root.children if str(c.label) == "images/")
        assert images.is_expanded  # auto-expanded because it holds a match
        # the non-matching folder stays collapsed
        docs = next(c for c in tree.root.children if str(c.label) == "docs/2024/")
        assert not docs.is_expanded


async def test_tree_view_thaw_acts_on_selected_leaf(tmp_path):
    fake = FakeService()  # cold.bin (DEEP_ARCHIVE) + doc.txt, both top-level
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("v")
        await pilot.pause()
        tree = app.query_one(Tree)
        tree.move_cursor(app._leaf_nodes["cold.bin"])
        assert app._selected().key == "cold.bin"
        await pilot.press("t")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert ("cold.bin", "Bulk", 7) in fake.restored


async def test_tree_view_greys_object_actions_on_folder(tmp_path):
    from textual.widgets._footer import FooterKey

    from bucklet.tui.app import BuckletFooter

    fake = _nested_fake()
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("v")
        await pilot.pause()
        tree = app.query_one(Tree)
        footer = app.query_one(BuckletFooter)

        def detail_disabled():
            fk = next(k for k in footer.query(FooterKey) if k.action == "detail")
            return fk._disabled

        # cursor on a folder: there's no object to act on, so the object keys
        # grey out (None), and the footer reflects it.
        folder = next(c for c in tree.root.children if str(c.label) == "images/")
        tree.move_cursor(folder)
        await pilot.pause()
        await pilot.pause()
        assert app._selected() is None
        for action in ("detail", "rename", "download"):
            assert app.check_action(action, ()) is None
        assert detail_disabled() is True
        assert app.check_action("upload", ()) is True  # bucket-wide keys stay live

        # expand the folder, then put the cursor on a file leaf: object keys return
        folder.expand()
        await pilot.pause()
        tree.move_cursor(app._leaf_nodes["images/logo.png"])
        await pilot.pause()
        await pilot.pause()
        assert app._selected().key == "images/logo.png"
        for action in ("detail", "rename", "download"):
            assert app.check_action(action, ()) is True
        assert detail_disabled() is False


# --- WYSIWYG: a custom (non-AWS) profile hides class/state/thaw -------------


def _non_aws_app(tmp_path):
    fake = FakeService()
    fake.profile.endpoint_url = "https://minio.example"  # -> is_aws False
    fake.objects = [ObjectInfo("a.txt", 100, None, "STANDARD")]
    fake._statuses = {}
    return fake, _app(tmp_path, fake)


async def test_non_aws_hides_state_and_class_columns(tmp_path):
    _fake, app = _non_aws_app(tmp_path)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        table = app.query_one(DataTable)
        labels = [str(col.label) for col in table.columns.values()]
        assert "State" not in labels
        assert "Class" not in labels
        assert labels == ["Size", "Modified", "Key"]


async def test_non_aws_hides_thaw_and_filter(tmp_path):
    _fake, app = _non_aws_app(tmp_path)
    async with app.run_test():
        await app.workers.wait_for_complete()
        assert app.check_action("thaw", ()) is False
        assert app.check_action("advanced_thaw", ()) is False
        assert app.check_action("filter", ()) is False
        assert "t" not in app.screen.active_bindings
        assert "f" not in app.screen.active_bindings
        # name-based actions stay available
        assert app.check_action("rename", ()) is True
        assert app.check_action("download", ()) is True


async def test_non_aws_treats_objects_as_available(tmp_path):
    # Even if a custom endpoint reports an archival class, a non-AWS profile must
    # treat the object as downloadable (not route it through hidden thaw advice).
    fake = FakeService()
    fake.profile.endpoint_url = "https://minio.example"
    fake.objects = [ObjectInfo("x.bin", 10, None, "DEEP_ARCHIVE")]
    fake._statuses = {}
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("g")  # download
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "x.bin" in fake.downloaded


async def test_non_aws_bar_omits_state_counts(tmp_path):
    from textual.widgets import Static

    _fake, app = _non_aws_app(tmp_path)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        bar = str(app.query_one("#bar", Static).render())
        assert "cold" not in bar and "thawing" not in bar and "ready" not in bar
        assert "1 objects (100B)" in bar  # count + total size still shown


async def test_non_aws_upload_dialog_hides_class(tmp_path):
    from textual.css.query import NoMatches

    _fake, app = _non_aws_app(tmp_path)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("u")
        await pilot.pause()
        with pytest.raises(NoMatches):
            app.screen.query_one("#class")
        assert app.screen.query_one("#basename", Checkbox) is not None


# --- bar shows the total size ----------------------------------------------


async def test_bar_shows_total_size(tmp_path):
    from textual.widgets import Static

    fake = FakeService()  # cold.bin 100B + doc.txt 50B = 150B
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        bar = str(app.query_one("#bar", Static).render())
        assert "2 objects (150B)" in bar


# --- add-profile form: segmented choices reveal only relevant fields --------


async def test_add_profile_form_toggles_fields(tmp_path):
    fake = FakeService()
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("a")
        await pilot.pause()
        screen = app.screen
        # AWS (default): storage class shown, endpoint hidden
        assert screen.query_one("#class-row").display is True
        assert screen.query_one("#endpoint-row").display is False
        # switch to custom S3-compatible
        _select_radio(screen, "conn", 1)
        await pilot.pause()
        assert screen.query_one("#class-row").display is False
        assert screen.query_one("#endpoint-row").display is True
        # credentials default to access keys; rclone row hidden until chosen
        assert screen.query_one("#keys-row").display is True
        assert screen.query_one("#rclone-row").display is False
        _select_radio(screen, "creds", 1)
        await pilot.pause()
        assert screen.query_one("#keys-row").display is False
        assert screen.query_one("#rclone-row").display is True


async def test_add_profile_custom_endpoint_saves(tmp_path):
    cfg = Config(tmp_path / "config.json")
    app = BuckletApp(config=cfg, service=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        screen = app.screen
        screen.query_one("#name", Input).value = "minio"
        screen.query_one("#bucket", Input).value = "bkt"
        _select_radio(screen, "conn", 1)  # custom
        _select_radio(screen, "creds", 2)  # env / IAM role
        await pilot.pause()
        screen.query_one("#endpoint", Input).value = "https://minio.example"
        screen.query_one("#ok", Button).press()
        await pilot.pause()
        stored = cfg.get("minio")
        assert stored.endpoint_url == "https://minio.example"
        assert stored.is_aws is False
        assert stored.access_key_id is None and stored.rclone_remote is None
        # region was left blank: a custom endpoint defaults it to "auto" so
        # botocore can still sign (what R2 wants), keeping the field truly optional.
        assert stored.region == "auto"


async def test_add_profile_custom_endpoint_keeps_explicit_region(tmp_path):
    cfg = Config(tmp_path / "config.json")
    app = BuckletApp(config=cfg, service=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        screen = app.screen
        screen.query_one("#name", Input).value = "wasabi"
        screen.query_one("#bucket", Input).value = "bkt"
        _select_radio(screen, "conn", 1)  # custom
        _select_radio(screen, "creds", 2)  # env / IAM role
        await pilot.pause()
        screen.query_one("#endpoint", Input).value = "https://s3.wasabisys.com"
        screen.query_one("#region", Input).value = "us-east-2"
        screen.query_one("#ok", Button).press()
        await pilot.pause()
        # an explicit region is preserved, not clobbered by the "auto" default
        assert cfg.get("wasabi").region == "us-east-2"
