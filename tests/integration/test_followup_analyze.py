"""End-to-end analyze() coverage for the issue and issue_comment (followup)
surfaces.

Before this, only the leaf helpers (_bot_reply_count, _already_replied_to_latest,
_user_dissatisfied) were unit-tested; neither surface had a test driving
analyze() end-to-end. That left the guard *ordering*, the per-branch artifact
fields, the escalate-once dedup, the fail-closed prompt_load_failed paths, and
the issue-respond second pass (prompt_id re-attribution, invoke-failure
handling) untested — a reorder, off-by-one, or a dropped fail-closed guard
would ship green. These lock that behavior in as a regression tripwire.

All AWS/GitHub clients are faked; nothing hits the network.
"""
import json

import pytest

from shadow import main as main_mod
from shadow import prompts as prompts_mod


BOT_ACTOR = "github-actions[bot]"
HAIKU = "us.anthropic.claude-haiku"


def _comment(login, body=""):
    return {"user": {"login": login}, "body": body}


class _FakeGH:
    def __init__(self, item, comments, files=None):
        self._item = item
        self._comments = comments
        self._files = files or {}
    def get_issue(self, number):
        return self._item
    def get_pr(self, number):
        return self._item
    def get_comments(self, number, max_pages=10):
        return self._comments
    def count_recent_workflow_runs_for_item(self, number):
        return 0  # never rate-limited in these tests
    def get_codebase_map(self):
        return ""
    def read_local_file(self, path):
        return self._files.get(path, "")
    def get_file_content(self, path, repo=None):
        return self._files.get(path, "")


class _FakeBedrock:
    """Returns queued responses in order (one per invoke call). Default queue is
    empty → invoke raises, so a test that unexpectedly reaches Bedrock fails
    loudly rather than passing silently. Set `responses` to a list where each
    element is a JSON string (a normal return) or None (a simulated failure)."""
    responses = []
    throttled = False
    failed_model = None

    def __init__(self, cfg):
        self._queue = list(type(self).responses)
    def last_failed_model(self):
        return type(self).failed_model
    def last_failure_was_throttle(self):
        return type(self).throttled
    def invoke(self, *a, **k):
        if not self._queue:
            raise AssertionError("bedrock.invoke reached with no queued response")
        return self._queue.pop(0)


class _FakeKB:
    def __init__(self, cfg):
        pass
    def load(self):
        pass
    def build_context(self, text):
        return ""


@pytest.fixture
def _run(monkeypatch, tmp_path):
    """Drive analyze() with faked clients and return the written artifact dict.

    event_type defaults to issue_comment (followup); pass "issues" for the
    issue-classification surface. `bedrock_responses` queues invoke() returns
    (JSON string or None); default [] means "must not reach Bedrock".
    """
    def run(item, comments, *, event_type="issue_comment", files=None,
            bedrock_responses=None, throttled=False, failed_model=None):
        artifact_path = tmp_path / "result.json"
        monkeypatch.setenv("ARTIFACT_PATH", str(artifact_path))
        # ARTIFACT_PATH is read into a module global at import; setattr (not a
        # bare assignment) so pytest restores it after the test and state can't
        # leak into the next test.
        monkeypatch.setattr(main_mod, "ARTIFACT_PATH", str(artifact_path))
        monkeypatch.setenv("GITHUB_TOKEN", "fake")
        monkeypatch.setenv("EVENT_TYPE", event_type)
        monkeypatch.setenv("EVENT_ACTION", "created")
        monkeypatch.setenv("ISSUE_NUMBER", "42")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        monkeypatch.setenv("BOT_REQUIRE_GUARDRAIL", "false")
        monkeypatch.setenv("SHADOW_REPO_ROOT", str(tmp_path))
        # Keep the artifact write hermetic — no CloudWatch network attempt.
        monkeypatch.setenv("SHADOW_CLOUDWATCH_DISABLED", "true")

        monkeypatch.setattr(_FakeBedrock, "responses", bedrock_responses or [])
        monkeypatch.setattr(_FakeBedrock, "throttled", throttled)
        monkeypatch.setattr(_FakeBedrock, "failed_model", failed_model)
        monkeypatch.setattr(main_mod, "GitHubClient",
                            lambda cfg: _FakeGH(item, comments, files))
        monkeypatch.setattr(main_mod, "BedrockClient", _FakeBedrock)
        monkeypatch.setattr(main_mod, "KnowledgeBase", _FakeKB)

        main_mod.analyze()
        return json.loads(artifact_path.read_text())
    return run


