"""The one error type bucklet raises.

Raise :class:`BuckletError` for any failure a user can act on: bad input, a
missing bucket, denied access, and so on. Front-ends catch it and print
``str(exc)`` with no traceback. Anything else is a bug, so we let it propagate.
"""

from __future__ import annotations


class BuckletError(Exception):
    """A user-facing error. Its message is shown as-is, with no traceback."""
