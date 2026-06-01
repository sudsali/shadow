"""Artifact integrity stamp binds the artifact to (repo, run_id, pr_number).
Replay or tamper between analyze and act jobs is detected. RUN_ATTEMPT is
intentionally excluded so `gh run rerun --failed-only` (act-only retry)
still verifies against the artifact analyze stamped at attempt N."""
import pytest


@pytest.fixture(autouse=True)
def _run_context(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "sudsali/shadow")
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("ISSUE_NUMBER", "42")
    yield


def _import():
    import importlib
    from shadow import main as shadow_main
    importlib.reload(shadow_main)
    return shadow_main


def test_stamp_then_verify_in_same_run_passes():
    m = _import()
    data = {"action": "RESPOND", "response": "hello"}
    m._stamp_artifact(data)
    ok, reason = m._verify_artifact_integrity(data)
    assert ok, reason


def test_tampered_body_detected():
    m = _import()
    data = {"action": "RESPOND", "response": "hello"}
    m._stamp_artifact(data)
    data["response"] = "evil"
    ok, reason = m._verify_artifact_integrity(data)
    assert not ok
    assert "modified" in reason


def test_replay_from_different_run_detected(monkeypatch):
    m = _import()
    data = {"action": "RESPOND", "response": "hello"}
    m._stamp_artifact(data)
    monkeypatch.setenv("GITHUB_RUN_ID", "99999")
    ok, reason = m._verify_artifact_integrity(data)
    assert not ok
    assert "different workflow_run" in reason or "replay" in reason


def test_unstamped_legacy_artifact_rejected():
    m = _import()
    data = {"action": "RESPOND", "response": "hello"}
    ok, reason = m._verify_artifact_integrity(data)
    assert not ok
    assert "_integrity" in reason or "pre-dates" in reason


def test_non_dict_rejected():
    m = _import()
    for val in ("garbage", None, 42, [1, 2, 3]):
        ok, _ = m._verify_artifact_integrity(val)
        assert not ok


def test_stamp_block_shape():
    m = _import()
    data = {"action": "RESPOND"}
    m._stamp_artifact(data)
    stamp = data["_integrity"]
    assert stamp["schema_version"] == 1
    assert len(stamp["body_sha256"]) == 64
    assert len(stamp["run_binding_sha256"]) == 64
    assert len(stamp["stamp"]) == 64


def test_run_binding_changes_with_pr_number(monkeypatch):
    m = _import()
    binding_a = m._compute_run_binding()
    monkeypatch.setenv("ISSUE_NUMBER", "999")
    binding_b = m._compute_run_binding()
    assert binding_a != binding_b


def test_run_attempt_does_not_affect_binding(monkeypatch):
    """gh run rerun --failed-only bumps RUN_ATTEMPT but leaves the artifact
    from analyze attempt N. Binding must be RUN_ATTEMPT-independent so
    act-only retries still verify."""
    m = _import()
    binding_a = m._compute_run_binding()
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "99")
    binding_b = m._compute_run_binding()
    assert binding_a == binding_b


def test_run_binding_survives_analyze_to_act_handoff(monkeypatch):
    """analyze and act run as separate workflow jobs — GITHUB_JOB differs
    between them. Including GITHUB_JOB in the binding would make act
    reject every artifact analyze stamps. The binding must be stable
    across the two jobs of one run."""
    m = _import()
    monkeypatch.setenv("GITHUB_JOB", "analyze")
    data = {"action": "RESPOND", "response": "hello"}
    m._stamp_artifact(data)
    monkeypatch.setenv("GITHUB_JOB", "act")
    ok, reason = m._verify_artifact_integrity(data)
    assert ok, reason


def test_double_stamp_is_idempotent_on_payload():
    """Re-stamping a dict that already carries _integrity must produce a
    verifiable artifact — _stamp_artifact strips the prior block before
    hashing so sign and verify stay symmetric."""
    m = _import()
    data = {"action": "RESPOND", "response": "hello"}
    m._stamp_artifact(data)
    first = data["_integrity"]["stamp"]
    m._stamp_artifact(data)
    second = data["_integrity"]["stamp"]
    assert first == second
    ok, _ = m._verify_artifact_integrity(data)
    assert ok


def test_verify_handles_unserializable_payload_gracefully():
    """A future caller that injects a non-JSON-serializable value must not
    crash _verify; instead, fail-closed with a reason."""
    m = _import()
    data = {"action": "RESPOND", "_integrity": {
        "schema_version": 1,
        "body_sha256": "0" * 64,
        "run_binding_sha256": "0" * 64,
        "stamp": "0" * 64,
    }}
    # set() is not JSON-serializable
    data["bad_field"] = {1, 2, 3}
    ok, reason = m._verify_artifact_integrity(data)
    assert not ok
    assert "unserializable" in reason or "TypeError" in reason
