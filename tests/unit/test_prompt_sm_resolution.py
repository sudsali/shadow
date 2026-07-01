"""Bring-your-own-prompt resolution: inline env text > SM secret name >
bundled disk default. The SM path lets a repo (e.g. deequ) keep its
language-tuned prompts while consuming the shared engine; an empty config
falls back to Shadow's bundled language-agnostic prompts.

SM fetches are mocked — these tests never touch real AWS.
"""
import logging

import pytest

from shadow import prompts as prompts_mod


@pytest.fixture(autouse=True)
def _clear_sm_cache():
    prompts_mod._sm_cache.clear()
    yield
    prompts_mod._sm_cache.clear()


def _stub_sm(monkeypatch, mapping):
    """Replace _read_from_sm with a dict-backed stub; '' models a miss."""
    def fake(name):
        return mapping.get(name, "")
    monkeypatch.setattr(prompts_mod, "_read_from_sm", fake)


def test_disk_default_when_nothing_set(monkeypatch):
    monkeypatch.delenv("PR_INVESTIGATOR_PROMPT", raising=False)
    monkeypatch.delenv("SM_PR_INVESTIGATOR_PROMPT", raising=False)
    text, source = prompts_mod._resolve_prompt(
        "PR_INVESTIGATOR_PROMPT", "pr-investigator.txt")
    assert source == "file:prompts/pr-investigator.txt"
    assert text  # the bundled prompt is non-empty


def test_sm_name_used_when_set(monkeypatch):
    monkeypatch.delenv("PR_CRITIC_PROMPT", raising=False)
    monkeypatch.setenv("SM_PR_CRITIC_PROMPT", "deequ-bot/pr-critic-prompt")
    _stub_sm(monkeypatch, {"deequ-bot/pr-critic-prompt": "SCALA CRITIC PROMPT"})
    text, source = prompts_mod._resolve_prompt(
        "PR_CRITIC_PROMPT", "pr-critic.txt")
    assert text == "SCALA CRITIC PROMPT"
    assert source == "sm:deequ-bot/pr-critic-prompt"


def test_inline_env_beats_sm(monkeypatch):
    # Inline text takes precedence over an SM name if both are set.
    monkeypatch.setenv("PR_REPORTER_PROMPT", "INLINE REPORTER")
    monkeypatch.setenv("SM_PR_REPORTER_PROMPT", "deequ-bot/pr-reporter-prompt")
    _stub_sm(monkeypatch, {"deequ-bot/pr-reporter-prompt": "SM REPORTER"})
    text, source = prompts_mod._resolve_prompt(
        "PR_REPORTER_PROMPT", "pr-reporter.txt")
    assert text == "INLINE REPORTER"
    assert source == "env:PR_REPORTER_PROMPT"


def test_sm_miss_fails_closed_not_disk_fallback(monkeypatch):
    # A configured-but-unresolvable SM name must return "" (fail closed),
    # NOT silently fall through to the bundled disk prompt — otherwise a
    # misconfigured secret masks as a working (wrong) prompt.
    monkeypatch.delenv("PR_INVESTIGATOR_PROMPT", raising=False)
    monkeypatch.setenv("SM_PR_INVESTIGATOR_PROMPT", "deequ-bot/typo-name")
    _stub_sm(monkeypatch, {})  # every fetch misses
    text, source = prompts_mod._resolve_prompt(
        "PR_INVESTIGATOR_PROMPT", "pr-investigator.txt")
    assert text == ""
    assert source == "sm:deequ-bot/typo-name"


def test_read_from_sm_returns_empty_on_boto_failure(monkeypatch):
    # No AWS creds / boto import path errors → "" (fail closed), no raise.
    prompts_mod._sm_cache.clear()

    class _Boom:
        def client(self, *a, **k):
            raise RuntimeError("no creds")
    monkeypatch.setitem(__import__("sys").modules, "boto3", _Boom())
    assert prompts_mod._read_from_sm("deequ-bot/whatever") == ""


