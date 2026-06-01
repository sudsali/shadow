"""_user_dissatisfied: phrase-anchored escalation signal.

Old loose substrings ("maintainer", "doesn't work", "still broken") matched
legitimate user prose like "thanks, maintainer team!" or
"this code doesn't work because foo is null" and silently turned a friendly
follow-up or a fresh bug report into an escalation. New tokens are full
phrases the user would write to express dissatisfaction with the bot
specifically.
"""
from shadow.main import _DISSATISFACTION_SIGNALS, _user_dissatisfied


class _FakeCfg:
    def __init__(self, bot_actor="github-actions[bot]"):
        self.bot_actor = bot_actor


def _user(body):
    return {"user": {"login": "alice"}, "body": body}


def _bot(body, login="github-actions[bot]"):
    return {"user": {"login": login}, "body": body}


def test_legitimate_thank_you_not_dissatisfaction():
    """The old "maintainer" substring matched 'thanks, maintainer team!'.
    This is a friendly close-out, not an escalation request."""
    cfg = _FakeCfg()
    comments = [
        _user("got an issue"),
        _bot("here's the fix"),
        _user("thanks, maintainer team!"),
    ]
    assert _user_dissatisfied(comments, cfg) is False


def test_legitimate_bug_report_not_dissatisfaction():
    """The old "doesn't work" substring matched
    'this code doesn't work because foo is null', which is a *bug report*
    about the codebase, not dissatisfaction with the bot."""
    cfg = _FakeCfg()
    comments = [
        _user("got an issue"),
        _bot("can you reproduce?"),
        _user("this code doesn't work because foo is null"),
    ]
    assert _user_dissatisfied(comments, cfg) is False


def test_legitimate_status_update_not_dissatisfaction():
    """The old "still broken" substring matched a status report. Status
    information is signal, not escalation intent."""
    cfg = _FakeCfg()
    comments = [
        _user("got an issue"),
        _bot("try this"),
        _user("still broken on macos but fixed on linux"),
    ]
    assert _user_dissatisfied(comments, cfg) is False


def test_explicit_dissatisfaction_caught_this_is_wrong():
    cfg = _FakeCfg()
    comments = [
        _user("got an issue"),
        _bot("here's the fix"),
        _user("this is wrong"),
    ]
    assert _user_dissatisfied(comments, cfg) is True


def test_explicit_dissatisfaction_caught_not_helpful():
    cfg = _FakeCfg()
    comments = [
        _user("got an issue"),
        _bot("here's the fix"),
        _user("not helpful"),
    ]
    assert _user_dissatisfied(comments, cfg) is True


def test_explicit_dissatisfaction_caught_speak_to_a_human():
    cfg = _FakeCfg()
    comments = [
        _user("got an issue"),
        _bot("here's the fix"),
        _user("please speak to a human"),
    ]
    assert _user_dissatisfied(comments, cfg) is True


def test_empty_comments_returns_false():
    cfg = _FakeCfg()
    assert _user_dissatisfied([], cfg) is False


def test_bot_self_reply_not_dissatisfaction():
    """Last comment authored by the bot must never trigger escalation,
    regardless of body content (the bot might quote a user's
    dissatisfaction phrase in its own reply)."""
    cfg = _FakeCfg()
    comments = [
        _user("got an issue"),
        _bot("user wrote 'this is wrong' — clarifying"),
    ]
    assert _user_dissatisfied(comments, cfg) is False


def test_bot_self_reply_with_custom_actor_not_dissatisfaction():
    """Same protection holds when the adopter has set bot_actor to a
    non-default login: dedup via _is_bot_comment must respect it."""
    cfg = _FakeCfg(bot_actor="shadow-bot[bot]")
    comments = [
        _user("got an issue"),
        _bot("this is wrong", login="shadow-bot[bot]"),
    ]
    assert _user_dissatisfied(comments, cfg) is False


def test_dissatisfaction_signals_are_phrase_anchored():
    """Guard against regression: no token should be a single common word
    that overlaps with normal prose. Each signal must be a multi-word
    phrase (or a tight 2-word combo unambiguous in context)."""
    for token in _DISSATISFACTION_SIGNALS:
        assert " " in token, f"single-word token {token!r} risks substring false-positives"
        # Bare "maintainer" / "broken" had been the regression. Sanity-check
        # they're gone.
        assert token != "maintainer"
        assert token != "doesn't work"
        assert token != "still broken"


def test_case_insensitive_match():
    """Body is lowercased before match so users typing in title case still
    trigger escalation."""
    cfg = _FakeCfg()
    comments = [
        _user("got an issue"),
        _bot("fix"),
        _user("This Is Wrong"),
    ]
    assert _user_dissatisfied(comments, cfg) is True
