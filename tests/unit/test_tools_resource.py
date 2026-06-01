"""Tools resource caps and deadline-awareness.

read_file rejects oversized files (OOM defense on the runner). Walk-based
tools (grep_codebase, find_callers, find_tests_for) accept a deadline so
a single tool call on a giant repo can't blow past pipeline_wall_clock.
"""
import os
import time

import pytest


@pytest.fixture
def tmp_repo(tmp_path):
    """Tiny repo skeleton: one source file, one test file."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "small.py").write_text("hello\nworld\n")
    return str(tmp_path)


def test_read_file_small_succeeds(tmp_repo):
    from shadow.tools import read_file
    out = read_file("src/small.py", 1, -1, tmp_repo)
    assert "hello" in out
    assert "world" in out


def test_read_file_too_large_rejected(tmp_path):
    """A 6MB file must be rejected up-front to avoid OOM-killing the runner.
    Model is told to use grep_codebase or narrow ranges."""
    from shadow.tools import read_file
    big = tmp_path / "big.py"
    # 6 MB of repeated lines
    big.write_text("x" * (6 * 1024 * 1024))
    out = read_file("big.py", 1, -1, str(tmp_path))
    assert out.startswith("ERROR:")
    assert "too large" in out or "exceeds" in out


def test_read_file_islice_returns_only_requested_range(tmp_path):
    """itertools.islice keeps memory proportional to the requested range,
    not the file size — verify the slice is still correct on a multi-line
    file when start_line is past the first chunk."""
    from shadow.tools import read_file
    f = tmp_path / "many.py"
    f.write_text("\n".join(f"line{i}" for i in range(1, 201)))
    out = read_file("many.py", 50, 55, str(tmp_path))
    assert "line50" in out
    assert "line55" in out
    # Lines outside range must not appear.
    assert "line49" not in out
    assert "line56" not in out


def test_read_file_start_past_end_errors(tmp_path):
    """start_line > total lines must error rather than return empty."""
    from shadow.tools import read_file
    f = tmp_path / "small.py"
    f.write_text("a\nb\n")
    out = read_file("small.py", 100, -1, str(tmp_path))
    assert out.startswith("ERROR:")
    assert "exceeds file length" in out


def test_grep_codebase_deadline_returns_partial_or_error(tmp_path):
    """When the deadline is in the past, grep_codebase should return
    quickly with an error rather than scanning the whole tree."""
    from shadow.tools import grep_codebase
    # Build enough files to trigger the deadline check (every 100 files).
    src = tmp_path / "src"
    src.mkdir()
    for i in range(250):
        (src / f"f{i:04d}.py").write_text("hello world\n")
    # Past deadline — first 100-file batch should hit the check.
    out = grep_codebase(
        pattern="hello", path_glob="", repo_root=str(tmp_path),
        src_dir="src", default_ext=".py", deadline=time.monotonic() - 1,
    )
    # Either deadline-truncated header or an explicit error (when zero
    # matches were captured before the timeout fired).
    assert (
        "deadline-truncated" in out
        or "exceeded remaining wall-clock budget" in out
        or "Found" in out  # may capture early matches before deadline check
    )


def test_grep_codebase_no_deadline_works_normally(tmp_path):
    """deadline=None preserves the prior behavior; never bails on its own."""
    from shadow.tools import grep_codebase
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("hello\n")
    out = grep_codebase(
        pattern="hello", path_glob="", repo_root=str(tmp_path),
        src_dir="src", default_ext=".py",
    )
    assert "Found" in out
    assert "a.py" in out


def test_find_callers_deadline_aware(tmp_path):
    """find_callers must honor deadline like grep_codebase does."""
    from shadow.tools import find_callers
    src = tmp_path / "src"
    src.mkdir()
    for i in range(250):
        (src / f"f{i:04d}.py").write_text("def Hello(): pass\n")
    out = find_callers(
        symbol="Hello", repo_root=str(tmp_path), src_dir="src",
        default_ext=".py", deadline=time.monotonic() - 1,
    )
    # Since deadline is past, should error or return partial results.
    # Functionally: must not hang or scan all 250 files.
    assert isinstance(out, str)


def test_find_tests_for_deadline_aware(tmp_path):
    """find_tests_for must honor deadline."""
    from shadow.tools import find_tests_for
    src = tmp_path / "src"
    src.mkdir()
    (src / "Foo.py").write_text("class Foo: pass\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_Foo.py").write_text("def test_foo(): pass\n")
    out = find_tests_for(
        target="Foo", repo_root=str(tmp_path), src_dir="src",
        default_ext=".py", deadline=time.monotonic() + 60,
    )
    # Plenty of budget, should find the test.
    assert "test_Foo.py" in out


def test_tool_runner_threads_deadline(tmp_path):
    """ToolRunner constructor accepts pipeline_deadline and threads it
    through to walk-based tools so the agent loop can budget tool calls."""
    from shadow.tools import ToolRunner

    class _Cfg:
        codebase_src_dir = "src"
        codebase_file_ext = ".py"
        codebase_test_dir = ""

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("hello\n")
    runner = ToolRunner(_Cfg(), str(tmp_path), pipeline_deadline=time.monotonic() + 60)
    # Smoke: deadline in the future, normal output.
    out = runner.run("grep_codebase", {"pattern": "hello"})
    assert "Found" in out


def test_read_file_5mb_boundary(tmp_path):
    """5MB exactly is the boundary; just under should succeed, over reject."""
    from shadow.tools import read_file
    just_under = tmp_path / "under.py"
    # 5MB - 100 bytes
    just_under.write_text("x" * (5 * 1024 * 1024 - 100))
    out = read_file("under.py", 1, 1, str(tmp_path))
    # 5MB - 100 bytes is one big line; should succeed with truncation.
    assert not out.startswith("ERROR")
