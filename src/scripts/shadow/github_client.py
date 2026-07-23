import datetime
import logging
import os
import re
import time

import requests

logger = logging.getLogger("shadow")

# GitHub returns 422 above 65,536 UTF-16 code units on comment/review bodies.
# Python str length counts CODEPOINTS — a body of 65k emoji codepoints can
# exceed 130k UTF-16 units and 422 even though `len(body) == 65000`. Truncate
# by UTF-16 code units to match what GitHub actually counts.
_GITHUB_COMMENT_MAX_UNITS = 65_536
_COMMENT_TRUNCATE_AT_UNITS = 64_000
_COMMENT_TRUNCATE_MARKER = "\n\n_… [truncated by Shadow at 64,000 chars; see workflow artifact for full output]_"


# Maximum total wait when respecting rate-limit Reset headers; longer than
# this and we surface the failure rather than block the act() process.
_MAX_RETRY_WAIT_SECONDS = 60
_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_BASE_DELAY_S = 1.0


def _is_rate_limited(resp):
    """True iff the response is a GitHub rate-limit 403 (X-RateLimit-Remaining=0).
    Distinguishes from a 403 due to permissions, which won't recover by retry."""
    if resp.status_code != 403:
        return False
    remaining = resp.headers.get("X-RateLimit-Remaining")
    return remaining is not None and remaining == "0"


def _retry_delay(resp, attempt, base_delay):
    """Compute backoff: exponential, capped, with rate-limit Reset awareness.
    Returns 0 if Reset header is bogus (negative or in distant past)."""
    base = base_delay * (2 ** attempt)
    reset = resp.headers.get("X-RateLimit-Reset") if resp is not None else None
    try:
        reset_at = int(reset) if reset else 0
    except (TypeError, ValueError):
        reset_at = 0
    if reset_at:
        wait = reset_at - time.time()
        if wait > 0:
            return min(max(base, wait), _MAX_RETRY_WAIT_SECONDS)
    return min(base, _MAX_RETRY_WAIT_SECONDS)


def _retry_request(func, max_attempts=_DEFAULT_MAX_ATTEMPTS,
                   base_delay=_DEFAULT_BASE_DELAY_S, sleep_fn=None):
    """Run `func()` returning a Response; retry on 5xx and rate-limit 403.

    Connection / Timeout errors retry transparently. Other exceptions
    propagate. Returns the final Response on exhaustion (which may be a
    5xx — caller decides whether to treat as success). sleep_fn defaults
    to time.sleep but is resolved at call time so tests can monkeypatch
    `time.sleep` on the module.
    """
    if sleep_fn is None:
        sleep_fn = time.sleep
    last_resp = None
    last_exc = None
    for attempt in range(max_attempts):
        try:
            resp = func()
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last_exc = e
            if attempt < max_attempts - 1:
                sleep_fn(min(base_delay * (2 ** attempt), _MAX_RETRY_WAIT_SECONDS))
                continue
            raise
        last_resp = resp
        # 5xx: server-side transient; retry.
        # 403 with X-RateLimit-Remaining=0: API rate limit; respect Reset.
        # Everything else: return as-is (success, 4xx user error, etc.).
        if resp.status_code >= 500 or _is_rate_limited(resp):
            if attempt < max_attempts - 1:
                sleep_fn(_retry_delay(resp, attempt, base_delay))
                continue
        return resp
    return last_resp


def _truncate_comment_body(body):
    """Truncate by UTF-16 code-unit count so GitHub's 65,536-unit cap is
    actually respected even when the body contains supplementary-plane
    codepoints (emoji, CJK extensions). Each such codepoint occupies 2
    UTF-16 code units; counting Python str length undercounts by ~2× in
    the worst case and lets a perfectly emoji-heavy body trigger 422.

    Non-string input → "" (defensive; GitHub API needs a string)."""
    if not isinstance(body, str):
        return ""
    if not body:
        return body
    encoded = body.encode("utf-16-le")
    if len(encoded) <= _GITHUB_COMMENT_MAX_UNITS * 2:
        return body
    truncated = encoded[: _COMMENT_TRUNCATE_AT_UNITS * 2].decode(
        "utf-16-le", errors="ignore"
    )
    return truncated + _COMMENT_TRUNCATE_MARKER


