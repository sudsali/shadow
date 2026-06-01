"""Replay bench: captured Critic + Reporter outputs from past runs are
replayed through the deterministic post-processing layer on every CI
build.

Not a prompt regression test (that needs Bedrock + budget). Catches
parser, formatter, dropped-field, sanitizer, and refutation-trail
regressions on a fixed corpus. Add a fixture when a new bug class
surfaces in production so the same regression can't ship twice."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "src" / "scripts"))

from shadow.main import (  # noqa: E402
    _critic_overturn_rate,
    _filter_and_format_inline_comments,
    _format_refutation_trail,
    _parse_reporter_output,
)


# ---- helpers ----


def _read(name):
    return (FIXTURES / name).read_text()


def _read_json(name):
    return json.loads(_read(name))


# ---- deequ-722: real bug, real disprove, real UPHELD ----


def test_deequ_722_critic_overturn_rate():
    text = _read("deequ-722-critic.txt")
    rate = _critic_overturn_rate(text)
    # 3 verdicts: 1 UPHELD, 2 OVERTURNED → 2/3 = 0.667
    assert rate == 0.667


def test_deequ_722_reporter_parses_with_one_kept_finding():
    raw = _read("deequ-722-reporter.json")
    comments, ok = _parse_reporter_output(raw)
    assert ok
    assert len(comments) == 1
    assert comments[0]["file"].endswith("StateProvider.scala")
    assert comments[0]["line"] == 124
    assert comments[0]["severity"] == "BUG"


def test_deequ_722_filter_renders_full_comment_with_trail():
    raw = _read("deequ-722-reporter.json")
    comments, _ = _parse_reporter_output(raw)
    kept = _filter_and_format_inline_comments(
        comments, incremental_files=set(), is_pr_update=False,
    )
    assert len(kept) == 1
    body = kept[0]["comment"]
    assert "**BUG**:" in body
    assert "Unguarded cast" in body
    assert "> Line 124 calls state.asInstanceOf" in body
    assert "<details>" in body
    assert "Hypothesis (Investigator)" in body
    assert "Disprove attempt (Critic)" in body
    assert "hypothesis" not in kept[0]
    assert "falsification_attempt" not in kept[0]
    assert "evidence" not in kept[0]


# ---- deequ-clean: nothing to post ----


def test_deequ_clean_critic_overturn_rate_none():
    """No verdicts → None (not zero — we don't pretend an empty run is
    a 0% overturn rate)."""
    text = _read("deequ-clean-critic.txt")
    assert _critic_overturn_rate(text) is None


def test_deequ_clean_reporter_parses_to_empty():
    raw = _read("deequ-clean-reporter.json")
    comments, ok = _parse_reporter_output(raw)
    assert ok
    assert comments == []


def test_deequ_clean_filter_yields_no_comments():
    comments, _ = _parse_reporter_output(_read("deequ-clean-reporter.json"))
    kept = _filter_and_format_inline_comments(
        comments, incremental_files=set(), is_pr_update=False,
    )
    assert kept == []


# ---- robustness: malformed model output never crashes the pipeline ----


@pytest.mark.parametrize("raw,parse_ok,expected_count", [
    ("hello world", False, 0),
    ("[]", False, 0),
    ("{}", True, 0),
    ('{"analysis": "a string"}', False, 0),
    ('{"analysis": []}', True, 0),
])
def test_reporter_parser_robust_against_malformed(raw, parse_ok, expected_count):
    comments, ok = _parse_reporter_output(raw)
    assert ok is parse_ok
    assert len(comments) == expected_count


def test_reporter_parser_drops_malformed_finding_keeps_good_ones():
    raw = json.dumps({
        "analysis": [
            None,                                             # wrong type
            {"file": "a.py", "line": 1, "disproved": False,
             "finding": {"severity": "BUG", "comment": "ok",
                         "evidence": ""}},
            {"finding": "not-a-dict"},                        # bad inner shape
            "string-instead-of-object",
        ],
    })
    comments, ok = _parse_reporter_output(raw)
    assert ok
    assert len(comments) == 1
    assert comments[0]["file"] == "a.py"


# ---- regression specifics ----


def test_line_field_rejects_bool_true():
    """isinstance(True, int) is True; without an explicit bool reject,
    {"line": true} would post on line 1 of every file."""
    comments = [{
        "file": "src/x.py",
        "line": True,  # not 1!
        "severity": "BUG",
        "comment": "x",
    }]
    kept = _filter_and_format_inline_comments(
        comments, incremental_files=set(), is_pr_update=False,
    )
    assert kept == []


def test_nit_dropped_on_pr_update():
    """is_pr_update=True drops NIT severity (author already saw on first
    pass)."""
    comments = [
        {"file": "x.py", "line": 1, "severity": "NIT", "comment": "n"},
        {"file": "x.py", "line": 2, "severity": "BUG", "comment": "b"},
    ]
    kept = _filter_and_format_inline_comments(
        comments, incremental_files=set(), is_pr_update=True,
    )
    assert len(kept) == 1
    assert kept[0]["severity"] == "BUG"


def test_incremental_files_filter_drops_unchanged_files():
    comments = [
        {"file": "a.py", "line": 1, "severity": "BUG", "comment": "1"},
        {"file": "b.py", "line": 1, "severity": "BUG", "comment": "2"},
    ]
    kept = _filter_and_format_inline_comments(
        comments, incremental_files={"a.py"}, is_pr_update=True,
    )
    assert len(kept) == 1
    assert kept[0]["file"] == "a.py"


def test_refutation_trail_omitted_when_no_disprove_data():
    comments = [{
        "file": "x.py", "line": 1, "severity": "BUG", "comment": "x",
    }]
    kept = _filter_and_format_inline_comments(
        comments, incremental_files=set(), is_pr_update=False,
    )
    assert "<details>" not in kept[0]["comment"]
    assert "Refutation trail" not in kept[0]["comment"]


def test_refutation_trail_present_on_real_finding():
    out = _format_refutation_trail("h", "f")
    assert "OVERTURNED" in out  # explains the disprove pattern
    assert "<details>" in out
