import re
import logging

logger = logging.getLogger("shadow")

# (pattern, category) — category is published in security_events; pattern
# isn't, since the matched substring itself is the leaked value.
_SECRET_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "aws_access_key"),
    # ASIA = AWS STS session-token prefix, distinct from long-lived AKIA.
    (re.compile(r"ASIA[0-9A-Z]{16}"), "aws_sts_token"),
    (re.compile(r"ghp_[0-9a-zA-Z]{36}"), "github_pat"),
    (re.compile(r"gho_[0-9a-zA-Z]{36}"), "github_pat"),
    (re.compile(r"ghs_[0-9a-zA-Z]{36}"), "github_pat"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{22,}"), "github_pat"),
    (re.compile(r"xox[bpras]-[A-Za-z0-9\-]+"), "slack_token"),
    (re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"), "anthropic_key"),
    (re.compile(r"sk_live_[A-Za-z0-9]{24,}"), "stripe_key"),
    (re.compile(r"AWS_SECRET_ACCESS_KEY\s*[=:]\s*\S+"), "aws_secret_assignment"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private_key_header"),
    (re.compile(r"AccountKey\s*=\s*[A-Za-z0-9+/=]{40,}"), "azure_storage_key"),
    (re.compile(r"_authToken\s*=\s*\S+"), "npm_authtoken"),
    # URL with embedded credentials: scheme://[user]:pw@host (any scheme, to
    # catch db URLs like postgres://). User is optional so `redis://:pw@host`
    # matches; the `scheme://` anchor avoids false positives on prose like
    # `meeting at 12:30@home`.
    (re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/:]*:[^\s/@]+@"), "url_with_credentials"),
    # JWT (header.payload.signature). Real tokens have base64url segments
    # of 16+ chars — the length floor avoids matching prose like
    # `eyJabc.field.method`. Allow whitespace between segments for tokens
    # wrapped across lines in copy-pasted logs.
    (re.compile(r"eyJ[A-Za-z0-9_\-]{16,}\s*\.\s*[A-Za-z0-9_\-]{8,}\s*\.\s*[A-Za-z0-9_\-]{8,}"), "jwt"),
    (re.compile(r"https?://[^\s]*\.corp\.amazon\.com[^\s]*"), "internal_amazon_url"),
    (re.compile(r"https?://[^\s]*\.a2z\.com[^\s]*"), "internal_amazon_url"),
    (re.compile(r"https?://[^\s]*\.amazon\.dev[^\s]*"), "internal_amazon_url"),
    (re.compile(r"hooks\.slack\.com/services/\S+"), "slack_webhook"),
]

_INJECTION_MARKERS = [
    "my system prompt is",
    "my instructions are",
    "here are my internal",
    "ignore previous instructions",
    "system:",
    "</user>",
    "developer message:",
    "<|im_start|>",
    "<|im_end|>",
]


# Module-global counter; reset_security_events() must be called at the
# start of each run or events leak across processes (e.g., between tests,
# between analyze invocations sharing one Python process).
_security_events = []


def get_security_events():
    """Return a copy of the events recorded since last reset()."""
    return list(_security_events)


def reset_security_events():
    _security_events.clear()


def _record_event(kind, detail):
    # Only category + count — never publish the matched value itself, since
    # that's the secret the filter is supposed to block.
    _security_events.append({
        "kind": kind,
        "detail": detail,
    })


def sanitize(text):
    if not isinstance(text, str):
        return ""
    if not text:
        return text
    for p, category in _SECRET_PATTERNS:
        if p.search(text):
            logger.error(f"BLOCKED: secret pattern {category}")
            _record_event("secret_blocked", category)
            return None
    lower = text.lower()
    for m in _INJECTION_MARKERS:
        if m in lower:
            logger.error(f"BLOCKED: injection marker '{m}'")
            _record_event("injection_blocked", m)
            return None
    text = _fix_accidental_issue_refs(text)
    return text


def _fix_accidental_issue_refs(text):
    """Wrap #N references outside code blocks in backticks to prevent GitHub auto-linking."""
    lines = text.split("\n")
    in_code_block = False
    fixed = []
    for line in lines:
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
        if not in_code_block:
            line = re.sub(r'(?<!`)#(\d+)(?!\w)(?!`)', r'`#\1`', line)
        fixed.append(line)
    return "\n".join(fixed)
