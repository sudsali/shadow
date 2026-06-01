"""Pre-install / pre-PR sanity check for Shadow.

Run after CloudFormation provisions the IAM role; catches the install
failures that otherwise surface as cryptic Bedrock 403s in the middle of
a real PR review.

  python -m shadow.doctor                           # auto-discover env
  python -m shadow.doctor --role-arn arn:... --region us-east-1

Each check prints:
  [PASS] / [WARN] / [FAIL]  description
              fix: <one-line remediation or doc link>

Exits 0 if no FAILs, 1 otherwise. WARN never blocks.
"""
import argparse
import json
import os
import sys
from pathlib import Path


_DOC_BASE = "https://github.com/sudsali/shadow#"


def _print(level, msg, fix=None):
    print(f"[{level}] {msg}")
    if fix:
        print(f"        fix: {fix}")


class _Result:
    def __init__(self):
        self.fails = 0
        self.warns = 0

    def passed(self, msg):
        _print("PASS", msg)

    def warned(self, msg, fix=None):
        self.warns += 1
        _print("WARN", msg, fix)

    def failed(self, msg, fix=None):
        self.fails += 1
        _print("FAIL", msg, fix)


def _check_role_arn(args, result):
    arn = args.role_arn or os.getenv("AWS_ROLE_ARN", "")
    if not arn:
        result.failed(
            "AWS_ROLE_ARN is unset.",
            f"set the AWS_ROLE_ARN env var or pass --role-arn (see {_DOC_BASE}aws-setup)",
        )
        return None
    if not arn.startswith("arn:aws:iam::") or ":role/" not in arn:
        result.failed(
            f"AWS_ROLE_ARN does not look like a role ARN: {arn!r}",
            "expected: arn:aws:iam::123456789012:role/shadow-bot-ci",
        )
        return None
    result.passed(f"AWS_ROLE_ARN parsed: {arn}")
    return arn


def _check_sts_caller(args, result, arn):
    if arn is None:
        return None
    try:
        import boto3
    except ImportError:
        result.failed(
            "boto3 is not installed.",
            "pip install -r requirements.txt",
        )
        return None
    try:
        from botocore.exceptions import ClientError, BotoCoreError, NoCredentialsError
        sts = boto3.client("sts")
        ident = sts.get_caller_identity()
    except (ClientError, BotoCoreError, NoCredentialsError) as e:
        result.failed(
            f"sts.GetCallerIdentity failed: {type(e).__name__}: {e}",
            f"see {_DOC_BASE}troubleshooting (Could not assume role)",
        )
        return None
    actual = ident.get("Arn", "")
    # When the doctor is run by the workflow itself, the assumed-role ARN
    # contains the role name; when run locally, it'll be a user ARN — that
    # also passes (we only need creds to call Bedrock for the next check).
    if "shadow-bot-ci" in actual:
        result.passed(f"STS identity resolves to shadow-bot-ci: {actual}")
    else:
        result.warned(
            f"STS identity is not the shadow-bot-ci role: {actual}",
            "expected when running locally; in CI this should be the assumed role",
        )
    return ident


def _check_bedrock_access(args, result):
    region = args.region or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
    print(f"        (region: {region} — set --region or AWS_REGION to override)")
    try:
        import boto3
        from botocore.exceptions import ClientError, BotoCoreError, NoCredentialsError
    except ImportError:
        result.failed("boto3 missing — see prior FAIL.")
        return
    # Real-invoke smoke check. ListInferenceProfiles returns AWS-managed
    # metadata regardless of whether the caller has Bedrock console "Model
    # access" granted — it would PASS while real Converse() 403s. A 1-token
    # converse is the only way to detect missing per-account model access
    # before a real PR review hits the same 403 mid-loop.
    # Use bedrock_client's helper so doctor and runtime stay in lockstep on
    # which model families reject sampling params (Opus 4.7 today).
    from .bedrock_client import _build_inference_config
    needed = [
        ("Investigator/Critic", "us.anthropic.claude-opus-4-7"),
        ("Reporter", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
    ]
    client = boto3.client("bedrock-runtime", region_name=region)
    failures = []
    for label, model_id in needed:
        try:
            client.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
                inferenceConfig=_build_inference_config(1, 0.0, model_id),
            )
        except (ClientError, BotoCoreError, NoCredentialsError) as e:
            failures.append((label, model_id, type(e).__name__, str(e)))
    if not failures:
        result.passed(f"Bedrock invoke succeeded in {region} for both models")
        return
    for label, model_id, exc_type, exc_msg in failures:
        result.failed(
            f"Bedrock {label} model {model_id} unreachable in {region}: {exc_type}",
            f"console → Bedrock → Model access (region: {region}) — see {_DOC_BASE}aws-setup; "
            f"if you provisioned via CFN, ensure --region matches the BedrockRegion you chose",
        )


