"""Command-line front-end.

Every subcommand operates on one profile (``--profile NAME``, accepted before or
after the subcommand; it falls back to the configured default). Running bucklet
with no subcommand launches the Textual TUI.

The CLI covers everything the TUI does, with one deliberate exception: object
deletion. Deleting is destructive and offered only interactively, in the TUI,
and only when bucklet is launched with ``--allow-deletion``. There is no delete
subcommand, by design.
"""

# PYTHON_ARGCOMPLETE_OK
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import storage
from .config import Config
from .errors import BuckletError
from .formatting import fmt_date, human, parse_size
from .models import TUNABLES, Profile
from .service import Service

try:
    import argcomplete
    from argcomplete.completers import DirectoriesCompleter, FilesCompleter

    _FILES = FilesCompleter()
    _DIRS = DirectoriesCompleter()
except ImportError:  # tab completion is optional; the CLI works fine without it
    argcomplete = None
    _FILES = _DIRS = None

PROG = "bucklet"


def _set_completer(action, completer):
    """Attach a tab-completion source to an argparse action, if argcomplete is installed."""
    if argcomplete is not None and completer is not None:
        action.completer = completer
    return action


def _class_completer(**_):
    """Complete --class with the canonical storage classes and their aliases."""
    return [c.lower() for c in storage.STORAGE_CLASSES] + [a.lower() for a in storage._ALIASES]


def _profile_completer(**_):
    """Complete --profile with saved profile names (read from the config, no network)."""
    try:
        return Config.load().names()
    except Exception:
        return []


def build_parser():
    # Shared --profile, added to the top parser and every subparser so it works
    # in either position. SUPPRESS keeps a subparser default from clobbering a
    # value given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    _set_completer(
        common.add_argument(
            "--profile",
            metavar="NAME",
            default=argparse.SUPPRESS,
            help="profile to use (a saved name, or a raw bucket name); "
            "defaults to the configured default profile",
        ),
        _profile_completer,
    )

    class_list = ", ".join(c.lower() for c in storage.STORAGE_CLASSES)
    class_help = f"storage class (e.g. {class_list})"

    p = argparse.ArgumentParser(
        prog=PROG, parents=[common], description="Manage S3 objects in any storage class."
    )
    # TUI-only guard: with no subcommand bucklet opens the TUI, and this flag is
    # what unlocks object deletion there. It has no effect on the subcommands.
    p.add_argument(
        "--allow-deletion",
        action="store_true",
        help="allow deleting objects in the TUI (no effect on the subcommands)",
    )
    sub = p.add_subparsers(dest="cmd")

    up = sub.add_parser("up", parents=[common], help="upload files/dirs (mirrors absolute path)")
    _set_completer(up.add_argument("paths", nargs="+"), _FILES)
    _set_completer(
        up.add_argument("-c", "--class", dest="storage_class", metavar="CLASS", help=class_help),
        _class_completer,
    )
    up.add_argument("--prefix", default="", help="key prefix to store objects under")
    up.add_argument(
        "--basename-key",
        action="store_true",
        help="key each object by its name relative to the path given, not its full absolute path",
    )

    get = sub.add_parser("get", parents=[common], help="download objects (globs allowed)")
    get.add_argument("keys", nargs="+")
    _set_completer(
        get.add_argument("-o", "--outdir", default=".", help="output directory (default .)"),
        _DIRS,
    )

    thaw = sub.add_parser("thaw", parents=[common], help="thaw archived objects (globs allowed)")
    thaw.add_argument("keys", nargs="+")
    thaw.add_argument(
        "--tier",
        choices=["Bulk", "Standard", "Expedited"],
        default="Bulk",
        help="thaw tier (default Bulk, ~48h, cheapest)",
    )
    thaw.add_argument("--standard", action="store_true", help="shortcut for --tier Standard (~12h)")
    thaw.add_argument(
        "--days", type=int, default=7, help="days to keep the thawed copy (default 7)"
    )

    ls = sub.add_parser("ls", parents=[common], help="list objects")
    ls.add_argument("prefix", nargs="?", default="")
    ls.add_argument("-l", "--long", action="store_true", help="long format showing class and state")
    ls.add_argument("--search", metavar="TERM", help="only keys containing TERM")
    ls.add_argument(
        "--state",
        choices=[storage.AVAILABLE, storage.COLD, storage.THAWING, storage.THAWED],
        help="only objects in this state (HEADs archived objects to refine)",
    )

    stat = sub.add_parser(
        "stat", parents=[common], help="show detailed status of objects (globs allowed)"
    )
    stat.add_argument("keys", nargs="+")

    _build_profile_parser(sub, common, class_help)
    return p


