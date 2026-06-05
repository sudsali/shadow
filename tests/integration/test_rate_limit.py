"""Spam-defense + cost-amplification protection.

Two layers tested here:
  1. Per-(repo, item) hourly rate limit (cfg.max_runs_per_hour) — caps how
     many times a single PR/issue can spin up the workflow within an hour
     window. Defends against a force-pushing attacker who closes/reopens
     or edits PR title to sidestep the per-item already_commented /
     max_bot_replies guards.
  2. Pre-flight diff/file-count caps (cfg.max_diff_for_review,
     cfg.max_files_for_review) — escalates oversized PRs before any
     Bedrock call. A 50-file PR amplifies multiplicatively across
     Investigator + Critic + Reporter; pre-flight escalation costs ~$0
     instead of ~$5+.
"""
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


def _set_required_env(monkeypatch):
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)


def _clear_rate_env(monkeypatch):
    """Strip any rate-limit / pre-flight-cap env vars so a previous test's
    monkeypatch doesn't leak through autouse fixture ordering."""
    for v in (
        "BOT_MAX_RUNS_PER_HOUR",
        "BOT_MAX_DIFF_FOR_REVIEW_CHARS",
        "BOT_MAX_FILES_FOR_REVIEW",
    ):
        monkeypatch.delenv(v, raising=False)


# --------------------------------------------------------------------------- #
# Per-(repo, item) hourly rate limit
# --------------------------------------------------------------------------- #


def test_rate_limit_default_20(monkeypatch):
    _set_required_env(monkeypatch)
    _clear_rate_env(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("SHADOW_REPO_ROOT", tmp)
        from shadow.config import Config
        cfg = Config()
        assert cfg.max_runs_per_hour == 20


def test_rate_limit_yaml_override(monkeypatch):
    _set_required_env(monkeypatch)
    _clear_rate_env(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, ".shadow.yml").write_text(
            "bot:\n  max_runs_per_hour: 5\n"
        )
        monkeypatch.setenv("SHADOW_REPO_ROOT", tmp)
        from shadow.config import Config
        assert Config().max_runs_per_hour == 5


def test_rate_limit_env_beats_yaml(monkeypatch):
    _set_required_env(monkeypatch)
    _clear_rate_env(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, ".shadow.yml").write_text(
            "bot:\n  max_runs_per_hour: 5\n"
        )
        monkeypatch.setenv("SHADOW_REPO_ROOT", tmp)
        monkeypatch.setenv("BOT_MAX_RUNS_PER_HOUR", "3")
        from shadow.config import Config
        assert Config().max_runs_per_hour == 3


def test_rate_limit_garbage_yaml_falls_back(monkeypatch):
    """A yaml typo that doesn't parse as int must not silently disable the
    cap or crash Config(). Out-of-range parsed ints clamp to the [0, 100]
    boundary with a warning instead — see test_config_init's
    test_max_runs_per_hour_out_of_range_clamps_with_warning."""
    _set_required_env(monkeypatch)
    for garbage in ("not-a-number", "true"):
        _clear_rate_env(monkeypatch)
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".shadow.yml").write_text(
                f"bot:\n  max_runs_per_hour: {garbage}\n"
            )
            monkeypatch.setenv("SHADOW_REPO_ROOT", tmp)
            from shadow.config import Config
            assert Config().max_runs_per_hour == 20, f"failed for: {garbage!r}"


def test_rate_limit_zero_disables(monkeypatch):
    """0 means 'no rate limit'. analyze() must not gate on cfg.max_runs_per_hour
    in that case so adopters who track usage externally aren't double-gated."""
    _set_required_env(monkeypatch)
    _clear_rate_env(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, ".shadow.yml").write_text(
            "bot:\n  max_runs_per_hour: 0\n"
        )
        monkeypatch.setenv("SHADOW_REPO_ROOT", tmp)
        from shadow.config import Config
        assert Config().max_runs_per_hour == 0


# --------------------------------------------------------------------------- #
# Pre-flight diff / file-count cap
# --------------------------------------------------------------------------- #


def test_max_diff_for_review_default(monkeypatch):
    _set_required_env(monkeypatch)
    _clear_rate_env(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("SHADOW_REPO_ROOT", tmp)
        from shadow.config import Config
        cfg = Config()
        assert cfg.max_diff_for_review == 100_000


def test_max_diff_for_review_env_override(monkeypatch):
    _set_required_env(monkeypatch)
    _clear_rate_env(monkeypatch)
    monkeypatch.setenv("BOT_MAX_DIFF_FOR_REVIEW_CHARS", "50000")
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("SHADOW_REPO_ROOT", tmp)
        from shadow.config import Config
        cfg = Config()
        assert cfg.max_diff_for_review == 50000


def test_max_files_for_review_default(monkeypatch):
    _set_required_env(monkeypatch)
    _clear_rate_env(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("SHADOW_REPO_ROOT", tmp)
        from shadow.config import Config
        cfg = Config()
        assert cfg.max_files_for_review == 50


def test_max_files_for_review_env_override(monkeypatch):
    _set_required_env(monkeypatch)
    _clear_rate_env(monkeypatch)
    monkeypatch.setenv("BOT_MAX_FILES_FOR_REVIEW", "10")
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("SHADOW_REPO_ROOT", tmp)
        from shadow.config import Config
        cfg = Config()
        assert cfg.max_files_for_review == 10


@pytest.fixture(autouse=True)
def _reload_config():
    """Config reads env at import-time; force fresh load per test."""
    import importlib
    import shadow.config  # noqa: F401
    importlib.reload(__import__("shadow.config", fromlist=["Config"]))
    yield
