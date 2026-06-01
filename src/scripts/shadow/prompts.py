"""/prompts/<name>.txt; env var override per prompt.

Precedence: env var > on-disk file > "". Callers MUST fail closed on ""
for any prompt that drives a pipeline stage (the Investigator emits
HYPOTHESIS/STATUS markers the pipeline can't synthesize from a generic
prompt, so silent fallback would mask a misconfig as a clean-PR result).
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


def _get_prompt(env_var, filename):
    val = os.getenv(env_var, "")
    if val:
        return val
    return _read_from_disk(filename)


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
    """Empty disables the commit phase — loop falls back to terminate-without-conclusion."""
    return _get_prompt("PR_INVESTIGATOR_COMMIT_PROMPT", "pr-investigator-commit.txt")


def get_pr_critic_commit_prompt():
    return _get_prompt("PR_CRITIC_COMMIT_PROMPT", "pr-critic-commit.txt")


# Legacy stubs return "" so callers from main.py don't crash; they already check empty and bail.
def get_issue_prompt():
    return os.getenv("ISSUE_CLASSIFY_PROMPT", "")


def get_issue_respond_prompt():
    return os.getenv("ISSUE_RESPOND_PROMPT", "")


def get_pr_file_review_prompt():
    return os.getenv("PR_FILE_REVIEW_PROMPT", "")


def get_followup_prompt():
    return os.getenv("FOLLOWUP_PROMPT", "")


def get_pr_file_review_report_prompt():
    return os.getenv("PR_FILE_REVIEW_REPORT_PROMPT", "")


def prompt_version(template):
    return hashlib.sha256(template.encode()).hexdigest()[:8]


# Active pipeline prompts captured by compute_prompt_provenance(). The order
# of this tuple is the published artifact ordering; do not reorder casually.
_ACTIVE_PROMPTS = (
    ("investigator", "PR_INVESTIGATOR_PROMPT", "pr-investigator.txt"),
    ("critic", "PR_CRITIC_PROMPT", "pr-critic.txt"),
    ("reporter", "PR_REPORTER_PROMPT", "pr-reporter.txt"),
    ("investigator_commit", "PR_INVESTIGATOR_COMMIT_PROMPT",
     "pr-investigator-commit.txt"),
    ("critic_commit", "PR_CRITIC_COMMIT_PROMPT", "pr-critic-commit.txt"),
)


def compute_prompt_provenance():
    """Return per-stage prompt fingerprints + a single rollup hash.

    Adopters writing audit pipelines can verify which prompt content actually
    ran for a given artifact. Tampering with `sudsali/shadow`'s prompt files
    or an env-var override changes the rollup hash.
    """
    per_prompt = []
    rollup = hashlib.sha256()
    for stage, env_var, filename in _ACTIVE_PROMPTS:
        env_val = os.getenv(env_var, "")
        if env_val:
            text = env_val
            source = f"env:{env_var}"
        else:
            text = _read_from_disk(filename)
            source = f"file:prompts/{filename}"
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
