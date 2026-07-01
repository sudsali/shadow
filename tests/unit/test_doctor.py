"""shadow doctor preflight checks. AWS-dependent checks (STS, Bedrock)
are tested via mocks at the function boundary; YAML and prompt checks
hit real fixtures."""
import argparse
import tempfile
from pathlib import Path

import pytest

from shadow import doctor


class _Args:
    def __init__(self, **kw):
        self.role_arn = kw.get("role_arn")
        self.region = kw.get("region")
        self.repo_root = kw.get("repo_root")


def test_check_role_arn_unset_fails(monkeypatch):
    monkeypatch.delenv("AWS_ROLE_ARN", raising=False)
    r = doctor._Result()
    out = doctor._check_role_arn(_Args(role_arn=None), r)
    assert out is None
    assert r.fails == 1


def test_check_role_arn_malformed_fails():
    r = doctor._Result()
    out = doctor._check_role_arn(_Args(role_arn="not-an-arn"), r)
    assert out is None
    assert r.fails == 1


def test_check_role_arn_well_formed_passes():
    r = doctor._Result()
    out = doctor._check_role_arn(
        _Args(role_arn="arn:aws:iam::123456789012:role/shadow-bot-ci"),
        r,
    )
    assert out == "arn:aws:iam::123456789012:role/shadow-bot-ci"
    assert r.fails == 0


def test_check_role_arn_env_fallback(monkeypatch):
    monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/shadow-bot-ci")
    r = doctor._Result()
    out = doctor._check_role_arn(_Args(role_arn=None), r)
    assert out is not None
    assert r.fails == 0


def test_check_shadow_yml_missing_warns():
    with tempfile.TemporaryDirectory() as tmp:
        r = doctor._Result()
        doctor._check_shadow_yml(_Args(repo_root=tmp), r)
        assert r.fails == 0
        assert r.warns == 1


def test_check_shadow_yml_valid_passes(tmp_path):
    (tmp_path / ".shadow.yml").write_text(
        "codebase:\n  src_dir: src\n"
    )
    (tmp_path / "src").mkdir()
    r = doctor._Result()
    doctor._check_shadow_yml(_Args(repo_root=str(tmp_path)), r)
    assert r.fails == 0


def test_check_shadow_yml_bad_yaml_fails(tmp_path):
    # Real malformed yaml — unbalanced brace.
    (tmp_path / ".shadow.yml").write_text("codebase: {src_dir: [}")
    r = doctor._Result()
    doctor._check_shadow_yml(_Args(repo_root=str(tmp_path)), r)
    assert r.fails == 1


def test_check_shadow_yml_nonexistent_src_dir_fails(tmp_path):
    (tmp_path / ".shadow.yml").write_text(
        "codebase:\n  src_dir: nope/does/not/exist\n"
    )
    r = doctor._Result()
    doctor._check_shadow_yml(_Args(repo_root=str(tmp_path)), r)
    assert r.fails == 1


def test_check_prompts_loadable_passes_in_repo():
    # Run from the repo so prompts/*.txt actually exist.
    r = doctor._Result()
    doctor._check_prompts_loadable(_Args(), r)
    assert r.fails == 0
    assert r.warns == 0


def test_check_prompts_warns_on_silent_sm_fallback(monkeypatch):
    # S1 regression: an SM-configured commit prompt whose secret MISSES silently
    # falls back to the bundled disk default (loaded=True). The doctor must WARN
    # — a green check here would certify a misconfigured BYO setup as healthy.
    from shadow import prompts
    monkeypatch.setattr(prompts, "_read_from_sm", lambda name: "")  # every SM miss
    monkeypatch.setenv("SM_PR_CRITIC_COMMIT_PROMPT", "my-bot/pr-critic-commit-prompt")
    r = doctor._Result()
    doctor._check_prompts_loadable(_Args(), r)
    assert r.fails == 0          # commit prompt still loads (disk fallback)
    assert r.warns == 1          # but the doctor flags the silent fallback


def test_check_prompts_no_warn_when_sm_hits(monkeypatch):
    # Contrast: SM configured AND hits → source is sm:, no warn.
    from shadow import prompts
    monkeypatch.setattr(prompts, "_read_from_sm", lambda name: "CUSTOM COMMIT")
    monkeypatch.setenv("SM_PR_CRITIC_COMMIT_PROMPT", "my-bot/pr-critic-commit-prompt")
    r = doctor._Result()
    doctor._check_prompts_loadable(_Args(), r)
    assert r.fails == 0 and r.warns == 0