def _item(labels=None, state="open", author="human"):
    return {
        "user": {"login": author},
        "state": state,
        "title": "Something broke",
        "body": "details",
        "html_url": "https://github.com/owner/repo/issues/42",
        "labels": [{"name": n} for n in (labels or [])],
    }


# --------------------------------------------------------------------------- #
# Followup (issue_comment) guard chain
# --------------------------------------------------------------------------- #


def test_bot_last_comment_skips(_run):
    # Last comment is the bot's own → SKIP, don't reply to yourself.
    art = _run(_item(), [_comment("human", "hi"), _comment(BOT_ACTOR, "reply")])
    assert art["action"] == "SKIP"
    assert art["reason"] == "bot_last_comment"


def test_max_replies_reached_escalates_once(_run):
    # Two bot replies already (cap=2) and a fresh user comment → ESCALATE.
    comments = [_comment(BOT_ACTOR, "r1"), _comment(BOT_ACTOR, "r2"), _comment("human", "again")]
    art = _run(_item(), comments)
    assert art["action"] == "ESCALATE"
    assert art["reason"] == "max_replies_reached"


def test_max_replies_already_escalated_skips(_run):
    # G7 dedup: same capped thread but the escalate label is already present →
    # the human was already notified, so SKIP instead of re-pinging every time.
    comments = [_comment(BOT_ACTOR, "r1"), _comment(BOT_ACTOR, "r2"), _comment("human", "again")]
    art = _run(_item(labels=["needs-human"]), comments)
    assert art["action"] == "SKIP"
    assert art["reason"] == "already_escalated"


def test_user_dissatisfied_escalates(_run):
    # Under the cap, but the user says the bot was wrong after its reply.
    comments = [_comment(BOT_ACTOR, "here you go"), _comment("human", "this is wrong")]
    art = _run(_item(), comments)
    assert art["action"] == "ESCALATE"
    assert art["reason"] == "user_dissatisfied"


def test_followup_prompt_load_failed_escalates(_run, monkeypatch):
    # Fail closed: an empty followup prompt (missing/clobbered file or bad SM
    # secret) ESCALATEs with prompt_load_failed rather than replying blind.
    monkeypatch.setattr(prompts_mod, "get_followup_prompt", lambda: "")
    art = _run(_item(), [_comment("human", "please help")])
    assert art["action"] == "ESCALATE"
    assert art["reason"] == "prompt_load_failed"
    # Fail-closed model attribution: names the model that would have run the
    # reply (Haiku reporter), not the Investigator's Opus.
    assert art["model_id"].startswith(HAIKU)


# --------------------------------------------------------------------------- #
# Issue-classification surface (EVENT_TYPE=issues) + issue-respond second pass
# --------------------------------------------------------------------------- #


def test_issue_prompt_load_failed_escalates(_run, monkeypatch):
    # Issue-classify surface fails closed on an empty classify prompt, mirroring
    # followup (the previously-untested twin of the followup guard).
    monkeypatch.setattr(prompts_mod, "get_issue_prompt", lambda: "")
    art = _run(_item(), [], event_type="issues")
    assert art["action"] == "ESCALATE"
    assert art["reason"] == "prompt_load_failed"
    assert art["model_id"].startswith(HAIKU)


def test_issue_respond_second_pass_reattributes_prompt_id(_run, monkeypatch):
    # First pass asks to read a file; second pass produces the citation-backed
    # answer. The artifact's prompt_id must name the issue-respond prompt (the
    # one that produced the posted answer), NOT the classify prompt.
    monkeypatch.setattr(prompts_mod, "get_issue_prompt", lambda: "CLASSIFY PROMPT")
    monkeypatch.setattr(prompts_mod, "get_issue_respond_prompt", lambda: "RESPOND PROMPT")
    first = json.dumps({"action": "RESPOND", "read_files": ["src/x.py"], "response": "draft"})
    second = json.dumps({"action": "RESPOND", "response": "cited answer", "labels": ["bug"]})
    art = _run(_item(), [_comment("human", "why does x fail?")],
               event_type="issues", files={"src/x.py": "def x(): ..."},
               bedrock_responses=[first, second])
    assert art["action"] == "RESPOND"
    assert art["response"] == "cited answer"
    assert art["prompt_id"] == prompts_mod.prompt_version("RESPOND PROMPT")
    assert art["prompt_id"] != prompts_mod.prompt_version("CLASSIFY PROMPT")


