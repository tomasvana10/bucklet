#!/usr/bin/env python3
"""One-off: move an old deeparch/archy config over to bucklet.

bucklet used to be called archy, and before that, deeparch. The package no
longer migrates old configs on startup, so run this once to bring saved profiles
across:

    uv run python migrate_config.py

It reads the first of these that exists:

    ~/.config/archy/config.json
    ~/.config/deeparch/config.json

and writes it to bucklet's config location, without overwriting an existing
bucklet config. Profiles coming from deeparch (which only ever uploaded to
DEEP_ARCHIVE) keep that as their default upload class. Delete this script once
you've run it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from bucklet.config import default_config_dir


def main() -> int:
    target = default_config_dir() / "config.json"
    if target.exists():
        print(f"bucklet config already exists at {target}; nothing to do.")
        return 0

    candidates = [
        (Path.home() / ".config" / "archy" / "config.json", False),
        (Path.home() / ".config" / "deeparch" / "config.json", True),
    ]
    source, from_deeparch = next(((p, d) for p, d in candidates if p.exists()), (None, False))
    if source is None:
        print("no old archy or deeparch config found; nothing to migrate.")
        return 0

    data = json.loads(source.read_text())
    profiles = data.get("profiles") or {}
    if from_deeparch:
        for stored in profiles.values():
            stored.setdefault("storage_class", "DEEP_ARCHIVE")

    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"default": data.get("default"), "profiles": profiles}, indent=2)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(payload)

    print(f"migrated {len(profiles)} profile(s) from {source} -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
