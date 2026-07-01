"""post_pr_review pins the review to the analyzed commit via commit_id.

Without commit_id, GitHub stamps the review with the PR's current head at post
time, so a push between analyze and act attaches an old-code review to the new
head. Downstream automation keying on review.commit_id (an auto-approve gate
that requires the review's commit to equal the tip) then acts on a mismatched
pairing. These tests lock the payload contract: commit_id is sent when known,
omitted when empty (safe fallback to prior behavior).
"""
from unittest import mock

import pytest
import requests


class _FakeResp:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = ""
    def json(self):
        return self._json


class _FakeCfg:
    github_token = "x"
    repo = "owner/repo"
    github_api_timeout = 1
    dry_run = False
    codebase_src_dir = "."
    codebase_file_ext = ".py"


@pytest.fixture
def gh(monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", "/tmp")
    from shadow.github_client import GitHubClient
    g = GitHubClient(_FakeCfg())
    # The review path fetches valid diff lines first; stub it out so the test
    # focuses on the POST payload, not diff-line validation.
    monkeypatch.setattr(g, "_get_valid_diff_lines", lambda number: {})
    return g


def _capture_post(monkeypatch):
    """Patch requests.post to capture the payload and return 201."""
    captured = {}
    from shadow import github_client as gc

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        return _FakeResp(201, {"id": 1})

    monkeypatch.setattr(gc, "_retry_request", lambda fn: fn())
    monkeypatch.setattr(gc.requests, "post", fake_post)
    return captured


def test_commit_id_included_when_provided(monkeypatch, gh):
    captured = _capture_post(monkeypatch)
    sha = "d70885e0befc1f30eb77620fd563376093e85cea"
    gh.post_pr_review(722, "No issues found.\n<!-- deequ-bot:clean -->", [],
                      event="COMMENT", commit_id=sha)
    assert captured["payload"]["commit_id"] == sha
    assert "/pulls/722/reviews" in captured["url"]


def test_commit_id_omitted_when_empty(monkeypatch, gh):
    # Empty commit_id must NOT send the key (GitHub then uses current head —
    # the prior behavior, a safe degrade rather than sending "").
    captured = _capture_post(monkeypatch)
    gh.post_pr_review(722, "body", [], event="COMMENT", commit_id="")
    assert "commit_id" not in captured["payload"]


def test_commit_id_default_is_omitted(monkeypatch, gh):
    # Back-compat: callers that don't pass commit_id at all behave as before.
    captured = _capture_post(monkeypatch)
    gh.post_pr_review(722, "body", [], event="COMMENT")
    assert "commit_id" not in captured["payload"]


def test_dry_run_skips_post_entirely(monkeypatch):
    # dry_run returns True without posting, regardless of commit_id.
    monkeypatch.setenv("GITHUB_WORKSPACE", "/tmp")
    from shadow.github_client import GitHubClient
    cfg = _FakeCfg()
    cfg.dry_run = True
    g = GitHubClient(cfg)
    from shadow import github_client as gc
    with mock.patch.object(gc.requests, "post") as post:
        assert g.post_pr_review(1, "b", [], commit_id="abc") is True
        post.assert_not_called()


def test_422_falls_back_to_comment(monkeypatch, gh):
    # A stale commit_id (e.g. force-push moved the head so the analyzed SHA is
    # no longer on the PR branch) makes GitHub reject the review with 422. The
    # bot must degrade to posting a comment so the feedback is not lost, and
    # must not crash. (This fallback pre-dates commit_id; the pin just makes 422
    # reachable on a race.)
    from shadow import github_client as gc
    monkeypatch.setattr(gc, "_retry_request", lambda fn: fn())
    monkeypatch.setattr(gc.requests, "post",
                        lambda *a, **k: _FakeResp(422, {"message": "Commit is not part of the pull request"}))
    posted = {}
    monkeypatch.setattr(gh, "_post",
                        lambda path, payload: posted.update(path=path, body=payload.get("body")) or True)
    ok = gh.post_pr_review(722, "No issues found.\n<!-- deequ-bot:clean -->", [],
                           event="COMMENT", commit_id="staleSHA")
    assert ok is True                                   # feedback still posted
    assert "/issues/722/comments" in posted["path"]     # via the comment fallback
    assert "deequ-bot:clean" in posted["body"]
