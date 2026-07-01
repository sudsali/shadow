"""Repository-agnostic prompt resolver: bring-your-own-prompt via inline env
text or Secrets Manager, else Shadow's bundled defaults. `_resolve_prompt`
owns the precedence; whitespace-only content at any tier is normalized to ""
so a pipeline-stage prompt that resolves empty fails closed (the Investigator
emits HYPOTHESIS/STATUS markers the pipeline can't synthesize, so a silent
fallback would mask a misconfig as a clean-PR result).
"""
import hashlib
import logging
import os
from pathlib import Path

logger = logging.getLogger("shadow")


# Path layout: src/scripts/shadow/prompts.py → ../../../prompts. Update
# this depth if the package is relocated.
_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"


def _read_from_disk(filename):
    p = _PROMPTS_DIR / filename
    if not p.exists():
        logger.error("Required prompt missing: %s", p)
        return ""
    try:
        return p.read_text(encoding="utf-8")
    # UnicodeDecodeError is a ValueError, not OSError — without ValueError
    # in the except, a non-UTF-8 prompt file crashes the analyze run with
    # an unhandled exception.
    except (OSError, ValueError) as e:
        logger.error("Failed to read prompt %s: %s", p, type(e).__name__)
        return ""


# Successful Secrets Manager fetches are memoized for the process so the
# provenance pass doesn't re-fetch what the pipeline already loaded. Failures
# are NOT cached: a transient error on first read must not poison later reads.
_sm_cache = {}


def _clienterror_code(exc):
    """Best-effort botocore error code, never raising. `exc.response` may be
    absent, None, or a non-dict on non-ClientError exceptions; any failure to
    extract yields "" — this runs inside a fail-closed except block where a
    second exception would be catastrophic."""
    try:
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            error = response.get("Error")
            if isinstance(error, dict):
                code = error.get("Code")
                if isinstance(code, str):
                    return code
    except Exception:  # noqa: BLE001 — diagnostics must never mask the real error
        pass
    return ""