def test_check_prompts_warns_lists_all_fallen_back_stages(monkeypatch, capsys):
    # Multi-stage: BOTH commit prompts configured-but-missing must both appear
    # in the single WARN — guards against a regression that reports only the
    # first fallen-back stage.
    from shadow import prompts
    monkeypatch.setattr(prompts, "_read_from_sm", lambda name: "")  # all miss
    monkeypatch.setenv("SM_PR_INVESTIGATOR_COMMIT_PROMPT", "my-bot/pr-investigator-commit-prompt")
    monkeypatch.setenv("SM_PR_CRITIC_COMMIT_PROMPT", "my-bot/pr-critic-commit-prompt")
    r = doctor._Result()
    doctor._check_prompts_loadable(_Args(), r)
    assert r.fails == 0 and r.warns == 1
    out = capsys.readouterr().out
    assert "investigator_commit" in out and "critic_commit" in out


def test_main_returns_nonzero_on_fails(monkeypatch, capsys):
    monkeypatch.delenv("AWS_ROLE_ARN", raising=False)
    # The bedrock + STS checks will fail without creds — that's enough to
    # confirm the exit-code branch.
    rc = doctor.main(["--region", "us-east-1", "--repo-root", "/tmp/nonexistent-repo-xyz"])
    assert rc == 1


def test_main_passes_when_no_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(doctor, "_check_sts_caller", lambda a, r, arn: None)
    monkeypatch.setattr(doctor, "_check_bedrock_access", lambda a, r: None)
    monkeypatch.setattr(doctor, "_check_guardrail", lambda a, r: None)
    (tmp_path / ".shadow.yml").write_text("codebase:\n  src_dir: .\n")
    rc = doctor.main([
        "--role-arn", "arn:aws:iam::123456789012:role/shadow-bot-ci",
        "--region", "us-east-1",
        "--repo-root", str(tmp_path),
    ])
    assert rc == 0


def test_check_guardrail_unset_warns_with_actionable_fix(monkeypatch):
    """No GUARDRAIL_ID set — doctor must WARN (not fail), and the fix
    line must mention the CFN GuardrailId output so adopters know where
    the value comes from."""
    monkeypatch.delenv("GUARDRAIL_ID", raising=False)
    r = doctor._Result()
    doctor._check_guardrail(_Args(region="us-east-1"), r)
    assert r.warns == 1
    assert r.fails == 0


def test_check_guardrail_set_calls_get_guardrail(monkeypatch):
    """GUARDRAIL_ID set — doctor must call bedrock:GetGuardrail and PASS
    when the call succeeds. Mocks boto3 to avoid a live AWS dependency."""
    monkeypatch.setenv("GUARDRAIL_ID", "gr-abc123")
    monkeypatch.setenv("GUARDRAIL_VERSION", "1")
    calls = []

    class _MockBedrock:
        def get_guardrail(self, **kw):
            calls.append(kw)
            return {"name": "shadow-guardrail", "status": "READY"}

    class _MockBoto3:
        @staticmethod
        def client(name, region_name):
            assert name == "bedrock"
            return _MockBedrock()

    import sys as _sys
    monkeypatch.setitem(_sys.modules, "boto3", _MockBoto3)
    r = doctor._Result()
    doctor._check_guardrail(_Args(region="us-east-1"), r)
    assert r.fails == 0
    assert r.warns == 0
    # Confirm the call really went through with the configured version.
    assert calls == [{
        "guardrailIdentifier": "gr-abc123",
        "guardrailVersion": "1",
    }]


def test_check_guardrail_failed_get_call_reports_failure(monkeypatch):
    """If GetGuardrail raises (typo'd ID, missing permission, region
    mismatch), doctor FAILs with an actionable message naming the three
    most likely causes — not a generic 'AWS error'."""
    monkeypatch.setenv("GUARDRAIL_ID", "gr-typo")
    monkeypatch.delenv("GUARDRAIL_VERSION", raising=False)

    from botocore.exceptions import ClientError

    class _MockBedrock:
        def get_guardrail(self, **kw):
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException",
                           "Message": "guardrail not found"}},
                "GetGuardrail",
            )

    class _MockBoto3:
        @staticmethod
        def client(name, region_name):
            return _MockBedrock()

    import sys as _sys
    monkeypatch.setitem(_sys.modules, "boto3", _MockBoto3)
    r = doctor._Result()
    doctor._check_guardrail(_Args(region="us-east-1"), r)
    assert r.fails == 1
