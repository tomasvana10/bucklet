"""Command-line front-end.

Every subcommand operates on one profile (``--profile NAME``, accepted before
or after the subcommand; falls back to the configured default). Running archy
with no subcommand launches the Textual TUI. The CLI is a complete superset of
the TUI: anything you can do interactively you can script here.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import storage
from .config import Config
from .errors import ArchyError
from .formatting import fmt_date, human
from .models import Profile
from .service import Service

PROG = "archy"


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    # Shared --profile, added to the top parser and every subparser so it works
    # in either position. SUPPRESS keeps a subparser default from clobbering a
    # value given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--profile",
        metavar="NAME",
        default=argparse.SUPPRESS,
        help="profile to use (a saved name, or a raw bucket name); "
        "defaults to the configured default profile",
    )

    class_help = "storage class (e.g. %s)" % ", ".join(
        c.lower() for c in storage.STORAGE_CLASSES
    )

    p = argparse.ArgumentParser(
        prog=PROG,
        parents=[common],
        description="Browse, upload, download and restore objects across S3 buckets "
        "of any storage class — from the CLI or the Textual TUI.",
    )
    sub = p.add_subparsers(dest="cmd")

    up = sub.add_parser("up", parents=[common], help="upload files/dirs (mirrors absolute path)")
    up.add_argument("paths", nargs="+")
    up.add_argument("-c", "--class", dest="storage_class", metavar="CLASS", help=class_help)
    up.add_argument("--prefix", default="", help="key prefix to store objects under")

    get = sub.add_parser("get", parents=[common], help="download objects (globs allowed)")
    get.add_argument("keys", nargs="+")
    get.add_argument("-o", "--outdir", default=".", help="output directory (default .)")

    thaw = sub.add_parser("thaw", parents=[common], help="restore archived objects (globs allowed)")
    thaw.add_argument("keys", nargs="+")
    thaw.add_argument(
        "--tier",
        choices=["Bulk", "Standard", "Expedited"],
        default="Bulk",
        help="restore tier (default Bulk, ~48h, cheapest)",
    )
    thaw.add_argument(
        "--standard", action="store_true", help="shortcut for --tier Standard (~12h)"
    )
    thaw.add_argument("--days", type=int, default=7, help="days to keep the restored copy (default 7)")

    ls = sub.add_parser("ls", parents=[common], help="list objects")
    ls.add_argument("prefix", nargs="?", default="")
    ls.add_argument("-l", "--long", action="store_true", help="long format with class + state")
    ls.add_argument("--search", metavar="TERM", help="only keys containing TERM")
    ls.add_argument(
        "--state",
        choices=[storage.AVAILABLE, storage.COLD, storage.THAWING, storage.THAWED],
        help="only objects in this state (HEADs archived objects to refine)",
    )

    stat = sub.add_parser("stat", parents=[common], help="show detailed status of objects (globs allowed)")
    stat.add_argument("keys", nargs="+")

    _build_profile_parser(sub, common, class_help)
    return p


def _build_profile_parser(sub, common, class_help) -> None:
    pf = sub.add_parser("profile", parents=[common], help="manage saved profiles")
    ps = pf.add_subparsers(dest="pcmd")

    add = ps.add_parser("add", parents=[common], help="add or overwrite a profile")
    add.add_argument("name")
    add.add_argument("--bucket", required=True)
    add.add_argument("--region")
    add.add_argument("-c", "--class", dest="storage_class", metavar="CLASS", help="default upload " + class_help)
    add.add_argument("--access-key", dest="access_key_id")
    add.add_argument("--secret", dest="secret_access_key")
    add.add_argument("--rclone-remote", dest="rclone_remote", help="rclone remote to read credentials from")
    add.add_argument("--endpoint-url", dest="endpoint_url", help="custom S3 endpoint (for S3-compatible storage)")
    add.add_argument("--default", action="store_true", help="make this the default profile")

    ps.add_parser("ls", parents=[common], help="list saved profiles")

    rm = ps.add_parser("rm", parents=[common], help="remove a profile")
    rm.add_argument("name")

    dflt = ps.add_parser("default", parents=[common], help="set the default profile")
    dflt.add_argument("name")

    show = ps.add_parser("show", parents=[common], help="show a resolved profile")
    show.add_argument("name", nargs="?")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _profile_arg(args) -> str | None:
    return getattr(args, "profile", None)


def _open_service(config: Config, args, *, validate: bool = True) -> Service:
    profile = config.resolve(_profile_arg(args))
    if profile is None or not profile.bucket:
        raise ArchyError(
            "no profile configured — add one with "
            "'archy profile add NAME --bucket BUCKET ...', or pass --profile."
        )
    return Service.open(profile, validate=validate)


class _Progress:
    """Tiny single-line percentage reporter for one transfer (to stderr)."""

    def __init__(self, label: str, size: int):
        self.label = label
        self.size = max(size, 1)
        self.sent = 0

    def __call__(self, n: int) -> None:
        self.sent += n
        pct = min(100, self.sent * 100 // self.size)
        sys.stderr.write(f"\r    {self.label} {pct:3d}%")
        sys.stderr.flush()

    def done(self, message: str) -> None:
        sys.stderr.write(f"\r    {message}{' ' * 8}\n")
        sys.stderr.flush()


# --------------------------------------------------------------------------- #
# command handlers
# --------------------------------------------------------------------------- #
def cmd_up(config: Config, args) -> int:
    service = _open_service(config, args)
    plan = service.plan_upload(args.paths, prefix=args.prefix)
    if not plan:
        print("nothing to upload.")
        return 0
    cls = service.resolve_storage_class(args.storage_class)
    rc = 0
    for i, (local, key) in enumerate(plan, 1):
        size = local.stat().st_size
        print(f"[{i}/{len(plan)}] {local} ({human(size)}) -> {key} [{cls.lower()}]")
        progress = _Progress("up", size)
        try:
            service.upload(local, key, storage_class=args.storage_class, progress=progress)
            progress.done("done")
        except ArchyError as exc:
            progress.done(f"error: {exc}")
            rc = 1
    return rc


def cmd_get(config: Config, args) -> int:
    service = _open_service(config, args)
    resolution = service.resolve_keys(args.keys)
    for miss in resolution.missing:
        sys.stderr.write(f"no match: {miss}\n")
    if not resolution.matched:
        raise ArchyError("no matching objects.")
    outdir = Path(args.outdir)
    rc = 0
    for key in resolution.matched:
        dest = outdir / key
        try:
            service.download(key, dest)
            print(f"ok   {key} -> {dest}")
        except ArchyError as exc:
            print(f"ERR  {key} — {exc}")
            rc = 1
    return rc


def cmd_thaw(config: Config, args) -> int:
    service = _open_service(config, args)
    tier = "Standard" if args.standard else args.tier
    resolution = service.resolve_keys(args.keys)
    for miss in resolution.missing:
        sys.stderr.write(f"no match: {miss}\n")
    if not resolution.matched:
        raise ArchyError("no matching objects.")
    rc = 0
    for key in resolution.matched:
        try:
            message = service.restore(key, tier=tier, days=args.days)
            print(f"ok   {key} — {message}")
        except ArchyError as exc:
            print(f"ERR  {key} — {exc}")
            rc = 1
    return rc


def cmd_ls(config: Config, args) -> int:
    service = _open_service(config, args)
    objects = service.list_objects(args.prefix or "")
    if args.search:
        term = args.search.lower()
        objects = [o for o in objects if term in o.key.lower()]

    states: dict[str, str] = {}
    if args.state:
        # Resolve states: a listing never carries the Restore header, so any
        # object that could be archived/restoring needs a HEAD; the rest are
        # known to be 'available' straight from the listing.
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


def cmd_stat(config: Config, args) -> int:
    service = _open_service(config, args)
    resolution = service.resolve_keys(args.keys)
    for miss in resolution.missing:
        sys.stderr.write(f"no match: {miss}\n")
    if not resolution.matched:
        raise ArchyError("no matching objects.")
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
            print("  note     : archived — run 'archy thaw' before downloading")
    return 0


def cmd_profile(config: Config, args) -> int:
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
        print(f"default -> {args.name}")
        return 0
    if pcmd == "show":
        return _profile_show(config, args)
    raise ArchyError("usage: archy profile {add|ls|rm|default|show} ...")


def _profile_add(config: Config, args) -> int:
    cls = storage.DEFAULT_STORAGE_CLASS
    if args.storage_class:
        cls = storage.normalize_storage_class(args.storage_class)
    profile = Profile(
        name=args.name,
        bucket=args.bucket,
        region=args.region,
        access_key_id=args.access_key_id,
        secret_access_key=args.secret_access_key,
        rclone_remote=args.rclone_remote,
        endpoint_url=args.endpoint_url,
        storage_class=cls,
    )
    config.add(profile, make_default=args.default)
    config.save()
    tag = " (default)" if config.default == profile.name else ""
    print(f"saved profile '{profile.name}'{tag}")
    return 0


def _profile_ls(config: Config) -> int:
    if not config.profiles:
        print("no profiles. Add one:  archy profile add NAME --bucket BUCKET [--class CLASS] ...")
        return 0
    for name in config.names():
        prof = config.get(name)
        marker = "*" if config.default == name else " "
        print(
            f"{marker} {name:<16} {(prof.bucket or '?'):<40} "
            f"{(prof.region or '?'):<15} {prof.storage_class.lower():<14} [{prof.credential_source}]"
        )
    return 0


def _profile_show(config: Config, args) -> int:
    profile = config.resolve(args.name)
    if profile is None:
        raise ArchyError("no such profile (and no default set)")
    archival = storage.needs_restore(profile.storage_class)
    print(f"profile  : {profile.name}")
    print(f"bucket   : {profile.bucket or '?'}")
    print(f"region   : {profile.region or '(default)'}")
    print(f"class    : {profile.storage_class}" + ("  [uploads are archived — need thaw]" if archival else ""))
    if profile.endpoint_url:
        print(f"endpoint : {profile.endpoint_url}")
    print(f"creds    : {profile.credential_source}")
    if profile.has_explicit_keys:
        print(f"key id   : {profile.access_key_id}")
        print("secret   : ****")
    return 0


_HANDLERS = {
    "up": cmd_up,
    "get": cmd_get,
    "thaw": cmd_thaw,
    "ls": cmd_ls,
    "stat": cmd_stat,
    "profile": cmd_profile,
}


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = Config.load()
        if args.cmd is None:
            from .tui.app import run_tui

            run_tui(config, _profile_arg(args))
            return 0
        return _HANDLERS[args.cmd](config, args)
    except ArchyError as exc:
        sys.stderr.write(f"{PROG}: {exc}\n")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