class GitHubClient:
    def __init__(self, cfg):
        self._token = cfg.github_token
        self._repo = cfg.repo
        self._timeout = cfg.github_api_timeout
        self._dry_run = cfg.dry_run
        self._cfg = cfg
        self._repo_root = os.getenv("GITHUB_WORKSPACE", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
        self._headers = {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github.v3+json",
        }

    @property
    def repo_root(self):
        """Absolute path to the checked-out repo (GITHUB_WORKSPACE in CI)."""
        return self._repo_root

    def get_issue(self, number):
        return self._get(f"/repos/{self._repo}/issues/{number}")

    def get_comments(self, number, max_pages=10):
        comments = []
        page = 1
        while page <= max_pages:
            batch = self._get(f"/repos/{self._repo}/issues/{number}/comments?per_page=100&page={page}")
            if not batch:
                break
            comments.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return comments

    def get_pr(self, number):
        return self._get(f"/repos/{self._repo}/pulls/{number}")

    def get_pr_diff(self, number):
        """Fetch the unified diff for a PR. Returns text on success, None on
        any failure (caller distinguishes empty diff from fetch failure —
        a silent empty fetch on a 5xx would let analyze() RESPOND with
        'No issues found' on a transient outage)."""
        headers = {**self._headers, "Accept": "application/vnd.github.v3.diff"}
        try:
            resp = _retry_request(lambda: requests.get(
                f"https://api.github.com/repos/{self._repo}/pulls/{number}",
                headers=headers, timeout=self._timeout,
            ))
            if resp is None or resp.status_code != 200:
                logger.warning("PR diff fetch failed: status=%s",
                               resp.status_code if resp is not None else "no_response")
                return None
            return resp.text
        except (requests.RequestException, ValueError) as e:
            logger.error(f"PR diff fetch failed: {e}")
            return None

    def get_compare_diff(self, base_sha, head_sha):
        """Fetch the diff between two commits using the Compare API.
        Returns text on success, None on any failure (e.g. force-push where
        base_sha no longer exists, or a transient 5xx). The caller must
        distinguish None (fetch error) from "" (empty diff is unreachable
        for a real Compare result, so used here only for missing-input)."""
        if not base_sha or not head_sha:
            return ""
        headers = {**self._headers, "Accept": "application/vnd.github.v3.diff"}
        try:
            resp = _retry_request(lambda: requests.get(
                f"https://api.github.com/repos/{self._repo}/compare/{base_sha}...{head_sha}",
                headers=headers, timeout=self._timeout,
            ))
            if resp is None or resp.status_code != 200:
                status = resp.status_code if resp is not None else "no_response"
                logger.warning(f"Compare API {base_sha[:7]}...{head_sha[:7]}: {status}")
                return None
            return resp.text
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Compare diff failed: {e}")
            return None

    def get_ci_status(self, sha):
        """Check commit statuses and check runs. Returns (passed, summary).
        passed: True (all green), False (something failed), None (pending/unknown)."""
        status = self._get(f"/repos/{self._repo}/commits/{sha}/status")
        if status is None:
            return None, "CI status unavailable"
        combined_state = status.get("state", "pending")

        check_data = self._get(f"/repos/{self._repo}/commits/{sha}/check-runs")
        runs = check_data.get("check_runs", []) if check_data else []

        workflow_name = os.getenv("GITHUB_WORKFLOW", "")
        bot_prefix = workflow_name.lower() + " / " if workflow_name else ""
        external_runs = [
            r for r in runs
            if not (bot_prefix and r.get("name", "").lower().startswith(bot_prefix))
        ]

        failed = []
        pending = []
        for r in external_runs:
            name = r.get("name") or "(unnamed)"
            if r.get("status") != "completed":
                pending.append(name)
            elif r.get("conclusion") not in ("success", "neutral", "skipped"):
                failed.append(name)

        if failed:
            return False, f"CI failing: {', '.join(failed)}"
        if pending:
            return None, f"CI pending: {', '.join(pending)}"
        if combined_state == "failure":
            return False, "CI failing (status checks)"
        if combined_state == "pending":
            return None, "CI pending (status checks)"
        return True, "CI passed"

    def get_pr_files(self, number, max_pages=10):
        """Paginate through PR files. Default max_pages=10 caps at 1000
        files (per_page=100 × 10) so a 5000-file PR doesn't burn the
        wall-clock budget here. Below 30 files this fits in one page."""
        files = []
        page = 1
        while page <= max_pages:
            batch = self._get(
                f"/repos/{self._repo}/pulls/{number}/files?per_page=100&page={page}"
            )
            if not batch:
                break
            files.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return files

    def get_pr_review_comments(self, number, max_pages=10):
        comments = []
        page = 1
        while page <= max_pages:
            batch = self._get(f"/repos/{self._repo}/pulls/{number}/comments?per_page=100&page={page}")
            if not batch:
                break
            comments.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return comments

    def count_recent_workflow_runs_for_item(self, item_number,
                                            since_minutes=60):
        """Count prior runs of *this* workflow in the last N minutes that
        targeted the same PR/issue, excluding the in-flight current run.
        Returns int on success, None on API error (caller fails open).

        Resolves "this workflow" via env GITHUB_WORKFLOW (the workflow's
        display name from GitHub Actions). Adopters use arbitrary caller
        filenames (`shadow.yml`, `pr-review.yml`, …), so a per-workflow-id
        endpoint cannot be hard-coded — we query all repo runs and filter.

        Match logic:
          - Same workflow: run.name == GITHUB_WORKFLOW
          - Same item (PR): run.pull_requests contains a PR with .number == item_number
          - Same item (issue/issue_comment): name OR display_title contains `#<n>`
          - Exclude current run: run.id != GITHUB_RUN_ID

        The issue/issue_comment match works ONLY when the adopter's caller
        workflow embeds the item number via `run-name:` (the reusable
        workflow's own run-name is ignored under workflow_call). See
        `examples/caller-workflow.yml`.
        """
        try:
            since = (datetime.datetime.now(datetime.timezone.utc)
                     - datetime.timedelta(minutes=since_minutes))
            since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
            url = f"https://api.github.com/repos/{self._repo}/actions/runs"
            params = {"created": f">={since_iso}", "per_page": 100}
            resp = requests.get(url, headers=self._headers, params=params,
                                timeout=self._timeout)
            if resp.status_code != 200:
                logger.warning("Workflow runs API: %d", resp.status_code)
                return None
            data = resp.json() or {}
            runs = data.get("workflow_runs") or []
            target = str(item_number)
            this_workflow = os.getenv("GITHUB_WORKFLOW", "")
            self_run_id = os.getenv("GITHUB_RUN_ID", "")
            count = 0
            needle = re.compile(rf"#\s*{re.escape(target)}\b")
            for r in runs:
                if str(r.get("id") or "") == self_run_id:
                    continue
                if this_workflow and (r.get("name") or "") != this_workflow:
                    continue
                # Strongest signal: the API attaches PR records when the
                # event was a pull_request*. Match by number.
                pr_match = any(
                    str((pr or {}).get("number") or "") == target
                    for pr in (r.get("pull_requests") or [])
                )
                if pr_match:
                    count += 1
                    continue
                # Issue/issue_comment paths surface the number in the name
                # via run-name templating (`Shadow #${{ inputs.issue_number }}`).
                name = r.get("name") or ""
                display = r.get("display_title") or ""
                if needle.search(name) or needle.search(display):
                    count += 1
            return count
        except (requests.RequestException, ValueError, TypeError, KeyError) as e:
            logger.warning("Workflow runs API failed (%s); skipping rate limit",
                           type(e).__name__)
            return None

    def get_codebase_map(self):
        """List source files (excluding tests) as relative paths."""
        src_dir = self._cfg.codebase_src_dir
        file_ext = self._cfg.codebase_file_ext
        full_dir = os.path.join(self._repo_root, src_dir)
        prefix = self._repo_root.rstrip("/") + "/"
        try:
            paths = []
            for root, dirs, files in os.walk(full_dir):
                dirs[:] = [d for d in dirs if d not in ("examples", "__pycache__", ".git", "tests", "test")]
                for f in files:
                    if f.endswith(file_ext):
                        full = os.path.join(root, f)
                        rel = full[len(prefix):] if full.startswith(prefix) else full
                        paths.append(rel)
            return "\n".join(sorted(paths))
        except Exception as e:
            logger.error(f"Codebase map failed: {e}")
            return ""

    def read_local_file(self, path):
        repo_root = os.path.realpath(self._repo_root)
        if repo_root == "/":
            logger.error("Blocked: repo root is /")
            return ""
        full_path = os.path.realpath(os.path.join(self._repo_root, path))
        if not (full_path.startswith(repo_root + os.sep) or full_path == repo_root):
            logger.error(f"Blocked path traversal: {path}")
            return ""
        try:
            with open(full_path, "r", errors="replace") as f:
                return f.read()
        except Exception:
            return ""

    def get_file_content(self, path, repo=None, ref=None):
        target = repo or self._repo
        url = f"https://api.github.com/repos/{target}/contents/{path}"
        if ref:
            url += f"?ref={ref}"
        headers = {**self._headers, "Accept": "application/vnd.github.v3.raw"}
        try:
            resp = requests.get(url, headers=headers, timeout=self._timeout)
            return resp.text if resp.status_code == 200 else ""
        except Exception as e:
            logger.error(f"File fetch failed ({path}): {e}")
            return ""

    def post_comment(self, number, body):
        if self._dry_run:
            logger.info(f"[DRY RUN] Comment on #{number}: {body[:80]}...")
            return True
        body = _truncate_comment_body(body)
        return self._post(f"/repos/{self._repo}/issues/{number}/comments", {"body": body})

    def post_pr_review(self, number, summary, inline_comments, event="COMMENT",
                       commit_id=""):
        if self._dry_run:
            logger.info(
                "[DRY RUN] PR review on #%s: %d inline comments, event=%s, commit_id=%s",
                number, len(inline_comments), event, commit_id or "(current head)")
            return True

        # Get valid diff lines per file from the PR
        valid_lines = self._get_valid_diff_lines(number)

        valid_comments = []
        invalid_comments = []
        for ic in inline_comments:
            line = ic.get("line")
            path = ic.get("file", "")
            if line and path in valid_lines and line in valid_lines[path]:
                valid_comments.append({"path": path, "body": ic["comment"], "line": line, "side": "RIGHT"})
            else:
                invalid_comments.append(ic)

        body = summary
        if invalid_comments:
            body += "\n\n**Additional feedback:**\n"
            for ic in invalid_comments:
                line_ref = f":{ic['line']}" if ic.get('line') else ""
                body += f"\n`{ic['file']}{line_ref}` — {ic['comment']}\n"

        # GitHub rejects review/issue bodies above 65,536 chars with HTTP 422.
        # Truncate at 64K with a marker so the request still posts something
        # actionable instead of erroring out.
        body = _truncate_comment_body(body)
        valid_comments = [
            {**c, "body": _truncate_comment_body(c["body"])} for c in valid_comments
        ]

        payload = {"body": body, "event": event}
        # Pin the review to the analyzed commit. Without commit_id GitHub stamps
        # it with the current head at post time, so a push between analyze and
        # act would bind old-code analysis to the new head — and an auto-approve
        # gate keying on review.commit_id == tip would then act on a mismatch.
        if commit_id:
            payload["commit_id"] = commit_id
        if valid_comments:
            payload["comments"] = valid_comments

        try:
            resp = _retry_request(
                lambda: requests.post(
                    f"https://api.github.com/repos/{self._repo}/pulls/{number}/reviews",
                    headers=self._headers, json=payload, timeout=self._timeout,
                ),
            )
            if resp is not None and resp.status_code in (200, 201):
                return True
            if resp is not None:
                logger.error(f"PR review API failed: {resp.status_code}, falling back to comment")
                logger.error(f"Response: {resp.text[:500]}")
        except (requests.exceptions.RequestException, ValueError) as e:
            logger.error(f"PR review API failed: {type(e).__name__}, falling back to comment")

        body = summary
        if inline_comments:
            body += "\n\n**Inline feedback:**\n"
            for ic in inline_comments:
                line_ref = f":{ic['line']}" if ic.get('line') else ""
                body += f"\n`{ic['file']}{line_ref}` — {ic['comment']}\n"
        body = _truncate_comment_body(body)
        return self._post(f"/repos/{self._repo}/issues/{number}/comments", {"body": body})

    def _get_valid_diff_lines(self, number):
        """Extract valid right-side line numbers from each file's diff hunks."""
        import re
        valid = {}
        files = self.get_pr_files(number)
        for f in files:
            path = f.get("filename", "")
            patch = f.get("patch", "")
            if not patch:
                continue
            lines = set()
            current_line = None
            for line in patch.split("\n"):
                hunk = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
                if hunk:
                    current_line = int(hunk.group(1))
                    continue
                if current_line is None:
                    continue
                if line.startswith("-"):
                    continue
                if line.startswith("\\"):
                    continue
                lines.add(current_line)
                current_line += 1
            valid[path] = lines
        return valid

    def add_labels(self, number, labels):
        if not labels:
            return True
        if self._dry_run:
            logger.info(f"[DRY RUN] Labels on #{number}: {labels}")
            return True
        return self._post(f"/repos/{self._repo}/issues/{number}/labels", {"labels": labels})

    def _get(self, path):
        try:
            resp = _retry_request(
                lambda: requests.get(
                    f"https://api.github.com{path}", headers=self._headers,
                    timeout=self._timeout,
                ),
            )
            if resp is not None and resp.status_code == 200:
                return resp.json()
            if resp is not None:
                logger.error(f"GET {path}: {resp.status_code}")
        except (requests.exceptions.RequestException, ValueError) as e:
            logger.error(f"GET {path}: {type(e).__name__}")
        return None

    def _post(self, path, payload):
        try:
            resp = _retry_request(
                lambda: requests.post(
                    f"https://api.github.com{path}", headers=self._headers,
                    json=payload, timeout=self._timeout,
                ),
            )
            if resp is not None and resp.status_code in (200, 201):
                return True
            if resp is not None:
                logger.error(f"POST {path}: {resp.status_code}")
        except (requests.exceptions.RequestException, ValueError) as e:
            logger.error(f"POST {path}: {type(e).__name__}")
        return False
