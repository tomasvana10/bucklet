"""Modal dialogs used by the TUI.

Each screen collects input and hands it back through ``dismiss(...)`` (or
``dismiss(None)`` on cancel), so the app can drive them with
``push_screen(screen, callback)``.
"""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Select, Static

from .. import storage
from ..errors import BuckletError
from ..formatting import human, parse_count, parse_size
from ..models import TUNABLES, Profile

_CLASS_OPTIONS = [(c.lower(), c) for c in storage.STORAGE_CLASSES]


class DetailScreen(ModalScreen[None]):
    """Read-only object detail. Any of esc/enter/q closes it."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("enter", "dismiss", "Close"),
        Binding("q", "dismiss", "Close"),
    ]

    def __init__(self, title: str, lines: list[str]):
        super().__init__()
        self._title = title
        self._lines = lines

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, classes="dialog-title")
            yield Static("\n".join(self._lines))
            yield Label("esc / enter to close", classes="hint")

    def action_dismiss(self):
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """Yes/no confirmation for a destructive action.

    Returns ``True`` only on an explicit confirm; esc, ``n`` and Cancel all
    return ``False``. Focus starts on Cancel so a stray Enter never deletes.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("n", "cancel", "No"),
        Binding("y", "confirm", "Yes"),
    ]

    def __init__(self, title: str, lines: list[str], confirm_label: str = "Delete"):
        super().__init__()
        self._title = title
        self._lines = lines
        self._confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, classes="dialog-title")
            yield Static("\n".join(self._lines))
            yield Label("y to confirm · n / esc to cancel", classes="hint")
            with Horizontal(classes="buttons"):
                yield Button(self._confirm_label, id="ok", variant="error")
                yield Button("Cancel", id="cancel")

    def on_mount(self):
        self.query_one("#cancel", Button).focus()

    @on(Button.Pressed, "#ok")
    def action_confirm(self):
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self):
        self.dismiss(False)


class PromptScreen(ModalScreen[str | None]):
    """One-line text prompt (used for search)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, label: str, value: str = ""):
        super().__init__()
        self._label = label
        self._value = value

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._label, classes="dialog-title")
            yield Input(value=self._value, id="value")
            yield Label("enter to apply · esc to cancel", classes="hint")

    def on_mount(self):
        self.query_one("#value", Input).focus()

    @on(Input.Submitted)
    def _submit(self):
        self.dismiss(self.query_one("#value", Input).value)

    def action_cancel(self):
        self.dismiss(None)


class ProfileScreen(ModalScreen[str | None]):
    """Pick a saved profile by name."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, names: list[str], labels: list[str]):
        super().__init__()
        self._names = names
        self._labels = labels

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Switch profile", classes="dialog-title")
            yield OptionList(*self._labels, id="profiles")
            yield Label("enter to open · esc to cancel", classes="hint")

    def on_mount(self):
        self.query_one("#profiles", OptionList).focus()

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected):
        self.dismiss(self._names[event.option_index])

    def action_cancel(self):
        self.dismiss(None)


