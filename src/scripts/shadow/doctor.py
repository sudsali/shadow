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
    # which model families reject sampling params.
    from .bedrock_client import _build_inference_config
    from . import shadow_config
    # Validate the models the adopter actually configured, not hardcoded ones —
    # otherwise doctor greens an install whose real model (e.g. an Opus 4.8
    # override) is unreachable. Resolve exactly as Config does: env > .shadow.yml
    # > default, so a yaml-only override is validated too.
    _default_opus = "us.anthropic.claude-opus-4-7"
    _default_haiku = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    yml = shadow_config.load(args.repo_root or os.getenv("SHADOW_REPO_ROOT", "."))
    investigator = shadow_config.env_or(
        "BEDROCK_MODEL_ID", shadow_config.get(yml, "models", "investigator"), _default_opus)
    reporter = shadow_config.env_or(
        "BEDROCK_REPORTER_MODEL_ID", shadow_config.get(yml, "models", "reporter"), _default_haiku)
    critic = shadow_config.env_or(
        "BEDROCK_CRITIC_MODEL_ID", shadow_config.get(yml, "models", "critic"), investigator)
    issue = shadow_config.env_or(
        "BEDROCK_ISSUE_MODEL_ID", shadow_config.get(yml, "models", "issue"), reporter)
    # (label, model_id, needs_structured_output). The Reporter and Issue paths
    # send outputConfig.textFormat, which some models (Opus 4.8 / Sonnet 5)
    # reject — probe those WITH a schema so a misconfigured issue/reporter model
    # fails preflight here instead of hard-failing on the first real issue.
    needed = [("Investigator", investigator, False), ("Reporter", reporter, True)]
    if critic != investigator:
        needed.append(("Critic", critic, False))
    if issue != reporter:
        needed.append(("Issue", issue, True))
    client = boto3.client("bedrock-runtime", region_name=region)
    # Minimal schema mirroring the runtime structured-output shape.
    _probe_schema = json.dumps({"type": "object", "properties": {"ok": {"type": "string"}},
                                "required": ["ok"], "additionalProperties": False})
    failures = []
    for label, model_id, needs_struct in needed:
        try:
            kwargs = {
                "modelId": model_id,
                "messages": [{"role": "user", "content": [{"text": "hi"}]}],
                "inferenceConfig": _build_inference_config(16, 0.0, model_id),
            }
            if needs_struct:
                kwargs["outputConfig"] = {"textFormat": {"type": "json_schema",
                    "structure": {"jsonSchema": {"schema": _probe_schema, "name": "probe"}}}}
            client.converse(**kwargs)
        except (ClientError, BotoCoreError, NoCredentialsError) as e:
            failures.append((label, model_id, type(e).__name__, str(e)))
    if not failures:
        models = ", ".join(sorted({m for _label, m, _s in needed}))
        result.passed(f"Bedrock invoke succeeded in {region} for: {models}")
        return
    for label, model_id, exc_type, exc_msg in failures:
        struct_hint = ""
        _lm = exc_msg.lower()
        if label in ("Reporter", "Issue") and ("output_config" in _lm or "outputconfig" in _lm
                                                or "extra inputs are not permitted" in _lm):
            struct_hint = (" — this model may not support structured output "
                           "over Bedrock (use Haiku 4.5 or Sonnet 4.6, not Opus 4.8 / Sonnet 5)")
        result.failed(
            f"Bedrock {label} model {model_id} unreachable in {region}: {exc_type}{struct_hint}",
            f"console → Bedrock → Model access (region: {region}) — see {_DOC_BASE}aws-setup; "
            f"if you provisioned via CFN, ensure --region matches the BedrockRegion you chose",
        )