def test_commit_prompt_sm_miss_falls_back_to_disk(monkeypatch):
    # Commit prompts are NOT fail-closed (getters guard with `or None`). Since
    # prompt_sm_prefix sets all five SM_* names together, an adopter who only
    # created the three core secrets must not silently lose the commit phase —
    # an SM miss on a commit prompt falls back to the bundled disk prompt.
    monkeypatch.delenv("PR_INVESTIGATOR_COMMIT_PROMPT", raising=False)
    monkeypatch.setenv("SM_PR_INVESTIGATOR_COMMIT_PROMPT", "deequ-bot/missing")
    _stub_sm(monkeypatch, {})  # commit secret does not exist
    text, source = prompts_mod._resolve_prompt(
        "PR_INVESTIGATOR_COMMIT_PROMPT", "pr-investigator-commit.txt",
        sm_fallback_to_disk=True)
    assert text  # bundled disk commit prompt is non-empty
    assert source == "file:prompts/pr-investigator-commit.txt"


def test_core_prompt_sm_miss_does_NOT_fall_back(monkeypatch):
    # Contrast: core prompts must fail closed, not fall back — a missing core
    # secret is a misconfig that should ESCALATE, not silently run the default.
    monkeypatch.delenv("PR_CRITIC_PROMPT", raising=False)
    monkeypatch.setenv("SM_PR_CRITIC_PROMPT", "deequ-bot/missing")
    _stub_sm(monkeypatch, {})
    text, source = prompts_mod._resolve_prompt(
        "PR_CRITIC_PROMPT", "pr-critic.txt")  # sm_fallback_to_disk defaults False
    assert text == ""
    assert source == "sm:deequ-bot/missing"


def test_commit_prompt_sm_hit_returns_sm_text_not_disk(monkeypatch):
    # Fallback-enabled must still PREFER SM text when the secret HITS —
    # enabling disk-fallback must not accidentally always use disk.
    monkeypatch.delenv("PR_CRITIC_COMMIT_PROMPT", raising=False)
    monkeypatch.setenv("SM_PR_CRITIC_COMMIT_PROMPT", "deequ-bot/pr-critic-commit-prompt")
    _stub_sm(monkeypatch, {"deequ-bot/pr-critic-commit-prompt": "CUSTOM COMMIT NUDGE"})
    text, source = prompts_mod._resolve_prompt(
        "PR_CRITIC_COMMIT_PROMPT", "pr-critic-commit.txt", sm_fallback_to_disk=True)
    assert text == "CUSTOM COMMIT NUDGE"
    assert source == "sm:deequ-bot/pr-critic-commit-prompt"


def test_read_from_sm_binary_only_secret_returns_empty(monkeypatch):
    # A SecretBinary-only secret has no SecretString key → "" (treated as miss),
    # no crash. Also covers SecretString present-but-None via the `or ""`.
    prompts_mod._sm_cache.clear()

    class _Client:
        def __init__(self, resp):
            self._resp = resp
        def get_secret_value(self, SecretId):
            return self._resp

    class _Boto:
        def __init__(self, resp):
            self._resp = resp
        def client(self, *a, **k):
            return _Client(self._resp)

    import sys
    # binary-only: no SecretString key
    monkeypatch.setitem(sys.modules, "boto3", _Boto({"SecretBinary": b"\x00"}))
    assert prompts_mod._read_from_sm("deequ-bot/binary") == ""
    prompts_mod._sm_cache.clear()
    # SecretString explicitly None
    monkeypatch.setitem(sys.modules, "boto3", _Boto({"SecretString": None}))
    assert prompts_mod._read_from_sm("deequ-bot/nullstr") == ""


