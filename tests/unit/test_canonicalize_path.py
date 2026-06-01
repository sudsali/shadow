"""_canonicalize_path is the gate between Reporter-emitted file paths and
the GitHub inline-comment API. A misbehaving Reporter (or prompt-injected
investigator that managed to pipe through) emitting "/etc/passwd" or
"../../escape" must NOT result in a comment that names a path outside the
repo, since the GitHub API will reject some but the artifact (7d retention)
would still leak the attempted path. The function returns "" for any path
that escapes the repo root; downstream `_filter_and_format_inline_comments`
drops the finding entirely on empty path."""
import pytest

from shadow.main import _canonicalize_path


@pytest.mark.parametrize("evil,expected", [
    ("/etc/passwd", ""),
    ("../../etc/shadow", ""),
    ("../foo", ""),
    ("subdir/../escape", ""),
    ("C:\\Windows", ""),
    ("a/./b", "a/b"),
    ("./foo", "foo"),
    ("a/b", "a/b"),
    ("", ""),
    ("a\\b", "a/b"),  # Windows-separator → posix
])
def test_canonicalize_path(evil, expected):
    assert _canonicalize_path(evil) == expected


def test_none_returns_empty():
    assert _canonicalize_path(None) == ""


def test_int_returns_empty():
    assert _canonicalize_path(123) == ""


def test_null_byte_rejected():
    """Embedded NUL must reject — some filesystems treat the path as
    truncated at the NUL ("safe.py\\x00/etc/passwd" → "safe.py" on the
    underlying syscall). Don't let the canonical form mismatch what
    actually opens."""
    assert _canonicalize_path("safe.py\x00/etc/passwd") == ""


def test_dict_returns_empty():
    """Defensive: a Reporter could conceivably emit `file: {nested: ...}`
    on a malformed output. Don't crash; return ""."""
    assert _canonicalize_path({"path": "x"}) == ""


def test_list_returns_empty():
    assert _canonicalize_path(["x", "y"]) == ""


def test_bool_returns_empty():
    """`bool` is a subclass of `int`, so `isinstance(True, str)` is False.
    Locks the contract that True/False go to ""."""
    assert _canonicalize_path(True) == ""
    assert _canonicalize_path(False) == ""


def test_double_dot_alone_rejected():
    assert _canonicalize_path("..") == ""


def test_dot_alone_returns_empty():
    """`.` normalizes to "." → drop (no file)."""
    assert _canonicalize_path(".") == ""


def test_nested_traversal_rejected():
    """Reporter emits `a/b/../../../../etc/passwd` — normpath collapses
    to "../../etc/passwd". Must reject."""
    assert _canonicalize_path("a/b/../../../../etc/passwd") == ""


def test_safe_nested_path_passes():
    """Sanity check the happy path so the rejection logic doesn't
    accidentally drop legitimate deep paths."""
    assert _canonicalize_path("src/scripts/shadow/main.py") == "src/scripts/shadow/main.py"
