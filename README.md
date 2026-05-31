# bucklet

A small command-line and TUI tool for moving objects in and out of S3 buckets,
including the cold storage classes (Glacier and Deep Archive) that need a thaw
before you can download them. The CLI scripts cleanly and the TUI is for poking
around; they share one core, so anything you can click you can also script.

It works with plain buckets too. The thaw step only shows up for objects whose
live storage class actually needs it, so a standard bucket never asks you to
thaw anything.

## Install

```bash
uv sync                 # create the dev venv with everything
uv run bucklet --help

# or install the CLI on your PATH
uv tool install .
bucklet --help
```

Needs Python 3.10 or newer. The runtime dependencies are boto3, textual,
platformdirs, and argcomplete.

## Shell completion

bucklet ships tab completion through
[argcomplete](https://github.com/kislyuk/argcomplete). It completes subcommands,
flags, `--class` and `--tier` values, and your saved profile names, none of which
touch the network.

A package installer can't switch this on for you, since nothing pip installs is
allowed to edit your shell config, so it takes one small step. For fish:

```fish
register-python-argcomplete --shell fish bucklet | source
```

Drop that into `~/.config/fish/completions/bucklet.fish` to keep it. For bash or
zsh, use `register-python-argcomplete bucklet` instead.

If you'd rather not register tools one by one, run
`activate-global-python-argcomplete` once. After that, every argcomplete-based
CLI you install (bucklet included) completes with no further per-tool setup.

## Profiles

A profile is a name for one bucket: how to reach it, which credentials to use,
and the default storage class to upload with. Choose one per command with
`--profile NAME` (before or after the subcommand). Without it, bucklet uses the
profile you set as default. A `--profile` value that isn't a saved name is
treated as a raw bucket name, using the standard AWS credential chain.

```bash
# an archival bucket (uploads default to deep archive)
bucklet profile add cold --bucket my-cold-bucket --region ap-southeast-2 \
    --class deep_archive --default

# a plain bucket (uploads default to standard)
bucklet profile add web --bucket my-web-bucket --region us-east-1 --class standard

bucklet profile ls            # list saved profiles
bucklet profile show cold     # show a resolved profile (creds source, region, class)
bucklet profile default web   # change the default
bucklet profile rm web        # remove one
```

Credentials are resolved in order: explicit keys saved in the profile
(`--access-key` / `--secret`), then the rclone remote it names
(`--rclone-remote`, read from `rclone.conf`), then the standard AWS chain
(environment, `~/.aws`, instance role). For S3-compatible storage, point it at a
custom endpoint with `--endpoint-url`.

## Commands

```bash
bucklet                       # launch the TUI (default or --profile)
bucklet up PATH [PATH...]     # upload files or dirs (keys mirror the absolute path)
bucklet up PATH --class standard   # override the profile's default class for one upload
bucklet up PATH --prefix backups   # store under a key prefix
bucklet get  KEY [KEY...]     # download objects (globs allowed) into -o/--outdir
bucklet thaw KEY [KEY...]     # restore archived objects (--tier / --standard / --days)
bucklet ls   [PREFIX]         # list objects (-l long, --search TERM, --state STATE)
bucklet stat KEY [KEY...]     # detailed status of objects (globs allowed)
bucklet profile ...           # manage profiles (add / ls / rm / default / show)
```

`thaw` works per object. Archived objects start restoring; anything already
available reports "no thaw needed" instead. You can mix classes in one bucket:
upload a single file with `bucklet up file --class deep_archive` into an
otherwise-standard profile, then `bucklet thaw` that one object later.

### Storage classes

`--class` takes any of these, case-insensitive, with `-` or `_`, plus short
aliases: `standard`, `reduced_redundancy` (`rr`), `standard_ia` (`ia`),
`onezone_ia` (`onezone`), `intelligent_tiering` (`it`, `intelligent`,
`tiering`), `glacier_ir` (`ir`), `glacier` (`flexible`), `deep_archive` (`deep`,
`da`, `archive`). Only `glacier` and `deep_archive` ever need a thaw.

## TUI

Run `bucklet` (optionally with `--profile NAME`). The window opens straight away
and fills in once the bucket loads. Keys:

| key | action |
|-----|--------|
| ↑ ↓ | move selection |
| `i` / enter | object detail |
| `t` / `T` | thaw selected (Bulk / Standard), only when it's cold |
| `g` | download selected |
| `u` | upload (path, class, prefix) |
| `r` | refresh statuses |
| `/` | search by substring |
| `f` | cycle the state filter (all / cold / thawing / thawed / available) |
| `p` | switch saved profile |
| `a` | add a profile |
| `q` | quit (Ctrl+C does too) |

## Configuration

The config lives at `~/.config/bucklet/config.json` on Linux (the platform's
standard per-user config dir elsewhere). Override it with `$BUCKLET_CONFIG_DIR`.
It is written 0600 because it may hold secret keys. `$RCLONE_CONFIG` picks which
rclone config to read remotes from.

## Layout

```
src/bucklet/
  storage.py     storage-class vocabulary and object-state logic (pure)
  models.py      Profile, ObjectInfo and ObjectStatus dataclasses
  errors.py      the BuckletError exception type
  formatting.py  byte-size and date display helpers
  rclone.py      read credentials from an rclone remote
  config.py      saved profiles and the default selection
  s3.py          thin boto3 wrappers (errors become BuckletError)
  service.py     the UI-agnostic core both front-ends use
  cli.py         argparse front-end
  tui/           Textual front-end (the app plus its modal screens)
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for how the pieces fit together.

## Development

```bash
uv sync                 # install everything, including the dev tools
uv run pytest           # full suite (moto for S3, Textual pilot for the TUI)
uv run pytest --cov=bucklet
pre-commit install      # run ruff check and format on every commit
```
