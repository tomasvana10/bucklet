"""Tests for the pure key->folder-tree builder (no UI, no AWS)."""

from bucklet.tree import build_key_tree, leaf_name


def _dirs(node):
    return {d.name: d for d in node.dirs}


def _files(node):
    return [f.name for f in node.files]


def test_single_chain_collapses_like_github():
    # x/y/z/file.txt -> one folder "x/y/z" holding file.txt, not three nested.
    root = build_key_tree(["x/y/z/file.txt"])
    assert [d.name for d in root.dirs] == ["x/y/z"]
    folder = root.dirs[0]
    assert folder.path == "x/y/z"
    assert _files(folder) == ["file.txt"]
    assert folder.dirs == []


def test_chain_stops_collapsing_at_a_branch():
    # x has two children (the chain y/z and the file a.txt), so x stays its own
    # node; only y/z below it collapses.
    root = build_key_tree(["x/y/z/file.txt", "x/a.txt"])
    assert [d.name for d in root.dirs] == ["x"]
    x = root.dirs[0]
    assert _files(x) == ["a.txt"]
    assert [d.name for d in x.dirs] == ["y/z"]
    assert _files(x.dirs[0]) == ["file.txt"]


def test_top_level_single_folder_is_not_swallowed():
    # The root is never collapsed: a lone top-level folder still shows.
    root = build_key_tree(["only/a.txt"])
    assert [d.name for d in root.dirs] == ["only"]
    assert _files(root.dirs[0]) == ["a.txt"]


def test_files_and_dirs_are_sorted():
    root = build_key_tree(["b/2.txt", "b/1.txt", "a.txt"])
    assert _files(root) == ["a.txt"]  # top-level file
    assert [d.name for d in root.dirs] == ["b"]
    assert _files(root.dirs[0]) == ["1.txt", "2.txt"]


def test_each_file_carries_its_full_key():
    root = build_key_tree(["docs/2024/report.txt"])
    leaf = root.dirs[0].files[0]
    assert leaf.key == "docs/2024/report.txt"
    assert leaf.name == "report.txt"


def test_folder_marker_key_is_a_leaf():
    # A zero-byte "folder marker" object (key ends in '/') stays selectable.
    root = build_key_tree(["a/b/"])
    a = root.dirs[0]
    assert a.name == "a"
    assert a.files[0].name == "b/"
    assert a.files[0].key == "a/b/"


def test_empty_and_degenerate_keys():
    assert build_key_tree([]).dirs == []
    # "" and "/" have no place in the tree and are skipped
    root = build_key_tree(["", "/", "real.txt"])
    assert _files(root) == ["real.txt"]


def test_leaf_name_matches_builder():
    assert leaf_name("a/b/c.txt") == "c.txt"
    assert leaf_name("c.txt") == "c.txt"
    assert leaf_name("a/b/") == "b/"
    assert leaf_name("top/") == "top/"
