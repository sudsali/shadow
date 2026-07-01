"""SlackClient escalation delivery. The Slack surface had zero test coverage;
these guard the enable/dry-run gating, secret redaction, HTML-escape, the
webhook-token log-hygiene fix (a transport error must not leak the /services/
.../<token> path), and the return-bool contract the delivery-failure metric
relies on. requests.post is always mocked — these never hit the network.
"""
import logging
import types

import pytest

from shadow import slack_client
from shadow.slack_client import SlackClient


def _cfg(**kw):
    """Minimal cfg stand-in exposing only what SlackClient reads."""
    return types.SimpleNamespace(
        slack_webhook_url=kw.get("slack_webhook_url", "https://hooks.slack.com/services/T/B/tok"),
        enable_slack=kw.get("enable_slack", True),
        dry_run=kw.get("dry_run", False),
        bot_name=kw.get("bot_name", "shadow"),
        repo=kw.get("repo", "owner/repo"),
    )


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


def test_disabled_returns_true_without_post(monkeypatch):
    # Slack off (empty webhook) is an intentional skip, not a failure — must
    # return True and never POST so the delivery-failure metric stays quiet.
    calls = []
    monkeypatch.setattr(slack_client.requests, "post",
                        lambda *a, **k: calls.append((a, k)) or _Resp(200))
    c = SlackClient(_cfg(enable_slack=False))
    assert c.send_escalation(1, "t", "https://github.com/x/y/issues/1", ["bug"]) is True
    assert calls == []


def test_dry_run_returns_true_without_post(monkeypatch):
    calls = []
    monkeypatch.setattr(slack_client.requests, "post",
                        lambda *a, **k: calls.append((a, k)) or _Resp(200))
    c = SlackClient(_cfg(dry_run=True))
    assert c.send_escalation(1, "t", "https://github.com/x/y/issues/1", []) is True
    assert calls == []


def test_success_posts_and_returns_true(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        return _Resp(200)

    monkeypatch.setattr(slack_client.requests, "post", fake_post)
    c = SlackClient(_cfg())
    ok = c.send_escalation(7, "Null deref in parser", "https://github.com/x/y/issues/7", ["bug"])
    assert ok is True
    assert captured["url"] == "https://hooks.slack.com/services/T/B/tok"
    assert "Issue #7" in captured["payload"]["text"]


def test_secret_in_title_redacted_never_posted_raw(monkeypatch):
    # A title carrying an AWS key must be redacted (sanitize() returns None on
    # a hard secret hit → "[redacted by Shadow]"), never sent verbatim.
    captured = {}
    monkeypatch.setattr(slack_client.requests, "post",
                        lambda url, json=None, timeout=None: captured.update(payload=json) or _Resp(200))
    c = SlackClient(_cfg())
    secret = "AKIA" + "A" * 16
    c.send_escalation(1, f"leak {secret} here", "https://github.com/x/y/issues/1", [])
    body = captured["payload"]["text"]
    assert secret not in body
    assert "[redacted by Shadow]" in body


def test_html_metachars_in_title_escaped(monkeypatch):
    captured = {}
    monkeypatch.setattr(slack_client.requests, "post",
                        lambda url, json=None, timeout=None: captured.update(payload=json) or _Resp(200))
    c = SlackClient(_cfg())
    c.send_escalation(1, "a < b & c > d", "https://github.com/x/y/issues/1", [])
    body = captured["payload"]["text"]
    assert "&lt;" in body and "&gt;" in body and "&amp;" in body
    assert "< b" not in body  # raw '<' must not survive


def test_non_200_returns_false(monkeypatch):
    monkeypatch.setattr(slack_client.requests, "post",
                        lambda url, json=None, timeout=None: _Resp(500))
    c = SlackClient(_cfg())
    assert c.send_escalation(1, "t", "https://github.com/x/y/issues/1", []) is False


def test_transport_error_returns_false_and_does_not_leak_token(monkeypatch, caplog):
    # THE log-hygiene fix: requests embeds the full webhook URL (incl. the
    # secret /services/.../<token> path) in its exception string. _send must
    # log only the exception type, never str(e).
    token = "SUPERSECRETTOKEN12345"
    webhook = f"https://hooks.slack.com/services/T00/B00/{token}"

    def boom(url, json=None, timeout=None):
        # Realistic requests error: the URL (with token) is in the message.
        raise ConnectionError(
            f"HTTPSConnectionPool(host='hooks.slack.com', port=443): "
            f"Max retries exceeded with url: /services/T00/B00/{token}"
        )

    monkeypatch.setattr(slack_client.requests, "post", boom)
    c = SlackClient(_cfg(slack_webhook_url=webhook))
    with caplog.at_level(logging.ERROR, logger="shadow"):
        assert c.send_escalation(1, "t", "https://github.com/x/y/issues/1", []) is False
    # The token (and the webhook path) must not appear anywhere in the logs.
    assert token not in caplog.text
    assert "/services/" not in caplog.text
    assert "ConnectionError" in caplog.text  # the type IS logged for triage
