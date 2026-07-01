"""Provenance is the supply-chain audit trail; its shape is part of the
public contract adopters parse from the artifact."""
import os

import pytest

from shadow.prompts import compute_prompt_provenance


@pytest.fixture(autouse=True)
def _isolate_prompt_env(monkeypatch):
    """Keep provenance offline+deterministic: clear any inline/SM prompt env
    the ambient environment may export, so _resolve_prompt never issues a
    Secrets Manager network call from these pure-hashing contract tests.
    Derived from _ACTIVE_PROMPTS so a newly-added prompt is isolated too."""
    from shadow import prompts as prompts_mod
    for _stage, env_var, _filename, _fb in prompts_mod._ACTIVE_PROMPTS:
        monkeypatch.delenv(env_var, raising=False)
        monkeypatch.delenv(f"SM_{env_var}", raising=False)


# All eight prompts the bot loads at runtime — the four surfaces are
# first-class in the audit rollup, not PR-only. Membership (not a frozen
# count/order) so the correct multi-surface fix isn't blocked by the contract,
# while a *shrink* below this set still fails loudly.
_EXPECTED_STAGES = {
    "investigator", "critic", "reporter",
    "investigator_commit", "critic_commit",
    "issue_classify", "issue_respond", "followup",
}


def test_provenance_has_rollup_and_prompts():
    p = compute_prompt_provenance()
    assert isinstance(p["rollup_sha256"], str) and len(p["rollup_sha256"]) == 64
    assert isinstance(p["prompts"], list)
    stages = {e["stage"] for e in p["prompts"]}
    assert stages == _EXPECTED_STAGES


def test_each_prompt_entry_has_required_fields():
    p = compute_prompt_provenance()
    for entry in p["prompts"]:
        assert set(entry.keys()) >= {"stage", "source", "sha256", "len", "loaded"}
        assert len(entry["sha256"]) == 64


def test_all_active_surfaces_covered():
    # Every stage in _ACTIVE_PROMPTS must appear in provenance and vice versa —
    # guards against a surface silently dropping out of the audit rollup.
    from shadow import prompts as prompts_mod
    active = {stage for stage, *_ in prompts_mod._ACTIVE_PROMPTS}
    p = compute_prompt_provenance()
    assert {e["stage"] for e in p["prompts"]} == active == _EXPECTED_STAGES


def test_issue_prompt_swap_changes_rollup(monkeypatch):
    # Tamper-evidence parity for the issue surface: overriding the issue-classify
    # prompt must change the rollup hash, just like a PR-prompt swap does.
    p1 = compute_prompt_provenance()
    monkeypatch.setenv("ISSUE_CLASSIFY_PROMPT", "tampered issue-classify content")
    p2 = compute_prompt_provenance()
    assert p1["rollup_sha256"] != p2["rollup_sha256"]
    ic = next(e for e in p2["prompts"] if e["stage"] == "issue_classify")
    assert ic["source"] == "env:ISSUE_CLASSIFY_PROMPT"


def test_rollup_changes_when_prompt_overridden(monkeypatch):
    p1 = compute_prompt_provenance()
    monkeypatch.setenv("PR_INVESTIGATOR_PROMPT", "different content")
    p2 = compute_prompt_provenance()
    assert p1["rollup_sha256"] != p2["rollup_sha256"]
    # Source field reflects the override.
    inv = next(e for e in p2["prompts"] if e["stage"] == "investigator")
    assert inv["source"] == "env:PR_INVESTIGATOR_PROMPT"


def test_rollup_stable_across_calls():
    # Idempotent — same disk + same env yield same hash.
    assert compute_prompt_provenance()["rollup_sha256"] == \
        compute_prompt_provenance()["rollup_sha256"]


def test_loaded_flag_false_when_prompt_missing(monkeypatch, tmp_path):
    # Force a missing-file path by overriding the prompts dir to empty.
    from shadow import prompts as prompts_mod
    monkeypatch.setattr(prompts_mod, "_PROMPTS_DIR", tmp_path)
    monkeypatch.delenv("PR_INVESTIGATOR_PROMPT", raising=False)
    p = compute_prompt_provenance()
    inv = next(e for e in p["prompts"] if e["stage"] == "investigator")
    assert inv["loaded"] is False
    assert inv["len"] == 0
