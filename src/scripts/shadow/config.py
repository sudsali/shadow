import os
import re
import sys
import logging

from . import shadow_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("shadow")


# Built-in default model. Module-level so the cost-pricing test can assert
# the pricing table covers it without re-encoding the literal in two places.
_DEFAULT_MODEL = "us.anthropic.claude-opus-4-7"
# Reporter built-in default. Haiku because the Reporter is JSON-formatting
# only — Opus's reasoning depth is wasted there, and Haiku is the model
# documented as the default in the README.
_DEFAULT_REPORTER_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


class Config:
    def __init__(self):
        # env > .shadow.yml > built-in default
        yml = shadow_config.load(os.getenv("SHADOW_REPO_ROOT", "."))

        self.github_token = _require("GITHUB_TOKEN")
        self.event_type = _require("EVENT_TYPE")
        self.event_action = os.getenv("EVENT_ACTION", "")
        self.issue_number = _require("ISSUE_NUMBER")
        if not self.issue_number.isdigit():
            logger.error(f"ISSUE_NUMBER must be numeric: {self.issue_number}")
            sys.exit(1)
        self.repo = _require("GITHUB_REPOSITORY")
        self.event_before = os.getenv("EVENT_BEFORE", "")
        self.event_after = os.getenv("EVENT_AFTER", "")

        self.bedrock_model_id = _scrub_model_id(shadow_config.env_or(
            "BEDROCK_MODEL_ID",
            shadow_config.get(yml, "models", "investigator"),
            _DEFAULT_MODEL,
        ), _DEFAULT_MODEL)
        # Reporter is JSON-formatting only; Haiku handles structured output
        # well enough AND is the model the issue path REQUIRES (Opus 4.7
        # rejects outputConfig.textFormat over Bedrock today). Adopters who
        # override BEDROCK_MODEL_ID (Investigator) without setting Reporter
        # silently get Haiku here — log loud at INFO so the asymmetry is
        # visible in workflow logs rather than discovered via "why does my
        # Reporter look weaker than my Investigator".
        self.reporter_model_id = _scrub_model_id(shadow_config.env_or(
            "BEDROCK_REPORTER_MODEL_ID",
            shadow_config.get(yml, "models", "reporter"),
            _DEFAULT_REPORTER_MODEL,
        ), _DEFAULT_REPORTER_MODEL)
        if self.reporter_model_id != self.bedrock_model_id:
            logger.info(
                "Reporter model: %s (Investigator: %s). Reporter default is "
                "Haiku for structured-output compatibility; set "
                "BEDROCK_REPORTER_MODEL_ID to override.",
                self.reporter_model_id, self.bedrock_model_id,
            )
        # Critic runs converse_with_tools — override must support tool use.
        self.critic_model_id = _scrub_model_id(shadow_config.env_or(
            "BEDROCK_CRITIC_MODEL_ID",
            shadow_config.get(yml, "models", "critic"),
            self.bedrock_model_id,
        ), self.bedrock_model_id)
        if self.critic_model_id != self.bedrock_model_id:
            logger.info("Critic model override: %s (default: %s)", self.critic_model_id, self.bedrock_model_id)

        self.kb_s3_bucket = os.getenv("KB_S3_BUCKET", "")
        self.kb_s3_key = os.getenv("KB_S3_KEY", "")

        self.slack_webhook_url = os.getenv("SLACK_WEBHOOK_URL", "")
        self.guardrail_id = os.getenv("GUARDRAIL_ID", "")
        self.guardrail_version = os.getenv("GUARDRAIL_VERSION") or "DRAFT"

        self.dry_run = os.getenv("DRY_RUN", "false").lower() == "true"

        # Guardrail-required mode. Defaults ON: a production run (DRY_RUN=false)
        # without GUARDRAIL_ID set is a misconfiguration — the CFN Launch Stack
        # provisions one by default and outputs the ID; if the adopter skipped
        # pasting it into a secret, refuse rather than silently run with the
        # local sanitizer alone. DRY_RUN=true bypasses the check so adopters
        # can still validate end-to-end without a guardrail in the dry path.
        # Override BOT_REQUIRE_GUARDRAIL=false to point at a custom hand-built
        # guardrail provisioned outside the CFN stack and not yet wired in,
        # OR to deliberately accept the local-sanitizer-only stance for a
        # specific deployment (not recommended).
        self.require_guardrail = os.getenv(
            "BOT_REQUIRE_GUARDRAIL", "true",
        ).strip().lower() not in ("0", "false", "no", "off")
        if (
            self.require_guardrail
            and not self.dry_run
            and not self.guardrail_id
        ):
            logger.error(
                "BOT_REQUIRE_GUARDRAIL=true and DRY_RUN=false but GUARDRAIL_ID "
                "is unset. Production runs require a Bedrock guardrail. The "
                "CFN Launch Stack provisions one by default and emits "
                "GuardrailId + GuardrailVersion outputs — paste both into your "
                "repo secrets (Settings → Secrets and variables → Actions). "
                "To override (custom guardrail, dry-only fork, etc.), set "
                "BOT_REQUIRE_GUARDRAIL=false. See "
                "https://github.com/sudsali/shadow#security-model"
            )
            sys.exit(1)
        self.enable_slack = bool(self.slack_webhook_url)
        self.enable_repo_search = os.getenv("ENABLE_REPO_SEARCH", "true").lower() == "true"

        self.upstream_repo = os.getenv("UPSTREAM_REPO", self.repo)
        # _str_or_default forces non-string yaml values (int, list, bool) to
        # the default — without it `file_ext: 1` crashes downstream tools.
        self.codebase_src_dir = _str_or_default(shadow_config.env_or(
            "CODEBASE_SRC_DIR",
            shadow_config.get(yml, "codebase", "src_dir"),
            ".",
        ), ".")
        self.codebase_file_ext = _str_or_default(shadow_config.env_or(
            "CODEBASE_FILE_EXT",
            shadow_config.get(yml, "codebase", "file_ext"),
            "",
        ), "")
        self.codebase_test_dir = _str_or_default(shadow_config.env_or(
            "CODEBASE_TEST_DIR",
            shadow_config.get(yml, "codebase", "test_dir"),
            "",
        ), "")
        # Interpolated into a markdown ```{lang}``` fence; backticks or
        # newlines from a typo close the block and leak content as prose.
        self.codebase_language = _scrub_codeblock_token(shadow_config.env_or(
            "CODEBASE_LANGUAGE",
            shadow_config.get(yml, "codebase", "language"),
            "",
        ))

        # Renaming lets multiple bots coexist on one repo. Marker comments
        # (<!-- shadow:clean -->) embed this; HTML-comment-breaking chars
        # (<, >, --, newlines) would let an adopter typo eat real PR text.
        self.bot_name = _scrub_marker_token(shadow_config.env_or(
            "BOT_NAME",
            shadow_config.get(yml, "bot", "name"),
            "shadow",
        ), "shadow")
        # Posted to GitHub Labels API; reject non-strings, oversized, and
        # whitespace-only values so a malformed yaml doesn't trigger a 422.
        self.escalate_label = _scrub_label(shadow_config.env_or(
            "BOT_ESCALATE_LABEL",
            shadow_config.get(yml, "bot", "escalate_label"),
            "needs-human",
        ), "needs-human")
        # GitHub login the bot's comments appear under. Default matches the
        # GHA built-in token; adopters running under a custom GitHub App or
        # service account override via BOT_GITHUB_ACTOR env or yaml.
        self.bot_actor = _scrub_label(shadow_config.env_or(
            "BOT_GITHUB_ACTOR",
            shadow_config.get(yml, "bot", "github_actor"),
            "github-actions[bot]",
        ), "github-actions[bot]")
        # After this many bot replies on one issue/PR, the next user comment
        # escalates instead of triggering another reply. Anti-abuse + cost
        # control. Default 2 matches the original deequ-bot tuning.
        self.max_bot_replies = _int_in_range_or_default(shadow_config.env_or(
            "BOT_MAX_REPLIES",
            shadow_config.get(yml, "bot", "max_replies"),
            2,
        ), 2, name="BOT_MAX_REPLIES (or yaml bot.max_replies)")
        # Per-(repo, item) hourly rate limit on Shadow workflow runs. Defends
        # against PR/issue spam triggering a fleet of expensive Bedrock calls
        # past the per-item already_commented / max_bot_replies guards. 0
        # disables the limit so adopters who handle this externally aren't
        # double-gated.
        self.max_runs_per_hour = _int_in_range_or_default(shadow_config.env_or(
            "BOT_MAX_RUNS_PER_HOUR",
            shadow_config.get(yml, "bot", "max_runs_per_hour"),
            20,
        ), 20, name="BOT_MAX_RUNS_PER_HOUR (or yaml bot.max_runs_per_hour)")
        # Pre-flight diff/file-count caps. A 50-file PR ripples through
        # Investigator → Critic → Reporter, each potentially reading 5+
        # files; cost amplification is multiplicative. Escalating before
        # any Bedrock call burns ~$0; a runaway pipeline burns ~$5+.
        self.max_diff_for_review = _int_env("BOT_MAX_DIFF_FOR_REVIEW_CHARS", 100_000)
        self.max_files_for_review = _int_env("BOT_MAX_FILES_FOR_REVIEW", 50)

        self.bedrock_timeout = 240
        self.max_context_chars = 800000
        self.max_github_search_results = 8
        self.github_api_timeout = 10
        self.allowed_labels = {
            "bug", "enhancement", "question", "documentation", "help-wanted",
        }

        # Default ON. BOT_AGENT_PIPELINE=0 makes PR events ESCALATE with
        # reason `pipeline_disabled`; issue/followup paths still run their
        # legacy issue-classify / followup prompts. To disable the bot
        # fleet-wide, set repo/org variable SHADOW_DISABLED=true (gates
        # both jobs in the reusable workflow).
        _pipeline_env = os.getenv("BOT_AGENT_PIPELINE", "").strip().lower()
        self.agent_pipeline = _pipeline_env not in ("0", "false", "no", "off")

        self.investigator_max_turns = _int_env("BOT_INVESTIGATOR_MAX_TURNS", 15)
        self.investigator_max_tool_calls = _int_env("BOT_INVESTIGATOR_MAX_TOOL_CALLS", 50)
        self.investigator_max_tool_output_chars = _int_env(
            "BOT_INVESTIGATOR_MAX_TOOL_OUTPUT", 400_000,
        )

        self.critic_max_turns = _int_env("BOT_CRITIC_MAX_TURNS", 10)
        self.critic_max_tool_calls = _int_env("BOT_CRITIC_MAX_TOOL_CALLS", 30)
        self.critic_max_tool_output_chars = _int_env(
            "BOT_CRITIC_MAX_TOOL_OUTPUT", 200_000,
        )
        # _int_env handles negatives; this guards the literal `=0` case which
        # would silently drop the diff (Investigator/Critic see empty string).
        agent_max_diff = _int_env("BOT_AGENT_MAX_DIFF_CHARS", 200_000)
        if agent_max_diff == 0:
            logger.warning(
                "BOT_AGENT_MAX_DIFF_CHARS=0 would drop the diff; using default 200000",
            )
            agent_max_diff = 200_000
        self.agent_max_diff_chars = agent_max_diff
        self.pipeline_wall_clock_seconds = _int_env("BOT_PIPELINE_WALL_CLOCK_S", 480)
        # Reporter latency on slow Bedrock days runs 20-45s.
        self.reporter_min_remaining_seconds = _int_env("BOT_REPORTER_MIN_REMAINING_S", 60)


