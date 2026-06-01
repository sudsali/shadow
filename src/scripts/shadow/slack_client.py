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
        if not self._enabled:
            return
        if self._dry_run:
            logger.info(f"[DRY RUN] Slack escalation for #{number}")
            return
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
        self._send({"text": text})

    def _send(self, payload):
        try:
            resp = requests.post(self._webhook, json=payload, timeout=10)
            if resp.status_code != 200:
                logger.error(f"Slack: {resp.status_code}")
        except Exception as e:
            logger.error(f"Slack failed: {e}")
