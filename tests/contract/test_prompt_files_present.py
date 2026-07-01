"""Every prompt the bot loads fails closed if its file goes missing.

Catching it at test-time beats catching it at production-time when the
workflow's only signal is `prompt_load_failed: ...`. This covers ALL four
surfaces — PR review AND issue triage / issue-respond / followup — because
all eight prompts are customer-visible and fail closed identically.

EXPECTED is derived from prompts._ACTIVE_PROMPTS (the single source of truth
the engine, provenance, and doctor all iterate) so the PR and issue/followup
sets cannot drift apart: adding a prompt to the engine automatically requires
its file here.
"""
from pathlib import Path

from shadow import prompts as prompts_mod

PROMPTS = Path(__file__).resolve().parents[2] / "prompts"

# One source of truth: the filenames the engine actually loads at runtime.
EXPECTED = tuple(filename for _stage, _env, filename, _fb in prompts_mod._ACTIVE_PROMPTS)


def test_expected_covers_all_surfaces():
    # Guard the derivation itself: all three issue/followup prompts and all
    # five PR prompts must be present, so a regression that dropped them from
    # _ACTIVE_PROMPTS (shrinking the guarded set) fails loudly here.
    assert set(EXPECTED) >= {
        "pr-investigator.txt", "pr-investigator-commit.txt", "pr-critic.txt",
        "pr-critic-commit.txt", "pr-reporter.txt",
        "issue-classify.txt", "issue-respond.txt", "followup.txt",
    }


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