def _build_profile_parser(
    sub: argparse._SubParsersAction,
    common: argparse.ArgumentParser,
    class_help: str,
):
    pf = sub.add_parser("profile", parents=[common], help="manage saved profiles")
    ps = pf.add_subparsers(dest="pcmd")

    add = ps.add_parser("add", parents=[common], help="add or overwrite a profile")
    add.add_argument("name")
    add.add_argument("--bucket", required=True)
    add.add_argument("--region")
    _set_completer(
        add.add_argument(
            "-c",
            "--class",
            dest="storage_class",
            metavar="CLASS",
            help="default upload " + class_help,
        ),
        _class_completer,
    )
    add.add_argument("--access-key", dest="access_key_id")
    add.add_argument("--secret", dest="secret_access_key")
    add.add_argument(
        "--rclone-remote", dest="rclone_remote", help="rclone remote to read credentials from"
    )
    add.add_argument(
        "--endpoint-url", dest="endpoint_url", help="custom S3 endpoint (for S3-compatible storage)"
    )
    add.add_argument("--default", action="store_true", help="make this the default profile")

    ps.add_parser("ls", parents=[common], help="list saved profiles")

    rm = ps.add_parser("rm", parents=[common], help="remove a profile")
    rm.add_argument("name")

    dflt = ps.add_parser("default", parents=[common], help="set the default profile")
    dflt.add_argument("name")

    show = ps.add_parser("show", parents=[common], help="show a resolved profile")
    show.add_argument("name", nargs="?")

    tune = ps.add_parser(
        "tune", parents=[common], help="set per-profile transfer tuning (chunk size, concurrency)"
    )
    tune.add_argument("name")
    for t in TUNABLES:
        tune.add_argument(
            "--" + t.key.replace("_", "-"),
            dest=t.key,
            metavar="SIZE" if t.is_size else "N",
            help=f"{t.label} (default {_fmt_tunable(t)})",
        )
    tune.add_argument(
        "--reset",
        nargs="+",
        metavar="FIELD",
        choices=[t.key.replace("_", "-") for t in TUNABLES] + ["all"],
        help="reset the named setting(s) to default ('all' for every one)",
    )


def _profile_arg(args: argparse.Namespace) -> str | None:
    return getattr(args, "profile", None)


def _open_service(config: Config, args: argparse.Namespace, *, validate: bool = True):
    profile = config.resolve(_profile_arg(args))
    if profile is None or not profile.bucket:
        raise BuckletError(
            "no profile configured. add one with "
            "'bucklet profile add NAME --bucket BUCKET ...', or pass --profile."
        )
    return Service.open(profile, validate=validate)


