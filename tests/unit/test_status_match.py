"""Unit tests for the permissive STATUS / VERDICT regexes.

Both _has_confirmed_findings and _critic_overturn_rate had strict line-
anchored patterns that silently dropped findings whenever the model
emitted markdown-bolded markers (`**STATUS:**`) or used `:` instead of
`|` as the id/verdict separator. These tests pin the permissive
behavior so a future tightening can't reintroduce silent skips.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "scripts"))

from shadow.main import _has_confirmed_findings, _critic_overturn_rate  # noqa: E402


# ---------- STATUS: CONFIRMED variants ----------


def test_status_confirmed_plain():
    """Original strict-form input must keep matching."""
    assert _has_confirmed_findings("STATUS: CONFIRMED")


def test_status_confirmed_markdown_bold_label():
    """Drift to `**STATUS:** CONFIRMED` must not silently skip the Critic."""
    assert _has_confirmed_findings("**STATUS:** CONFIRMED")


def test_status_confirmed_no_space_after_colon():
    """`STATUS:CONFIRMED` (no space) must match — model output is noisy."""
    assert _has_confirmed_findings("STATUS:CONFIRMED")


def test_status_confirmed_markdown_bold_value():
    """`STATUS: **CONFIRMED**` must match."""
    assert _has_confirmed_findings("STATUS: **CONFIRMED**")


def test_status_confirmed_in_paragraph():
    """Embedded in surrounding narrative is fine — the strict line-anchored
    form was the regression risk, not the relaxation."""
    text = (
        "I investigated the API surface.\n"
        "Hypothesis: race condition on shutdown.\n"
        "STATUS: CONFIRMED — see logs at line 42.\n"
    )
    assert _has_confirmed_findings(text)


def test_status_not_confirmed_does_not_match():
    """Negative cases stay negative."""
    assert not _has_confirmed_findings("STATUS: REJECTED")
    assert not _has_confirmed_findings("CONFIRMED that the build passed")
    assert not _has_confirmed_findings("")


# ---------- VERDICT: ... | UPHELD/OVERTURNED variants ----------


def test_verdict_plain_pipe_upheld():
    """Original strict-form input keeps producing the right rate."""
    text = "VERDICT: C1 | UPHELD | rationale here"
    # 1 UPHELD, 0 OVERTURNED → rate 0.0
    assert _critic_overturn_rate(text) == 0.0


def test_verdict_markdown_bold_label_upheld():
    """`**VERDICT:** C1 | UPHELD` must count."""
    text = "**VERDICT:** C1 | UPHELD | rationale"
    assert _critic_overturn_rate(text) == 0.0


def test_verdict_colon_separator_overturned():
    """`VERDICT: C1: OVERTURNED` (colon instead of pipe) must count."""
    text = "VERDICT: C1: OVERTURNED | rationale"
    assert _critic_overturn_rate(text) == 1.0


def test_verdict_lowercase_capitalization():
    """Case variants must count (re.IGNORECASE)."""
    text = "verdict: c1 | upheld | rationale"
    assert _critic_overturn_rate(text) == 0.0


def test_verdict_mixed_format_counts_each():
    """Each parseable verdict line counts; format-mixing must not silently
    drop entries."""
    text = (
        "VERDICT: C1 | UPHELD | strict form\n"
        "**VERDICT:** C2 | OVERTURNED | bold form\n"
        "VERDICT: C3: UPHELD | colon form\n"
    )
    # 2 UPHELD, 1 OVERTURNED → 1/3 = 0.333
    assert _critic_overturn_rate(text) == 0.333


def test_verdict_none_when_no_parseable_lines():
    """No verdicts → None (we don't pretend an empty run is 0% overturned)."""
    assert _critic_overturn_rate("Just some narrative without any verdicts.") is None