def _require(name):
    val = os.getenv(name)
    if not val:
        logger.error(f"Missing required env var: {name}")
        sys.exit(1)
    return val


def _int_env(name, default):
    """Garbage env values fall back to default rather than killing analyze()
    before any artifact is written. Negative values also fall back to default
    with a named warning — every _int_env caller is a positive cost/wall-clock
    cap whose downstream consumer either gates on `> 0` (max_diff_for_review,
    max_files_for_review at main.py:1152-1153) or treats negative values as
    immediate-exhaustion (tool-call / turn / budget caps), so a `-1` typo
    would silently disable pre-flight defenses or short-circuit the agent
    pipeline."""
    raw = os.getenv(name)
    try:
        n = int(raw) if raw else default
    except (TypeError, ValueError):
        logger.warning(
            "%s=%r is not parseable as int; using default %d", name, raw, default,
        )
        return default
    if n < 0:
        logger.warning(
            "%s=%d is negative; using default %d", name, n, default,
        )
        return default
    return n


def _str_or_default(val, default):
    return val if isinstance(val, str) else default


def _scrub_codeblock_token(val):
    if not isinstance(val, str):
        return ""
    return val.replace("`", "").replace("\n", "").replace("\r", "").strip()


def _scrub_label(val, default):
    """GitHub label spec: non-empty, ≤50 chars, no commas / control chars."""
    if not isinstance(val, str):
        return default
    val = "".join(c for c in val if c.isprintable() and c != ",").strip()
    if not val:
        return default
    return val[:50]


