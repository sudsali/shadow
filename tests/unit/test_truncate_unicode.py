"""GitHub's comment/review body cap is 65,536 UTF-16 code units, not Python
codepoints. A body composed of supplementary-plane codepoints (emoji,
ancient scripts, certain CJK extensions) consumes 2 UTF-16 units per char.
Truncating by `len(body)` undercounts by up to 2× and lets a 65k-emoji body
slip past the cap and trigger a 422 from GitHub.

These tests lock the post-truncation byte length to ≤ the documented cap
in UTF-16 LE bytes (each code unit = 2 bytes)."""
from shadow.github_client import (
    _truncate_comment_body,
    _GITHUB_COMMENT_MAX_UNITS,
    _COMMENT_TRUNCATE_AT_UNITS,
    _COMMENT_TRUNCATE_MARKER,
)


def _utf16_units(s):
    return len(s.encode("utf-16-le")) // 2


def test_short_body_passthrough():
    assert _truncate_comment_body("hello") == "hello"


def test_empty_body_passthrough():
    """Empty string returns as-is (truthy check shortcuts)."""
    assert _truncate_comment_body("") == ""


def test_none_returns_empty_string():
    """Non-string defensively coerces to ""; `requests.json` wants a str."""
    assert _truncate_comment_body(None) == ""


def test_under_cap_unmodified():
    body = "a" * (_GITHUB_COMMENT_MAX_UNITS - 100)
    assert _truncate_comment_body(body) == body


def test_at_exact_cap_passes_through():
    """`len(encoded) <= cap*2` should pass; lock equality boundary."""
    body = "a" * _GITHUB_COMMENT_MAX_UNITS  # ASCII = 1 unit each
    out = _truncate_comment_body(body)
    assert out == body  # unchanged


def test_oversize_ascii_truncated_with_marker():
    body = "a" * (_GITHUB_COMMENT_MAX_UNITS + 1000)
    out = _truncate_comment_body(body)
    assert out.endswith(_COMMENT_TRUNCATE_MARKER)
    # Total UTF-16 units must be ≤ cap + marker units.
    total_units = _utf16_units(out)
    assert total_units <= _COMMENT_TRUNCATE_AT_UNITS + _utf16_units(_COMMENT_TRUNCATE_MARKER)


def test_emoji_truncation_respects_utf16():
    """Emoji like U+1F525 ("fire") encodes as a UTF-16 surrogate pair (2
    code units). A naive `body[:64000]` slice that uses Python codepoint
    indexing would still fit 64,000 emoji = 128,000 UTF-16 units → 422.
    UTF-16-aware truncation must keep the result under 65,536 units."""
    body = "\U0001F525" * 40_000  # 40,000 emoji = 80,000 UTF-16 units
    out = _truncate_comment_body(body)
    # The truncated portion plus the marker must stay under the GitHub cap.
    assert _utf16_units(out) <= _GITHUB_COMMENT_MAX_UNITS


def test_truncation_does_not_split_surrogate_pair():
    """Slicing UTF-16 bytes mid-surrogate would produce an invalid char.
    `decode("utf-16-le", errors="ignore")` drops the orphan surrogate
    rather than emitting U+FFFD; either way the result must be valid."""
    # Construct a body where the cut point falls in the middle of a
    # surrogate pair: 31,999 emoji = 63,998 units. Cap is 64,000 units →
    # cut sits exactly between codepoint pairs, so we go one unit past
    # to land mid-surrogate.
    body = "\U0001F525" * 32_001  # 64,002 units; cuts mid-surrogate
    out = _truncate_comment_body(body)
    # Must round-trip through encode/decode without raising.
    out.encode("utf-16-le").decode("utf-16-le", errors="ignore")
    assert _utf16_units(out) <= _GITHUB_COMMENT_MAX_UNITS


def test_mixed_ascii_and_emoji_under_cap():
    """ASCII + emoji body where total UTF-16 units exceed 65,536."""
    body = ("\U0001F525" * 30_000) + ("a" * 10_000)  # 70,000 units
    out = _truncate_comment_body(body)
    assert _utf16_units(out) <= _GITHUB_COMMENT_MAX_UNITS
    assert out.endswith(_COMMENT_TRUNCATE_MARKER)
