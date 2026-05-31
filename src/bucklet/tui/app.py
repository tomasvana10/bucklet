"""The bucklet Textual application.

The app holds no S3 logic of its own. Every action calls a
:class:`~bucklet.service.Service` method (the same one the CLI uses) from a
threaded worker so the UI stays responsive, then reflects the result. The
profile opens in the background too, so the window appears straight away.
Thaw is offered only when the selected object's live state actually needs it.
"""

from __future__ import annotations

from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header, Static
from textual.worker import get_current_worker

from .. import storage
from ..config import Config
from ..errors import BuckletError
from ..formatting import STATE_STYLE, fmt_date, human
from ..models import ObjectInfo, ObjectStatus, Profile
from ..service import Service
from .screens import (
    AddProfileScreen,
    ConfirmScreen,
    DetailScreen,
    ProfileScreen,
    PromptScreen,
    SettingsScreen,
    UploadScreen,
)

# DataTable column keys.
COL_STATE, COL_SIZE, COL_MOD, COL_CLASS, COL_KEY = "state", "size", "mod", "class", "key"

_FILTER_CYCLE = [None, storage.COLD, storage.THAWING, storage.THAWED, storage.AVAILABLE]

# Actions that operate on the selected object; greyed out when nothing is listed.
_OBJECT_ACTIONS = frozenset({"detail", "thaw", "download", "delete"})


class BuckletFooter(Footer):
    """The standard footer, with a divider after the object-specific keys.

    Textual's footer has no notion of sections, so we let it build its keys and
    then slot a thin divider in after the last object action (Info/Thaw/Get/
    Delete), separating them from the bucket-wide actions that follow.
    """

    DEFAULT_CSS = """
    BuckletFooter .footer-divider {
        width: 1;
        color: $foreground 40%;
        background: $footer-item-background;
    }
    """

    def compose(self) -> ComposeResult:
        widgets = list(super().compose())
        last_object_key = -1
        for i, widget in enumerate(widgets):
            action = getattr(widget, "action", None)
            if action and action.split("(", 1)[0] in _OBJECT_ACTIONS:
                last_object_key = i
        if 0 <= last_object_key < len(widgets) - 1:
            widgets.insert(last_object_key + 1, Static("│", classes="footer-divider"))
        yield from widgets


