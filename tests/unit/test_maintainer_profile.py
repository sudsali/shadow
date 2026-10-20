"""Maintainer review profile, line ranges and within-PR dedup.

The profile is opt-in: `precise` keeps v1.9 behavior. Line ranges and dedup apply to both profiles.
"""
import importlib
import json
import tempfile
from pathlib import Path

import pytest

from shadow import agent_loop, main, prompts

REQUIRED_ENV = {"GITHUB_TOKEN": "fake-token", "EVENT_TYPE": "pull_request_target", "ISSUE_NUMBER": "42",
                "GITHUB_REPOSITORY": "owner/repo", "BOT_REQUIRE_GUARDRAIL": "false"}
NOTES = (
    "ID: C1\nFILE: src/a.py\nLINE: 10-14\nSTATUS: CONFIRMED\nSEVERITY: BUG\nCONFIDENCE: 9\n\n"
    "ID: C2\nFILE: src/a.py\nLINE: 40\nSTATUS: CONFIRMED\nSEVERITY: DESIGN\nCONFIDENCE: 7\n\n"
    "ID: C3\nFILE: `src/b.py`\nLINE: 5\nSTATUS: CONFIRMED\nSEVERITY: DESIGN\nCONFIDENCE: 9\n"
)


def _config(monkeypatch, **env):
    for k, v in {**REQUIRED_ENV, **env}.items():
        monkeypatch.setenv(k, v)
    importlib.reload(__import__("shadow.config", fromlist=["Config"]))
    from shadow.config import Config
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("SHADOW_REPO_ROOT", tmp)
        return Config()


def _reporter_json(*items):
    finding = {"severity": "BUG", "comment": "c", "evidence": "e"}
    return json.dumps({"analysis": [{"file": "a.py", "hypothesis": "h", "falsification_attempt": "f",
                                     "disproved": False, "finding": finding, **i} for i in items]})


def test_reporter_start_line_kept_only_when_before_line():
    out, ok = main._parse_reporter_output(_reporter_json({"line": 20, "start_line": 12}, {"line": 20, "start_line": 25},
                                                         {"line": 20, "start_line": True}, {"line": 5}))
    assert ok
    assert [c.get("start_line") for c in out] == [12, None, None, None]


def test_line_ranges_restored_from_investigator_notes():
    out = main._attach_line_ranges([{"file": "src/a.py", "line": 10}, {"file": "src/a.py", "line": 14},
                                    {"file": "src/b.py", "line": 5}], NOTES)
    assert [(c.get("start_line"), c["line"]) for c in out] == [(10, 14), (10, 14), (None, 5)]


def test_dedupe_keeps_the_more_severe_overlapping_finding_in_original_order():
    comments = [{"file": "a.py", "line": 12, "severity": "DESIGN", "id": 1},
                {"file": "a.py", "line": 14, "start_line": 10, "severity": "BUG", "id": 2},
                {"file": "a.py", "line": 30, "severity": "EDGE_CASE", "id": 3},
                {"file": "b.py", "line": 12, "severity": "NIT", "id": 4}]
    assert [c["id"] for c in main._dedupe_findings(comments)] == [2, 3, 4]


def test_dedupe_matches_paths_after_canonicalizing():
    comments = [{"file": "./a.py", "line": 10, "severity": "BUG"}, {"file": "a.py", "line": 10, "severity": "NIT"}]
    assert len(main._dedupe_findings(comments)) == 1


def test_postprocess_applies_the_severity_floor_only_under_the_maintainer_profile():
    comments = [{"file": "src/a.py", "line": 14, "severity": "BUG"}, {"file": "src/a.py", "line": 50, "severity": "MISSING_TEST"}]
    assert len(main._postprocess_findings(_Cfg("precise"), comments, NOTES)) == 2
    kept = main._postprocess_findings(_Cfg("maintainer"), comments, NOTES)
    assert [(c["severity"], c.get("start_line"), c["line"]) for c in kept] == [("BUG", 10, 14)]


def test_dedupe_keeps_findings_more_than_two_lines_apart():
    comments = [{"file": "a.py", "line": 10, "severity": "BUG"}, {"file": "a.py", "line": 13, "severity": "BUG"}]
    assert len(main._dedupe_findings(comments)) == 2


