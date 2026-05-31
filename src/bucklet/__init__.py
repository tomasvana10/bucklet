"""bucklet: browse, upload, download and restore S3 objects from a CLI or TUI.

bucklet works with buckets in any storage class. It only offers a restore
("thaw") for objects whose live class actually needs one (GLACIER or
DEEP_ARCHIVE), so a plain bucket never shows it.

The CLI and the Textual TUI are thin front-ends over one UI-agnostic core
(:mod:`bucklet.service`). The modules underneath it are:

    bucklet.storage   storage-class vocabulary and object-state logic (pure)
    bucklet.models    the Profile, ObjectInfo and ObjectStatus dataclasses
    bucklet.rclone    reads credentials from an rclone remote
    bucklet.config    saved profiles and the default selection
    bucklet.s3        thin boto3 wrappers that raise BuckletError
    bucklet.service   the high-level operations both front-ends call
    bucklet.cli       the argparse front-end
    bucklet.tui       the Textual front-end
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