def test_sm_cache_memoizes_hit_but_not_miss(monkeypatch):
    # The cache's whole point: a successful fetch is fetched once; a failure
    # is NOT cached so a transient error doesn't poison later reads.
    prompts_mod._sm_cache.clear()
    calls = {"n": 0}

    class _Client:
        def __init__(self, outer):
            self.outer = outer
        def get_secret_value(self, SecretId):
            calls["n"] += 1
            return {"SecretString": self.outer.mode}

    class _Boto:
        mode = ""  # start: miss (empty)
        def client(self, *a, **k):
            return _Client(self)

    boto = _Boto()
    import sys
    monkeypatch.setitem(sys.modules, "boto3", boto)

    # First: empty → not cached.
    assert prompts_mod._read_from_sm("deequ-bot/x") == ""
    assert calls["n"] == 1
    # Second: still not cached (miss not memoized) → re-fetches; now returns text.
    boto.mode = "RECOVERED"
    assert prompts_mod._read_from_sm("deequ-bot/x") == "RECOVERED"
    assert calls["n"] == 2
    # Third: now cached → no new fetch.
    assert prompts_mod._read_from_sm("deequ-bot/x") == "RECOVERED"
    assert calls["n"] == 2


def test_provenance_commit_sm_miss_labeled_file_not_sm(monkeypatch):
    # THE invariant the _ACTIVE_PROMPTS sm_fallback field exists to guarantee:
    # a commit-prompt SM miss runs on the bundled disk prompt, so provenance
    # MUST label it file:, not sm: — the audit rollup reflects what ran.
    monkeypatch.delenv("PR_CRITIC_COMMIT_PROMPT", raising=False)
    monkeypatch.setenv("SM_PR_CRITIC_COMMIT_PROMPT", "deequ-bot/missing-commit")
    _stub_sm(monkeypatch, {})  # commit secret misses
    prov = prompts_mod.compute_prompt_provenance()
    cc = next(e for e in prov["prompts"] if e["stage"] == "critic_commit")
    assert cc["source"] == "file:prompts/pr-critic-commit.txt"
    assert cc["loaded"] is True


def test_whitespace_only_sm_content_normalized_to_empty_fails_closed(monkeypatch):
    # A whitespace-only secret must be treated as absent, not run the reviewer
    # with a blank system prompt. Core prompt (no fallback) → "" so the
    # fail-closed guard fires.
    monkeypatch.delenv("PR_INVESTIGATOR_PROMPT", raising=False)
    monkeypatch.setenv("SM_PR_INVESTIGATOR_PROMPT", "deequ-bot/pr-investigator-prompt")
    _stub_sm(monkeypatch, {"deequ-bot/pr-investigator-prompt": "   \n\t  "})
    text, source = prompts_mod._resolve_prompt(
        "PR_INVESTIGATOR_PROMPT", "pr-investigator.txt")
    assert text == ""  # normalized, NOT the whitespace → guard fails closed
    assert source == "sm:deequ-bot/pr-investigator-prompt"


def test_whitespace_only_inline_env_ignored(monkeypatch):
    # A whitespace-only inline override must not win over SM/disk.
    monkeypatch.setenv("PR_CRITIC_PROMPT", "   ")
    monkeypatch.delenv("SM_PR_CRITIC_PROMPT", raising=False)
    text, source = prompts_mod._resolve_prompt("PR_CRITIC_PROMPT", "pr-critic.txt")
    # falls through to the bundled disk prompt (non-empty), not the "   "
    assert text and text.strip()
    assert source == "file:prompts/pr-critic.txt"


def test_whitespace_only_sm_name_treated_as_unset(monkeypatch):
    # A whitespace-only SM_<var> (e.g. a whitespace prompt_sm_prefix) must be
    # treated as "no SM configured" → bundled disk default, not a guaranteed
    # miss that fails closed.
    monkeypatch.delenv("PR_REPORTER_PROMPT", raising=False)
    monkeypatch.setenv("SM_PR_REPORTER_PROMPT", "   ")
    text, source = prompts_mod._resolve_prompt("PR_REPORTER_PROMPT", "pr-reporter.txt")
    assert text and text.strip()
    assert source == "file:prompts/pr-reporter.txt"


