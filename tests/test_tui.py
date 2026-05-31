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


class FakeService:
    def __init__(self):
        self.profile = Profile(
            name="t", bucket="b", region="us-east-1", storage_class="DEEP_ARCHIVE"
        )
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


def _app(tmp_path: Path, fake: FakeService, *, allow_deletion: bool = False):
    return BuckletApp(
        config=Config(tmp_path / "config.json"), service=fake, allow_deletion=allow_deletion
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
    from textual.widgets import Static

    app = BuckletApp(
        config=Config(tmp_path / "config.json"), service=None, error="cannot open 'b': denied"
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        message = app.query_one("#message", Static)
        assert "denied" in str(message.render())
        assert message.has_class("error")


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
    from textual.widgets import Static

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
        assert "deleted" in str(app.query_one("#message", Static).render())


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
    from textual.widgets import Static

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
        message = app.query_one("#message", Static)
        assert "access denied" in str(message.render())
        assert message.has_class("error")


# --- graceful degradation when the bucket can't be listed -------------------


async def test_list_failure_shows_empty_table(tmp_path):
    from textual.widgets import Static

    fake = FakeService()
    fake.list_error = "access denied (check the IAM policy and keys)"
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        # no crash; empty table and a surfaced error
        assert app.query_one(DataTable).row_count == 0
        assert app.objects == []
        message = app.query_one("#message", Static)
        assert "access denied" in str(message.render())
        assert message.has_class("error")


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