class _UploadProgress:
    """Single-line aggregate progress for a batch upload (to stderr).

    Concurrent uploads can't each own a progress line without garbling, so this
    shows one rolling total. Updates are throttled to whole-percent / file
    boundaries to keep the write rate sane across many small files.
    """

    def __init__(self):
        self._last: tuple[int, int] = (-1, -1)

    def __call__(self, sent: int, total: int, done: int, total_files: int):
        pct = min(100, sent * 100 // total)
        if (pct, done) == self._last:
            return
        self._last = (pct, done)
        sys.stderr.write(
            f"\r  {done}/{total_files} files · {human(sent)}/{human(total)} {pct:3d}%   "
        )
        sys.stderr.flush()

    def finish(self):
        sys.stderr.write("\n")
        sys.stderr.flush()


def cmd_up(config: Config, args: argparse.Namespace):
    service = _open_service(config, args)
    plan = service.plan_upload(args.paths, prefix=args.prefix, basename_key=args.basename_key)
    if not plan:
        print("nothing to upload.")
        return 0
    cls = service.resolve_storage_class(args.storage_class)
    conc = service.profile.tuning.upload_concurrency
    print(f"uploading {len(plan)} file(s) -> [{cls.lower()}], up to {conc} at a time")
    reporter = _UploadProgress()
    results = service.upload_many(plan, storage_class=args.storage_class, progress=reporter)
    reporter.finish()
    failures = [(key, err) for key, err in results if err is not None]
    for key, err in failures:
        print(f"ERR  {key}: {err}")
    ok = len(results) - len(failures)
    summary = f"{ok}/{len(results)} uploaded"
    if failures:
        summary += f", {len(failures)} failed"
    print(summary, file=sys.stderr)
    return 1 if failures else 0


def cmd_get(config: Config, args: argparse.Namespace):
    service = _open_service(config, args)
    resolution = service.resolve_keys(args.keys)
    for miss in resolution.missing:
        sys.stderr.write(f"no match: {miss}\n")
    if not resolution.matched:
        raise BuckletError("no matching objects.")
    outdir = Path(args.outdir)
    rc = 0
    for key in resolution.matched:
        dest = outdir / key
        try:
            service.download(key, dest)
            print(f"ok   {key} -> {dest}")
        except BuckletError as exc:
            print(f"ERR  {key}: {exc}")
            rc = 1
    return rc


def cmd_thaw(config: Config, args: argparse.Namespace):
    service = _open_service(config, args)
    tier = "Standard" if args.standard else args.tier
    resolution = service.resolve_keys(args.keys)
    for miss in resolution.missing:
        sys.stderr.write(f"no match: {miss}\n")
    if not resolution.matched:
        raise BuckletError("no matching objects.")
    rc = 0
    for key in resolution.matched:
        try:
            message = service.restore(key, tier=tier, days=args.days)
            print(f"ok   {key}: {message}")
        except BuckletError as exc:
            print(f"ERR  {key}: {exc}")
            rc = 1
    return rc


def cmd_ls(config: Config, args: argparse.Namespace):
    service = _open_service(config, args)
    objects = service.list_objects(args.prefix or "")
    if args.search:
        term = args.search.lower()
        objects = [o for o in objects if term in o.key.lower()]

    states: dict[str, str] = {}
    if args.state:
        # A listing never carries the Restore header, so any object that could be
        # archived or restoring needs a HEAD; the rest are known to be available
        # straight from the listing.
        for o in objects:
            if (o.storage_class or "").upper() in storage.RESTORABLE_CLASSES:
                states[o.key] = service.status(o.key).state
            else:
                states[o.key] = o.baseline_state
        objects = [o for o in objects if states.get(o.key) == args.state]

    if not objects:
        print("(no objects)")
        return 0
    for o in objects:
        if args.long:
            state = states.get(o.key, o.baseline_state)
            label = storage.STATE_LABEL.get(state, "?")
            print(
                f"{human(o.size):>10}  {fmt_date(o.last_modified)}  "
                f"{o.storage_class:<20} {label:<6} {o.key}"
            )
        else:
            print(o.key)
    total = sum(o.size for o in objects)
    print(f"\n{len(objects)} object(s), {human(total)} total", file=sys.stderr)
    return 0


def cmd_stat(config: Config, args: argparse.Namespace):
    service = _open_service(config, args)
    resolution = service.resolve_keys(args.keys)
    for miss in resolution.missing:
        sys.stderr.write(f"no match: {miss}\n")
    if not resolution.matched:
        raise BuckletError("no matching objects.")
    for key in resolution.matched:
        st = service.status(key)
        print(key)
        print(f"  class    : {st.storage_class}")
        print(f"  state    : {st.state}" + (f" ({st.error})" if st.error else ""))
        if st.size is not None:
            print(f"  size     : {human(st.size)} ({st.size} bytes)")
        if st.last_modified is not None:
            print(f"  modified : {fmt_date(st.last_modified)}")
        if st.restore_expiry:
            print(f"  restored : until {st.restore_expiry}")
        if storage.can_thaw(st.state):
            print("  note     : archived; run 'bucklet thaw' before downloading")
    return 0


def cmd_profile(config: Config, args: argparse.Namespace):
    pcmd = getattr(args, "pcmd", None)
    if pcmd == "add":
        return _profile_add(config, args)
    if pcmd == "ls" or pcmd is None:
        return _profile_ls(config)
    if pcmd == "rm":
        config.remove(args.name)
        config.save()
        print(f"removed '{args.name}'")
        return 0
    if pcmd == "default":
        config.set_default(args.name)
        config.save()
        print(f"default set to {args.name}")
        return 0
    if pcmd == "show":
        return _profile_show(config, args)
    if pcmd == "tune":
        return _profile_tune(config, args)
    # Unreachable via the CLI: argparse rejects an unknown subcommand (printing
    # its own usage with the valid choices) long before we get here. No need to
    # restate the command list and let it drift.
    raise BuckletError(f"unknown profile subcommand: {pcmd!r}")


def _profile_add(config: Config, args: argparse.Namespace):
    cls = storage.DEFAULT_STORAGE_CLASS
    if args.storage_class:
        cls = storage.normalize_storage_class(args.storage_class)
    # `add` overwrites connection settings, but it has no flags for the transfer
    # tuning (that's `profile tune`'s job), so carry any existing tuning across
    # an overwrite rather than silently wiping it.
    prior = config.stored(args.name) if config.has(args.name) else {}
    profile = Profile(
        name=args.name,
        bucket=args.bucket,
        region=args.region,
        access_key_id=args.access_key_id,
        secret_access_key=args.secret_access_key,
        rclone_remote=args.rclone_remote,
        endpoint_url=args.endpoint_url,
        storage_class=cls,
        multipart_threshold=prior.get("multipart_threshold"),
        multipart_chunksize=prior.get("multipart_chunksize"),
        upload_concurrency=prior.get("upload_concurrency"),
        max_concurrency=prior.get("max_concurrency"),
    )
    config.add(profile, make_default=args.default)
    config.save()
    tag = " (default)" if config.default == profile.name else ""
    print(f"saved profile '{profile.name}'{tag}")
    return 0


def _profile_ls(config: Config):
    if not config.profiles:
        print("no profiles. add one:  bucklet profile add NAME --bucket BUCKET [--class CLASS] ...")
        return 0
    for name in config.names():
        prof = config.get(name)
        marker = "*" if config.default == name else " "
        bucket = prof.bucket or "?"
        region = prof.region or "?"
        print(
            f"{marker} {name:<16} {bucket:<40} {region:<15} "
            f"{prof.storage_class.lower():<14} [{prof.credential_source}]"
        )
    return 0


def _profile_show(config: Config, args: argparse.Namespace):
    profile = config.resolve(args.name)
    if profile is None:
        raise BuckletError("no such profile (and no default set)")
    archival = storage.needs_restore(profile.storage_class)
    note = "  [uploads are archived, need thaw]" if archival else ""
    print(f"profile  : {profile.name}")
    print(f"bucket   : {profile.bucket or '?'}")
    print(f"region   : {profile.region or '(default)'}")
    print(f"class    : {profile.storage_class}{note}")
    if profile.endpoint_url:
        print(f"endpoint : {profile.endpoint_url}")
    print(f"creds    : {profile.credential_source}")
    if profile.has_explicit_keys:
        print(f"key id   : {profile.access_key_id}")
        print("secret   : ****")
    _print_tuning(profile)
    return 0


def _fmt_tunable(t) -> str:
    """Human form of a tunable's default (a size or a count)."""
    return human(t.default) if t.is_size else str(t.default)


def _parse_count(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise BuckletError(f"expected a whole number, got {raw!r}") from exc
    if value <= 0:
        raise BuckletError(f"must be positive: {raw!r}")
    return value


def _print_tuning(profile: Profile):
    print("tuning   :")
    for t in TUNABLES:
        raw = getattr(profile, t.key)
        effective = t.default if raw is None else raw
        shown = human(effective) if t.is_size else str(effective)
        tag = "  (default)" if raw is None else ""
        print(f"  {t.label:<20} {shown}{tag}")


def _profile_tune(config: Config, args: argparse.Namespace):
    name = args.name
    if not config.has(name):
        raise BuckletError(f"no such profile: {name}")
    stored = config.stored(name)
    resets = set(getattr(args, "reset", None) or [])
    if "all" in resets:
        resets = {t.key.replace("_", "-") for t in TUNABLES}
    changed = False
    for t in TUNABLES:
        raw = getattr(args, t.key, None)
        if raw is not None:
            # An explicit value wins over a reset of the same field.
            stored[t.key] = parse_size(raw) if t.is_size else _parse_count(raw)
            changed = True
        elif t.key.replace("_", "-") in resets:
            if stored.pop(t.key, None) is not None:
                changed = True
    if changed:
        config.save()
    _print_tuning(config.get(name))
    return 0


_HANDLERS = {
    "up": cmd_up,
    "get": cmd_get,
    "thaw": cmd_thaw,
    "ls": cmd_ls,
    "stat": cmd_stat,
    "profile": cmd_profile,
}


def main(argv: list[str] | None = None):
    parser = build_parser()
    if argcomplete is not None:
        argcomplete.autocomplete(parser)
    args = parser.parse_args(argv)
    try:
        config = Config.load()
        if args.cmd is None:
            from .tui.app import run_tui

            run_tui(config, _profile_arg(args), allow_deletion=args.allow_deletion)
            return 0
        return _HANDLERS[args.cmd](config, args)
    except BuckletError as exc:
        sys.stderr.write(f"{PROG}: {exc}\n")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
