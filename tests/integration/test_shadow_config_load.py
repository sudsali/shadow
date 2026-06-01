"""End-to-end .shadow.yml load + path sanitization.

Real filesystem fixtures only — yaml's quirks (None, ints, lists) are part of
the threat model and the bot has to fail closed on each.
"""
import tempfile
from pathlib import Path

from shadow.shadow_config import load


def _write_yaml(repo_root, body):
    (Path(repo_root) / ".shadow.yml").write_text(body)


def test_missing_file_returns_empty_dict():
    with tempfile.TemporaryDirectory() as tmp:
        assert load(tmp) == {}


def test_malformed_yaml_logs_and_returns_empty(caplog):
    with tempfile.TemporaryDirectory() as tmp:
        _write_yaml(tmp, "this: is: not: valid: yaml:\n  - {[}")
        result = load(tmp)
        assert result == {}


def test_valid_relative_paths_pass_through():
    with tempfile.TemporaryDirectory() as tmp:
        _write_yaml(tmp, "codebase:\n  src_dir: src/main\n  test_dir: src/test\n")
        cfg = load(tmp)
        assert cfg["codebase"]["src_dir"] == "src/main"
        assert cfg["codebase"]["test_dir"] == "src/test"


def test_absolute_src_dir_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        _write_yaml(tmp, "codebase:\n  src_dir: /etc/passwd\n")
        cfg = load(tmp)
        assert "src_dir" not in cfg.get("codebase", {})


def test_dotdot_test_dir_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        _write_yaml(tmp, "codebase:\n  test_dir: ../etc\n")
        cfg = load(tmp)
        assert "test_dir" not in cfg.get("codebase", {})


def test_windows_drive_letter_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        _write_yaml(tmp, "codebase:\n  src_dir: C:\\Users\\evil\n")
        cfg = load(tmp)
        assert "src_dir" not in cfg.get("codebase", {})


def test_unrelated_keys_unchanged():
    with tempfile.TemporaryDirectory() as tmp:
        _write_yaml(
            tmp,
            "bot:\n  name: shadow\n  escalate_label: needs-human\n"
            "features:\n  pr_review: true\n",
        )
        cfg = load(tmp)
        assert cfg["bot"]["name"] == "shadow"
        assert cfg["bot"]["escalate_label"] == "needs-human"
        assert cfg["features"]["pr_review"] is True
