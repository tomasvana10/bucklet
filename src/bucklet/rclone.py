"""Read S3 credentials out of an rclone remote.

This lets a profile reuse an rclone ``s3`` remote you already configured
instead of re-typing keys. Encrypted or unreadable configs simply yield
``None`` so the caller can fall back to the AWS chain.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path


def default_rclone_conf() -> Path:
    """Where rclone keeps its config, honouring ``$RCLONE_CONFIG``."""
    return Path(
        os.environ.get("RCLONE_CONFIG", str(Path.home() / ".config" / "rclone" / "rclone.conf"))
    )


# rclone key -> the Profile field it maps to.
_FIELDS = {
    "access_key_id": "access_key_id",
    "secret_access_key": "secret_access_key",
    "region": "region",
    "endpoint": "endpoint_url",
}


def creds_from_rclone(remote: str | None, conf_path: Path | None = None) -> dict | None:
    """Return a dict of profile fields parsed from an rclone remote, or None.

    Only keys present in the remote are returned, so the caller can merge this
    over explicit profile values without clobbering them with ``None``.
    """
    if not remote:
        return None
    path = conf_path or default_rclone_conf()
    if not Path(path).exists():
        return None
    cp = configparser.ConfigParser()
    try:
        cp.read(path)
    except (configparser.Error, OSError, UnicodeDecodeError):
        return None  # e.g. an encrypted rclone.conf
    if remote not in cp:
        return None
    section = cp[remote]
    out: dict[str, str] = {}
    for rclone_key, field in _FIELDS.items():
        value = section.get(rclone_key)
        if value:
            out[field] = value
    return out or None
