"""GitHub client retry + rate-limit handling tests.

_get and _post retry on 5xx and on rate-limit 403 with X-RateLimit-Remaining=0.
On exhaustion the response is returned (caller decides). Connection /
Timeout errors retry transparently and propagate after max attempts.
"""
from unittest import mock

import pytest
import requests


class _FakeResp:
    def __init__(self, status_code, headers=None, json_data=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self._json = json_data
        self.text = text

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
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
    return GitHubClient(_FakeCfg())


def test_5xx_then_200_retries_and_succeeds(monkeypatch, gh):
    """Transient 5xx must retry; never silently drop a posted comment."""
    from shadow import github_client as gc
    sleeps = []
    monkeypatch.setattr(gc.time, "sleep", lambda s: sleeps.append(s))
    responses = [
        _FakeResp(503),
        _FakeResp(200, json_data={"ok": True}),
    ]
    with mock.patch.object(gc.requests, "get", side_effect=responses) as get:
        result = gh._get("/repos/x/y/issues/1")
    assert result == {"ok": True}
    assert get.call_count == 2
    assert len(sleeps) == 1


def test_three_5xx_returns_none(monkeypatch, gh):
    """After max_attempts of 5xx the request fails; caller logs and moves on."""
    from shadow import github_client as gc
    monkeypatch.setattr(gc.time, "sleep", lambda s: None)
    responses = [_FakeResp(503), _FakeResp(503), _FakeResp(503)]
    with mock.patch.object(gc.requests, "get", side_effect=responses) as get:
        result = gh._get("/repos/x/y/issues/1")
    assert result is None
    assert get.call_count == 3


def test_403_rate_limit_respects_reset(monkeypatch, gh):
    """403 with X-RateLimit-Remaining=0 honors X-RateLimit-Reset header."""
    from shadow import github_client as gc
    sleeps = []
    monkeypatch.setattr(gc.time, "sleep", lambda s: sleeps.append(s))
    # Pin time.time so test is deterministic.
    monkeypatch.setattr(gc.time, "time", lambda: 1000.0)
    responses = [
        _FakeResp(
            403,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1003"},
        ),
        _FakeResp(200, json_data={"ok": True}),
    ]
    with mock.patch.object(gc.requests, "get", side_effect=responses):
        result = gh._get("/repos/x/y/issues/1")
    assert result == {"ok": True}
    # Reset is 3s in the future, exponential default is base_delay=1s; should
    # sleep ~3s (the larger of the two).
    assert sleeps and sleeps[0] >= 1.0


def test_403_without_rate_limit_does_not_retry(monkeypatch, gh):
    """403 without X-RateLimit-Remaining=0 is permission failure; no retry."""
    from shadow import github_client as gc
    monkeypatch.setattr(gc.time, "sleep", lambda s: None)
    responses = [_FakeResp(403)]
    with mock.patch.object(gc.requests, "get", side_effect=responses) as get:
        result = gh._get("/repos/x/y/issues/1")
    assert result is None
    assert get.call_count == 1


def test_403_with_remaining_nonzero_no_retry(monkeypatch, gh):
    """403 with X-RateLimit-Remaining>0 means a permission failure, not rate
    limit; no retry."""
    from shadow import github_client as gc
    monkeypatch.setattr(gc.time, "sleep", lambda s: None)
    responses = [_FakeResp(403, headers={"X-RateLimit-Remaining": "10"})]
    with mock.patch.object(gc.requests, "get", side_effect=responses) as get:
        result = gh._get("/repos/x/y/issues/1")
    assert result is None
    assert get.call_count == 1


def test_connection_error_retries(monkeypatch, gh):
    """ConnectionError retries with exponential backoff up to max_attempts."""
    from shadow import github_client as gc
    sleeps = []
    monkeypatch.setattr(gc.time, "sleep", lambda s: sleeps.append(s))
    err = requests.exceptions.ConnectionError("network down")
    side_effects = [err, err, _FakeResp(200, json_data={"ok": True})]
    with mock.patch.object(gc.requests, "get", side_effect=side_effects) as get:
        result = gh._get("/repos/x/y/issues/1")
    assert result == {"ok": True}
    assert get.call_count == 3


def test_connection_error_exhausts_propagates(monkeypatch, gh):
    """Three ConnectionErrors in a row → propagate (caught by the outer try/
    except in _get and converted to None)."""
    from shadow import github_client as gc
    monkeypatch.setattr(gc.time, "sleep", lambda s: None)
    err = requests.exceptions.ConnectionError("network down")
    with mock.patch.object(gc.requests, "get", side_effect=[err, err, err]) as get:
        result = gh._get("/repos/x/y/issues/1")
    assert result is None
    assert get.call_count == 3


def test_post_5xx_then_201_succeeds(monkeypatch, gh):
    """POST retry on 5xx — same logic as GET, validated independently."""
    from shadow import github_client as gc
    monkeypatch.setattr(gc.time, "sleep", lambda s: None)
    responses = [_FakeResp(502), _FakeResp(201, json_data={"ok": True})]
    with mock.patch.object(gc.requests, "post", side_effect=responses) as post:
        result = gh._post("/repos/x/y/issues/1/comments", {"body": "hi"})
    assert result is True
    assert post.call_count == 2


def test_max_retry_wait_is_capped(monkeypatch, gh):
    """A wildly-future Reset header must not block the act() process for
    minutes; retry delay caps at _MAX_RETRY_WAIT_SECONDS."""
    from shadow import github_client as gc
    sleeps = []
    monkeypatch.setattr(gc.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(gc.time, "time", lambda: 1000.0)
    # Reset 1 hour in the future
    responses = [
        _FakeResp(
            403,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "4600"},
        ),
        _FakeResp(200, json_data={"ok": True}),
    ]
    with mock.patch.object(gc.requests, "get", side_effect=responses):
        gh._get("/repos/x/y/issues/1")
    assert sleeps and sleeps[0] <= 60


def test_get_pr_files_paginates(monkeypatch, gh):
    """get_pr_files now paginates with per_page=100; PRs >30 files no longer
    silently degrade to first page."""
    page1 = [{"filename": f"f{i}.py"} for i in range(100)]
    page2 = [{"filename": f"g{i}.py"} for i in range(50)]
    calls = []

    def fake_get(path):
        calls.append(path)
        if "&page=1" in path:
            return page1
        if "&page=2" in path:
            return page2
        return []

    monkeypatch.setattr(gh, "_get", fake_get)
    files = gh.get_pr_files(42)
    assert len(files) == 150
    assert any("&page=1" in c for c in calls)
    assert any("&page=2" in c for c in calls)


def test_get_pr_files_stops_at_short_page(monkeypatch, gh):
    """When a page is < per_page items, stop paginating immediately."""
    short_page = [{"filename": "f.py"}]
    calls = []

    def fake_get(path):
        calls.append(path)
        return short_page

    monkeypatch.setattr(gh, "_get", fake_get)
    files = gh.get_pr_files(42)
    assert len(files) == 1
    assert len(calls) == 1