def test_real_prompt_returned_byte_for_byte(monkeypatch):
    # Presence is decided on .strip(), but the ORIGINAL bytes (incl. trailing
    # newline) must be returned so provenance hashes exactly what ran.
    monkeypatch.setenv("PR_CRITIC_PROMPT", "REAL PROMPT\n")
    monkeypatch.delenv("SM_PR_CRITIC_PROMPT", raising=False)
    text, _ = prompts_mod._resolve_prompt("PR_CRITIC_PROMPT", "pr-critic.txt")
    assert text == "REAL PROMPT\n"  # not stripped


class _ExcWithResponse(Exception):
    def __init__(self, response):
        self.response = response


class _ExcResponseRaises(Exception):
    @property
    def response(self):
        raise RuntimeError("boom accessing .response")


@pytest.mark.parametrize("exc, expected", [
    (RuntimeError("no response attr"), ""),                       # response absent
    (_ExcWithResponse(None), ""),                                 # response None
    (_ExcWithResponse(["not", "a", "dict"]), ""),                 # response non-dict
    (_ExcWithResponse({}), ""),                                   # no Error key
    (_ExcWithResponse({"Error": "boom"}), ""),                    # Error non-dict
    (_ExcWithResponse({"Error": {}}), ""),                        # Error missing Code
    (_ExcWithResponse({"Error": {"Code": 500}}), ""),             # Code non-str
    (_ExcWithResponse({"Error": {"Code": "AccessDeniedException"}}), "AccessDeniedException"),
    (_ExcResponseRaises(), ""),                                   # .response itself raises
])
def test_clienterror_code_is_total(exc, expected):
    # A raise here would fire INSIDE _read_from_sm's fail-closed except and
    # defeat the whole guarantee. It must extract-or-"" for every shape, never
    # raise.
    assert prompts_mod._clienterror_code(exc) == expected


def test_read_from_sm_logs_error_code_on_clienterror(monkeypatch, caplog):
    # A real ClientError-shaped failure returns "" (fail closed) AND surfaces
    # the botocore Code so an operator can tell AccessDenied from NotFound.
    prompts_mod._sm_cache.clear()

    class _Client:
        def get_secret_value(self, SecretId):
            raise _ExcWithResponse({"Error": {"Code": "ResourceNotFoundException"}})

    class _Boto:
        def client(self, *a, **k):
            return _Client()

    import sys
    monkeypatch.setitem(sys.modules, "boto3", _Boto())
    import types
    monkeypatch.setitem(sys.modules, "botocore.config",
                        types.SimpleNamespace(Config=lambda **k: None))
    with caplog.at_level(logging.ERROR, logger="shadow"):
        assert prompts_mod._read_from_sm("my-bot/x") == ""
    assert "ResourceNotFoundException" in caplog.text


def test_whitespace_only_disk_prompt_normalized_to_empty(monkeypatch):
    # Clobbered-install case: a bundled prompt file that is whitespace-only must
    # normalize to "" so the fail-closed guard fires, not run the reviewer blank.
    monkeypatch.delenv("PR_INVESTIGATOR_PROMPT", raising=False)
    monkeypatch.delenv("SM_PR_INVESTIGATOR_PROMPT", raising=False)
    monkeypatch.setattr(prompts_mod, "_read_from_disk", lambda fn: "   \n\t ")
    text, source = prompts_mod._resolve_prompt(
        "PR_INVESTIGATOR_PROMPT", "pr-investigator.txt")
    assert text == ""
    assert source == "file:prompts/pr-investigator.txt"


def test_provenance_labels_sm_source(monkeypatch):
    monkeypatch.delenv("PR_CRITIC_PROMPT", raising=False)
    monkeypatch.setenv("SM_PR_CRITIC_PROMPT", "deequ-bot/pr-critic-prompt")
    _stub_sm(monkeypatch, {"deequ-bot/pr-critic-prompt": "SCALA CRITIC"})
    prov = prompts_mod.compute_prompt_provenance()
    critic = next(e for e in prov["prompts"] if e["stage"] == "critic")
    assert critic["source"] == "sm:deequ-bot/pr-critic-prompt"
    assert critic["loaded"] is True
