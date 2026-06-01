"""Build a folder tree from S3 keys, the way a file browser does.

This is the pure backing for the TUI's tree view: it has no boto3 and no UI, so
the fiddly bit — collapsing single-child directory chains — is unit-tested on
its own. Given keys like ``x/y/z/file.txt`` it produces one folder node labelled
``x/y/z`` containing ``file.txt``, instead of three nested folders each holding
only the next, exactly like GitHub's tree or VS Code's "compact folders". The
moment a directory holds more than one thing the chain stops collapsing, so the
branch point stays visible.

Keys are split on ``/``. A key that ends in ``/`` (a zero-byte "folder marker"
some tools create) is kept as a selectable leaf under its parent, named with the
trailing slash, so it can still be inspected or renamed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass
class FileLeaf:
    """A file in the tree: its display name (last path segment) and full key."""

    name: str
    key: str


@dataclass
class DirNode:
    """A folder. ``name`` is what to show (possibly a collapsed ``a/b/c``);
    ``path`` is the real key prefix it stands for. The root has both empty."""

    name: str
    path: str
    dirs: list[DirNode] = field(default_factory=list)
    files: list[FileLeaf] = field(default_factory=list)


def leaf_name(key: str) -> str:
    """The display name of the file at ``key``: its last path segment.

    A trailing slash (a folder-marker object) is kept so the leaf still reads
    as one. Matches how :func:`build_key_tree` names its leaves, so the TUI can
    relabel a single leaf in place without rebuilding the tree.
    """
    stripped = key.rstrip("/")
    base = stripped.rsplit("/", 1)[-1] if "/" in stripped else stripped
    return f"{base}/" if key.endswith("/") else base


class _Raw:
    """A mutable trie node used only while building, before compression."""

    __slots__ = ("dirs", "files")

    def __init__(self):
        self.dirs: dict[str, _Raw] = {}
        self.files: list[tuple[str, str]] = []  # (display name, full key)


def build_key_tree(keys: Iterable[str]) -> DirNode:
    """Turn a flat iterable of keys into a compressed :class:`DirNode` tree.

    Folders and files are each sorted by name. Single-child directory chains
    are collapsed (see the module docstring); the root itself is never
    collapsed, so a lone top-level folder still shows.
    """
    root = _Raw()
    for key in keys:
        segments = [s for s in key.split("/") if s]
        if not segments:
            continue  # "" or "/"-only keys have no place in the tree
        dir_segments = segments[:-1]
        node = root
        for seg in dir_segments:
            node = node.dirs.setdefault(seg, _Raw())
        node.files.append((leaf_name(key), key))
    return _build(root, name="", path="")


def _build(raw: _Raw, name: str, path: str) -> DirNode:
    """Convert a raw trie node to a :class:`DirNode`, compressing chains.

    Children are built (and so compressed) first, bottom-up. A non-root
    directory with no files and exactly one subdirectory is then folded into
    that subdirectory, joining their names with ``/`` and keeping the deeper
    prefix as the path. Because each child is already maximally compressed, one
    fold per level is enough.
    """
    dirs = [
        _build(raw.dirs[cname], cname, f"{path}/{cname}" if path else cname)
        for cname in sorted(raw.dirs)
    ]
    files = sorted((FileLeaf(n, k) for n, k in raw.files), key=lambda f: f.name)
    node = DirNode(name=name, path=path, dirs=dirs, files=files)
    while node.name and not node.files and len(node.dirs) == 1:
        child = node.dirs[0]
        node = DirNode(
            name=f"{node.name}/{child.name}",
            path=child.path,
            dirs=child.dirs,
            files=child.files,
        )
    return node
