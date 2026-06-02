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
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    OptionList,
    RadioButton,
    RadioSet,
    Select,
    Static,
)

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
        with VerticalScroll(id="dialog"):
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
        with VerticalScroll(id="dialog"):
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
        with VerticalScroll(id="dialog"):
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


class RenameScreen(ModalScreen[str | None]):
    """Edit an object's key. Prefilled with the current key; returns the new
    key on confirm, or ``None`` on cancel. The rename itself — the copy, the
    delete-permission check, the rollback — lives in the service; this screen
    only collects the new name.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, key: str):
        super().__init__()
        self._key = key

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Label("Rename object", classes="dialog-title")
            yield Label("new key (the object's full path in the bucket)")
            yield Input(value=self._key, id="value")
            yield Label("enter to rename · esc to cancel", classes="hint")
            with Horizontal(classes="buttons"):
                yield Button("Rename", id="ok", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self):
        self.query_one("#value", Input).focus()

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def _submit(self):
        self.dismiss(self.query_one("#value", Input).value.strip())

    @on(Button.Pressed, "#cancel")
    def action_cancel(self):
        self.dismiss(None)


class AdvancedThawScreen(ModalScreen[dict | None]):
    """Restore with control over the trade-off the quick thaw hides.

    Pick a retrieval tier (Bulk is cheapest and slowest, Expedited dearest and
    fastest) and how many days the restored copy stays downloadable before S3
    lets it lapse back to cold. Returns ``{"tier": str, "days": int}`` or
    ``None`` on cancel.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    # Index-aligned with the RadioButtons below.
    _TIERS = ("Bulk", "Standard", "Expedited")

    def __init__(self, *, default_days: int = 7):
        super().__init__()
        self._default_days = default_days

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Label("Advanced thaw", classes="dialog-title")
            yield Label("retrieval tier")
            with RadioSet(id="tier", classes="segmented"):
                yield RadioButton("Bulk · ~48h, cheapest", value=True)
                yield RadioButton("Standard · ~12h")
                yield RadioButton("Expedited · ~5min, dearest")
            yield Label("days to keep thawed before re-archival")
            yield Input(value=str(self._default_days), id="days")
            yield Label("enter to thaw · esc to cancel", classes="hint")
            with Horizontal(classes="buttons"):
                yield Button("Thaw", id="ok", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self):
        self.query_one("#tier", RadioSet).focus()

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def _confirm(self):
        idx = self.query_one("#tier", RadioSet).pressed_index
        tier = self._TIERS[idx if 0 <= idx < len(self._TIERS) else 0]
        try:
            days = parse_count(self.query_one("#days", Input).value)
        except BuckletError as exc:
            self.notify(f"days: {exc}", severity="error")
            return
        self.dismiss({"tier": tier, "days": days})

    @on(Button.Pressed, "#cancel")
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
        with VerticalScroll(id="dialog"):
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
    """Form to create a new profile.

    Two segmented choices keep the form honest, and only the fields they imply
    are shown (WYSIWYG): the *connection* (AWS S3 vs a custom S3-compatible
    endpoint) decides between a storage-class picker and an endpoint box — a
    custom endpoint is assumed to have no archival tiers, so it shows no class —
    and the *credentials* choice reveals access keys, an rclone remote, or
    nothing at all (the AWS environment / IAM-role chain). Textual has no real
    segmented control, so these are :class:`RadioSet`\\ s laid out as a row.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Label("Add profile", classes="dialog-title")
            yield Label("name")
            yield Input(id="name", placeholder="my-bucket")
            yield Label("bucket")
            yield Input(id="bucket", placeholder="s3 bucket name")
            yield Label("region (optional)")
            yield Input(id="region", placeholder="e.g. ap-southeast-2, or auto for R2")

            yield Label("connection")
            with RadioSet(id="conn", classes="segmented"):
                yield RadioButton("AWS S3", value=True)
                yield RadioButton("Custom S3-compatible")
            # AWS shows the storage class; a custom endpoint shows the URL.
            with Vertical(id="class-row"):
                yield Label("default upload storage class")
                yield Select(
                    _CLASS_OPTIONS,
                    value=storage.DEFAULT_STORAGE_CLASS,
                    allow_blank=False,
                    id="class",
                )
            with Vertical(id="endpoint-row"):
                yield Label("endpoint url")
                yield Input(id="endpoint", placeholder="https://…")

            yield Label("credentials")
            with RadioSet(id="creds", classes="segmented"):
                yield RadioButton("Access keys", value=True)
                yield RadioButton("rclone remote")
                yield RadioButton("Environment / IAM role")
            with Vertical(id="keys-row"):
                yield Label("access key id")
                yield Input(id="access_key", placeholder="AKIA…")
                yield Label("secret access key")
                yield Input(id="secret", password=True)
            with Vertical(id="rclone-row"):
                yield Label("rclone remote")
                yield Input(id="rclone", placeholder="remote name")

            with Horizontal(classes="buttons"):
                yield Button("Save", id="ok", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self):
        self._sync_visibility()
        self.query_one("#name", Input).focus()

    def _is_aws(self) -> bool:
        return self.query_one("#conn", RadioSet).pressed_index == 0

    def _creds_index(self) -> int:
        return self.query_one("#creds", RadioSet).pressed_index

    @on(RadioSet.Changed)
    def _radio_changed(self):
        self._sync_visibility()

    def _sync_visibility(self):
        """Show only the fields the current radio choices call for."""
        try:
            aws = self._is_aws()
            creds = self._creds_index()
        except Exception:
            return  # a Changed can fire mid-mount, before the rows exist
        self.query_one("#class-row").display = aws
        self.query_one("#endpoint-row").display = not aws
        self.query_one("#keys-row").display = creds == 0
        self.query_one("#rclone-row").display = creds == 1

    @on(Button.Pressed, "#ok")
    def _save(self):
        name = self.query_one("#name", Input).value.strip()
        bucket = self.query_one("#bucket", Input).value.strip()
        if not name or not bucket:
            self.notify("name and bucket are required", severity="error")
            return
        region = self.query_one("#region", Input).value.strip() or None
        endpoint = None
        storage_class = storage.DEFAULT_STORAGE_CLASS
        if self._is_aws():
            storage_class = self.query_one("#class", Select).value
        else:
            endpoint = self.query_one("#endpoint", Input).value.strip()
            if not endpoint:
                self.notify("a custom endpoint url is required", severity="error")
                return
            # botocore needs a region to sign even against a custom endpoint;
            # "auto" is what R2 wants and most others tolerate, so blank is fine.
            region = region or "auto"
        access_key = secret = rclone = None
        creds = self._creds_index()
        if creds == 0:
            access_key = self.query_one("#access_key", Input).value.strip() or None
            secret = self.query_one("#secret", Input).value.strip() or None
        elif creds == 1:
            rclone = self.query_one("#rclone", Input).value.strip() or None
            if not rclone:
                self.notify("an rclone remote name is required", severity="error")
                return
        self.dismiss(
            {
                "name": name,
                "bucket": bucket,
                "region": region,
                "storage_class": storage_class,
                "rclone_remote": rclone,
                "access_key_id": access_key,
                "secret_access_key": secret,
                "endpoint_url": endpoint,
            }
        )

    @on(Button.Pressed, "#cancel")
    def action_cancel(self):
        self.dismiss(None)


class UploadScreen(ModalScreen[dict | None]):
    """Choose a file/dir, a key prefix, and how to key the objects.

    The storage-class picker is shown only for an AWS profile; a custom
    S3-compatible endpoint has no archival tiers, so it's hidden and the
    profile default is used (WYSIWYG). The "key by name" checkmark switches
    keys from the mirrored absolute path to a path relative to what you picked
    (the ``--basename-key`` of the CLI).
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, default_class: str, *, is_aws: bool = True):
        super().__init__()
        self._default_class = default_class
        self._is_aws = is_aws

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Label("Upload", classes="dialog-title")
            yield Label("path (file or directory)")
            yield Input(id="path", placeholder="/path/to/file")
            if self._is_aws:
                yield Label("storage class")
                yield Select(
                    _CLASS_OPTIONS, value=self._default_class, allow_blank=False, id="class"
                )
            yield Label("key prefix (optional)")
            yield Input(id="prefix")
            yield Checkbox("use base name as key", id="basename")
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
        storage_class = (
            self.query_one("#class", Select).value if self._is_aws else self._default_class
        )
        self.dismiss(
            {
                "path": path,
                "storage_class": storage_class,
                "prefix": self.query_one("#prefix", Input).value.strip(),
                "basename_key": self.query_one("#basename", Checkbox).value,
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
