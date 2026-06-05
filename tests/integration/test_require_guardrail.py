"""BOT_REQUIRE_GUARDRAIL=true (default) makes Config() refuse to load when
DRY_RUN=false and GUARDRAIL_ID is unset. This is defense-in-depth on top of
the CFN-provisioned default guardrail: even if an adopter skips the CFN AND
forgets to wire GUARDRAIL_ID into a secret, production runs cannot quietly
fall back to local-sanitizer-only.
"""
import tempfile

import pytest


REQUIRED_ENV_BASE = {
    "GITHUB_TOKEN": "fake-token",
    "EVENT_TYPE": "pull_request_target",
    "ISSUE_NUMBER": "42",
    "GITHUB_REPOSITORY": "owner/repo",
}


def _set_base(monkeypatch):
    for k, v in REQUIRED_ENV_BASE.items():
        monkeypatch.setenv(k, v)


def test_production_run_without_guardrail_exits(monkeypatch):
    """DRY_RUN=false + BOT_REQUIRE_GUARDRAIL unset (defaults true) +
    GUARDRAIL_ID unset => Config() must SystemExit(1) before any Bedrock
    call. The error message names the misconfig + the fix."""
    _set_base(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "false")
    # explicitly unset GUARDRAIL_ID + BOT_REQUIRE_GUARDRAIL
    monkeypatch.delenv("GUARDRAIL_ID", raising=False)
    monkeypatch.delenv("BOT_REQUIRE_GUARDRAIL", raising=False)
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("SHADOW_REPO_ROOT", tmp)
        from shadow.config import Config
        with pytest.raises(SystemExit) as excinfo:
            Config()
        assert excinfo.value.code == 1


def test_dry_run_without_guardrail_does_not_exit(monkeypatch):
    """DRY_RUN=true bypasses the require check so adopters can validate
    end-to-end without a guardrail. The artifact is written but no
    comments are posted, so a missing guardrail can't cause harm in
    the dry-run path."""
    _set_base(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("GUARDRAIL_ID", raising=False)
    monkeypatch.delenv("BOT_REQUIRE_GUARDRAIL", raising=False)
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("SHADOW_REPO_ROOT", tmp)
        from shadow.config import Config
        cfg = Config()
        assert cfg.dry_run is True
        assert cfg.require_guardrail is True
        assert cfg.guardrail_id == ""


def test_explicit_opt_out_lets_production_run_without_guardrail(monkeypatch):
    """BOT_REQUIRE_GUARDRAIL=false lets a production run proceed without a
    guardrail — for adopters pointing at a custom hand-built guardrail
    not yet wired in, OR who deliberately accept the local-sanitizer-only
    stance for a specific deployment. The override exists so the require
    check isn't a hard wedge against legitimate non-default setups."""
    _set_base(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("BOT_REQUIRE_GUARDRAIL", "false")
    monkeypatch.delenv("GUARDRAIL_ID", raising=False)
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("SHADOW_REPO_ROOT", tmp)
        from shadow.config import Config
        cfg = Config()
        assert cfg.require_guardrail is False
        assert cfg.guardrail_id == ""


def test_production_run_with_guardrail_proceeds(monkeypatch):
    """The happy path: production run, GUARDRAIL_ID set, require check
    passes. cfg.guardrail_id and cfg.guardrail_version reach the
    BedrockClient."""
    _set_base(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("GUARDRAIL_ID", "gr-abc123")
    monkeypatch.setenv("GUARDRAIL_VERSION", "1")
    monkeypatch.delenv("BOT_REQUIRE_GUARDRAIL", raising=False)
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("SHADOW_REPO_ROOT", tmp)
        from shadow.config import Config
        cfg = Config()
        assert cfg.guardrail_id == "gr-abc123"
        assert cfg.guardrail_version == "1"
        assert cfg.require_guardrail is True


@pytest.mark.parametrize("falsy_value", ["0", "false", "FALSE", "no", "off", "False"])
def test_require_guardrail_off_spellings(monkeypatch, falsy_value):
    """The require flag accepts the same off-spellings the rest of the
    codebase does (BOT_AGENT_PIPELINE, etc.), case-insensitively. Lock the
    spelling list so a future refactor doesn't drop one."""
    _set_base(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("BOT_REQUIRE_GUARDRAIL", falsy_value)
    monkeypatch.delenv("GUARDRAIL_ID", raising=False)
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("SHADOW_REPO_ROOT", tmp)
        from shadow.config import Config
        cfg = Config()
        assert cfg.require_guardrail is False, f"failed for: {falsy_value!r}"


@pytest.fixture(autouse=True)
def _reload_config():
    """Config reads env at import-time; force fresh load per test."""
    import importlib
    import shadow.config  # noqa: F401
    importlib.reload(__import__("shadow.config", fromlist=["Config"]))
    yield
