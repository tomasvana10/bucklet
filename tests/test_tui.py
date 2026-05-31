"""Smoke tests for the Textual TUI, driven with a fake (no-network) service.

The app is UI-only: it calls Service methods. A fake service lets us exercise
the app's wiring (loading, filtering, thaw gating, detail) without AWS.
"""

from __future__ import annotations

from textual.widgets import DataTable

from archy import storage
from archy.config import Config
from archy.models import ObjectInfo, ObjectStatus, Profile
from archy.tui.app import ArchyApp


class FakeService:
    def __init__(self):
        self.profile = Profile(name="t", bucket="b", region="us-east-1", storage_class="DEEP_ARCHIVE")
        self.objects = [
            ObjectInfo("cold.bin", 100, None, "DEEP_ARCHIVE"),
            ObjectInfo("doc.txt", 50, None, "STANDARD"),
        ]
        self._statuses = {
            "cold.bin": ObjectStatus("cold.bin", storage.COLD, "DEEP_ARCHIVE", 100),
        }
        self.restored: list[tuple[str, str]] = []
        self.downloaded: list[str] = []

    def list_objects(self, prefix=""):
        return list(self.objects)

    def status(self, key):
        return self._statuses.get(key) or ObjectStatus(key, storage.AVAILABLE, "STANDARD")

    def restore(self, key, *, tier="Bulk", days=7):
        self.restored.append((key, tier))
        return "restore requested"

    def download(self, key, dest, progress=None):
        self.downloaded.append(key)
        return dest


def _app(tmp_path, fake) -> ArchyApp:
    return ArchyApp(config=Config(tmp_path / "config.json"), service=fake)


async def test_table_populates(tmp_path):
    fake = FakeService()
    app = _app(tmp_path, fake)
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
    fake = FakeService()
    app = _app(tmp_path, fake)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("i")
        await pilot.pause()
        assert len(app.screen_stack) > 1


async def test_initial_error_is_shown(tmp_path):
    from textual.widgets import Static

    app = ArchyApp(config=Config(tmp_path / "config.json"), service=None,
                   error="cannot open 'b': denied")
    async with app.run_test() as pilot:
        await pilot.pause()
        message = app.query_one("#message", Static)
        assert "denied" in str(message.render())
        assert message.has_class("error")


# -- run_tui entry-point wiring (no event loop needed) --------------------- #
def test_run_tui_wires_open_error(tmp_path, monkeypatch):
    from archy.errors import ArchyError
    from archy.tui import app as app_mod

    cfg = Config(tmp_path / "config.json")
    cfg.add(Profile(name="p", bucket="bkt"))
    captured = {}
    monkeypatch.setattr(app_mod.ArchyApp, "run", lambda self, *a, **k: captured.update(app=self))

    def boom(profile, validate=True):
        raise ArchyError("denied")

    monkeypatch.setattr(app_mod.Service, "open", boom)
    app_mod.run_tui(cfg, "p")
    app = captured["app"]
    assert app.service is None
    assert "cannot open" in app._initial_error and "denied" in app._initial_error


def test_run_tui_wires_service(tmp_path, monkeypatch):
    from archy.tui import app as app_mod

    cfg = Config(tmp_path / "config.json")
    cfg.add(Profile(name="p", bucket="bkt"))
    captured = {}
    sentinel = object()
    monkeypatch.setattr(app_mod.ArchyApp, "run", lambda self, *a, **k: captured.update(app=self))
    monkeypatch.setattr(app_mod.Service, "open", lambda profile, validate=True: sentinel)
    app_mod.run_tui(cfg, "p")
    assert captured["app"].service is sentinel
    assert captured["app"]._initial_error == ""
