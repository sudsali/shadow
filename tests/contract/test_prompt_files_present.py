"""The agentic pipeline fails closed if any of these prompt files goes missing.

Catching it at test-time beats catching it at production-time when the
workflow's only signal is `prompt_load_failed: investigator,critic,reporter`.
"""
from pathlib import Path

PROMPTS = Path(__file__).resolve().parents[2] / "prompts"

EXPECTED = (
    "pr-investigator.txt",
    "pr-investigator-commit.txt",
    "pr-critic.txt",
    "pr-critic-commit.txt",
    "pr-reporter.txt",
)


def test_all_prompt_files_present():
    missing = [name for name in EXPECTED if not (PROMPTS / name).exists()]
    assert not missing, f"missing prompt files: {missing}"


def test_prompt_files_are_non_empty():
    for name in EXPECTED:
        body = (PROMPTS / name).read_text()
        assert body.strip(), f"{name} is empty"


def test_no_scala_specific_path_glob_remaining():
    # A hardcoded path_glob=".scala" silently filters out findings in
    # adopter repos that aren't Scala; pin against that regression.
    inv = (PROMPTS / "pr-investigator.txt").read_text()
    crit = (PROMPTS / "pr-critic.txt").read_text()
    assert 'path_glob=".scala"' not in inv
    assert 'path_glob=".scala"' not in crit
