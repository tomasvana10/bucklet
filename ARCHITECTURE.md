# Architecture

bucklet is a small tool for working with objects in S3 buckets, including the
archival classes (Glacier and Deep Archive) that need a restore before you can
download them. It does the same job two ways: a scriptable CLI and a Textual
TUI. Both are thin shells over one core, so neither can do something the other
can't.

## Scope

What bucklet does:

- List, upload, download, inspect, and rename objects in a bucket. The TUI can
  list either flat or as a collapsed folder tree.
- Restore ("thaw") archived objects, and only offer that when an object's live
  storage class actually needs it — quickly, or via a dialog that picks the
  tier and how long the copy stays thawed.
- Keep named profiles, each pointing at one bucket with its own credentials and
  a default upload class. A profile is either AWS or a custom S3-compatible
  endpoint, and the TUI shows only what that kind supports (see "WYSIWYG").

What it leaves out, on purpose:

- It is not a sync tool. There is no diffing and no mirroring. Reach for rclone
  or the AWS CLI when you need those.
- Deleting objects is supported, but deliberately fenced off: it is offered only
  in the TUI, only when launched with `--allow-deletion`, and always behind a
  confirmation. There is no delete subcommand on the CLI. See "Deletion" below.
- It does not manage buckets, lifecycle rules, or IAM. It assumes the bucket and
  the credentials already exist.
- It keeps no database or local cache. Every listing and status comes straight
  from S3.

## Layout

The package is a stack. Lower layers know nothing about the ones above them.

    bucklet/
      storage.py     storage-class vocabulary and object-state logic (pure)
      tree.py        builds a collapsed folder tree from object keys (pure)
      models.py      the Profile, ObjectInfo and ObjectStatus dataclasses
      errors.py      the BuckletError exception type
      formatting.py  byte-size and date display helpers
      rclone.py      reads credentials from an rclone remote
      config.py      saved profiles, default selection, atomic writes
      s3.py          thin boto3 wrappers that raise BuckletError
      service.py     the high-level operations both front-ends call
      cli.py         the argparse front-end
      tui/           the Textual front-end (the app plus its modal screens)

## Design notes

### One core, two front-ends

`service.Service` is where the work happens. It binds a resolved profile to a
boto3 client and exposes plain methods: `list_objects`, `status`, `restore`,
`download`, `upload`, `upload_many`, `delete`, `rename`, `resolve_keys`. The CLI
and the TUI both call those and only deal with presenting the results. Add a
capability to the service and both front-ends can use it.

Two are TUI-only. `delete` is the heavily-fenced one (see "Deletion"). `rename`
lives in the service too but is only surfaced in the TUI (there's no `mv`
subcommand); see "Renaming" for why it's offered without the deletion fence.

### The pure layer

`storage.py` has no boto3 and no UI. It answers three questions: which classes
exist, which ones need a restore, and what state an object is in given its class
and the S3 `Restore` header. Keeping it pure means the fiddliest logic is unit
tested without touching AWS.

### boto3 is imported lazily

`s3.py` imports boto3 inside its functions, not at module top. boto3 is slow to
import, and much of what bucklet does (printing help, managing profiles) never
needs it. The CLI and the TUI both start before any network code loads.

### Errors

Anything a user can act on is raised as `BuckletError` with a short message.
Front-ends catch it, print the message, and either exit non-zero (CLI) or flash
it in the message stack (TUI). Everything else is a bug and is left to crash with
a traceback.

The TUI has no toasts: every message — errors, warnings, progress, results — is
a timed line in the `MessageStack` above the footer. Lines auto-expire; a `key`
updates one line in place (so a stream of progress readouts stays a single line)
and clears it when done. `App.flash(text, *, severity, timeout, key)` is the only
notification entry point.

### Credentials

A profile resolves its credentials in order: explicit keys saved in the profile,
then an rclone remote it names, then the standard boto3 chain (environment,
`~/.aws`, instance role). The config file can hold secret keys, so it is written
through a temp file created 0600 from the outset and then renamed into place.

### The TUI threading model

Textual runs one event loop, and any S3 call would block it. So every operation
runs in a worker thread and reports back with `call_from_thread`. The profile
opens in a worker too, which is why the window appears at once instead of
waiting on a `head_bucket` round trip. Archived objects get a `head_object` only
when a listing can't reveal their state, so a plain bucket loads in one call.

### Uploading many files

`Service.upload_many` uploads a plan of `(local, key)` pairs through a
`ThreadPoolExecutor`. boto3 already parallelises the *parts* of one large file;
this adds *file-level* parallelism, which is the win for many small/medium files
where each is a single round-trip. A failure on one file is captured and
returned as `(key, error)` rather than aborting the batch — an archive key that
can't write one object shouldn't sink the rest. The boto3 client's
`max_pool_connections` is sized from the profile's concurrency (files × parts)
so the parallel transfers don't starve each other on a default pool of 10.

### Transfer tuning

The multipart thresholds and the two concurrency knobs are per-profile, each
optional and defaulting to a shared constant (see `models.TUNABLES`, the single
source of truth that drives both `profile tune` and the TUI settings screen). A
knob is stored only when set, so "reset to default" is just removing it — which
is why the TUI exposes reset as "clear the field". `Profile.tuning` resolves the
stored values plus defaults into a `Tuning` the s3 layer reads per transfer.