def _check_guardrail(args, result):
    """Validate the Bedrock guardrail wiring. The CFN Launch Stack provisions
    a default guardrail by default (ProvisionGuardrail=true) and emits a
    GuardrailId + GuardrailVersion output. If GUARDRAIL_ID is unset here, the
    most likely cause is the adopter forgot to copy the CFN output into a
    repo secret. We call bedrock:GetGuardrail to confirm the role can read
    the configured guardrail at the configured version — a cheap server-side
    check that catches typos + unset versions before a real PR review."""
    region = args.region or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
    guardrail_id = os.getenv("GUARDRAIL_ID", "").strip()
    guardrail_version = os.getenv("GUARDRAIL_VERSION", "").strip() or "DRAFT"

    if not guardrail_id:
        result.warned(
            "GUARDRAIL_ID is not set; the bot will run without a Bedrock-server-side "
            "prompt-injection scanner.",
            f"if you used the CFN Launch Stack, copy the GuardrailId output into a "
            f"GUARDRAIL_ID repo secret; see {_DOC_BASE}security-model. To intentionally "
            f"run without a guardrail (DRY runs, custom guardrail wired separately, etc.), "
            f"set BOT_REQUIRE_GUARDRAIL=false to opt out of the production-mode require check.",
        )
        return

    try:
        import boto3
        from botocore.exceptions import ClientError, BotoCoreError, NoCredentialsError
    except ImportError:
        result.failed("boto3 missing — see prior FAIL.")
        return

    try:
        bedrock_admin = boto3.client("bedrock", region_name=region)
        bedrock_admin.get_guardrail(
            guardrailIdentifier=guardrail_id,
            guardrailVersion=guardrail_version,
        )
    except (ClientError, BotoCoreError, NoCredentialsError) as e:
        result.failed(
            f"Bedrock GetGuardrail failed for {guardrail_id} v{guardrail_version}: "
            f"{type(e).__name__}",
            f"verify GUARDRAIL_ID matches the CFN GuardrailId output; verify "
            f"GUARDRAIL_VERSION matches the GuardrailVersion output (typically a "
            f"number like '1', not 'DRAFT', after stack publishes the version); "
            f"verify the role has bedrock:GetGuardrail permission. The CFN template "
            f"grants this on the provisioned guardrail when ProvisionGuardrail=true.",
        )
        return
    result.passed(
        f"Bedrock guardrail {guardrail_id} v{guardrail_version} accessible in {region}"
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
    if missing:
        names = [p["stage"] for p in missing]
        result.failed(
            f"prompt files missing or unreadable: {names}",
            f"verify the shadow_ref pin includes prompts/ directory; see {_DOC_BASE}troubleshooting",
        )
        return
    # Silent-misconfig guard: a stage whose SM_<var> is configured but whose
    # resolved source is a disk file means the SM fetch missed and a commit
    # prompt silently fell back to the bundled default — the adopter believes
    # their custom prompt is live when it is not. Loaded=True hides this, so
    # inspect the source label against the configured SM_* env vars.
    fell_back = [
        (stage, env_var)
        for stage, env_var, _filename, _fb in prompts._ACTIVE_PROMPTS
        if os.getenv(f"SM_{env_var}", "").strip()
        and _stage_source(prov, stage).startswith("file:")
    ]
    if fell_back:
        stages = [s for s, _ in fell_back]
        result.warned(
            f"SM prompt(s) configured but fell back to bundled defaults: {stages} "
            "(SM secret missing/unreadable/wrong-region — the custom prompt is NOT active)",
            "verify the secret names exist and the IAM role allows "
            f"secretsmanager:GetSecretValue in this region; see {_DOC_BASE}troubleshooting",
        )
        return
    result.passed(
        f"all {len(prov['prompts'])} prompts loaded across all surfaces "
        f"(rollup {prov['rollup_sha256'][:16]}...)"
    )


def _stage_source(prov, stage):
    for entry in prov["prompts"]:
        if entry["stage"] == stage:
            return entry.get("source", "")
    return ""


def _check_slack(args, result):
    """Validate the Slack webhook format if configured. Slack is optional and
    off by default, so an unset webhook is an informational PASS. When set, a
    malformed URL would only surface as a swallowed delivery failure at the
    first real escalation — this catches a typo'd/expired-shaped URL at
    preflight instead, closing the last integration doctor didn't cover. We
    check shape only (never POST — that would spam the channel) and never log
    the URL, whose path segment is a bearer credential."""
    url = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        result.passed("SLACK_WEBHOOK_URL not set — Slack escalations disabled (optional).")
        return
    if not url.startswith("https://hooks.slack.com/services/"):
        result.warned(
            "SLACK_WEBHOOK_URL is set but does not look like a Slack webhook "
            "(expected https://hooks.slack.com/services/...).",
            "verify the URL copied from Slack → Incoming Webhooks; a malformed "
            "URL fails silently at escalation time (delivery is best-effort)",
        )
        return
    result.passed("SLACK_WEBHOOK_URL is set and well-formed.")


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

    print("Guardrail")
    _check_guardrail(args, result)
    print()

    print("Repo config")
    _check_shadow_yml(args, result)
    _check_prompts_loadable(args, result)
    print()

    print("Slack")
    _check_slack(args, result)
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
