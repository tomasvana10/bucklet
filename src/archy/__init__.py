"""archy — browse, upload, download and restore objects across S3 buckets.

archy works with buckets of *any* storage class. Restore ("thaw") is offered
only for objects whose live storage class actually requires it
(GLACIER / DEEP_ARCHIVE), so a plain bucket simply never shows it.

The public surface is split so that the CLI and the Textual TUI are two thin
front-ends over the same UI-agnostic core (:mod:`archy.service`):

    archy.storage   storage-class vocabulary + object-state logic (pure)
    archy.models    Profile / ObjectInfo / ObjectStatus dataclasses
    archy.rclone    read credentials out of an rclone remote
    archy.config    saved profiles, default selection, migration
    archy.s3        thin boto3 wrappers (errors -> ArchyError)
    archy.service   high-level operations used by every front-end
    archy.cli       argparse front-end
    archy.tui       Textual front-end
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
