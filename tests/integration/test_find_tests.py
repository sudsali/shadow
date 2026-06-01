"""find_tests_for + _infer_test_dir against real filesystem fixtures.

Each language's convention is captured separately so a regression in
multi-language support shows up as a specific failure rather than a vague
"no tests found" report.
"""
import os
import tempfile
from pathlib import Path

from shadow.tools import _infer_test_dir, find_tests_for


def test_infer_configured_dir_wins():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "my-tests"))
        result = _infer_test_dir(tmp, "src", configured_test_dir="my-tests")
        assert result == os.path.realpath(os.path.join(tmp, "my-tests"))


def test_infer_maven_layout():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "src/test/java"))
        result = _infer_test_dir(tmp, "src/main/java")
        assert result == os.path.realpath(os.path.join(tmp, "src/test/java"))


def test_infer_python_tests_layout():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "tests"))
        result = _infer_test_dir(tmp, "src")
        assert result == os.path.realpath(os.path.join(tmp, "tests"))


def test_infer_returns_none_when_nothing_exists():
    with tempfile.TemporaryDirectory() as tmp:
        assert _infer_test_dir(tmp, "src") is None


def test_infer_rejects_absolute_configured_path():
    # /etc resolves outside the repo root → falls through to defaults.
    with tempfile.TemporaryDirectory() as tmp:
        assert _infer_test_dir(tmp, "src", configured_test_dir="/etc") is None


def test_python_test_prefix_convention():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "tests"))
        Path(tmp, "tests", "test_foo.py").write_text("x = 1\n")
        out = find_tests_for("foo.py", tmp, "src", ".py")
        assert "tests/test_foo.py" in out


def test_go_underscore_test_convention():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "tests"))
        Path(tmp, "tests", "foo_test.go").write_text("package foo\n")
        out = find_tests_for("foo.go", tmp, "src", ".go")
        assert "tests/foo_test.go" in out


def test_java_pascal_convention():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "src/test/java"))
        Path(tmp, "src/test/java", "MyClassTest.java").write_text("class\n")
        out = find_tests_for("MyClass.java", tmp, "src/main/java", ".java")
        assert "MyClassTest.java" in out


def test_scala_spec_convention():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "src/test/scala"))
        Path(tmp, "src/test/scala", "FooSpec.scala").write_text("class\n")
        out = find_tests_for("Foo.scala", tmp, "src/main/scala", ".scala")
        assert "FooSpec.scala" in out


def test_no_matches_returns_message():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "tests"))
        out = find_tests_for("Nothing.py", tmp, "src", ".py")
        assert "No tests found" in out


def test_empty_default_ext_does_not_crash():
    # Empty default_ext: `name[:-0]` is "" (Python slice quirk); the
    # identifier-shape check must not strip when ext is empty.
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "tests"))
        out = find_tests_for("Foo", tmp, "src", "")
        assert ("No tests found" in out) or ("Tests for" in out)