def test_severity_floor_posts_defects_and_confident_design_only():
    comments = [{"file": "src/a.py", "line": 14, "start_line": 10, "severity": "BUG"},
                {"file": "src/a.py", "line": 40, "severity": "DESIGN"},
                {"file": "src/b.py", "line": 5, "severity": "DESIGN"},
                {"file": "src/a.py", "line": 50, "severity": "MISSING_TEST"},
                {"file": "src/a.py", "line": 60, "severity": "NIT"},
                {"file": "src/c.py", "line": 1, "severity": "EDGE_CASE"}]
    kept = main._apply_severity_floor(comments, NOTES)
    assert [(c["file"], c["severity"]) for c in kept] == [("src/a.py", "BUG"), ("src/b.py", "DESIGN"), ("src/c.py", "EDGE_CASE")]


DIFF = "".join(
    f"diff --git a/{p} b/{p}\n{extra}--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-x\n+{'y' * size}\n"
    for p, extra, size in (("src/a.py", "", 5), ("tests/test_a.py", "", 5), ("docs/guide.md", "", 5),
                           ("src/gone.py", "deleted file mode 100644\n", 5), ("src/big.py", "", 50),
                           ("qa/check.py", "", 5)))


def test_diff_chunks_skip_tests_docs_deletions_and_test_dir():
    assert [p for p, _ in main._diff_file_chunks(DIFF, test_dir="qa")] == ["src/a.py", "src/big.py"]


def test_diff_chunks_keep_largest_files_over_the_cap():
    assert [p for p, _ in main._diff_file_chunks(DIFF, max_files=1)] == ["src/big.py"]


class _Cfg:
    def __init__(self, profile):
        self.review_profile, self.codebase_test_dir, self.per_file_max_files = profile, "", 12


def _fake_run(calls):
    def run(**kwargs):
        calls.append(kwargs["user_prompt"])
        return agent_loop.AgentResult(text="ID: C1\nFILE: x\n", turns=2, tool_calls=3, input_tokens=10)
    return run


def test_precise_profile_runs_one_investigator_loop(monkeypatch):
    calls = []
    monkeypatch.setattr(agent_loop, "run", _fake_run(calls))
    main._run_investigator(_Cfg("precise"), DIFF, user_prompt="<diff>all</diff>", pipeline_deadline=None)
    assert calls == ["<diff>all</diff>"]


def test_maintainer_profile_runs_one_loop_per_product_file_and_merges(monkeypatch):
    calls = []
    monkeypatch.setattr(agent_loop, "run", _fake_run(calls))
    result = main._run_investigator(_Cfg("maintainer"), DIFF, user_prompt="unused", pipeline_deadline=None)
    assert len(calls) == 3 and calls[0].startswith("Investigate only src/a.py.")
    assert result.turns == 6 and result.tool_calls == 9 and result.input_tokens == 30
    assert "## Scope: src/big.py" in result.text and "ID: F2-C1" in result.text


def test_maintainer_profile_stops_starting_loops_near_the_deadline(monkeypatch):
    calls = []
    monkeypatch.setattr(agent_loop, "run", _fake_run(calls))
    monkeypatch.setattr(main.time, "monotonic", lambda: 1000.0)
    main._run_investigator(_Cfg("maintainer"), DIFF, user_prompt="unused", pipeline_deadline=1000.0 + 100)
    assert calls == []


def test_profile_config_defaults_and_validation(monkeypatch):
    monkeypatch.delenv("BOT_REVIEW_PROFILE", raising=False)
    monkeypatch.delenv("BOT_PIPELINE_WALL_CLOCK_S", raising=False)
    cfg = _config(monkeypatch)
    assert (cfg.review_profile, cfg.pipeline_wall_clock_seconds) == ("precise", 480)
    cfg = _config(monkeypatch, BOT_REVIEW_PROFILE="Maintainer")
    assert (cfg.review_profile, cfg.pipeline_wall_clock_seconds) == ("maintainer", 780)
    assert _config(monkeypatch, BOT_REVIEW_PROFILE="bogus").review_profile == "precise"


def test_maintainer_profile_swaps_bundled_prompts_but_overrides_win(monkeypatch):
    for var in ("PR_CRITIC_PROMPT", "SM_PR_CRITIC_PROMPT"):
        monkeypatch.delenv(var, raising=False)
    try:
        prompts.set_profile("maintainer")
        assert prompts._resolve_prompt("PR_CRITIC_PROMPT", "pr-critic.txt")[1] == "file:prompts/pr-critic-maintainer.txt"
        assert "Your default verdict is UPHELD" in prompts.get_pr_critic_prompt()
        assert prompts._resolve_prompt("PR_INVESTIGATOR_PROMPT", "pr-investigator.txt")[1] == "file:prompts/pr-investigator.txt"
        monkeypatch.setenv("PR_CRITIC_PROMPT", "adopter prompt")
        assert prompts._resolve_prompt("PR_CRITIC_PROMPT", "pr-critic.txt") == ("adopter prompt", "env:PR_CRITIC_PROMPT")
    finally:
        prompts.set_profile("precise")
    assert prompts._resolve_prompt("PR_REPORTER_PROMPT", "pr-reporter.txt")[1] == "file:prompts/pr-reporter.txt"


