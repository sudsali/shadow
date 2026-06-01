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


def test_main_returns_nonzero_on_fails(monkeypatch, capsys):
    monkeypatch.delenv("AWS_ROLE_ARN", raising=False)
    # The bedrock + STS checks will fail without creds — that's enough to
    # confirm the exit-code branch.
    rc = doctor.main(["--region", "us-east-1", "--repo-root", "/tmp/nonexistent-repo-xyz"])
    assert rc == 1


def test_main_passes_when_no_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(doctor, "_check_sts_caller", lambda a, r, arn: None)
    monkeypatch.setattr(doctor, "_check_bedrock_access", lambda a, r: None)
    (tmp_path / ".shadow.yml").write_text("codebase:\n  src_dir: .\n")
    rc = doctor.main([
        "--role-arn", "arn:aws:iam::123456789012:role/shadow-bot-ci",
        "--region", "us-east-1",
        "--repo-root", str(tmp_path),
    ])
    assert rc == 0
