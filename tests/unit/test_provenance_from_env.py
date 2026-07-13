"""_build_provenance_from_env feeds the audit-trail model attribution on the
early-exit SKIP/ESCALATE paths (no Config in scope). It must mirror Config's
env>default resolution — critically, reporter AND issue default to Haiku, not
to BEDROCK_MODEL_ID — or those artifacts misattribute which model ran.
"""
import pytest

from shadow import main as m

_OPUS = "us.anthropic.claude-opus-4-8"
_HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_MODEL_ENVS = ("BEDROCK_MODEL_ID", "BEDROCK_REPORTER_MODEL_ID",
               "BEDROCK_CRITIC_MODEL_ID", "BEDROCK_ISSUE_MODEL_ID")


@pytest.fixture(autouse=True)
def _clear(monkeypatch):
    for v in _MODEL_ENVS:
        monkeypatch.delenv(v, raising=False)


def _models():
    return m._build_provenance_from_env()["models"]


def test_all_four_model_keys_present():
    assert set(_models()) == {"investigator", "critic", "reporter", "issue"}


def test_defaults_reporter_and_issue_are_haiku():
    mo = _models()
    assert mo["investigator"] == _OPUS
    assert mo["reporter"] == _HAIKU
    assert mo["issue"] == _HAIKU


def test_opus_investigator_does_not_drag_reporter_or_issue_to_opus(monkeypatch):
    # The bug this guards: setting only BEDROCK_MODEL_ID must NOT make the
    # reporter/issue provenance report Opus (runtime actually runs Haiku).
    monkeypatch.setenv("BEDROCK_MODEL_ID", "us.anthropic.claude-opus-4-8")
    mo = _models()
    assert mo["investigator"] == "us.anthropic.claude-opus-4-8"
    assert mo["critic"] == "us.anthropic.claude-opus-4-8"   # critic follows investigator
    assert mo["reporter"] == _HAIKU
    assert mo["issue"] == _HAIKU


def test_issue_follows_reporter_env(monkeypatch):
    monkeypatch.setenv("BEDROCK_REPORTER_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
    assert _models()["issue"] == "us.anthropic.claude-sonnet-4-6"


def test_issue_env_wins_over_reporter(monkeypatch):
    monkeypatch.setenv("BEDROCK_REPORTER_MODEL_ID", _HAIKU)
    monkeypatch.setenv("BEDROCK_ISSUE_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
    mo = _models()
    assert mo["reporter"] == _HAIKU
    assert mo["issue"] == "us.anthropic.claude-sonnet-4-6"