def test_maintainer_profile_reads_its_own_secret_then_the_bundled_prompt(monkeypatch):
    secrets = {"bot/pr-critic-prompt": "strict base", "bot/pr-critic-maintainer-prompt": "maintainer secret",
               "bot/pr-investigator-prompt": "adopter investigator"}
    monkeypatch.setattr(prompts, "_read_from_sm", lambda name: secrets.get(name, ""))
    monkeypatch.delenv("PR_CRITIC_PROMPT", raising=False)
    monkeypatch.delenv("PR_REPORTER_PROMPT", raising=False)
    monkeypatch.setenv("SM_PR_CRITIC_PROMPT", "bot/pr-critic-prompt")
    monkeypatch.setenv("SM_PR_REPORTER_PROMPT", "bot/pr-reporter-prompt")
    monkeypatch.setenv("SM_PR_INVESTIGATOR_PROMPT", "bot/pr-investigator-prompt")
    assert prompts._resolve_prompt("PR_CRITIC_PROMPT", "pr-critic.txt") == ("strict base", "sm:bot/pr-critic-prompt")
    try:
        prompts.set_profile("maintainer")
        assert prompts._resolve_prompt("PR_CRITIC_PROMPT", "pr-critic.txt") == ("maintainer secret", "sm:bot/pr-critic-maintainer-prompt")
        text, source = prompts._resolve_prompt("PR_REPORTER_PROMPT", "pr-reporter.txt")
        assert source == "file:prompts/pr-reporter-maintainer.txt" and text.strip()
        assert prompts._resolve_prompt("PR_INVESTIGATOR_PROMPT", "pr-investigator.txt") == ("adopter investigator", "sm:bot/pr-investigator-prompt")
    finally:
        prompts.set_profile("precise")


def test_maintainer_prompt_files_exist_for_every_swap():
    root = Path(prompts._PROMPTS_DIR)
    for name in prompts._PROFILE_FILES["maintainer"].values():
        assert (root / name).read_text().strip()


def test_formatted_comments_use_the_profile_wording():
    finding = {"file": "a.py", "line": 3, "severity": "BUG", "comment": "c", "hypothesis": "h", "falsification_attempt": "f"}
    keep = main._filter_and_format_inline_comments([dict(finding)], incremental_files=set(), is_pr_update=False, profile="maintainer")
    strict = main._filter_and_format_inline_comments([dict(finding)], incremental_files=set(), is_pr_update=False)
    assert "keeps a finding unless" in keep[0]["comment"] and "default verdict is OVERTURNED" in strict[0]["comment"]


def test_refutation_trail_wording_follows_profile():
    assert "default verdict is OVERTURNED" in main._format_refutation_trail("h", "f")
    keep = main._format_refutation_trail("h", "f", keep_by_default=True)
    assert "keeps a finding unless" in keep and "OVERTURNED" not in keep


@pytest.fixture
def gh(monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", "/tmp")
    from shadow.github_client import GitHubClient

    class Cfg:
        github_token, repo, github_api_timeout, dry_run, codebase_src_dir, codebase_file_ext = "x", "o/r", 1, False, ".", ".py"
    g = GitHubClient(Cfg())
    monkeypatch.setattr(g, "_get_valid_diff_lines", lambda number: {"a.py": {10, 11, 12, 20, 21}})
    return g


def test_review_posts_multi_line_comment_only_within_one_hunk(monkeypatch, gh):
    from shadow import github_client as gc
    captured = {}

    class Resp:
        status_code, text = 201, ""

        def json(self):
            return {"id": 1}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        return Resp()
    monkeypatch.setattr(gc, "_retry_request", lambda fn: fn())
    monkeypatch.setattr(gc.requests, "post", fake_post)
    gh.post_pr_review(1, "summary", [{"file": "a.py", "line": 12, "start_line": 10, "comment": "in hunk"},
                                     {"file": "a.py", "line": 20, "start_line": 12, "comment": "crosses hunks"}])
    first, second = captured["payload"]["comments"]
    assert (first["start_line"], first["start_side"], first["line"]) == (10, "RIGHT", 12)
    assert "start_line" not in second and second["line"] == 20