class AddProfileScreen(ModalScreen[dict | None]):
    """Form to create a new profile."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Label("Add profile", classes="dialog-title")
            yield Label("name")
            yield Input(id="name", placeholder="my-bucket")
            yield Label("bucket")
            yield Input(id="bucket", placeholder="s3 bucket name")
            yield Label("region")
            yield Input(id="region", placeholder="e.g. ap-southeast-2")
            yield Label("default upload storage class")
            yield Select(
                _CLASS_OPTIONS, value=storage.DEFAULT_STORAGE_CLASS, allow_blank=False, id="class"
            )
            yield Label("rclone remote (leave blank to type keys)")
            yield Input(id="rclone", placeholder="rclone remote name")
            yield Label("access key id (optional)")
            yield Input(id="access_key", placeholder="AKIA…")
            yield Label("secret access key (optional)")
            yield Input(id="secret", password=True)
            yield Label("endpoint url (optional, for S3-compatible)")
            yield Input(id="endpoint", placeholder="https://…")
            with Horizontal(classes="buttons"):
                yield Button("Save", id="ok", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self):
        self.query_one("#name", Input).focus()

    @on(Button.Pressed, "#ok")
    def _save(self):
        name = self.query_one("#name", Input).value.strip()
        bucket = self.query_one("#bucket", Input).value.strip()
        if not name or not bucket:
            self.notify("name and bucket are required", severity="error")
            return
        self.dismiss(
            {
                "name": name,
                "bucket": bucket,
                "region": self.query_one("#region", Input).value.strip() or None,
                "storage_class": self.query_one("#class", Select).value,
                "rclone_remote": self.query_one("#rclone", Input).value.strip() or None,
                "access_key_id": self.query_one("#access_key", Input).value.strip() or None,
                "secret_access_key": self.query_one("#secret", Input).value.strip() or None,
                "endpoint_url": self.query_one("#endpoint", Input).value.strip() or None,
            }
        )

    @on(Button.Pressed, "#cancel")
    def action_cancel(self):
        self.dismiss(None)


class UploadScreen(ModalScreen[dict | None]):
    """Choose a file/dir, a storage class, and an optional key prefix."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, default_class: str):
        super().__init__()
        self._default_class = default_class

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Upload", classes="dialog-title")
            yield Label("path (file or directory)")
            yield Input(id="path", placeholder="/path/to/file")
            yield Label("storage class")
            yield Select(_CLASS_OPTIONS, value=self._default_class, allow_blank=False, id="class")
            yield Label("key prefix (optional)")
            yield Input(id="prefix")
            with Horizontal(classes="buttons"):
                yield Button("Upload", id="ok", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self):
        self.query_one("#path", Input).focus()

    @on(Button.Pressed, "#ok")
    def _upload(self):
        path = self.query_one("#path", Input).value.strip()
        if not path:
            self.notify("a path is required", severity="error")
            return
        self.dismiss(
            {
                "path": path,
                "storage_class": self.query_one("#class", Select).value,
                "prefix": self.query_one("#prefix", Input).value.strip(),
            }
        )

    @on(Button.Pressed, "#cancel")
    def action_cancel(self):
        self.dismiss(None)


class SettingsScreen(ModalScreen[dict | None]):
    """Edit a profile's transfer tuning.

    One input per :data:`~bucklet.models.TUNABLES` entry, pre-filled with the
    profile's current value (blank when it uses the default, with the default
    shown as the placeholder). Saving returns ``{key: int | None}`` where a
    blank field maps to ``None`` — i.e. clearing a field resets just that one
    setting to its default.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, profile: Profile):
        super().__init__()
        self._profile = profile

    def _field_id(self, key: str) -> str:
        return f"tune-{key.replace('_', '-')}"

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Label(f"Settings · {self._profile.name}", classes="dialog-title")
            yield Label("clear a field to reset it to the default", classes="hint")
            for t in TUNABLES:
                raw = getattr(self._profile, t.key)
                default_str = human(t.default) if t.is_size else str(t.default)
                current = "" if raw is None else (human(raw) if t.is_size else str(raw))
                yield Label(t.label)
                yield Input(
                    value=current,
                    placeholder=f"default {default_str}",
                    id=self._field_id(t.key),
                )
            with Horizontal(classes="buttons"):
                yield Button("Save", id="ok", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self):
        self.query_one(f"#{self._field_id(TUNABLES[0].key)}", Input).focus()

    @on(Button.Pressed, "#ok")
    def _save(self):
        values: dict[str, int | None] = {}
        for t in TUNABLES:
            text = self.query_one(f"#{self._field_id(t.key)}", Input).value.strip()
            if not text:
                values[t.key] = None  # reset to default
                continue
            try:
                values[t.key] = parse_size(text) if t.is_size else parse_count(text)
            except BuckletError as exc:
                self.notify(f"{t.label}: {exc}", severity="error")
                return
        self.dismiss(values)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self):
        self.dismiss(None)
