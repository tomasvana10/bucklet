# Architecture

bucklet is a small tool for working with objects in S3 buckets, including the
archival classes (Glacier and Deep Archive) that need a restore before you can
download them. It does the same job two ways: a scriptable CLI and a Textual
TUI. Both are thin shells over one core, so neither can do something the other
can't.

## Scope

What bucklet does:

- List, upload, download, and inspect objects in a bucket.
- Restore ("thaw") archived objects, and only offer that when an object's live
  storage class actually needs it.
- Keep named profiles, each pointing at one bucket with its own credentials and
  a default upload class.

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
`download`, `upload`, `upload_many`, `delete`, `resolve_keys`. The CLI and the
TUI both call those and only deal with presenting the results. Add a capability
to the service and both front-ends can use it.

The one exception is `delete`: the capability lives in the service like the
rest, but only the TUI surfaces it (see "Deletion"). The CLI never calls it.

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

## Configuration

The config lives at the platform's per-user config path (`~/.config/bucklet` on
Linux), located with `platformdirs`. Set `$BUCKLET_CONFIG_DIR` to override it,
which is how the tests keep their config out of your home directory.

### Versioning and migrations

The file carries a `version`. On load, `config._migrate` walks it up to
`CONFIG_VERSION` one step at a time, so adding a new format is just appending the
next `if version < N:` block — each step reshapes the dict and bumps `version`.
A file with no `version` is the original pre-versioning layout, treated as v1
(the same shape), so it just gains a stamp. The upgrade is written back on load
(best-effort — a read-only dir won't fail the load), and a config from a *newer*
bucklet is refused rather than silently downgraded. Every `save` writes the
current version, so the only thing a new migration has to get right is the
transform from the previous shape.

## Tests

`pytest` drives everything. S3 is faked with moto, so the boto3 and service
tests run offline against an in-memory bucket. The TUI is exercised with
Textual's pilot, using a fake service that records what it was asked to do.
