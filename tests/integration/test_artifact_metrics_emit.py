"""Every _write_artifact call now emits CloudWatch — including SKIP /
ESCALATE paths outside the agent pipeline. Validates that the issue path,
author_is_bot, and prompt_load_failed flows all reach CloudWatch's
emit_metrics so operator dashboards undercount no flow."""
import importlib
import json
import os
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _run_context(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_REPOSITORY", "sudsali/shadow")
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("ISSUE_NUMBER", "42")
    artifact = tmp_path / "bot_result.json"
    monkeypatch.setenv("ARTIFACT_PATH", str(artifact))
    yield


def _import():
    from shadow import main as shadow_main
    importlib.reload(shadow_main)
    return shadow_main


def test_skip_artifact_emits_cloudwatch():
    """A SKIP write (e.g. author_is_bot) must reach the CloudWatch emitter
    so Invocations counts every run."""
    m = _import()
    with mock.patch.object(m, "_emit_cloudwatch_for_artifact") as emit:
        m._write_artifact({"action": "SKIP", "reason": "author_is_bot"})
    emit.assert_called_once()
    args, _ = emit.call_args
    assert args[0]["action"] == "SKIP"
    assert args[0]["reason"] == "author_is_bot"


def test_escalate_artifact_emits_cloudwatch():
    """ESCALATE writes from non-pipeline paths must also emit so operators
    see all escalations on the dashboard, not just agent-pipeline ones."""
    m = _import()
    with mock.patch.object(m, "_emit_cloudwatch_for_artifact") as emit:
        m._write_artifact({
            "action": "ESCALATE", "reason": "prompt_load_failed",
            "labels": [], "response": "",
        })
    emit.assert_called_once()
    args, _ = emit.call_args
    assert args[0]["reason"] == "prompt_load_failed"


def test_emit_cloudwatch_for_artifact_uses_reason_dim(monkeypatch):
    """The reason from the artifact propagates to the Reason dimension on
    every CloudWatch metric so dashboards group by cause."""
    m = _import()
    captured = {}

    def fake_emit(*, repository, metrics, action, pipeline, reason):
        captured["reason"] = reason
        captured["action"] = action
        return True, "ok"

    # Patch the actual cloudwatch module's emit_metrics; mocking via
    # sys.modules doesn't work once shadow.cloudwatch has already been
    # imported (the parent package's namespace caches the resolved module).
    from shadow import cloudwatch as cw
    with mock.patch.object(cw, "emit_metrics", fake_emit):
        m._emit_cloudwatch_for_artifact({
            "action": "SKIP", "reason": "throttled", "metrics": {"totals": {}},
        })
    assert captured["reason"] == "throttled"
    assert captured["action"] == "SKIP"


def test_emit_cloudwatch_falls_back_to_action_when_no_reason(monkeypatch):
    """An artifact without a reason (e.g. RESPOND happy path) still emits;
    Reason dim falls back to the action label so every metric carries one."""
    m = _import()
    captured = {}

    def fake_emit(*, repository, metrics, action, pipeline, reason):
        captured["reason"] = reason
        return True, "ok"

    from shadow import cloudwatch as cw
    with mock.patch.object(cw, "emit_metrics", fake_emit):
        m._emit_cloudwatch_for_artifact({"action": "RESPOND"})
    assert captured["reason"] == "RESPOND"


def test_emit_cloudwatch_skips_when_no_repository(monkeypatch):
    """No GITHUB_REPOSITORY → silent skip (avoids tests writing artifacts to
    /tmp during pytest from spamming a real CloudWatch namespace)."""
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    m = _import()
    from shadow import cloudwatch as cw
    fake_emit = mock.Mock()
    with mock.patch.object(cw, "emit_metrics", fake_emit):
        m._emit_cloudwatch_for_artifact({"action": "SKIP", "reason": "x"})
    fake_emit.assert_not_called()


def test_post_failure_emits_dedicated_metric_without_invocations_double_count():
    """When act() detects a False posted_status, _emit_post_metrics fires with
    base_metrics=False so analyze()'s Invocations count isn't doubled, and a
    `post_failure_count` rides as the dedicated PostFailures metric."""
    m = _import()
    captured = {}

    def fake_emit(*, repository, metrics, action, pipeline, reason,
                   base_metrics=True, post_failure_count=0):
        captured["reason"] = reason
        captured["base_metrics"] = base_metrics
        captured["pipeline"] = pipeline
        captured["post_failure_count"] = post_failure_count
        return True, "ok"

    from shadow import cloudwatch as cw
    with mock.patch.object(cw, "emit_metrics", fake_emit):
        m._emit_post_metrics("RESPOND", {"comment": False, "labels": True}, "agentic")
    assert captured.get("reason") == "post_failure"
    assert captured.get("base_metrics") is False
    assert captured.get("pipeline") == "agentic"
    assert captured.get("post_failure_count") == 1


def test_post_failure_no_emit_when_all_succeeded():
    """All-True posted_status → no emission; the metric only fires for
    actual failures so the alert doesn't trip on healthy days."""
    m = _import()
    from shadow import cloudwatch as cw
    fake_emit = mock.Mock()
    with mock.patch.object(cw, "emit_metrics", fake_emit):
        m._emit_post_metrics("RESPOND", {"comment": True, "labels": True}, "agentic")
    fake_emit.assert_not_called()
