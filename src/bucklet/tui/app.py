"""The bucklet Textual application.

The app holds no S3 logic of its own. Every action calls a
:class:`~bucklet.service.Service` method (the same one the CLI uses) from a
threaded worker so the UI stays responsive, then reflects the result. The
profile opens in the background too, so the window appears straight away.
Thaw is offered only when the selected object's live state actually needs it.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Footer, Header, Static, Tree
from textual.worker import get_current_worker

if TYPE_CHECKING:
    from textual.timer import Timer
    from textual.widgets.tree import TreeNode

from .. import storage
from ..config import Config
from ..errors import BuckletError
from ..formatting import STATE_STYLE, fmt_date, human, thaw_remaining
from ..models import ObjectInfo, ObjectStatus, Profile
from ..service import Service
from ..tree import DirNode, FileLeaf, build_key_tree, leaf_name
from .screens import (
    AddProfileScreen,
    AdvancedThawScreen,
    ConfirmScreen,
    DetailScreen,
    ProfileScreen,
    PromptScreen,
    RenameScreen,
    SettingsScreen,
    UploadScreen,
)

# DataTable column keys.
COL_STATE, COL_SIZE, COL_MOD, COL_CLASS, COL_KEY = "state", "size", "mod", "class", "key"

# Width of the State column / tree state prefix. Wide enough for a thawed row's
# countdown, e.g. "ready (50m)", not just the bare "ready" label.
STATE_COL_WIDTH = 11

_FILTER_CYCLE = [None, storage.COLD, storage.THAWING, storage.THAWED, storage.AVAILABLE]

# Footer layout: actions grouped into sections, drawn left to right with a thin
# vertical divider between groups. The view toggle gets its own section, and the
# profile group is prefixed with a muted "Profile" label. Anything not listed here
# (e.g. the command palette key) sits outside the grouping and draws no divider.
_FOOTER_GROUPS: tuple[tuple[str, ...], ...] = (
    ("detail", "rename", "thaw", "advanced_thaw", "download", "delete"),
    ("view",),
    ("upload", "search", "filter", "refresh", "quit"),
    ("switch_profile", "add_profile", "settings"),
)
_FOOTER_GROUP_OF = {action: i for i, group in enumerate(_FOOTER_GROUPS) for action in group}
_FOOTER_PROFILE_GROUP = 3  # the group prefixed with a muted "Profile" label

# Actions that operate on the selected object; greyed out when nothing is listed.
# These are exactly the first footer group.
_OBJECT_ACTIONS = frozenset(_FOOTER_GROUPS[0])

# Actions that only make sense on a genuine AWS bucket (storage classes / restores);
# hidden entirely for a custom S3-compatible profile, where everything is available.
_AWS_ONLY_ACTIONS = frozenset({"thaw", "advanced_thaw", "filter"})

# Thawing anything larger than this prompts for confirmation first (restores can
# be slow and, on Expedited, costly). Shown to the user via human().
THAW_CONFIRM_BYTES = 100 * 1024 * 1024


class BuckletFooter(Footer):
    """The standard footer, split into sections by thin vertical dividers.

    Textual's footer has no notion of sections, so we let it build its keys and
    then walk them, dropping a divider in wherever the action moves to the next
    group in ``_FOOTER_GROUPS``. The profile group is prefixed with a muted
    "Profile" label, and the View key shows an icon for the active view.

    On a terminal too narrow for every key, the footer scrolls sideways rather
    than dropping keys off the edge. Textual has no wrapping layout, so the row
    can't reflow onto a second line; instead it gains a thin horizontal scrollbar
    and grows from one row to two to make room for it, but only while the content
    actually overflows. A wide terminal keeps the usual single-row footer.
    """

    DEFAULT_CSS = """
    BuckletFooter {
        height: auto;
        max-height: 2;
        overflow-x: auto;
        overflow-y: hidden;
        scrollbar-size-horizontal: 1;
        scrollbar-size-vertical: 0;
    }
    BuckletFooter .footer-divider {
        width: 1;
        color: $foreground 40%;
        background: $footer-item-background;
    }
    BuckletFooter .footer-group-label {
        width: auto;
        color: $text-muted;
        background: $footer-item-background;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        prev_group: int | None = None
        for widget in super().compose():
            action = getattr(widget, "action", None)
            base = action.split("(", 1)[0] if action else None
            group = _FOOTER_GROUP_OF.get(base)
            if group is not None and group != prev_group:
                if prev_group is not None:
                    yield Static("│", classes="footer-divider")
                if group == _FOOTER_PROFILE_GROUP:
                    yield Static("Profile", classes="footer-group-label")
                prev_group = group
            if base == "view":
                tree = getattr(self.app, "view_mode", "flat") == "tree"
                widget.description = f"{'Tree' if tree else 'Flat'} View"
            yield widget


# severity -> the CSS class that colours a message line.
_SEVERITY_CLASS = {"information": "-info", "warning": "-warning", "error": "-error"}


class MessageItem(Static):
    """One line in the MessageStack, carrying its text and severity for tests."""

    def __init__(self, text: str, severity: str):
        super().__init__(text, classes="msg")
        self.message_text = text
        self.severity = severity
        self.add_class(_SEVERITY_CLASS.get(severity, "-info"))

    def set_content(self, text: str, severity: str):
        self.message_text = text
        self.severity = severity
        for cls in _SEVERITY_CLASS.values():
            self.remove_class(cls)
        self.add_class(_SEVERITY_CLASS.get(severity, "-info"))
        self.update(text)


class MessageStack(Vertical):
    """Inline, auto-expiring message area above the footer — bucklet's only
    notification surface (there are no toasts).

    Each line carries a severity (colour) and a timeout, after which it removes
    itself, so the stack stays compact and nothing lingers. Posting with a
    ``key`` updates the existing line in place and restarts its timer instead of
    stacking a new one — that's how a stream of progress updates stays one line.
    Oldest lines are trimmed once the stack exceeds :data:`MAX`.
    """

    MAX = 5

    DEFAULT_CSS = """
    MessageStack {
        height: auto;
        max-height: 6;
        padding: 0 1;
    }
    MessageStack .msg { height: 1; color: $text-muted; }
    MessageStack .msg.-warning { color: $warning; text-style: bold; }
    MessageStack .msg.-error { color: $error; text-style: bold; }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._items: list[MessageItem] = []
        self._keyed: dict[str, MessageItem] = {}
        # Note: not `_timers` — that name belongs to Textual's MessagePump.
        self._expiry: dict[MessageItem, Timer] = {}

    @property
    def messages(self) -> list[tuple[str, str]]:
        """The currently shown (text, severity) pairs, oldest first."""
        return [(item.message_text, item.severity) for item in self._items]

    def post(self, text: str, *, severity: str = "information", timeout: float = 5.0, key=None):
        if not text:
            if key is not None and key in self._keyed:
                self._expire(self._keyed[key])
            return
        if key is not None and key in self._keyed:
            item = self._keyed[key]
            item.set_content(text, severity)
            self._arm(item, timeout)
            return
        item = MessageItem(text, severity)
        self._items.append(item)
        if key is not None:
            self._keyed[key] = item
        self.mount(item)
        self._arm(item, timeout)
        self._trim()

    def _arm(self, item: MessageItem, timeout: float):
        old = self._expiry.pop(item, None)
        if old is not None:
            old.stop()
        self._expiry[item] = self.set_timer(timeout, lambda: self._expire(item))

    def _expire(self, item: MessageItem):
        timer = self._expiry.pop(item, None)
        if timer is not None:
            timer.stop()
        if item in self._items:
            self._items.remove(item)
        for k in [k for k, v in self._keyed.items() if v is item]:
            del self._keyed[k]
        try:
            item.remove()
        except Exception:
            pass  # already detached

    def _trim(self):
        while len(self._items) > self.MAX:
            # Prefer dropping the oldest *unkeyed* line, so a keyed progress line
            # (the kind updated in place) isn't silently forgotten — which would
            # make its next update stack a new line instead of replacing it.
            keyed = set(self._keyed.values())
            victim = next((i for i in self._items if i not in keyed), self._items[0])
            self._expire(victim)


class BuckletApp(App):
    TITLE = "bucklet"

    CSS = """
    #bar { height: 1; background: $panel; color: $text; padding: 0 1; }
    DataTable { height: 1fr; }
    Tree { height: 1fr; padding: 0 1; }

    DetailScreen, PromptScreen, ProfileScreen, AddProfileScreen, UploadScreen,
    ConfirmScreen, SettingsScreen, RenameScreen, AdvancedThawScreen {
        align: center middle;
    }
    #dialog {
        width: 60;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        padding: 0 1;
        border: round $primary;
        background: $surface;
    }
    /* Dialogs are kept dense to match the main UI: one-line inputs and buttons,
       no chunky borders, so a modal never dwarfs the table behind it. */
    #dialog Input {
        height: 1;
        border: none;
        padding: 0 1;
        background: $boost;
        margin-bottom: 1;
    }
    #dialog Input:focus { background: $primary 25%; }
    #dialog Button { height: 1; min-width: 10; border: none; }
    #dialog Select, #dialog SelectCurrent { height: 1; border: none; }
    /* Match the spacing of the Input fields, which carry a margin-bottom of 1,
       so the storage-class dropdown isn't crammed against the field below it. */
    #dialog Select { margin-bottom: 1; }
    #dialog SelectCurrent { background: $boost; }
    #dialog Checkbox { height: 1; border: none; padding: 0; background: transparent; }
    /* The field-group wrappers (#class-row, #endpoint-row, …) are plain Vertical
       containers, which default to height: 1fr and would collapse to nothing
       inside the auto-height dialog, taking their fields with them. */
    #dialog Vertical { height: auto; }
    #dialog .segmented { height: auto; border: none; padding: 0; margin-bottom: 1; }
    #dialog .segmented RadioButton { border: none; padding: 0; background: transparent; }
    .dialog-title { text-style: bold; color: $secondary; margin-bottom: 1; }
    .hint { color: $text-muted; }
    .buttons { height: auto; align-horizontal: right; margin-top: 1; }
    .buttons Button { margin-left: 1; }
    """

    BINDINGS = [
        # Object-specific actions (greyed out when nothing is listed; see
        # check_action). Footer group 0.
        Binding("i", "detail", "Info"),
        Binding("e", "rename", "Rename"),
        Binding("t", "thaw('Bulk')", "Thaw"),
        Binding("T", "advanced_thaw", "Thaw+"),
        Binding("g", "download", "Get"),
        # Only shown/active when launched with --allow-deletion; see check_action.
        Binding("d", "delete", "Delete"),
        # The view toggle gets its own footer section; the footer puts an icon on
        # this label to show whether the flat or tree view is live.
        Binding("v", "view", "View"),
        # Bucket-/app-wide actions.
        Binding("u", "upload", "Upload"),
        Binding("slash", "search", "Search"),
        Binding("f", "filter", "Filter"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
        # Profile management: its own footer section, labelled "Profile".
        Binding("p", "switch_profile", "Switch"),
        Binding("a", "add_profile", "Add"),
        Binding("s", "settings", "Settings"),
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
        # "flat" is the DataTable; "tree" is the folder view (toggle with 'v').
        self.view_mode = "flat"
        self._by_key: dict[str, ObjectInfo] = {}
        self._leaf_nodes: dict[str, TreeNode] = {}
        # Which column layout the table currently holds (None until first built);
        # tracked so we only rebuild columns when AWS-ness actually changes.
        self._table_aws: bool | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(id="bar")
        yield DataTable(id="objects", zebra_stripes=True, cursor_type="row")
        yield Tree("", id="tree")
        yield MessageStack(id="messages")
        yield BuckletFooter()

    def on_mount(self):
        # The tree's own root is just a container for the top-level entries.
        tree = self.query_one("#tree", Tree)
        tree.show_root = False
        # A pre-attached service (the tests' path) carries its own remembered
        # view; otherwise the flat table is the default until a profile opens.
        if self.service is not None:
            self.view_mode = self._profile_view(self.service.profile)
        self._apply_view()
        self._update_bar()
        if self.service is not None:
            self.reload()
        elif self._initial_profile is not None:
            self.flash(f"opening {self._initial_profile.name}…", key="status", timeout=10.0)
            self._open_worker(self._initial_profile)
        elif self._initial_error:
            self.flash(self._initial_error, severity="error", timeout=8.0)
        else:
            self.flash("no profile open. press a to add one or p to switch", timeout=10.0)
        self.set_interval(20.0, self.poll_thawing)

    def reload(self):
        if self.service is None:
            return
        self.statuses = {}
        self.flash("loading…", key="status", timeout=10.0)
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
            # Replace the "loading…" line (key="status") with the error.
            self.call_from_thread(
                self.flash, f"list error: {exc}", severity="error", timeout=8.0, key="status"
            )
            return
        if worker.is_cancelled:
            return
        self.call_from_thread(self._populate, objects)
        # Refine only archived objects; everything else is already accurate. A
        # custom S3 profile has no archival classes, so it needs no HEADs at all.
        for obj in objects:
            if worker.is_cancelled or service is not self.service:
                return
            if service.profile.is_aws and storage.needs_restore(obj.storage_class):
                status = service.status(obj.key)
                # The blocking HEAD can outlast a profile switch (cancellation is
                # cooperative); don't let a stale status pollute the new profile.
                if worker.is_cancelled or service is not self.service:
                    return
                self.call_from_thread(self._apply_status, status)
        self.call_from_thread(self.refresh_view)
        self.call_from_thread(self.flash, "", key="status")  # clear "loading…"

    def _populate(self, objects: list[ObjectInfo]):
        self.objects = objects
        self._by_key = {o.key: o for o in objects}
        self.refresh_view()

    def _apply_status(self, status: ObjectStatus):
        self.statuses[status.key] = status
        if self.view_mode == "tree":
            self._relabel_leaf(status.key)
        else:
            table = self.query_one("#objects", DataTable)
            try:
                table.update_cell(status.key, COL_STATE, self._state_cell(status.state, status))
                table.update_cell(status.key, COL_CLASS, status.storage_class)
            except Exception:
                pass  # row not visible (filtered out), or no such column (non-AWS)
        self._update_bar()

    def _is_aws(self) -> bool:
        """Whether the open profile is genuine AWS (storage classes / restores).

        Drives WYSIWYG everywhere: a custom S3-compatible profile has no State
        or Class column, no thaw, and no upload class picker. With no profile
        open we assume AWS — there's nothing on screen for it to matter to yet.
        """
        return self.service is None or self.service.profile.is_aws

    def _state_of(self, obj: ObjectInfo):
        # A custom S3 profile has no archival states; treat everything as
        # available so it's never routed through thaw guidance (WYSIWYG).
        if not self._is_aws():
            return storage.AVAILABLE
        status = self.statuses.get(obj.key)
        return status.state if status else obj.baseline_state

    def _state_filtered(self):
        """Objects passing the active state filter (the set the views show)."""
        if self.state_filter is None:
            return list(self.objects)
        return [o for o in self.objects if self._state_of(o) == self.state_filter]

    def _state_display(self, state: str, status: ObjectStatus | None) -> str:
        """The state label, with a thawed object's remaining window appended.

        A ``ready`` row becomes ``ready (2d)`` once we know when its restored copy
        lapses back to cold, so the table shows how long it stays downloadable.
        """
        label = storage.STATE_LABEL.get(state, "?")
        if state == storage.THAWED and status is not None:
            left = thaw_remaining(status.restore_expiry)
            if left:
                return f"{label} ({left})"
        return label

    def _state_cell(self, state: str, status: ObjectStatus | None = None):
        return Text(self._state_display(state, status), style=STATE_STYLE.get(state, ""))

    def refresh_view(self):
        if self.view_mode == "tree":
            self._refresh_tree()
        else:
            self._refresh_table()
        self._update_bar()
        # Whether there's anything to act on just changed, so re-evaluate which
        # object actions the footer shows as enabled (see check_action).
        self.refresh_bindings()

    def _ensure_columns(self):
        """(Re)build the table columns for the current profile's AWS-ness.

        A custom S3-compatible profile drops the State and Class columns — its
        objects are always available, so both would only ever read the same
        thing (WYSIWYG). Rebuilt only when AWS-ness flips, since it clears rows.
        """
        aws = self._is_aws()
        if self._table_aws == aws:
            return
        self._table_aws = aws
        table = self.query_one("#objects", DataTable)
        table.clear(columns=True)
        if aws:
            table.add_column("State", key=COL_STATE, width=STATE_COL_WIDTH)
        table.add_column("Size", key=COL_SIZE, width=10)
        table.add_column("Modified", key=COL_MOD, width=17)
        if aws:
            table.add_column("Class", key=COL_CLASS, width=20)
        table.add_column("Key", key=COL_KEY)

    def _refresh_table(self):
        self._ensure_columns()
        table = self.query_one("#objects", DataTable)
        table.clear()
        aws = self._is_aws()
        rows = self._state_filtered()
        if self.search_term:
            term = self.search_term.lower()
            rows = [o for o in rows if term in o.key.lower()]
        self.displayed = rows
        for obj in rows:
            stored = self.statuses.get(obj.key)
            cls = stored.storage_class if stored else obj.storage_class
            if aws:
                table.add_row(
                    self._state_cell(self._state_of(obj), stored),
                    human(obj.size),
                    fmt_date(obj.last_modified),
                    cls,
                    obj.key,
                    key=obj.key,
                )
            else:
                table.add_row(human(obj.size), fmt_date(obj.last_modified), obj.key, key=obj.key)

    def _refresh_tree(self):
        """Rebuild the folder tree from the (state-filtered) objects.

        Search doesn't filter here as it does in the flat view — it highlights
        matching leaves and expands their folders so every match is on screen,
        the rest staying collapsed.
        """
        tree = self.query_one("#tree", Tree)
        tree.clear()
        self._leaf_nodes = {}
        self.displayed = self._state_filtered()
        root = build_key_tree([o.key for o in self.displayed])
        term = self.search_term.lower() if self.search_term else ""
        self._add_dir(tree.root, root, term)
        tree.root.expand()

    def _add_dir(self, tnode: TreeNode, dirnode: DirNode, term: str) -> bool:
        """Populate ``tnode`` from ``dirnode``; return True if anything below
        matched ``term`` (so the caller can expand to reveal it)."""
        matched = False
        for child in dirnode.dirs:
            branch = tnode.add(Text(f"{child.name}/", style="bold"), data=None)
            if self._add_dir(branch, child, term):
                branch.expand()
                matched = True
        for leaf in dirnode.files:
            node = tnode.add_leaf(self._leaf_label(leaf, term), data=leaf.key)
            self._leaf_nodes[leaf.key] = node
            if term and term in leaf.key.lower():
                matched = True
        return matched

    def _leaf_label(self, leaf: FileLeaf, term: str) -> Text:
        """A file's tree label: state (AWS only), name (highlit on a match), size."""
        obj = self._by_key.get(leaf.key)
        label = Text()
        if self._is_aws() and obj is not None:
            state = self._state_of(obj)
            status = self.statuses.get(leaf.key)
            label.append(
                f"{self._state_display(state, status):<{STATE_COL_WIDTH}}",
                style=STATE_STYLE.get(state, ""),
            )
        hit = bool(term) and term in leaf.key.lower()
        label.append(leaf.name, style="bold reverse" if hit else "")
        if obj is not None:
            label.append(f"  {human(obj.size)}", style="dim")
        return label

    def _relabel_leaf(self, key: str):
        """Refresh one tree leaf in place (e.g. after a status poll), keeping
        the tree's expansion state intact."""
        node = self._leaf_nodes.get(key)
        if node is None:
            return
        term = self.search_term.lower() if self.search_term else ""
        node.set_label(self._leaf_label(FileLeaf(leaf_name(key), key), term))

    def _update_bar(self):
        bar = self.query_one("#bar", Static)
        if self.service is None:
            self.title = "bucklet"
            self.sub_title = ""
            bar.update("no profile open")
            return
        prof = self.service.profile
        # The header carries the profile + region; the bar carries the bucket and
        # counts, so the two don't repeat each other.
        self.title = f"bucklet · profile '{prof.name}' · region {prof.region or '?'}"
        self.sub_title = ""
        total = sum(o.size for o in self.objects)

        # Built as a Text (not a markup string) so bucket names / search terms
        # can't be misread as console markup. The bucket is followed by the count
        # and total size; the per-state counts (coloured) come next but only for
        # AWS — a custom S3 profile has no archival states to count (WYSIWYG). An
        # active filter/search is bold so it stands out.
        text = Text(f"{prof.bucket}   ", style="dim")
        text.append(f"{len(self.objects)} objects ({human(total)})")
        if self._is_aws():
            counts = {state: 0 for state in storage.STATES}
            for obj in self.objects:
                counts[self._state_of(obj)] = counts.get(self._state_of(obj), 0) + 1
            ready = counts[storage.THAWED] + counts[storage.AVAILABLE]
            text.append(" · ")
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

    def flash(
        self,
        text: str,
        *,
        severity: str = "information",
        timeout: float = 5.0,
        key: str | None = None,
    ):
        """Show a message in the stack above the footer (bucklet's only notifier).

        Every message expires after ``timeout`` seconds. Pass a ``key`` to update
        one line in place (e.g. a progress readout) instead of stacking; posting
        empty text for a key clears it.
        """
        from textual.css.query import NoMatches

        try:
            stack = self.query_one(MessageStack)
        except NoMatches:
            return  # stack not mounted yet / already torn down
        stack.post(text, severity=severity, timeout=timeout, key=key)

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
            if service is not self.service:
                return  # profile switched mid-poll; don't apply a stale status
            self.call_from_thread(self._apply_status, status)

    def _selected(self):
        if self.view_mode == "tree":
            node = self.query_one("#tree", Tree).cursor_node
            # Only file leaves carry a key; a folder selects nothing actionable.
            if node is not None and node.data:
                return self._by_key.get(node.data)
            return None
        if not self.displayed:
            return None
        table = self.query_one("#objects", DataTable)
        row = table.cursor_row
        if 0 <= row < len(self.displayed):
            return self.displayed[row]
        return None

    def _require_service(self):
        if self.service is None:
            self.flash("open a profile first (a / p)", severity="warning")
            return False
        return True

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Gate the footer keys to the current context.

        Textual reads the return value as: ``True`` show+enable, ``None`` show
        but grey out (still no-op), ``False`` drop entirely. So deletion without
        --allow-deletion returns False (gone); the storage-class actions (thaw,
        filter) return False on a custom S3 profile that has no such notion; and
        the object actions return None whenever no object is selected to act on,
        i.e. nothing is listed, or a folder is highlighted in the tree view,
        leaving the bucket-wide keys available.
        """
        if action == "delete" and not self.allow_deletion:
            return False
        if action in _AWS_ONLY_ACTIONS and self.service is not None and not self._is_aws():
            return False
        if action in _OBJECT_ACTIONS and self._selected() is None:
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
        ]
        # Class / state / thaw only mean something on AWS; a custom S3 object is
        # simply downloadable (WYSIWYG).
        if self._is_aws():
            lines.append(f"class    : {status.storage_class if status else obj.storage_class}")
            lines.append(f"state    : {state}")
            if status and status.restore_expiry:
                left = thaw_remaining(status.restore_expiry)
                tail = f" ({left} left)" if left else ""
                lines.append(f"thawed   : until {status.restore_expiry}{tail}")
            if storage.can_thaw(state):
                lines += ["", "archived. press t (quick) or T (advanced) to thaw"]
            elif storage.can_download(state):
                lines += ["", "press g to download"]
        else:
            lines += ["", "press g to download"]
        self.push_screen(DetailScreen(f"Object · {obj.key}", lines))

    @on(Tree.NodeSelected)
    def _tree_node_selected(self, event: Tree.NodeSelected):
        # Enter/click on a file leaf opens its detail, mirroring RowSelected in
        # the flat view; on a folder the Tree just toggles it.
        if self.view_mode == "tree" and event.node.data:
            self.action_detail()

    @on(Tree.NodeHighlighted)
    def _tree_node_highlighted(self):
        # Moving the cursor between a folder and a file changes whether there's
        # an object to act on, so re-evaluate which object keys the footer enables.
        if self.view_mode == "tree":
            self.refresh_bindings()

    def action_thaw(self, tier: str):
        """Quick thaw: restore at ``tier`` for the default window, confirming
        first when the object is large (see :data:`THAW_CONFIRM_BYTES`)."""
        obj = self._thawable()
        if obj is None:
            return
        self._confirm_large_then(obj, lambda: self._thaw_worker(obj.key, tier, 7))

    def action_advanced_thaw(self):
        """Thaw with a chosen tier and retention window, via a dialog. The
        large-object confirmation comes after the dialog, so the choice is made
        first and only one prompt is in flight at a time."""
        obj = self._thawable()
        if obj is None:
            return

        def go(data: dict | None):
            if not data:
                return
            self._confirm_large_then(
                obj, lambda: self._thaw_worker(obj.key, data["tier"], data["days"])
            )

        self.push_screen(AdvancedThawScreen(), go)

    def _thawable(self) -> ObjectInfo | None:
        """The selected object if a thaw makes sense for it, else None (after
        flashing why not)."""
        if not self._require_service():
            return None
        obj = self._selected()
        if obj is None:
            return None
        state = self._state_of(obj)
        if not storage.can_thaw(state):
            self.flash(f"{obj.key} is {state}, no thaw needed", severity="warning")
            return None
        return obj

    def _confirm_large_then(self, obj: ObjectInfo, proceed):
        """Run ``proceed`` immediately, or behind a confirmation when ``obj``
        exceeds :data:`THAW_CONFIRM_BYTES`."""
        if obj.size > THAW_CONFIRM_BYTES:
            lines = [
                f"key  : {obj.key}",
                f"size : {human(obj.size)}",
                "",
                f"This object is over {human(THAW_CONFIRM_BYTES)}.",
                "Are you sure you want to thaw it?",
            ]
            self.push_screen(
                ConfirmScreen(f"Thaw · {obj.key}", lines, confirm_label="Thaw"),
                lambda ok: proceed() if ok else None,
            )
        else:
            proceed()

    @work(thread=True, group="op")
    def _thaw_worker(self, key: str, tier: str, days: int):
        service = self.service
        self.call_from_thread(
            self.flash, f"requesting {tier} thaw ({days}d): {key}…", key="op", timeout=10.0
        )
        try:
            message = service.restore(key, tier=tier, days=days)
            status = service.status(key)
        except BuckletError as exc:
            self.call_from_thread(
                self.flash, f"{key}: {exc}", severity="error", timeout=8.0, key="op"
            )
            return
        self.call_from_thread(self._apply_status, status)
        self.call_from_thread(self.flash, f"{key}: {message}", key="op")

    def action_download(self):
        if not self._require_service():
            return
        obj = self._selected()
        if obj is None:
            return
        state = self._state_of(obj)
        if not storage.can_download(state):
            if storage.can_thaw(state):
                self.flash(f"{obj.key} is cold, thaw it first (t)", severity="warning")
            else:
                self.flash(f"{obj.key} is {state}, not ready", severity="warning")
            return
        self._download_worker(obj.key, obj.size)

    @work(thread=True, group="op")
    def _download_worker(self, key: str, size: int):
        service = self.service
        dest = Path.cwd() / key
        total = max(size, 1)
        # boto3 downloads a multipart object on several threads, each calling this
        # callback, so the running total needs a lock (the upload path does the same).
        lock = threading.Lock()
        sent = {"n": 0}

        def progress(n: int):
            with lock:
                sent["n"] += n
                done = sent["n"]
            self.call_from_thread(
                self.flash,
                f"downloading {key}… {done * 100 // total}%",
                key="op",
                timeout=15.0,
            )

        try:
            service.download(key, dest, progress=progress)
        except BuckletError as exc:
            self.call_from_thread(
                self.flash, f"{key}: {exc}", severity="error", timeout=8.0, key="op"
            )
            return
        self.call_from_thread(self.flash, f"{key} downloaded to {dest}", key="op")

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
        self.call_from_thread(self.flash, f"deleting {key}…", key="op", timeout=10.0)
        try:
            message = service.delete(key)
        except BuckletError as exc:
            # A failed delete (commonly access denied on archive-only keys) must
            # leave the object exactly where it was, both on S3 and on screen.
            self.call_from_thread(
                self.flash, f"{key}: {exc}", severity="error", timeout=8.0, key="op"
            )
            return
        self.call_from_thread(self._remove_object, key)
        self.call_from_thread(self.flash, message, key="op")

    def _remove_object(self, key: str):
        """Drop a deleted object from the view without re-listing the bucket."""
        self.objects = [o for o in self.objects if o.key != key]
        self._by_key.pop(key, None)
        self.statuses.pop(key, None)
        self.refresh_view()

    def action_rename(self):
        if not self._require_service():
            return
        obj = self._selected()
        if obj is None:
            return
        self.push_screen(
            RenameScreen(obj.key),
            lambda new, old=obj.key: self._rename_worker(old, new) if new else None,
        )

    @work(thread=True, group="op")
    def _rename_worker(self, old_key: str, new_key: str):
        service = self.service
        if service is None:
            return
        self.call_from_thread(self.flash, f"renaming {old_key}…", key="op", timeout=10.0)
        try:
            message = service.rename(old_key, new_key)
        except BuckletError as exc:
            # The service messages already name the object where it helps, so we
            # don't prepend the key here (that's what doubled it: "k: k is …").
            self.call_from_thread(self.flash, str(exc), severity="error", timeout=8.0, key="op")
            return
        self.call_from_thread(self._rename_object, old_key, new_key)
        self.call_from_thread(self.flash, message, key="op")

    def _rename_object(self, old_key: str, new_key: str):
        """Reflect a successful rename locally, without re-listing (like delete),
        so the result message isn't wiped by a reload cycle. The new object keeps
        the old one's class; its cached status is dropped, so an archived object
        correctly shows cold again — the fresh server-side copy isn't restored."""
        obj = self._by_key.get(old_key)
        if obj is None:
            return
        stored = self.statuses.pop(old_key, None)
        cls = stored.storage_class if stored else obj.storage_class
        self.objects = [o for o in self.objects if o.key != old_key]
        self.objects.append(ObjectInfo(new_key, obj.size, obj.last_modified, cls))
        self.objects.sort(key=lambda o: o.key)
        self._by_key = {o.key: o for o in self.objects}
        self.refresh_view()

    def action_upload(self):
        if not self._require_service():
            return
        profile = self.service.profile
        default_class = storage.normalize_storage_class(profile.storage_class)
        self.push_screen(UploadScreen(default_class, is_aws=profile.is_aws), self._on_upload)

    def _on_upload(self, data: dict | None):
        if not data:
            return
        self._upload_worker(
            data["path"], data["storage_class"], data["prefix"], data.get("basename_key", False)
        )

    @work(thread=True, group="op")
    def _upload_worker(
        self, path: str, storage_class: str, prefix: str, basename_key: bool = False
    ):
        service = self.service
        try:
            plan = service.plan_upload([path], prefix=prefix, basename_key=basename_key)
        except BuckletError as exc:
            self.call_from_thread(self.flash, str(exc), severity="error", timeout=8.0, key="op")
            return
        if not plan:
            self.call_from_thread(self.flash, "nothing to upload", key="op")
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
                self.flash,
                f"uploading {done}/{total_files} files… {pct}%",
                key="op",
                timeout=15.0,
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
                self.flash,
                f"{len(failures)}/{len(results)} failed, {key}: {err}{extra}",
                severity="error",
                timeout=8.0,
                key="op",
            )
        else:
            self.call_from_thread(self.flash, f"uploaded {len(results)} file(s)", key="op")

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
                self.flash(
                    f"applied, but could not save config: {exc}", severity="error", timeout=8.0
                )
                return
        # Rebuild the client so the (locally constructed, no-network) pool sizing
        # reflects the new concurrency. The listing and filters are left as-is.
        old_client = self.service.client
        try:
            self.service = Service(profile, s3.build_client(profile))
        except BuckletError as exc:
            self.flash(str(exc), severity="error", timeout=8.0)
            return
        try:
            old_client.close()  # release the previous pool's connections
        except Exception:
            pass
        self.flash("settings updated")

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

    def action_view(self):
        """Toggle between the flat table and the folder (tree) view, and
        remember the choice for this profile."""
        self.view_mode = "tree" if self.view_mode == "flat" else "flat"
        self._apply_view(focus=True)
        self.refresh_view()
        self._persist_view()

    def _profile_view(self, profile: Profile) -> str:
        """The view to open a profile in, defaulting to the flat table."""
        return profile.view if profile.view in ("flat", "tree") else "flat"

    def _apply_view(self, *, focus: bool = False):
        """Show the widget for the current ``view_mode`` and hide the other."""
        table = self.query_one("#objects", DataTable)
        tree = self.query_one("#tree", Tree)
        table.display = self.view_mode == "flat"
        tree.display = self.view_mode == "tree"
        if focus:
            (tree if self.view_mode == "tree" else table).focus()

    def _persist_view(self):
        """Save the current view on the open profile, if it's a saved one.

        A raw-bucket profile has nowhere to store it, so the choice just lasts
        the session. A write failure is non-fatal for the same reason settings
        writes are: the view already applies in memory.
        """
        if self.service is None:
            return
        profile = self.service.profile
        profile.view = self.view_mode
        if self.config.has(profile.name):
            self.config.stored(profile.name)["view"] = self.view_mode
            try:
                self.config.save()
            except OSError:
                pass

    def action_switch_profile(self):
        names = self.config.names()
        if not names:
            self.flash("no saved profiles. press a to add one", severity="warning")
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
            self.call_from_thread(
                self.flash, f"cannot open '{profile.name}': {exc}", severity="error", timeout=8.0
            )
            return
        self.call_from_thread(self._activate, service)

    def _activate(self, service: Service):
        self.service = service
        self.search_term = ""
        self.state_filter = None
        self.view_mode = self._profile_view(service.profile)
        self._apply_view(focus=True)
        self.flash(f"opened profile '{service.profile.name}'")
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
