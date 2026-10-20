"""_build_provenance_from_env feeds the audit-trail model attribution on the
artifacts written without Config (early exits and issue answers). It must mirror
Config's env > .shadow.yml > default resolution — critically, reporter AND issue
default to Haiku, not to BEDROCK_MODEL_ID — or those artifacts misattribute which
model ran.
"""
import pytest

from shadow import main as m

_OPUS = "us.anthropic.claude-opus-4-8"
_HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_MODEL_ENVS = ("BEDROCK_MODEL_ID", "BEDROCK_REPORTER_MODEL_ID",
               "BEDROCK_CRITIC_MODEL_ID", "BEDROCK_ISSUE_MODEL_ID")


@pytest.fixture(autouse=True)
def _clear(monkeypatch, tmp_path):
    for v in _MODEL_ENVS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("SHADOW_REPO_ROOT", str(tmp_path))


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


@pytest.mark.parametrize("empty_value", ["", "   "])
def test_empty_or_whitespace_env_falls_back_to_defaults(monkeypatch, empty_value):
    # A set-but-empty/whitespace env var (e.g. `${{ secrets.X }}` where X is
    # unset -> GitHub passes "") must fall back to the default on EVERY stage,
    # matching Config.env_or. Guards the investigator line specifically: a bare
    # `getenv(name, default)` would stamp "" here while runtime resolves the
    # real default, mislabeling the audit provenance.
    for var in _MODEL_ENVS:
        monkeypatch.setenv(var, empty_value)
    mo = _models()
    assert mo["investigator"] == _OPUS
    assert mo["critic"] == _OPUS
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


def test_shadow_yml_models_resolve_exactly_like_config(monkeypatch, tmp_path):
    (tmp_path / ".shadow.yml").write_text(
        "models:\n  investigator: us.anthropic.claude-sonnet-4-6`\n  issue: us.anthropic.claude-sonnet-4-6\n")
    for k, v in {"GITHUB_TOKEN": "x", "EVENT_TYPE": "issues", "ISSUE_NUMBER": "1", "GITHUB_REPOSITORY": "o/r",
                 "BOT_REQUIRE_GUARDRAIL": "false", "BEDROCK_CRITIC_MODEL_ID": "us.anthropic.claude-opus-4-7"}.items():
        monkeypatch.setenv(k, v)
    from shadow.config import Config
    mo = _models()
    assert mo["investigator"] == "us.anthropic.claude-sonnet-4-6" and mo["reporter"] == _HAIKU
    assert mo == m._build_provenance(Config())["models"]
