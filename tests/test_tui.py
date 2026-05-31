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

    def list_objects(self, prefix: str = ""):
        return list(self.objects)

    def status(self, key: str):
        return self._statuses.get(key) or ObjectStatus(key, storage.AVAILABLE, "STANDARD")

    def restore(self, key: str, *, tier: str = "Bulk", days: int = 7):
        self.restored.append((key, tier))
        return "restore requested"

    def download(self, key: str, dest: Path, progress: Callable[[int], None] | None = None):
        self.downloaded.append(key)
        return dest


def _app(tmp_path: Path, fake: FakeService):
    return BuckletApp(config=Config(tmp_path / "config.json"), service=fake)


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