def test_issue_respond_empty_prompt_fails_closed(_run, monkeypatch):
    # Files fetched, but the respond prompt is empty (bad SM secret / clobbered
    # file). Must ESCALATE prompt_load_failed: issue_respond, NOT post the
    # weaker first-pass draft.
    monkeypatch.setattr(prompts_mod, "get_issue_prompt", lambda: "CLASSIFY PROMPT")
    monkeypatch.setattr(prompts_mod, "get_issue_respond_prompt", lambda: "")
    first = json.dumps({"action": "RESPOND", "read_files": ["src/x.py"], "response": "draft"})
    art = _run(_item(), [_comment("human", "why?")],
               event_type="issues", files={"src/x.py": "code"},
               bedrock_responses=[first])
    assert art["action"] == "ESCALATE"
    assert art["reason"] == "prompt_load_failed: issue_respond"


def test_issue_respond_second_pass_invoke_failure_escalates(_run, monkeypatch):
    # Second-pass Bedrock invoke fails (raw2 is None, not a throttle). Must
    # ESCALATE rather than silently fall through to the first-pass draft the
    # model flagged insufficient by requesting files.
    monkeypatch.setattr(prompts_mod, "get_issue_prompt", lambda: "CLASSIFY PROMPT")
    monkeypatch.setattr(prompts_mod, "get_issue_respond_prompt", lambda: "RESPOND PROMPT")
    first = json.dumps({"action": "RESPOND", "read_files": ["src/x.py"], "response": "draft"})
    art = _run(_item(), [_comment("human", "why?")],
               event_type="issues", files={"src/x.py": "code"},
               bedrock_responses=[first, None],
               failed_model=("us.anthropic.claude-haiku-4-5-20251001-v1:0", "ModelErrorException"))
    assert art["action"] == "ESCALATE"
    assert art["response"] == ""
    # First-pass draft must NOT be posted.
    assert "draft" not in json.dumps(art)


def test_issue_respond_second_pass_throttle_skips(_run, monkeypatch):
    # A throttle on the second pass SKIPs (rerun later) rather than paging.
    monkeypatch.setattr(prompts_mod, "get_issue_prompt", lambda: "CLASSIFY PROMPT")
    monkeypatch.setattr(prompts_mod, "get_issue_respond_prompt", lambda: "RESPOND PROMPT")
    first = json.dumps({"action": "RESPOND", "read_files": ["src/x.py"], "response": "draft"})
    art = _run(_item(), [_comment("human", "why?")],
               event_type="issues", files={"src/x.py": "code"},
               bedrock_responses=[first, None], throttled=True)
    assert art["action"] == "SKIP"
    assert art["reason"] == "throttled"


# --------------------------------------------------------------------------- #
# act() — Slack delivery-failure metric wiring
# --------------------------------------------------------------------------- #


class _FakeGHAct:
    """act()-side GitHub client: records posts, all succeed."""
    def __init__(self):
        self.comments = []
        self.labels = []
        self.reviews = []
    def post_comment(self, number, body):
        self.comments.append((number, body)); return True
    def add_labels(self, number, labels):
        self.labels.append((number, labels)); return True
    def post_pr_review(self, number, summary, inline_comments, event="COMMENT", commit_id=""):
        self.reviews.append({"number": number, "commit_id": commit_id,
                             "summary": summary}); return True


class _FailingSlack:
    def __init__(self, cfg):
        pass
    def send_escalation(self, number, title, url, labels):
        return False  # simulate a real delivery failure


