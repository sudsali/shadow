"""Pure helpers that gate adopter-supplied values into GitHub / markdown / Bedrock.

Adopter-typo isolation matters here: a malformed `.shadow.yml` should fall
back to safe defaults, not crash the workflow or escape into prose.
"""
import pytest

from shadow.config import (
    _scrub_attribution,
    _scrub_codeblock_token,
    _scrub_label,
    _scrub_marker_token,
    _str_or_default,
)


class TestScrubAttribution:
    def test_empty_default(self):
        assert _scrub_attribution("") == ""

    def test_non_string(self):
        assert _scrub_attribution(None) == ""
        assert _scrub_attribution(123) == ""

    def test_clean_value_preserved(self):
        # A normal one-line attribution (incl. a URL and a middot) survives.
        v = "Reviewed by Shadow · github.com/sudsali/shadow"
        assert _scrub_attribution(v) == v

    def test_strips_newlines_backticks_and_comment_delims(self):
        # Newlines collapse to spaces (stay one line); backticks + HTML-comment
        # delimiters removed so it can't break the footer or the clean-marker.
        out = _scrub_attribution("evil --> <!-- x `code`\nsecond")
        assert "\n" not in out
        assert "`" not in out
        assert "<!--" not in out and "-->" not in out

    def test_length_capped(self):
        assert len(_scrub_attribution("x" * 500)) == 200

    @pytest.mark.parametrize("marker", [
        "system:", "</user>", "ignore previous instructions",
        "my instructions are", "<|im_start|>",
    ])
    def test_injection_markers_removed(self, marker):
        # Attribution is appended OUTSIDE the sanitize() boundary, so it must
        # self-scrub the same markers — config must not become a way to slip an
        # injection marker into every posted comment.
        from shadow.sanitizer import sanitize
        out = _scrub_attribution(f"Reviewed by Shadow {marker} tail")
        assert marker.lower() not in out.lower()
        # And the scrubbed result must not itself trip the sanitizer.
        assert sanitize(out) is not None

    def test_at_and_hash_neutralized(self):
        # A typo'd value must not fire GitHub @mentions or #issue auto-links.
        out = _scrub_attribution("@maintainers see #555")
        assert "@​" in out and "#​" in out  # zwj-broken
        assert "@maintainers" not in out and "#555" not in out


@pytest.mark.parametrize("val,default,expected", [
    ("foo", ".", "foo"),
    ("", ".", ""),
    (None, ".", "."),
    (123, ".", "."),
    (True, ".", "."),
    ([], ".", "."),
])
def test_str_or_default(val, default, expected):
    assert _str_or_default(val, default) == expected


class TestScrubCodeblockToken:
    def test_clean_value(self):
        assert _scrub_codeblock_token("python") == "python"

    def test_strips_backticks_and_newlines(self):
        # ` and \n would close a markdown ```{lang}``` fence and leak content.
        assert _scrub_codeblock_token("\n```python\n") == "python"

    def test_strips_cr(self):
        assert _scrub_codeblock_token("py\rthon") == "python"

    def test_non_str_returns_empty(self):
        assert _scrub_codeblock_token(123) == ""
        assert _scrub_codeblock_token(None) == ""
        assert _scrub_codeblock_token(["python"]) == ""

    def test_strips_outer_whitespace(self):
        assert _scrub_codeblock_token("  python  ") == "python"


class TestScrubLabel:
    def test_clean_value(self):
        assert _scrub_label("needs-human", "d") == "needs-human"

    def test_rejects_non_str(self):
        assert _scrub_label(123, "d") == "d"
        assert _scrub_label(None, "d") == "d"
        assert _scrub_label(["a"], "d") == "d"

    def test_strips_control_chars_and_commas(self):
        # Newline / comma in a label name → GitHub Labels API 422.
        assert _scrub_label("a,b\n c", "d") == "ab c"

    def test_truncates_at_50(self):
        assert _scrub_label("x" * 60, "d") == "x" * 50

    def test_whitespace_only_returns_default(self):
        assert _scrub_label("   ", "d") == "d"
        assert _scrub_label("\n\t,", "d") == "d"

    def test_keeps_emoji(self):
        assert _scrub_label("urgent-🚨", "d") == "urgent-🚨"


class TestScrubMarkerToken:
    def test_clean_value(self):
        assert _scrub_marker_token("shadow", "d") == "shadow"

    def test_keeps_hyphens_and_underscores(self):
        assert _scrub_marker_token("my-bot_2", "d") == "my-bot_2"

    def test_strips_html_breakers(self):
        # < and > would escape `<!-- {bot}:clean -->` into real PR text.
        assert _scrub_marker_token("a><script>", "d") == "ascript"

    def test_strips_leading_trailing_dashes(self):
        # `--` is special inside HTML comments.
        assert _scrub_marker_token("--evil--", "d") == "evil"

    def test_non_str_returns_default(self):
        assert _scrub_marker_token(123, "d") == "d"
        assert _scrub_marker_token(None, "d") == "d"

    def test_empty_returns_default(self):
        assert _scrub_marker_token("", "d") == "d"
        assert _scrub_marker_token("---", "d") == "d"

    def test_truncates(self):
        assert _scrub_marker_token("x" * 50, "d") == "x" * 32

    def test_rejects_name_that_trips_sanitizer(self):
        # bot_name='system' would render `<!-- system:clean -->` whose
        # `system:` substring matches sanitizer's _INJECTION_MARKERS list,
        # ESCALATING every clean PR review silently. Reject at config-load.
        assert _scrub_marker_token("system", "shadow") == "shadow"
        assert _scrub_marker_token("my-system", "shadow") == "shadow"
