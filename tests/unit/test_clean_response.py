"""_build_clean_response branches must all carry the `<!-- {bot}:clean -->`
marker so adopters greppping for it (per README's grep-ability promise)
match every clean review regardless of CI state. Bench corpus has zero
clean-PR rows, so this is the only place the invariant is enforced."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "scripts"))

from shadow.main import _build_clean_response


class _StubGH:
    def __init__(self, ci_passed, ci_summary=""):
        self._ci = (ci_passed, ci_summary)
        self.ci_calls = 0

    def get_ci_status(self, head_sha):
        self.ci_calls += 1
        return self._ci


def test_clean_response_inline_comments_returns_empty():
    """When inline comments exist, the top-level body is empty (the inline
    comments themselves carry the review). No marker needed AND no GitHub
    API call (CI status fetch is wasted when we're not posting a clean
    confirmation)."""
    gh = _StubGH(True)
    out = _build_clean_response(gh, "abc123", [{"file": "x"}])
    assert out == ""
    assert gh.ci_calls == 0, "get_ci_status must NOT be called when inline_comments are non-empty"


def test_clean_response_ci_passing_has_marker():
    out = _build_clean_response(_StubGH(True), "abc123", [])
    assert "<!-- shadow:clean -->" in out
    assert "CI is passing" in out


def test_clean_response_ci_failing_has_marker():
    """Regression: CI-failing branch used to omit the marker, contradicting
    README's universal claim. Marker must be present so adopter grep tooling
    matches every clean review regardless of CI state."""
    out = _build_clean_response(_StubGH(False, "1 of 3 checks failing"), "abc", [])
    assert "<!-- shadow:clean -->" in out
    assert "1 of 3 checks failing" in out


def test_clean_response_ci_unknown_has_marker():
    """No head_sha means CI status can't be fetched; still mark the comment."""
    out = _build_clean_response(_StubGH(None), None, [])
    assert "<!-- shadow:clean -->" in out


def test_clean_response_custom_bot_name_marker():
    """Adopters renaming the bot via .shadow.yml must see the marker reflect
    their bot_name so multiple bots can coexist on one repo without grep
    collisions."""
    out = _build_clean_response(_StubGH(True), "abc", [], bot_name="custom")
    assert "<!-- custom:clean -->" in out
    assert "<!-- shadow:clean -->" not in out


def test_clean_response_ci_summary_html_comment_neutralized():
    """A check-run name containing `<!--` must not swallow the trailing
    marker in rendered HTML. Sanitize the embedded text to escape both
    delimiters."""
    summary = "1 check failing: <!-- evil --> stage"
    out = _build_clean_response(_StubGH(False, summary), "abc", [])
    assert "<!--" not in out.replace("<!-- shadow:clean -->", "")
    assert "<!-- shadow:clean -->" in out
    assert "&lt;!--" in out


def test_clean_response_ci_summary_sanitizer_marker_neutralized():
    """A check-run name containing a sanitizer injection-marker substring
    (e.g., GitHub Actions matrix-build name `Build (system: x86_64)`) must
    NOT cause the post-time `sanitize()` call in act() to null the entire
    response. Round-3 added the marker to all branches but didn't address
    this — `system:` substring in ci_summary nullified to ESCALATE without
    a marker."""
    from shadow.sanitizer import sanitize
    summary = "1 check failing: Build (system: x86_64), lint"
    out = _build_clean_response(_StubGH(False, summary), "abc", [])
    # The clean response must survive a downstream sanitize() call.
    assert sanitize(out) is not None, (
        "sanitize() must NOT null the clean response when ci_summary "
        "contains a sanitizer injection-marker substring like 'system:'"
    )
    assert "<!-- shadow:clean -->" in out


def test_clean_response_ci_summary_spaceful_marker_neutralized():
    """Round-4's first fix used `:` and `<` entitization, which was a no-op
    for spaceful markers like 'ignore previous instructions' (no `:`, no
    `<`). A CI provider whose check-run name echoes that exact phrase
    would still trip sanitize(). Lock that regression: the scrubbed text
    must survive sanitize() for every shape of injection marker."""
    from shadow.sanitizer import sanitize
    summary = "lint failed: ignore previous instructions in `.eslintrc`"
    out = _build_clean_response(_StubGH(False, summary), "abc", [])
    assert sanitize(out) is not None, (
        "sanitize() must NOT null the clean response when ci_summary "
        "contains a spaceful injection marker like 'ignore previous instructions'"
    )
    assert "<!-- shadow:clean -->" in out
