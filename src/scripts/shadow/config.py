import os
import sys
import logging

from . import shadow_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("shadow")


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

        _default_model = "us.anthropic.claude-opus-4-7"
        self.bedrock_model_id = _scrub_model_id(shadow_config.env_or(
            "BEDROCK_MODEL_ID",
            shadow_config.get(yml, "models", "investigator"),
            _default_model,
        ), _default_model)
        # Reporter is JSON-formatting only; a cheaper model handles structured output well enough.
        self.reporter_model_id = _scrub_model_id(shadow_config.env_or(
            "BEDROCK_REPORTER_MODEL_ID",
            shadow_config.get(yml, "models", "reporter"),
            self.bedrock_model_id,
        ), self.bedrock_model_id)
        if self.reporter_model_id != self.bedrock_model_id:
            logger.info("Reporter model override: %s (default: %s)", self.reporter_model_id, self.bedrock_model_id)
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

        self.bedrock_timeout = 240
        self.max_context_chars = 800000
        self.max_github_search_results = 8
        self.github_api_timeout = 10
        self.allowed_labels = {
            "bug", "enhancement", "question", "documentation", "help-wanted",
        }

        # Default ON. BOT_AGENT_PIPELINE=0 falls back to legacy two-phase, which
        # needs *_PROMPT env-var overrides — expect prompt_load_failed otherwise.
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
        # Non-positive would silently drop the diff; fail safe to default.
        agent_max_diff = _int_env("BOT_AGENT_MAX_DIFF_CHARS", 200_000)
        if agent_max_diff <= 0:
            logger.warning(
                "BOT_AGENT_MAX_DIFF_CHARS=%d is non-positive; using default 200_000",
                agent_max_diff,
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
    before any artifact is written."""
    try:
        return int(os.getenv(name) or default)
    except (TypeError, ValueError):
        logger.warning("Env var %s is not an integer; using default %d", name, default)
        return default


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
    """Embedded in `<!-- {bot}:clean -->`; reject HTML-comment-breaking chars."""
    if not isinstance(val, str):
        return default
    val = "".join(c for c in val if c.isalnum() or c in "-_").strip("-_")
    return val[:32] if val else default
