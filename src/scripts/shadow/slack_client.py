import logging
import requests

from .sanitizer import sanitize

logger = logging.getLogger("shadow")


class SlackClient:
    def __init__(self, cfg):
        self._webhook = cfg.slack_webhook_url
        self._enabled = cfg.enable_slack
        self._dry_run = cfg.dry_run
        self._bot_name = getattr(cfg, "bot_name", "shadow")
        self._repo = getattr(cfg, "repo", "")

    def send_escalation(self, number, title, url, labels):
        """Post an escalation to Slack. Returns True when delivery succeeded
        OR was intentionally skipped (Slack disabled / dry-run), False when a
        configured delivery failed (non-200 or transport error). The caller
        uses the bool to emit a delivery-failure metric; a skip is not a
        failure, so those return True to avoid false alarms."""
        if not self._enabled:
            return True
        if self._dry_run:
            logger.info(f"[DRY RUN] Slack escalation for #{number}")
            return True
        # Title is attacker-controllable; redact secret patterns before
        # piping to a Slack channel that may have longer retention than
        # the eventual GitHub deletion.
        scrubbed = sanitize(title)
        safe_source = scrubbed if scrubbed is not None else "[redacted by Shadow]"
        safe_title = (
            safe_source.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        label_text = ", ".join(f"`{l}`" for l in labels) if labels else "_none_"
        repo_prefix = f"{self._repo} " if self._repo else ""
        text = (
            f"*{repo_prefix}Issue #{number}*\n"
            f">{safe_title}\n\n"
            f"*Labels:* {label_text}\n"
            f"*Status:* {self._bot_name.title()} posted analysis on the issue\n\n"
            f"<{url}|View on GitHub>"
        )
        return self._send({"text": text})

    def _send(self, payload):
        """POST the payload to the webhook. Returns True on HTTP 200, False
        otherwise. Best-effort: never raises so a Slack outage can't fail the
        run (the primary escalation is the GitHub label+comment)."""
        try:
            resp = requests.post(self._webhook, json=payload, timeout=10)
            if resp.status_code != 200:
                logger.error("Slack delivery failed: HTTP %s", resp.status_code)
                return False
            return True
        except Exception as e:
            # Log ONLY the exception type, never str(e): requests embeds the
            # full request URL in transport-error messages, and the webhook's
            # /services/T.../B.../<token> path IS the bearer credential. GH
            # Actions masks the registered secret, but self-hosted runners and
            # plain-var configs would otherwise leak the token to logs. Mirrors
            # cloudwatch.py, which logs type(e).__name__ for the same reason.
            logger.error("Slack delivery failed: %s", type(e).__name__)
            return False
