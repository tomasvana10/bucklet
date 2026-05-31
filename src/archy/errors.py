"""Error type shared across archy.

:class:`ArchyError` is raised for every *expected* failure (bad input, missing
bucket, denied access, …). Front-ends catch it and show ``str(exc)`` without a
traceback; anything else is a real bug and is allowed to propagate.
"""

from __future__ import annotations


class ArchyError(Exception):
    """A user-facing error. Its message is shown verbatim, no traceback."""