class BuckletApp(App):
    TITLE = "bucklet"

    CSS = """
    #bar { height: 1; background: $panel; color: $text; padding: 0 1; }
    #message { height: 1; padding: 0 1; color: $text-muted; }
    #message.error { color: $error; text-style: bold; }
    DataTable { height: 1fr; }

    DetailScreen, PromptScreen, ProfileScreen, AddProfileScreen, UploadScreen,
    ConfirmScreen, SettingsScreen {
        align: center middle;
    }
    #dialog {
        width: 60;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        padding: 1;
        border: round $primary;
        background: $surface;
    }
    .dialog-title { text-style: bold; color: $secondary; }
    .hint { color: $text-muted; }
    .buttons { height: auto; align-horizontal: right; }
    .buttons Button { margin-left: 1; }
    """

    BINDINGS = [
        # Object-specific actions (greyed out when nothing is listed; see
        # check_action). The footer draws a divider after this group.
        Binding("i", "detail", "Info"),
        Binding("t", "thaw('Bulk')", "Thaw"),
        Binding("T", "thaw('Standard')", "Thaw+", show=False),
        Binding("g", "download", "Get"),
        # Only shown/active when launched with --allow-deletion; see check_action.
        Binding("d", "delete", "Delete"),
        # Bucket-/app-wide actions.
        Binding("u", "upload", "Upload"),
        Binding("s", "settings", "Settings"),
        Binding("r", "refresh", "Refresh"),
        Binding("slash", "search", "Search"),
        Binding("f", "filter", "Filter"),
        Binding("p", "switch_profile", "Profile"),
        Binding("a", "add_profile", "Add"),
        Binding("q", "quit", "Quit"),
        # Ctrl+C quits too, instead of nagging the user to press Ctrl+Q.
        Binding("ctrl+c", "quit", show=False, priority=True),
    ]

    def __init__(
        self,
        config: Config,
        service: Service | None = None,
        *,
        initial_profile: Profile | None = None,
        error: str = "",
        allow_deletion: bool = False,
    ):
        super().__init__()
        self.config = config
        self.service = service
        self.allow_deletion = allow_deletion
        self._initial_profile = initial_profile
        self._initial_error = error
        self.objects: list[ObjectInfo] = []
        self.statuses: dict[str, ObjectStatus] = {}
        self.displayed: list[ObjectInfo] = []
        self.search_term = ""
        self.state_filter: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(id="bar")
        yield DataTable(id="objects", zebra_stripes=True, cursor_type="row")
        yield Static(id="message")
        yield BuckletFooter()

    def on_mount(self):
        table = self.query_one("#objects", DataTable)
        table.add_column("St", key=COL_STATE, width=6)
        table.add_column("Size", key=COL_SIZE, width=10)
        table.add_column("Modified", key=COL_MOD, width=17)
        table.add_column("Class", key=COL_CLASS, width=20)
        table.add_column("Key", key=COL_KEY)
        self._update_bar()
        if self.service is not None:
            self.reload()
        elif self._initial_profile is not None:
            self.set_message(f"opening {self._initial_profile.name}…")
            self._open_worker(self._initial_profile)
        elif self._initial_error:
            self.set_message(self._initial_error, error=True)
        else:
            self.set_message("no profile open. press a to add one or p to switch")
        self.set_interval(20.0, self.poll_thawing)

    def reload(self):
        if self.service is None:
            return
        self.statuses = {}
        self.set_message("loading…")
        self._load_worker()

    @work(thread=True, exclusive=True, group="load")
    def _load_worker(self):
        service = self.service
        if service is None:
            return
        worker = get_current_worker()
        try:
            objects = service.list_objects()
        except BuckletError as exc:
            # The listing failed (no permission, bad region, network). Clear any
            # rows left over from a previous profile so we never show stale data,
            # and surface the error. The user can still switch/add a profile.
            self.call_from_thread(self._populate, [])
            self.call_from_thread(self.set_message, f"list error: {exc}", True)
            return
        if worker.is_cancelled:
            return
        self.call_from_thread(self._populate, objects)
        # Refine only archived objects; everything else is already accurate.
        for obj in objects:
            if worker.is_cancelled:
                return
            if storage.needs_restore(obj.storage_class):
                status = service.status(obj.key)
                self.call_from_thread(self._apply_status, status)
        self.call_from_thread(self.refresh_view)
        self.call_from_thread(self.set_message, "")

    def _populate(self, objects: list[ObjectInfo]):
        self.objects = objects
        self.refresh_view()

    def _apply_status(self, status: ObjectStatus):
        self.statuses[status.key] = status
        table = self.query_one("#objects", DataTable)
        try:
            table.update_cell(status.key, COL_STATE, self._state_cell(status.state))
            table.update_cell(status.key, COL_CLASS, status.storage_class)
        except Exception:
            pass  # row not currently visible (filtered out)
        self._update_bar()

    def _state_of(self, obj: ObjectInfo):
        status = self.statuses.get(obj.key)
        return status.state if status else obj.baseline_state

    def _filtered(self):
        out = self.objects
        if self.search_term:
            term = self.search_term.lower()
            out = [o for o in out if term in o.key.lower()]
        if self.state_filter is not None:
            out = [o for o in out if self._state_of(o) == self.state_filter]
        return out

    def _state_cell(self, state: str):
        from rich.text import Text

        return Text(storage.STATE_LABEL.get(state, "?"), style=STATE_STYLE.get(state, ""))

    def refresh_view(self):
        table = self.query_one("#objects", DataTable)
        table.clear()
        self.displayed = self._filtered()
        for obj in self.displayed:
            state = self._state_of(obj)
            stored = self.statuses.get(obj.key)
            table.add_row(
                self._state_cell(state),
                human(obj.size),
                fmt_date(obj.last_modified),
                stored.storage_class if stored else obj.storage_class,
                obj.key,
                key=obj.key,
            )
        self._update_bar()
        # Whether there's anything to act on just changed, so re-evaluate which
        # object actions the footer shows as enabled (see check_action).
        self.refresh_bindings()

    def _update_bar(self):
        from rich.text import Text

        bar = self.query_one("#bar", Static)
        if self.service is None:
            self.sub_title = "no profile"
            bar.update("no profile open")
            return
        prof = self.service.profile
        self.sub_title = f"{prof.name} · {prof.bucket}"
        counts = {state: 0 for state in storage.STATES}
        for obj in self.objects:
            state = self._state_of(obj)
            counts[state] = counts.get(state, 0) + 1
        ready = counts[storage.THAWED] + counts[storage.AVAILABLE]

        # Built as a Text (not a markup string) so bucket names / search terms
        # can't be misread as console markup. State counts wear their state
        # colour; an active filter/search is bold so it stands out (it replaces
        # the old toast — the table above already reflects the change).
        text = Text(f"{prof.bucket} [{prof.region or '?'}]   ", style="dim")
        text.append(f"{len(self.objects)} objects · ")
        text.append(f"cold {counts[storage.COLD]}", style=STATE_STYLE[storage.COLD])
        text.append(" · ")
        text.append(f"thawing {counts[storage.THAWING]}", style=STATE_STYLE[storage.THAWING])
        text.append(" · ")
        text.append(f"ready {ready}", style=STATE_STYLE[storage.AVAILABLE])
        if counts[storage.ERROR]:
            text.append(" · ")
            text.append(f"err {counts[storage.ERROR]}", style=STATE_STYLE[storage.ERROR])
        if self.state_filter is not None:
            text.append("   ")
            text.append(
                f"filter:{self.state_filter}",
                style=f"bold {STATE_STYLE.get(self.state_filter, 'white')}",
            )
        if self.search_term:
            text.append("   ")
            text.append(f"/{self.search_term}", style="bold cyan")
        bar.update(text)

    def set_message(self, text: str, error: bool = False):
        message = self.query_one("#message", Static)
        message.set_class(error, "error")
        message.update(text)
        if error and text:
            self.notify(text, severity="error")

    def poll_thawing(self):
        if self.service is None:
            return
        thawing = [k for k, s in self.statuses.items() if s.state == storage.THAWING]
        if thawing:
            self._restatus_worker(thawing)

    @work(thread=True, group="restatus")
    def _restatus_worker(self, keys: list[str]):
        service = self.service
        if service is None:
            return
        for key in keys:
            status = service.status(key)
            self.call_from_thread(self._apply_status, status)

    def _selected(self):
        if not self.displayed:
            return None
        table = self.query_one("#objects", DataTable)
        row = table.cursor_row
        if 0 <= row < len(self.displayed):
            return self.displayed[row]
        return None

    def _require_service(self):
        if self.service is None:
            self.notify("open a profile first (a / p)", severity="warning")
            return False
        return True

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Gate the footer keys to the current context.

        Textual reads the return value as: ``True`` show+enable, ``None`` show
        but grey out (still no-op), ``False`` drop entirely. So deletion without
        --allow-deletion returns False (gone), and the object actions return
        None when nothing is listed (greyed out, since there's nothing to act
        on) — leaving the bucket-wide keys always available.
        """
        if action == "delete" and not self.allow_deletion:
            return False
        if action in _OBJECT_ACTIONS and not self.displayed:
            return None
        return True

    @on(DataTable.RowSelected)
    def action_detail(self):
        obj = self._selected()
        if obj is None:
            return
        status = self.statuses.get(obj.key)
        state = status.state if status else obj.baseline_state
        lines = [
            f"key      : {obj.key}",
            f"size     : {human(obj.size)} ({obj.size} bytes)",
            f"modified : {fmt_date(obj.last_modified)}",
            f"class    : {status.storage_class if status else obj.storage_class}",
            f"state    : {state}",
        ]
        if status and status.restore_expiry:
            lines.append(f"restored : until {status.restore_expiry}")
        if storage.can_thaw(state):
            lines.append("")
            lines.append("archived. press t (Bulk) or T (Standard) to restore")
        elif storage.can_download(state):
            lines.append("")
            lines.append("press g to download")
        self.push_screen(DetailScreen(f"Object · {obj.key}", lines))

    def action_thaw(self, tier: str):
        if not self._require_service():
            return
        obj = self._selected()
        if obj is None:
            return
        state = self._state_of(obj)
        if not storage.can_thaw(state):
            self.notify(f"{obj.key} is {state}, no thaw needed", severity="warning")
            return
        self._thaw_worker(obj.key, tier)

    @work(thread=True, group="op")
    def _thaw_worker(self, key: str, tier: str):
        service = self.service
        self.call_from_thread(self.set_message, f"requesting {tier} restore: {key}…")
        try:
            message = service.restore(key, tier=tier)
            status = service.status(key)
        except BuckletError as exc:
            self.call_from_thread(self.set_message, f"{key}: {exc}", True)
            return
        self.call_from_thread(self._apply_status, status)
        self.call_from_thread(self.set_message, f"{key}: {message}")

    def action_download(self):
        if not self._require_service():
            return
        obj = self._selected()
        if obj is None:
            return
        state = self._state_of(obj)
        if not storage.can_download(state):
            if storage.can_thaw(state):
                self.notify(f"{obj.key} is cold, thaw it first (t)", severity="warning")
            else:
                self.notify(f"{obj.key} is {state}, not ready", severity="warning")
            return
        self._download_worker(obj.key, obj.size)

    @work(thread=True, group="op")
    def _download_worker(self, key: str, size: int):
        service = self.service
        dest = Path.cwd() / key
        total = max(size, 1)
        sent = {"n": 0}

        def progress(n: int):
            sent["n"] += n
            self.call_from_thread(
                self.set_message, f"downloading {key}… {sent['n'] * 100 // total}%"
            )

        try:
            service.download(key, dest, progress=progress)
        except BuckletError as exc:
            self.call_from_thread(self.set_message, f"{key}: {exc}", True)
            return
        self.call_from_thread(self.set_message, f"{key}: downloaded -> {dest}")

    def action_delete(self):
        # check_action gates this off the footer, but guard anyway: a stray key
        # press must never delete in a session that did not opt in.
        if not self.allow_deletion or not self._require_service():
            return
        obj = self._selected()
        if obj is None:
            return
        lines = [
            f"key  : {obj.key}",
            f"size : {human(obj.size)}",
            "",
            "This permanently deletes the object from the bucket.",
        ]
        self.push_screen(
            ConfirmScreen(f"Delete · {obj.key}", lines),
            lambda ok, key=obj.key: self._delete_worker(key) if ok else None,
        )

    @work(thread=True, group="op")
    def _delete_worker(self, key: str):
        service = self.service
        if service is None:
            return
        self.call_from_thread(self.set_message, f"deleting {key}…")
        try:
            message = service.delete(key)
        except BuckletError as exc:
            # A failed delete (commonly access denied on archive-only keys) must
            # leave the object exactly where it was, both on S3 and on screen.
            self.call_from_thread(self.set_message, f"{key}: {exc}", True)
            return
        self.call_from_thread(self._remove_object, key)
        self.call_from_thread(self.set_message, message)

    def _remove_object(self, key: str):
        """Drop a deleted object from the view without re-listing the bucket."""
        self.objects = [o for o in self.objects if o.key != key]
        self.statuses.pop(key, None)
        self.refresh_view()

    def action_upload(self):
        if not self._require_service():
            return
        default_class = storage.normalize_storage_class(self.service.profile.storage_class)
        self.push_screen(UploadScreen(default_class), self._on_upload)

    def _on_upload(self, data: dict | None):
        if not data:
            return
        self._upload_worker(data["path"], data["storage_class"], data["prefix"])

    @work(thread=True, group="op")
    def _upload_worker(self, path: str, storage_class: str, prefix: str):
        service = self.service
        try:
            plan = service.plan_upload([path], prefix=prefix)
        except BuckletError as exc:
            self.call_from_thread(self.set_message, str(exc), True)
            return
        if not plan:
            self.call_from_thread(self.set_message, "nothing to upload")
            return

        # Throttle UI updates to whole-percent / file boundaries: with many
        # small files the byte callback fires a lot, and each update crosses
        # threads. upload_many serialises this callback, so the closure state
        # needs no extra lock.
        last = {"pct": -1, "done": -1}

        def progress(sent: int, total: int, done: int, total_files: int):
            pct = min(100, sent * 100 // total)
            if (pct, done) == (last["pct"], last["done"]):
                return
            last["pct"], last["done"] = pct, done
            self.call_from_thread(
                self.set_message, f"uploading {done}/{total_files} files… {pct}%"
            )

        results = service.upload_many(plan, storage_class=storage_class, progress=progress)
        failures = [(key, err) for key, err in results if err is not None]
        # Refresh the listing here (not via reload) so the result message we set
        # next isn't immediately wiped by reload's own "loading…"/clear cycle.
        try:
            self.call_from_thread(self._populate, service.list_objects())
        except BuckletError:
            pass  # the upload outcome below matters more than refreshing the view
        if failures:
            key, err = failures[0]
            extra = f" (+{len(failures) - 1} more)" if len(failures) > 1 else ""
            self.call_from_thread(
                self.set_message,
                f"{len(failures)}/{len(results)} failed — {key}: {err}{extra}",
                True,
            )
        else:
            self.call_from_thread(self.set_message, f"uploaded {len(results)} file(s)")

    def action_settings(self):
        if not self._require_service():
            return
        self.push_screen(SettingsScreen(self.service.profile), self._on_settings)

    def _on_settings(self, values: dict | None):
        if values is None:
            return
        from .. import s3

        profile = self.service.profile
        for key, value in values.items():
            setattr(profile, key, value)
        # Persist when the profile is saved (a raw-bucket profile isn't); either
        # way the change applies to this session.
        if self.config.has(profile.name):
            stored = self.config.stored(profile.name)
            for key, value in values.items():
                if value is None:
                    stored.pop(key, None)  # cleared field == back to default
                else:
                    stored[key] = value
            try:
                self.config.save()
            except OSError as exc:
                # The in-memory profile still reflects the change; just tell the
                # user it didn't reach disk rather than crash the callback.
                self.set_message(f"applied, but could not save config: {exc}", True)
                return
        # Rebuild the client so the (locally constructed, no-network) pool sizing
        # reflects the new concurrency. The listing and filters are left as-is.
        old_client = self.service.client
        try:
            self.service = Service(profile, s3.build_client(profile))
        except BuckletError as exc:
            self.set_message(str(exc), True)
            return
        try:
            old_client.close()  # release the previous pool's connections
        except Exception:
            pass
        self.set_message("settings updated")

    def action_refresh(self):
        if self._require_service():
            self.reload()

    def action_search(self):
        self.push_screen(PromptScreen("search", self.search_term), self._on_search)

    def _on_search(self, term: str | None):
        if term is None:
            return
        self.search_term = term
        self.refresh_view()

    def action_filter(self):
        idx = _FILTER_CYCLE.index(self.state_filter)
        self.state_filter = _FILTER_CYCLE[(idx + 1) % len(_FILTER_CYCLE)]
        # No toast: the table and the colour-coded filter chip in the bar already
        # show the change, and a toast on every cycle was just noise.
        self.refresh_view()

    def action_switch_profile(self):
        names = self.config.names()
        if not names:
            self.notify("no saved profiles. press a to add one", severity="warning")
            return
        labels = []
        for name in names:
            prof = self.config.get(name)
            default = "  [default]" if self.config.default == name else ""
            labels.append(f"{name}   {prof.bucket}   ({prof.region or '?'}){default}")
        self.push_screen(ProfileScreen(names, labels), self._on_switch)

    def _on_switch(self, name: str | None):
        if name:
            self._open_worker(self.config.get(name))

    def action_add_profile(self):
        self.push_screen(AddProfileScreen(), self._on_add)

    def _on_add(self, data: dict | None):
        if not data:
            return
        profile = Profile(**data)
        self.config.add(profile)
        self.config.save()
        self._open_worker(profile)

    @work(thread=True, exclusive=True, group="open")
    def _open_worker(self, profile: Profile):
        try:
            service = Service.open(profile)
        except BuckletError as exc:
            self.call_from_thread(self.set_message, f"cannot open '{profile.name}': {exc}", True)
            return
        self.call_from_thread(self._activate, service)

    def _activate(self, service: Service):
        self.service = service
        self.search_term = ""
        self.state_filter = None
        self.set_message(f"opened profile '{service.profile.name}'")
        self.reload()


def run_tui(config: Config, profile_arg: str | None = None, *, allow_deletion: bool = False):
    """Launch the TUI right away; the profile opens in the background."""
    profile = config.resolve(profile_arg)
    initial_profile = profile if (profile and profile.bucket) else None
    error = ""
    if initial_profile is None and profile_arg:
        error = f"no bucket configured for '{profile_arg}'"
    BuckletApp(
        config=config,
        initial_profile=initial_profile,
        error=error,
        allow_deletion=allow_deletion,
    ).run()