def _int_in_range_or_default(val, default, *, name):
    """Yaml int, env str, or wrong type → default. Cap range [0, 100] so a
    yaml typo can't set the bound absurdly high. `0` is valid and means
    "no limit applied" (callers decide per-field semantics — e.g.,
    max_bot_replies=0 means escalate-on-first-followup, max_runs_per_hour=0
    means disable the rate limit). Garbage strings (`"--5"`, `"++5"`,
    `"5  abc"`) fall back to default rather than crash Config().

    Negative parsed ints fall back to default (with warning) rather than
    clamping to 0 — clamping a negative typo to 0 silently flips
    rate-limit/reply semantics into "disabled" / "escalate-on-first-reply",
    which is the exact opposite of what an operator typing -1 intended.
    Out-of-range positive ints clamp to 100 with warning so an operator
    who set BOT_MAX_RUNS_PER_HOUR=200 hoping to double the cap doesn't
    silently get the default (20) — the original rate-limit-tuning footgun.
    Both diagnostics name the env var so the misconfig is greppable in
    workflow logs.

    `name` is keyword-only and required so a future caller can't omit it
    and silently re-introduce the no-warning footgun."""
    if isinstance(val, bool):
        logger.warning(
            "%s=%r is a bool, not an int; using default %d", name, val, default,
        )
        return default
    if isinstance(val, int):
        n = val
    elif isinstance(val, str):
        try:
            n = int(val.strip())
        except (TypeError, ValueError):
            logger.warning(
                "%s=%r is not parseable as int; using default %d",
                name, val, default,
            )
            return default
    else:
        logger.warning(
            "%s=%r is %s, not an int; using default %d",
            name, val, type(val).__name__, default,
        )
        return default
    if n < 0:
        logger.warning(
            "%s=%d is negative; using default %d", name, n, default,
        )
        return default
    if n > 100:
        logger.warning(
            "%s=%d is above maximum (100); clamping to 100 (default would be %d)",
            name, n, default,
        )
        return 100
    return n


