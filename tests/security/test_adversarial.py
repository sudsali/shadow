"""Adversarial-payload coverage for the sanitizer.

Each payload should trigger `sanitize() returns None` — the contract for
"this string contains a credential or other secret and must not be emitted
to GitHub/Slack/the artifact." Failures here are real exfil paths.
"""
import pytest

from shadow.sanitizer import sanitize


# Database / message-queue / file-transfer URLs that carry credentials in
# the userinfo segment. The previous regex required `https?://` and let all
# of these slip through. Adding non-HTTPS schemes to the URL-with-creds
# pattern closes that gap.
SECRET_PAYLOADS = [
    "postgres://app:hunter2@db.internal:5432/prod",
    "mysql://root:s3cr3t@mysql.example.com/db",
    "mongodb://admin:supersecret@mongo:27017/",
    "redis://:redispass@redis-server:6379/0",
    "amqp://guest:guest@rabbitmq/vhost",
    # Additional schemes that the broadened pattern must also catch.
    "ftp://user:passwd@ftp.example.com/",
    "postgresql://app:hunter2@db.internal:5432/prod",
    "mongodb+srv://admin:supersecret@cluster0.mongodb.net/",
    # Embedded in prose to verify the regex anchors on `://user:pw@`,
    # not on string boundaries.
    "Connection string: postgres://app:hunter2@db.internal:5432/prod is in env.",
]


@pytest.mark.parametrize("payload", SECRET_PAYLOADS)
def test_url_with_credentials_blocks(payload):
    assert sanitize(payload) is None, (
        f"payload should have been blocked but passed through: {payload!r}"
    )


# Negative-case sanity checks: clean URLs (no credentials) must still pass.
PASS_THROUGH = [
    "postgres://db.internal:5432/prod",
    "mongodb://mongo:27017/",
    "https://example.com/path",
    "ftp://ftp.example.com/file.txt",
    # Hosts with port but no userinfo — `host:port@` is impossible because
    # the regex requires `@` after `:pw`, but verify host:port without `@`
    # passes anyway.
    "redis://redis-server:6379/0",
]


@pytest.mark.parametrize("payload", PASS_THROUGH)
def test_url_without_credentials_passes(payload):
    # Sanitize either returns the input or a transformed copy — the
    # important thing is it is not None (which signals "blocked").
    assert sanitize(payload) is not None, (
        f"clean URL must not be blocked: {payload!r}"
    )
