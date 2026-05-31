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

- It is not a sync tool. There is no diffing, no mirroring, no delete. Reach for
  rclone or the AWS CLI when you need those.
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
`download`, `upload`, `resolve_keys`. The CLI and the TUI both call those and
only deal with presenting the results. Add a capability to the service and both
front-ends can use it.

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
Front-ends catch it, print the message, and either exit non-zero (CLI) or show a
toast (TUI). Everything else is a bug and is left to crash with a traceback.

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

## Configuration

The config lives at the platform's per-user config path (`~/.config/bucklet` on
Linux), located with `platformdirs`. Set `$BUCKLET_CONFIG_DIR` to override it,
which is how the tests keep their config out of your home directory.

## Tests

`pytest` drives everything. S3 is faked with moto, so the boto3 and service
tests run offline against an in-memory bucket. The TUI is exercised with
Textual's pilot, using a fake service that records what it was asked to do.