def test_act_escalate_emits_slack_failure_metric(monkeypatch, tmp_path):
    # When an ESCALATE's Slack ping fails, act() must emit SlackDeliveryFailures
    # so a Slack-only team sees the channel go dark. Verifies the end-to-end
    # wiring (act → _emit_slack_failure_metric → cloudwatch.emit_metrics) the
    # unit tests don't cover.
    artifact = {
        "action": "ESCALATE", "labels": [], "response": "",
        "reason": "user_dissatisfied", "title": "t",
        "html_url": "https://github.com/owner/repo/issues/42",
        "number": 42, "is_pr": False, "prompt_id": "n/a",
        "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    }
    path = tmp_path / "result.json"
    path.write_text(json.dumps(artifact))
    monkeypatch.setattr(main_mod, "ARTIFACT_PATH", str(path))
    monkeypatch.setenv("ARTIFACT_PATH", str(path))
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    monkeypatch.setenv("EVENT_TYPE", "issues")
    monkeypatch.setenv("EVENT_ACTION", "created")
    monkeypatch.setenv("ISSUE_NUMBER", "42")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("BOT_REQUIRE_GUARDRAIL", "false")
    monkeypatch.setenv("SHADOW_REPO_ROOT", str(tmp_path))
    # Hand-built artifact has no integrity stamp — opt out of verification.
    monkeypatch.setenv("SHADOW_VERIFY_ARTIFACT", "false")

    emitted = []
    monkeypatch.setattr(main_mod, "GitHubClient", lambda cfg: _FakeGHAct())
    monkeypatch.setattr(main_mod, "SlackClient", _FailingSlack)

    def fake_emit(**kw):
        emitted.append(kw)
        return True, "ok"
    from shadow import cloudwatch
    monkeypatch.setattr(cloudwatch, "emit_metrics", fake_emit)

    main_mod.act()

    slack_emits = [e for e in emitted if e.get("slack_failure_count")]
    assert len(slack_emits) == 1
    assert slack_emits[0]["slack_failure_count"] == 1
    assert slack_emits[0]["pipeline"] == "issue"


def test_act_clean_pr_review_pins_commit_id(monkeypatch, tmp_path):
    # A clean PR RESPOND artifact carries head_sha; act() must pin the posted
    # review to that exact commit (so an auto-approve gate keying on the
    # review's commit_id sees the analyzed SHA, not a moved head).
    sha = "d70885e0befc1f30eb77620fd563376093e85cea"
    artifact = {
        "action": "RESPOND", "labels": [],
        "response": "No issues found.\n<!-- deequ-bot:clean -->",
        "inline_comments": [],
        "title": "t", "html_url": "https://github.com/owner/repo/pull/722",
        "number": 722, "is_pr": True, "prompt_id": "abc",
        "model_id": "us.anthropic.claude-opus-4-7",
        "head_sha": sha,
    }
    path = tmp_path / "result.json"
    path.write_text(json.dumps(artifact))
    monkeypatch.setattr(main_mod, "ARTIFACT_PATH", str(path))
    monkeypatch.setenv("ARTIFACT_PATH", str(path))
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    monkeypatch.setenv("EVENT_TYPE", "pull_request_target")
    monkeypatch.setenv("EVENT_ACTION", "opened")
    monkeypatch.setenv("ISSUE_NUMBER", "722")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("BOT_REQUIRE_GUARDRAIL", "false")
    monkeypatch.setenv("SHADOW_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("SHADOW_VERIFY_ARTIFACT", "false")
    monkeypatch.setenv("SHADOW_CLOUDWATCH_DISABLED", "true")

    gh = _FakeGHAct()
    monkeypatch.setattr(main_mod, "GitHubClient", lambda cfg: gh)
    monkeypatch.setattr(main_mod, "SlackClient", lambda cfg: _FailingSlack(cfg))

    main_mod.act()

    assert len(gh.reviews) == 1
    assert gh.reviews[0]["commit_id"] == sha
    assert "deequ-bot:clean" in gh.reviews[0]["summary"]


def _run_act_respond(monkeypatch, tmp_path, *, attribution=None):
    """Drive act() on a clean-PR RESPOND artifact; return the posted review summary."""
    artifact = {
        "action": "RESPOND", "labels": [], "response": "No issues found.",
        "inline_comments": [], "title": "t",
        "html_url": "https://github.com/owner/repo/pull/1", "number": 1,
        "is_pr": True, "prompt_id": "abc", "model_id": "us.anthropic.claude-opus-4-7",
        "head_sha": "abc123",
    }
    path = tmp_path / "result.json"
    path.write_text(json.dumps(artifact))
    monkeypatch.setattr(main_mod, "ARTIFACT_PATH", str(path))
    for k, v in {
        "ARTIFACT_PATH": str(path), "GITHUB_TOKEN": "fake",
        "EVENT_TYPE": "pull_request_target", "EVENT_ACTION": "opened",
        "ISSUE_NUMBER": "1", "GITHUB_REPOSITORY": "owner/repo",
        "BOT_REQUIRE_GUARDRAIL": "false", "SHADOW_REPO_ROOT": str(tmp_path),
        "SHADOW_VERIFY_ARTIFACT": "false", "SHADOW_CLOUDWATCH_DISABLED": "true",
    }.items():
        monkeypatch.setenv(k, v)
    if attribution is not None:
        monkeypatch.setenv("BOT_ATTRIBUTION", attribution)
    else:
        monkeypatch.delenv("BOT_ATTRIBUTION", raising=False)
    gh = _FakeGHAct()
    monkeypatch.setattr(main_mod, "GitHubClient", lambda cfg: gh)
    monkeypatch.setattr(main_mod, "SlackClient", lambda cfg: _FailingSlack(cfg))
    main_mod.act()
    return gh.reviews[0]["summary"]


def test_act_footer_includes_attribution_when_set(monkeypatch, tmp_path):
    summary = _run_act_respond(monkeypatch, tmp_path,
                               attribution="Reviewed by Shadow · github.com/sudsali/shadow")
    assert "Reviewed by Shadow" in summary
    assert "github.com/sudsali/shadow" in summary


def test_act_footer_omits_attribution_when_unset(monkeypatch, tmp_path):
    # Backward-compatible: no BOT_ATTRIBUTION → footer unchanged (no extra line).
    summary = _run_act_respond(monkeypatch, tmp_path, attribution=None)
    assert "Reviewed by" not in summary
    assert "Generated by AI" in summary  # the standard footer still present


@pytest.mark.parametrize("bad_head_sha", [None, 12345, ["x"], {"a": 1}, True])
def test_act_corrupted_head_sha_degrades_safely(monkeypatch, tmp_path, bad_head_sha):
    # A tampered/corrupted artifact with a non-str head_sha must NOT crash act();
    # it coerces to "" so commit_id is omitted (GitHub uses current head — the
    # prior behavior), and the review still posts.
    artifact = {
        "action": "RESPOND", "labels": [],
        "response": "No issues found.\n<!-- deequ-bot:clean -->",
        "inline_comments": [],
        "title": "t", "html_url": "https://github.com/owner/repo/pull/9",
        "number": 9, "is_pr": True, "prompt_id": "abc",
        "model_id": "us.anthropic.claude-opus-4-7",
        "head_sha": bad_head_sha,
    }
    path = tmp_path / "result.json"
    path.write_text(json.dumps(artifact))
    monkeypatch.setattr(main_mod, "ARTIFACT_PATH", str(path))
    monkeypatch.setenv("ARTIFACT_PATH", str(path))
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    monkeypatch.setenv("EVENT_TYPE", "pull_request_target")
    monkeypatch.setenv("EVENT_ACTION", "opened")
    monkeypatch.setenv("ISSUE_NUMBER", "9")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("BOT_REQUIRE_GUARDRAIL", "false")
    monkeypatch.setenv("SHADOW_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("SHADOW_VERIFY_ARTIFACT", "false")
    monkeypatch.setenv("SHADOW_CLOUDWATCH_DISABLED", "true")

    gh = _FakeGHAct()
    monkeypatch.setattr(main_mod, "GitHubClient", lambda cfg: gh)
    monkeypatch.setattr(main_mod, "SlackClient", lambda cfg: _FailingSlack(cfg))

    main_mod.act()   # must not raise

    assert len(gh.reviews) == 1
    assert gh.reviews[0]["commit_id"] == ""   # corrupted → omitted, safe fallback
