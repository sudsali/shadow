"""cfg.allowed_labels configurability + rate-limit label branding.

The issue-triage label allowlist was hardcoded, so adopters could not apply
repo-specific labels (e.g. python-deequ's `python`, dqdl's `java`) — the
engine dropped them at main.py before the Labels API call. Make it read from
.shadow.yml bot.allowed_labels (list) or BOT_ALLOWED_LABELS env (comma-sep),
defaulting to the original set so existing deployments are unchanged.

Also verifies the rate-limit escalation label tracks bot_name rather than a
hardcoded `shadow:` literal.
"""
import importlib
import tempfile
from pathlib import Path

import pytest


REQUIRED_ENV = {
    "GITHUB_TOKEN": "fake-token",
    "EVENT_TYPE": "pull_request_target",
    "ISSUE_NUMBER": "42",
    "GITHUB_REPOSITORY": "owner/repo",
    "BOT_REQUIRE_GUARDRAIL": "false",
}

_DEFAULT = {"bug", "enhancement", "question", "documentation", "help-wanted"}


def _set_required_env(monkeypatch):
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("BOT_ALLOWED_LABELS", raising=False)


@pytest.fixture(autouse=True)
def _reload_config():
    """Config reads env at import-time; force fresh load per test."""
    import shadow.config  # noqa: F401
    importlib.reload(__import__("shadow.config", fromlist=["Config"]))
    yield


def _cfg(monkeypatch, tmp, yml=None):
    if yml is not None:
        Path(tmp, ".shadow.yml").write_text(yml)
    monkeypatch.setenv("SHADOW_REPO_ROOT", tmp)
    from shadow.config import Config
    return Config()


def test_default_allowed_labels(monkeypatch):
    _set_required_env(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(monkeypatch, tmp)
        assert cfg.allowed_labels == _DEFAULT


def test_yaml_list_extends_labels(monkeypatch):
    _set_required_env(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(monkeypatch, tmp,
                   "bot:\n  allowed_labels:\n    - bug\n    - python\n    - java\n")
        assert cfg.allowed_labels == {"bug", "python", "java"}


def test_env_overrides_yaml(monkeypatch):
    _set_required_env(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, ".shadow.yml").write_text(
            "bot:\n  allowed_labels:\n    - bug\n"
        )
        monkeypatch.setenv("SHADOW_REPO_ROOT", tmp)
        monkeypatch.setenv("BOT_ALLOWED_LABELS", "enhancement,question")
        from shadow.config import Config
        cfg = Config()
        assert cfg.allowed_labels == {"enhancement", "question"}


def test_env_comma_separated(monkeypatch):
    _set_required_env(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("BOT_ALLOWED_LABELS", " bug , python ,java")
        monkeypatch.setenv("SHADOW_REPO_ROOT", tmp)
        from shadow.config import Config
        cfg = Config()
        # entries are whitespace-stripped by the label scrub
        assert cfg.allowed_labels == {"bug", "python", "java"}


def test_empty_yaml_list_falls_back_to_default(monkeypatch):
    """An empty list must not disable labeling entirely."""
    _set_required_env(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(monkeypatch, tmp, "bot:\n  allowed_labels: []\n")
        assert cfg.allowed_labels == _DEFAULT


def test_all_garbage_entries_fall_back(monkeypatch):
    """A list of only non-string / blank entries scrubs to empty -> default."""
    _set_required_env(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(monkeypatch, tmp,
                   "bot:\n  allowed_labels:\n    - 123\n    - \"   \"\n    - null\n")
        assert cfg.allowed_labels == _DEFAULT


def test_garbage_scalar_yaml_falls_back(monkeypatch):
    """A non-list, non-string scalar (int) drops to default rather than crash."""
    _set_required_env(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(monkeypatch, tmp, "bot:\n  allowed_labels: 5\n")
        assert cfg.allowed_labels == _DEFAULT


def test_mixed_valid_and_garbage_keeps_valid(monkeypatch):
    """Valid entries survive; garbage ones drop (partial list is not all-or-nothing)."""
    _set_required_env(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(monkeypatch, tmp,
                   "bot:\n  allowed_labels:\n    - python\n    - 123\n    - \"\"\n")
        assert cfg.allowed_labels == {"python"}


def test_scalar_yaml_string_is_split(monkeypatch):
    """A scalar yaml string 'a,b' is treated like the comma-sep env form."""
    _set_required_env(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(monkeypatch, tmp, "bot:\n  allowed_labels: \"python,java\"\n")
        assert cfg.allowed_labels == {"python", "java"}


def test_parse_helper_directly():
    """Unit-level: the pure helper, independent of env loading."""
    from shadow.config import _parse_allowed_labels, _DEFAULT_ALLOWED_LABELS
    d = _DEFAULT_ALLOWED_LABELS
    assert _parse_allowed_labels(None, None, d) == set(d)
    assert _parse_allowed_labels("python,java", None, d) == {"python", "java"}
    assert _parse_allowed_labels(None, ["bug"], d) == {"bug"}
    assert _parse_allowed_labels("  ", None, d) == set(d)      # blank env -> default
    assert _parse_allowed_labels(None, [], d) == set(d)        # empty list -> default
    assert _parse_allowed_labels("python", ["bug"], d) == {"python"}  # env > yaml


def test_rate_limit_label_reaches_github_branded(monkeypatch):
    """End-to-end through act(): a rate_limited ESCALATE artifact must actually
    apply a <bot_name>:rate-limited label to the issue. This label is NOT in
    allowed_labels, so it must be appended AFTER the allowlist filter (like
    escalate_label) — an earlier version wrote it into the artifact only to have
    it silently dropped by the filter, so it never reached GitHub. Branded with
    bot_name (deequ-bot:rate-limited), not a hardcoded shadow: literal."""
    import json
    from unittest import mock
    _set_required_env(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, ".shadow.yml").write_text("bot:\n  name: deequ-bot\n")
        monkeypatch.setenv("SHADOW_REPO_ROOT", tmp)
        artifact = Path(tmp, "bot_result.json")
        artifact.write_text(json.dumps({
            "action": "ESCALATE", "reason": "rate_limited",
            "labels": ["needs-human"], "response": "",
            "number": 42, "is_pr": True,
            "title": "t", "html_url": "https://github.com/o/r/pull/42",
            "prompt_id": "n/a", "model_id": "m",
        }))
        monkeypatch.setenv("ARTIFACT_PATH", str(artifact))
        monkeypatch.setenv("SHADOW_VERIFY_ARTIFACT", "false")  # skip HMAC in unit test

        import shadow.main as m
        importlib.reload(m)
        captured = {}
        fake_gh = mock.MagicMock()
        fake_gh.add_labels.side_effect = lambda n, labels: captured.setdefault("labels", labels) or True
        with mock.patch.object(m, "GitHubClient", return_value=fake_gh), \
             mock.patch.object(m, "SlackClient", return_value=mock.MagicMock()):
            m.act()

        assert "deequ-bot:rate-limited" in captured["labels"], captured
        assert "needs-human" in captured["labels"]  # escalate_label still applied
