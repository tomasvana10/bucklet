""" "Manage S3 objects in any storage class."

The CLI and the Textual TUI are thin frontends over one UI-agnostic core
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
