# archy

Browse, upload, download and **restore** objects across S3 buckets of **any
storage class** — from a scriptable CLI or a [Textual](https://textual.textualize.io)
TUI. The CLI is a complete superset of the TUI: anything you can do
interactively, you can script.

archy is the successor to `deeparch`. It is no longer Glacier-only: it works
with plain buckets too, and restore ("thaw") is offered **only for objects
whose live storage class actually needs it** (`GLACIER` / `DEEP_ARCHIVE`). A
standard bucket simply never shows it.

## Install

```bash
uv sync                 # dev: create the venv with all deps
uv run archy --help

# or install the CLI on your PATH
uv tool install .
archy --help
```

Requires Python ≥ 3.10. Runtime deps: `boto3`, `textual`.

## Profiles

A *profile* names one bucket plus how to reach it and a **default upload
storage class**. Commands run against a profile chosen with `--profile NAME`
(accepted before or after the subcommand); without it the configured default
profile is used. An unknown `--profile` value is treated as a raw bucket name
using the standard AWS credential chain.

```bash
# an archival bucket (uploads default to deep archive)
archy profile add cold --bucket my-cold-bucket --region ap-southeast-2 \
    --class deep_archive --default

# a plain bucket (uploads default to standard)
archy profile add web --bucket my-web-bucket --region us-east-1 --class standard

archy profile ls            # list saved profiles
archy profile show cold     # show a resolved profile (creds source, region, class)
archy profile default web   # change the default
archy profile rm web        # remove
```

Credentials for a profile come from, in order: explicit keys saved in the
profile (`--access-key` / `--secret`), the rclone remote it names
(`--rclone-remote`, parsed from `rclone.conf`), then the standard AWS chain
(env / `~/.aws` / instance role). Custom S3 endpoints are supported with
`--endpoint-url`.

## Commands

```bash
archy                        # launch the TUI (default / --profile)
archy up PATH [PATH...]      # upload files/dirs (keys mirror the absolute path)
archy up PATH --class standard   # override the profile's default class per-upload
archy up PATH --prefix backups   # store under a key prefix
archy get  KEY [KEY...]      # download objects (globs allowed) under -o/--outdir
archy thaw KEY [KEY...]      # restore archived objects (--tier / --standard / --days)
archy ls   [PREFIX]          # list objects (-l long, --search TERM, --state STATE)
archy stat KEY [KEY...]      # detailed status of objects (globs allowed)
archy profile ...            # manage profiles (add / ls / rm / default / show)
```

`thaw` works per-object: archived objects are restored; anything already
available reports "no thaw needed" instead. You can mix classes freely — e.g.
`archy up file --class deep_archive` into an otherwise-standard profile, then
`archy thaw` that one object later.

### Storage classes

`--class` accepts any of (case- and `-`/`_`-insensitive, plus short aliases):
`standard`, `reduced_redundancy`, `standard_ia` (`ia`), `onezone_ia`,
`intelligent_tiering` (`it`), `glacier_ir` (`ir`), `glacier`, `deep_archive`
(`deep`, `da`). Only `glacier` and `deep_archive` ever require a thaw.

## TUI

Run `archy` (optionally `--profile NAME`). Keys:

| key | action |
|-----|--------|
| ↑ ↓ | move selection |
| `i` / enter | object detail |
| `t` / `T` | thaw selected (Bulk / Standard) — only when it's cold |
| `g` | download selected |
| `u` | upload (path + class + prefix) |
| `r` | refresh statuses |
| `/` | search by substring |
| `f` | cycle state filter (all / cold / thawing / thawed / available) |
| `p` | switch saved profile |
| `a` | add a new profile |
| `q` | quit |

## Configuration

Config lives at `~/.config/archy/config.json` (override with `$ARCHY_CONFIG_DIR`),
written `0600` because it may hold secret keys. On first run, an existing
`~/.config/deeparch/config.json` is migrated automatically; migrated profiles
inherit `deep_archive` as their default upload class to preserve old behaviour.

`$RCLONE_CONFIG` selects the rclone config to read remotes from. The old
`$DEEPARCH_DEST` / `$DEEPARCH_BUCKET` variables are gone — use `--profile`.

## Layout

```
src/archy/
  storage.py   storage-class vocabulary + object-state logic (pure)
  models.py    Profile / ObjectInfo / ObjectStatus dataclasses
  rclone.py    read credentials from an rclone remote
  config.py    saved profiles, default selection, deeparch migration
  s3.py        thin boto3 wrappers (errors -> ArchyError)
  service.py   the UI-agnostic core used by every front-end
  cli.py       argparse front-end
  tui/         Textual front-end (app + modal screens)
```

## Development

```bash
uv sync
uv run pytest            # full suite (moto for S3, Textual pilot for the TUI)
uv run pytest --cov=archy
```
