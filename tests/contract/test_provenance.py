"""Provenance is the supply-chain audit trail; its shape is part of the
public contract adopters parse from the artifact."""
import os

import pytest

from shadow.prompts import compute_prompt_provenance


def test_provenance_has_rollup_and_prompts():
    p = compute_prompt_provenance()
    assert isinstance(p["rollup_sha256"], str) and len(p["rollup_sha256"]) == 64
    assert isinstance(p["prompts"], list) and len(p["prompts"]) == 5


def test_each_prompt_entry_has_required_fields():
    p = compute_prompt_provenance()
    for entry in p["prompts"]:
        assert set(entry.keys()) >= {"stage", "source", "sha256", "len", "loaded"}
        assert len(entry["sha256"]) == 64


def test_active_stages_present_in_order():
    p = compute_prompt_provenance()
    stages = [e["stage"] for e in p["prompts"]]
    assert stages == [
        "investigator", "critic", "reporter",
        "investigator_commit", "critic_commit",
    ]


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
