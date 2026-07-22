import os
import re
import sys
import logging

from . import shadow_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("shadow")


# Built-in default model. Module-level so the cost-pricing test can assert
# the pricing table covers it without re-encoding the literal in two places.
_DEFAULT_MODEL = "us.anthropic.claude-opus-4-8"
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
        # Overriding only BEDROCK_MODEL_ID leaves Reporter on the Haiku default;
        # the INFO log below makes that asymmetry visible in workflow logs.
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
        # Distinct knob from reporter (defaults to it): issue answers are
        # customer-facing prose worth a stronger model, but must still support
        # structured output over Bedrock (Haiku 4.5 / Sonnet 4.6 do; Opus does not).
        self.issue_model_id = _scrub_model_id(shadow_config.env_or(
            "BEDROCK_ISSUE_MODEL_ID",
            shadow_config.get(yml, "models", "issue"),
            self.reporter_model_id,
        ), self.reporter_model_id)
        if self.issue_model_id != self.reporter_model_id:
            logger.info("Issue-answer model: %s (reporter: %s)",
                        self.issue_model_id, self.reporter_model_id)
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

        # Default ON: a production run (DRY_RUN=false) without GUARDRAIL_ID
        # refuses rather than run sanitizer-only. DRY_RUN bypasses;
        # BOT_REQUIRE_GUARDRAIL=false opts out for a custom/hand-built guardrail.
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
        # Optional one-line attribution appended to the posted comment footer
        # (e.g. product name + link). Empty by default so footers are unchanged.
        self.bot_attribution = _scrub_attribution(shadow_config.env_or(
            "BOT_ATTRIBUTION",
            shadow_config.get(yml, "bot", "attribution"),
            "",
        ))
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
        # control.
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
        # Extensible so adopters can allow repo-specific labels (e.g. python, java).
        self.allowed_labels = _parse_allowed_labels(
            os.getenv("BOT_ALLOWED_LABELS"),
            shadow_config.get(yml, "bot", "allowed_labels"),
            _DEFAULT_ALLOWED_LABELS,
        )

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
    """Unparseable or negative env values fall back to default (never crash
    analyze() before an artifact is written). Negatives fall back rather than
    pass through because every caller is a positive cost/wall-clock cap where a
    `-1` would disable a pre-flight defense or short-circuit the pipeline."""
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


def _scrub_attribution(val):
    """Appended to comment footers OUTSIDE the sanitize() boundary, so it must
    self-scrub to the sanitizer's standard or adopter config becomes a bypass.
    Strips newlines/backticks/comment delimiters + injection markers, and
    zero-width-breaks `@`/`#` so a typo can't fire mentions/issue-refs."""
    if not isinstance(val, str):
        return ""
    val = val.replace("\r", " ").replace("\n", " ").replace("`", "")
    val = val.replace("<!--", "").replace("-->", "")
    # Drop injection markers using the sanitizer's live list (single source of
    # truth) so this can't drift out of sync as new markers are added.
    from .sanitizer import _INJECTION_MARKERS
    lowered = val.lower()
    for marker in _INJECTION_MARKERS:
        if marker in lowered:
            val = re.sub(re.escape(marker), "", val, flags=re.IGNORECASE)
            lowered = val.lower()
    # Break GitHub auto-linking of @mentions / #issue-refs from a typo'd value.
    val = val.replace("@", "@​").replace("#", "#​")
    return val.strip()[:200]


def _scrub_label(val, default):
    """GitHub label spec: non-empty, ≤50 chars, no commas / control chars."""
    if not isinstance(val, str):
        return default
    val = "".join(c for c in val if c.isprintable() and c != ",").strip()
    if not val:
        return default
    return val[:50]


_DEFAULT_ALLOWED_LABELS = frozenset({
    "bug", "enhancement", "question", "documentation", "help-wanted",
})


def _parse_allowed_labels(env_val, yaml_val, default):
    """env (comma-separated) > yaml (list) > default. Entries are scrubbed like
    posted labels; an empty/all-garbage result falls back to `default` rather
    than silently disabling labeling."""
    items = None
    if env_val is not None and env_val.strip():
        items = env_val.split(",")
    elif isinstance(yaml_val, (list, tuple)):
        items = yaml_val
    elif isinstance(yaml_val, str) and yaml_val.strip():
        # tolerate a scalar yaml string ("python,java") like the env form
        items = yaml_val.split(",")
    if items is None:
        return set(default)
    _sentinel = object()
    scrubbed = {
        lbl for lbl in (_scrub_label(x, _sentinel) for x in items)
        if lbl is not _sentinel
    }
    return scrubbed or set(default)


def _int_in_range_or_default(val, default, *, name):
    """int/str/wrong-type → default, clamped to [0, 100]. `0` is valid ("no
    limit"; per-field meaning). Negatives fall back to default rather than
    clamp to 0, which would flip rate-limit/reply semantics to "disabled" —
    the opposite of what `-1` intends. Positives above 100 clamp to 100 (not
    default) so BOT_MAX_RUNS_PER_HOUR=200 doesn't silently revert to 20.
    `name` is required (keyword-only) so a caller can't drop the warning."""
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
    """Embedded in `<!-- {bot}:clean -->`. Rejects HTML-comment-breaking chars
    and collapses `--`/`__` runs (HTML forbids `--` inside a comment, so
    `shadow--evil` would close the envelope early). Also rejects names whose
    rendered marker would substring-match a sanitizer injection marker (only
    `system` does today) — NOT imported from sanitizer._INJECTION_MARKERS,
    since that list grows and must not retroactively invalidate a deployed
    bot_name."""
    if not isinstance(val, str):
        return default
    val = "".join(c for c in val if c.isalnum() or c in "-_")
    val = re.sub(r"-{2,}", "-", val)
    val = re.sub(r"_{2,}", "_", val)
    val = val.strip("-_")
    val = val[:32]
    if not val:
        return default
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
