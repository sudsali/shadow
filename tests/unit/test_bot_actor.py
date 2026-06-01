"""cfg.bot_actor + _is_bot_comment dedup.

Adopters running Shadow under a custom GitHub App or service account need
the comment-author dedup to match their actor login, not the GHA built-in.
The 6 sites that previously hardcoded "github-actions[bot]" silently
flipped to "bot replies repeatedly to one issue" when an adopter swapped
the running identity. Default still matches GHA so the existing rollout
keeps working without yaml/env changes.
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
}


def _set_required_env(monkeypatch):
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)


@pytest.fixture(autouse=True)
def _reload_config():
    """Config reads env at import-time; force fresh load per test."""
    import shadow.config  # noqa: F401
    importlib.reload(__import__("shadow.config", fromlist=["Config"]))
    yield


def test_default_bot_actor(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.delenv("BOT_GITHUB_ACTOR", raising=False)
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("SHADOW_REPO_ROOT", tmp)
        from shadow.config import Config
        cfg = Config()
        assert cfg.bot_actor == "github-actions[bot]"


def test_yaml_override_bot_actor(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.delenv("BOT_GITHUB_ACTOR", raising=False)
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, ".shadow.yml").write_text(
            "bot:\n  github_actor: \"shadow-bot[bot]\"\n"
        )
        monkeypatch.setenv("SHADOW_REPO_ROOT", tmp)
        from shadow.config import Config
        cfg = Config()
        assert cfg.bot_actor == "shadow-bot[bot]"


def test_env_overrides_yaml(monkeypatch):
    _set_required_env(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, ".shadow.yml").write_text(
            "bot:\n  github_actor: \"yaml-bot[bot]\"\n"
        )
        monkeypatch.setenv("SHADOW_REPO_ROOT", tmp)
        monkeypatch.setenv("BOT_GITHUB_ACTOR", "foo[bot]")
        from shadow.config import Config
        cfg = Config()
        assert cfg.bot_actor == "foo[bot]"


def test_yaml_garbage_falls_back_to_default(monkeypatch):
    """A non-string yaml value (e.g., int) must drop to the default rather
    than crash the equality check downstream."""
    _set_required_env(monkeypatch)
    monkeypatch.delenv("BOT_GITHUB_ACTOR", raising=False)
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, ".shadow.yml").write_text("bot:\n  github_actor: 123\n")
        monkeypatch.setenv("SHADOW_REPO_ROOT", tmp)
        from shadow.config import Config
        cfg = Config()
        assert cfg.bot_actor == "github-actions[bot]"


class _FakeCfg:
    """Pure stand-in so _is_bot_comment unit tests don't drag in env loading."""
    def __init__(self, bot_actor="github-actions[bot]"):
        self.bot_actor = bot_actor


def test_is_bot_comment_matches_configured_actor():
    from shadow.main import _is_bot_comment
    cfg = _FakeCfg(bot_actor="shadow-bot[bot]")
    comment = {"user": {"login": "shadow-bot[bot]"}, "body": "hi"}
    assert _is_bot_comment(comment, cfg) is True


def test_is_bot_comment_third_party_bot_does_not_match():
    """Third-party GitHub Apps (dependabot[bot], renovate[bot], codecov[bot])
    must not be treated as Shadow's own. A `.endswith('[bot]')` fallback
    would let any of those trip `already_commented` and silently disable
    review on every PR they also touch."""
    from shadow.main import _is_bot_comment
    cfg = _FakeCfg(bot_actor="github-actions[bot]")
    for foreign in ("dependabot[bot]", "renovate[bot]", "codecov[bot]"):
        comment = {"user": {"login": foreign}, "body": "hi"}
        assert _is_bot_comment(comment, cfg) is False, foreign


def test_is_bot_comment_human_login():
    from shadow.main import _is_bot_comment
    cfg = _FakeCfg(bot_actor="github-actions[bot]")
    comment = {"user": {"login": "alice"}, "body": "hi"}
    assert _is_bot_comment(comment, cfg) is False


def test_is_bot_comment_handles_null_user():
    """GitHub returns null user for deleted accounts; must not crash."""
    from shadow.main import _is_bot_comment
    cfg = _FakeCfg(bot_actor="github-actions[bot]")
    comment = {"user": None, "body": "hi"}
    assert _is_bot_comment(comment, cfg) is False


def test_bot_reply_count_uses_configured_actor():
    """Counter for max_bot_replies cap must match cfg.bot_actor, not a
    hardcoded literal — otherwise an adopter running under a custom App
    gets unbounded replies."""
    from shadow.main import _bot_reply_count
    cfg = _FakeCfg(bot_actor="shadow-bot[bot]")
    comments = [
        {"user": {"login": "alice"}, "body": "issue"},
        {"user": {"login": "shadow-bot[bot]"}, "body": "reply 1"},
        {"user": {"login": "alice"}, "body": "follow-up"},
        {"user": {"login": "shadow-bot[bot]"}, "body": "reply 2"},
    ]
    assert _bot_reply_count(comments, cfg) == 2


def test_already_replied_to_latest_uses_configured_actor():
    from shadow.main import _already_replied_to_latest
    cfg = _FakeCfg(bot_actor="shadow-bot[bot]")
    comments = [
        {"user": {"login": "alice"}, "body": "issue"},
        {"user": {"login": "shadow-bot[bot]"}, "body": "reply"},
    ]
    assert _already_replied_to_latest(comments, cfg) is True


def test_already_replied_to_latest_false_when_user_last():
    from shadow.main import _already_replied_to_latest
    cfg = _FakeCfg(bot_actor="shadow-bot[bot]")
    comments = [
        {"user": {"login": "alice"}, "body": "issue"},
        {"user": {"login": "shadow-bot[bot]"}, "body": "reply"},
        {"user": {"login": "alice"}, "body": "follow-up"},
    ]
    assert _already_replied_to_latest(comments, cfg) is False