def _read_from_sm(secret_name):
    """Fetch a prompt from Secrets Manager by name. Returns "" on ANY failure
    so the caller fails closed — a misnamed or unreachable secret must not
    silently fall through to a different prompt; empty trips the pipeline's
    fail-closed guard and surfaces the misconfig instead of masking it as a
    clean-PR result."""
    if secret_name in _sm_cache:
        return _sm_cache[secret_name]
    try:
        import boto3
        from botocore.config import Config as BotoConfig
        region = (os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
                  or "us-east-1")
        # Bound the fetch. A prompt is a tiny call; the pipeline resolves five
        # and the provenance pass re-resolves misses, so an unresponsive-but-
        # routable SM endpoint must not stall the run toward its wall-clock
        # budget before failing closed. Mirrors bedrock_client's explicit
        # timeouts — every AWS client here caps its stall.
        client = boto3.client(
            "secretsmanager", region_name=region,
            config=BotoConfig(
                connect_timeout=5, read_timeout=5,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )
        text = client.get_secret_value(
            SecretId=secret_name).get("SecretString", "") or ""
    except Exception as e:  # noqa: BLE001 — any boto/SM failure must fail closed
        # botocore wraps AccessDenied / ResourceNotFound / wrong-region all as
        # ClientError; surface the error CODE (not the message — that could echo
        # secret contents) so an operator can tell a permissions miss from a
        # missing secret without reproducing. Extract defensively: e.response
        # may be absent or None, and Error/Code may be missing — a raise inside
        # this handler would defeat the fail-closed guarantee.
        code = _clienterror_code(e)
        logger.error(
            "Failed to load prompt from Secrets Manager '%s': %s%s",
            secret_name, type(e).__name__,
            f" ({code})" if code else "",
        )
        return ""
    if text:
        _sm_cache[secret_name] = text
    return text


def _resolve_prompt(env_var, filename, sm_fallback_to_disk=False):
    """Resolve one prompt with precedence:
      1. <env_var>          — inline prompt TEXT (highest; pre-existing hook)
      2. SM_<env_var>       — Secrets Manager secret NAME to fetch
      3. prompts/<filename> — Shadow's bundled language-agnostic default

    Bring-your-own-prompt if a repo supplies one; otherwise the shared default.
    Returns (text, source_label). Keep overrides deliberate — the bundled
    default is the single source everyone should improve, not fork per repo.

    sm_fallback_to_disk: an SM miss falls back to the bundled disk prompt
    instead of returning "". Set only for prompts that are NOT fail-closed
    (the commit-phase nudges, whose getters guard with `or None`): when a
    partial secret set omits them, falling back keeps the phase working rather
    than silently disabling it. Fail-closed (core) prompts must NOT set this —
    a missing core prompt must surface as "" so the caller escalates.
    """
    # Presence is decided on non-whitespace content at EVERY tier. Whitespace-
    # only text (fat-fingered secret, clobbered file, stray inline override) is
    # semantically empty and is normalized to "" here, so a single downstream
    # `if not prompt` check fails closed uniformly — it must never run the
    # reviewer with a blank system prompt (a silent wrong review). Real prompts
    # are returned byte-for-byte so provenance hashes exactly what ran.
    val = os.getenv(env_var, "")
    if val.strip():
        return val, f"env:{env_var}"
    sm_name = os.getenv(f"SM_{env_var}", "").strip()
    if sm_name:
        text = _read_from_sm(sm_name)
        if text.strip():
            return text, f"sm:{sm_name}"
        # SM miss / empty / whitespace-only. Commit-phase prompts fall back to
        # the bundled disk default (keeps the phase working on a partial secret
        # set); core prompts return "" from the SM tier and fail closed loudly.
        if not sm_fallback_to_disk:
            return "", f"sm:{sm_name}"
        # fall through to disk
    disk = _read_from_disk(filename)
    return (disk if disk.strip() else ""), f"file:prompts/{filename}"


def _get_prompt(env_var, filename, sm_fallback_to_disk=False):
    return _resolve_prompt(env_var, filename, sm_fallback_to_disk)[0]


def get_pr_investigator_prompt():
    """Empty return must fail closed; the legacy file-review prompt lacks
    HYPOTHESIS/STATUS markers and would mask misconfig as a clean PR."""
    return _get_prompt("PR_INVESTIGATOR_PROMPT", "pr-investigator.txt")


def get_pr_critic_prompt():
    """Empty return must fail closed; without the adversarial verdict step,
    all Investigator findings reach the Reporter unchallenged."""
    return _get_prompt("PR_CRITIC_PROMPT", "pr-critic.txt")


def get_pr_reporter_prompt():
    return _get_prompt("PR_REPORTER_PROMPT", "pr-reporter.txt")


def get_pr_investigator_commit_prompt():
    """Empty disables the commit phase — loop falls back to terminate-without-
    conclusion. Not fail-closed, so an SM miss falls back to the bundled disk
    prompt rather than silently dropping the phase."""
    return _get_prompt("PR_INVESTIGATOR_COMMIT_PROMPT",
                       "pr-investigator-commit.txt", sm_fallback_to_disk=True)


def get_pr_critic_commit_prompt():
    """Commit-phase nudge; same fail-open contract as the investigator commit
    prompt — an SM miss falls back to the bundled disk default."""
    return _get_prompt("PR_CRITIC_COMMIT_PROMPT",
                       "pr-critic-commit.txt", sm_fallback_to_disk=True)


def get_issue_prompt():
    return _get_prompt("ISSUE_CLASSIFY_PROMPT", "issue-classify.txt")


def get_issue_respond_prompt():
    return _get_prompt("ISSUE_RESPOND_PROMPT", "issue-respond.txt")


def get_followup_prompt():
    return _get_prompt("FOLLOWUP_PROMPT", "followup.txt")


def prompt_version(template):
    return hashlib.sha256(template.encode()).hexdigest()[:8]


# Active pipeline prompts captured by compute_prompt_provenance(): tuples of
# (stage, env_var, filename, sm_fallback_to_disk). Order is the published
# artifact ordering; do not reorder casually. Each sm_fallback flag MUST match
# the corresponding getter, or provenance mislabels a fell-back prompt's source.
_ACTIVE_PROMPTS = (
    ("investigator", "PR_INVESTIGATOR_PROMPT", "pr-investigator.txt", False),
    ("critic", "PR_CRITIC_PROMPT", "pr-critic.txt", False),
    ("reporter", "PR_REPORTER_PROMPT", "pr-reporter.txt", False),
    ("investigator_commit", "PR_INVESTIGATOR_COMMIT_PROMPT",
     "pr-investigator-commit.txt", True),
    ("critic_commit", "PR_CRITIC_COMMIT_PROMPT", "pr-critic-commit.txt", True),
)


def compute_prompt_provenance():
    """Return per-stage prompt fingerprints + a single rollup hash.

    Adopters writing audit pipelines can verify which prompt content actually
    ran for a given artifact. Tampering with `sudsali/shadow`'s prompt files
    or an env-var override changes the rollup hash.
    """
    per_prompt = []
    rollup = hashlib.sha256()
    for stage, env_var, filename, sm_fallback in _ACTIVE_PROMPTS:
        # Route through the same resolver the pipeline uses so the audit
        # rollup reflects what actually ran — an SM-sourced prompt is labeled
        # sm:<name>, not the disk fallback it would otherwise mask.
        text, source = _resolve_prompt(env_var, filename, sm_fallback)
        encoded = text.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        per_prompt.append({
            "stage": stage,
            "source": source,
            "sha256": digest,
            # Byte count, not codepoint count — matches the hashed domain
            # so adopter audit tooling can `assert len == os.path.getsize(prompt)`.
            "len": len(encoded),
            "loaded": bool(text),
        })
        # Bind stage to its hash so swapping prompts between stages also
        # changes the rollup.
        rollup.update(stage.encode("utf-8"))
        rollup.update(b"\x00")
        rollup.update(digest.encode("ascii"))
        rollup.update(b"\x00")
    return {
        "rollup_sha256": rollup.hexdigest(),
        "prompts": per_prompt,
    }