def _check_shadow_yml(args, result):
    repo_root = args.repo_root or os.getenv("SHADOW_REPO_ROOT") or "."
    path = Path(repo_root) / ".shadow.yml"
    if not path.exists():
        result.warned(
            f"no .shadow.yml at {path} — Shadow will use built-in defaults.",
            f"see examples/shadow.example.yml for the schema",
        )
        return
    try:
        import yaml
    except ImportError:
        result.warned(
            ".shadow.yml exists but PyYAML is not installed; will be ignored at runtime.",
            "pip install -r requirements.txt",
        )
        return
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        result.failed(
            f".shadow.yml is malformed YAML: {e}",
            "validate with `python -c 'import yaml; yaml.safe_load(open(\".shadow.yml\"))'`",
        )
        return
    src_dir = (data.get("codebase") or {}).get("src_dir")
    if not src_dir:
        result.warned(
            ".shadow.yml has no codebase.src_dir; Shadow will scan repo root.",
            "set codebase.src_dir to your project's primary source directory",
        )
    else:
        full = Path(repo_root) / src_dir
        if full.exists():
            result.passed(f".shadow.yml parses; src_dir resolves to {full}")
        else:
            result.failed(
                f".shadow.yml codebase.src_dir does not exist: {full}",
                "fix the path or remove the field to use repo root",
            )


def _check_prompts_loadable(args, result):
    try:
        from . import prompts
    except ImportError:
        # Allow standalone invocation without package context.
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from shadow import prompts
    prov = prompts.compute_prompt_provenance()
    missing = [p for p in prov["prompts"] if not p["loaded"]]
    if not missing:
        result.passed(
            f"all 5 pipeline prompts loaded (rollup {prov['rollup_sha256'][:16]}...)"
        )
        return
    names = [p["stage"] for p in missing]
    result.failed(
        f"prompt files missing or unreadable: {names}",
        f"verify the shadow_ref pin includes prompts/ directory; see {_DOC_BASE}troubleshooting",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Shadow Bot preflight check.")
    parser.add_argument("--role-arn", help="IAM role ARN to validate (default: $AWS_ROLE_ARN).")
    parser.add_argument("--region", help="Bedrock region (default: $AWS_REGION or us-east-1).")
    parser.add_argument("--repo-root", help="Path to adopter repo (default: $SHADOW_REPO_ROOT or '.').")
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON summary in addition to human output.")
    args = parser.parse_args(argv)

    print("Shadow doctor — preflight check\n")
    result = _Result()

    print("Identity")
    arn = _check_role_arn(args, result)
    _check_sts_caller(args, result, arn)
    print()

    print("Bedrock")
    _check_bedrock_access(args, result)
    print()

    print("Repo config")
    _check_shadow_yml(args, result)
    _check_prompts_loadable(args, result)
    print()

    summary = {"fails": result.fails, "warns": result.warns}
    if args.json:
        print(json.dumps({"summary": summary}))

    if result.fails:
        print(f"❌ {result.fails} fail(s), {result.warns} warning(s) — fix the FAILs before installing.")
        return 1
    if result.warns:
        print(f"⚠️  {result.warns} warning(s) — adopt anyway, or fix and re-run.")
    else:
        print("✅ all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
