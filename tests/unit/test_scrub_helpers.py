"""Pure helpers that gate adopter-supplied values into GitHub / markdown / Bedrock.

Adopter-typo isolation matters here: a malformed `.shadow.yml` should fall
back to safe defaults, not crash the workflow or escape into prose.
"""
import pytest

from shadow.config import (
    _scrub_codeblock_token,
    _scrub_label,
    _scrub_marker_token,
    _str_or_default,
)


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