def _scrub_model_id(val, default):
    """Bedrock model ID is interpolated into Markdown (review footer) and
    the audit-trail artifact. Reject backticks / whitespace / control chars
    so a yaml typo can't break the rendered comment or hide injection in
    the provenance block."""
    if not isinstance(val, str):
        return default
    val = "".join(
        c for c in val if c.isprintable() and not c.isspace() and c != "`"
    ).strip()
    if not val:
        return default
    return val[:128]


def _scrub_marker_token(val, default):
    """Embedded in `<!-- {bot}:clean -->`; reject HTML-comment-breaking chars.
    Collapses runs of `-`/`_` to a single char so adjacent dashes can't break
    out of the `<!-- ... -->` envelope (HTML forbids `--` inside a comment
    body — `<!-- shadow--evil:clean -->` would close the comment early).

    Also rejects a small, hardcoded list of token-suffix patterns whose
    rendered marker (`<!-- {name}:clean -->`) would trip the sanitizer's
    injection-marker list (e.g., bot_name='system' renders
    `<!-- system:clean -->`; the `system:` substring matches sanitizer's
    `system:` marker → every clean review nulls to ESCALATE without a
    marker). The reject-list is intentionally NOT imported from
    sanitizer._INJECTION_MARKERS — that list grows as new injection classes
    are discovered, and each addition must NOT silently invalidate adopters'
    deployed bot_name. Keep this list narrow and bump it explicitly with a
    CHANGELOG entry when a new bot_name pattern needs blocking."""
    if not isinstance(val, str):
        return default
    val = "".join(c for c in val if c.isalnum() or c in "-_")
    val = re.sub(r"-{2,}", "-", val)
    val = re.sub(r"_{2,}", "_", val)
    val = val.strip("-_")
    val = val[:32]
    if not val:
        return default
    # Hardcoded reject-list. The rendered marker shape is `:clean -->`, so
    # a bot_name ending in any of these tokens produces `<token>:clean` which
    # would substring-match sanitizer._INJECTION_MARKERS. Today only `system`
    # actually trips a marker; the others are reserved for future markers
    # added in lockstep with a CHANGELOG entry.
    _REJECTED_NAME_SUFFIXES = ("system",)
    lower_val = val.lower()
    for suffix in _REJECTED_NAME_SUFFIXES:
        if lower_val == suffix or lower_val.endswith("-" + suffix) or lower_val.endswith("_" + suffix):
            logger.warning(
                "bot.name=%r would render a marker tripping the sanitizer; "
                "using default %r",
                val, default,
            )
            return default
    return val