### Deletion

Deleting is the one destructive thing bucklet can do, so it is gated three ways.
The capability lives on the service (`Service.delete`, over the thin
`s3.delete_object` wrapper), but it is reachable only from the TUI, only when the
process was started with `--allow-deletion`, and only after a confirmation
dialog. The flag flows `cli.main` → `run_tui` → `BuckletApp(allow_deletion=…)`;
when it is off, `App.check_action` hides the `d` binding from the footer and
makes the key a no-op, and `action_delete` re-checks the flag as a backstop.

Failed deletes are expected, not exceptional: an archive-only key that lacks
`s3:DeleteObject` raises `AccessDenied`, which `s3.delete_object` turns into a
`BuckletError`. The TUI flashes that as an error and leaves the object on
screen, because it is still in the bucket. Only a successful delete drops the row
(removed locally, with no re-list, so deletion still works on buckets whose
listing is flaky). S3 deletion is idempotent, so there is no "already gone" case
to handle.

### Renaming

S3 has no rename, so `Service.rename` is a server-side copy to the new key
followed by a delete of the old one. It removes a key, but unlike `delete` it is
offered in the TUI ungated, because it never loses data: the copy always comes
first, and on any failure the copy is what gets undone.

The order is deliberate, all to avoid the "copied but couldn't delete, now
there's a duplicate" trap. It refuses up front when the new key is empty, equal,
or already taken, and when the source is archived (its bytes can't be copied
until thawed). Then — before copying — it probes delete permission with
`s3.can_delete`, which deletes a sentinel key that doesn't exist (DeleteObject is
idempotent, so this succeeds with the permission and 403s without it, touching no
real data). Only then does it copy and delete. If that final delete still fails
(an exact-key deny, an object lock the probe couldn't see), it deletes the fresh
copy to roll back and reports the original cause. The renamed object keeps the
source's storage class.

### The tree view

The flat `DataTable` is the default for a profile that has never been switched;
`v` toggles a `Tree` built from the keys by the pure `tree.build_key_tree`, and
the choice is remembered per profile (stored as `view`, see "Versioning and
migrations" for the v2 that added it). It collapses single-child directory chains the
way GitHub and file browsers do — `x/y/z/file.txt` becomes one folder `x/y/z`,
not three nested ones — and stops collapsing at the first branch, so structure
stays legible without a row per prefix level. Keeping the build pure means the
fiddly compression is unit-tested without a UI.

Search behaves differently per view, by design. Flat-view search filters rows.
Tree-view search would shred the structure if it filtered, so instead it
highlights matching leaves and expands their ancestor folders, leaving the rest
collapsed — every match on screen, nothing else forced open. Status polls relabel
a single leaf in place (via a key→node map) rather than rebuilding, so expansion
state survives.

### WYSIWYG: AWS vs custom S3

A profile is "AWS" when it has no custom endpoint (`Profile.is_aws`). The archival
storage classes, restores, and object states only exist on real AWS, so for a
custom S3-compatible profile the TUI hides everything that would only ever read
the same: the State and Class columns, the per-state counts in the bar, the
thaw actions, and the upload storage-class picker. The add-profile form is the
source of this — its connection segmented-button decides between an endpoint
(custom) and a storage class (AWS), so the rest follows from one choice.

### Thawing

`t` is the quick thaw (Bulk tier, default window). `T` opens a dialog to pick the
tier and how many days the restored copy stays available before S3 lets it lapse
back to cold. Either way, thawing an object larger than `THAW_CONFIRM_BYTES`
(100 MiB) first asks for confirmation, since a restore can be slow and, on the
Expedited tier, costly. The size threshold is one constant, shown to the user
through `human()` so the prompt and the rule can't drift apart.

Once an object is thawed, its `ready` state grows a countdown (`ready (2d)`,
`ready (50m)`) so you can see how long the restored copy stays downloadable
before it lapses back to cold. The window comes from the `expiry-date` in the
S3 `Restore` header, turned into a compact largest-unit string by
`formatting.thaw_remaining` (which takes an injectable `now`, so it's tested
without a clock).

## Configuration

The config lives at the platform's per-user config path (`~/.config/bucklet` on
Linux), located with `platformdirs`. Set `$BUCKLET_CONFIG_DIR` to override it,
which is how the tests keep their config out of your home directory.

### Versioning and migrations

The file carries a `version`. On load, `config._migrate` walks it up to
`CONFIG_VERSION` one step at a time, so adding a new format is just appending the
next `if version < N:` block — each step reshapes the dict and bumps `version`.
A file with no `version` is the original pre-versioning layout, treated as v1
(the same shape), so it just gains a stamp. v2 adds a per-profile `view` (the
TUI's flat/tree choice), backfilled to `flat` for profiles that predate it. The upgrade is written back on load
(best-effort — a read-only dir won't fail the load), and a config from a *newer*
bucklet is refused rather than silently downgraded. Every `save` writes the
current version, so the only thing a new migration has to get right is the
transform from the previous shape.

## Tests

`pytest` drives everything. S3 is faked with moto, so the boto3 and service
tests run offline against an in-memory bucket. The TUI is exercised with
Textual's pilot, using a fake service that records what it was asked to do.
