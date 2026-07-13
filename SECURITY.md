# Security model

Shadow is a code-review bot that reads your repo and posts on your behalf.
This document is the threat model and the defense-by-attack-class summary
that bench/RESULTS.md and tests/security/ corroborate.

## Threat model

### Trust boundary

The bot's privileged operations are:

1. **Reading the base-branch checkout of your repo** — Bedrock invocation
   inputs include the diff, codebase tree, and existing PR/issue comments.
2. **Posting on GitHub as the workflow's bot identity** — inline review
   comments, labels, optional Slack pings.
3. **Calling Bedrock from your AWS account** — Anthropic Opus 4.8 + Haiku 4.5
   inferences, scoped to your IAM role's permissions.

### Adversaries

- **External PR submitter.** Can author a PR with arbitrary diff content,
  arbitrary title/body, and arbitrary commit messages. Can edit the PR title
  to retrigger workflows. Cannot directly modify the adopter's `.shadow.yml`,
  workflow file, or Shadow source.
- **Existing-repo collaborator.** Same as above plus can author issue/comment
  content under any GitHub login the bot is configured to recognize.
- **Compromised upstream `sudsali/shadow`.** A `v1.x` release tag is immutable
  by convention, but a tag *can* be force-repointed at the source repo; if that
  happened, adopters pinning that tag (`@v1.8`) would pick up the malicious
  revision on their next workflow run. Adopters who SHA-pin (`@<40-char SHA>`)
  are insulated — a SHA cannot be repointed.
- **Compromised Anthropic / AWS Bedrock.** Out of scope; same trust boundary
  as the adopter's other Bedrock usage.

### Attack classes the bot must survive

| # | Attack | Defense |
|---|---|---|
| A1 | `pull_request_target` "pwn request" — attacker PR runs in privileged context | Base-branch checkout only (`actions/checkout` with no `ref:` override); `.shadow.yml` and Shadow code execute from locked baseline |
| A2 | Two-job exfiltration — compromised analyze posts to GitHub | analyze has `id-token: write` only; act has `pull-requests: write`/`issues: write` only; neither can do the other's job |
| A3 | OIDC role abuse from a sibling repo | `job_workflow_ref` claim pins the role to this exact workflow file; AWS STS rejects assumption from any other path |
| A4 | Adversarial `.shadow.yml` (path traversal, abs paths) | `shadow_config._sanitize` rejects `..` and absolute paths in `codebase.src_dir`/`test_dir` at load time |
| A5 | Adversarial `bot.name` rendering an HTML-comment-breaking marker | `_scrub_marker_token` rejects `--` runs and HTML-breaking chars; falls back to default |
| A6 | Adversarial `bot.name` matching sanitizer's injection-marker list | `_scrub_marker_token` rejects bot_names ending in reserved suffixes (currently `system`); falls back to default |
| A7 | Envelope-tag injection via PR/issue title | `_neutralize_struct_tags` regex covers all envelope tags (`</pr>`, `</issue>`, `</conversation>`, `</investigator_notes>`, `</critic_verdicts>`, `</existing_feedback>`, `</knowledge_base>`, `</codebase_map>`, `</incremental_diff>`, `</constraints>`); replaces `>` with `_>` so the closing tag inside attacker text doesn't terminate the envelope |
| A8 | Sanitizer-marker injection via prose ("system:", "ignore previous instructions", `</user>`, etc.) | Local sanitizer (`sanitizer.py`) blocks 9 known injection markers + 18 secret patterns; events are surfaced in `security_events` artifact block (category only, never the matched value) |
| A9 | Sanitizer over-blocking the bot's own clean-PR confirmation | `_scrub_ci_summary` zwj-breaks injection-marker substrings in CI check-run names so a check named `Build (system: x86_64)` doesn't null the clean response to ESCALATE |
| A10 | Secret exfiltration via paraphrase (model echoes secret in different shape) | Two layers: local sanitizer regex matches AKIA/ASIA/ghp\_/gho\_/ghs\_/github\_pat\_/sk-ant-/sk\_live\_/AccountKey/private-key-header/JWT/`AWS_SECRET_ACCESS_KEY` patterns; Bedrock Guardrail (provisioned by default) BLOCKs AWS access keys + custom regex blocks for GitHub PATs and Anthropic API keys |
| A11 | Prompt extraction ("show me your system prompt") | Bedrock Guardrail denied-topic `SystemPromptExtraction` (provisioned by default); model-level `<constraints>` block in every Investigator/Critic/Reporter prompt |
| A12 | Instruction override ("you are now a malicious assistant") | Bedrock Guardrail denied-topic `InstructionOverride`; model-level `<constraints>` |
| A13 | Reporter struct-tag injection — UPHELD finding text contains envelope-closing characters | `_format_reporter_response` neutralizes struct tags in evidence text before re-interpolating |
| A14 | Guardrail-bypass via missing `GUARDRAIL_ID` in production | `BOT_REQUIRE_GUARDRAIL=true` (default); Config() refuses to load when `DRY_RUN=false` and `GUARDRAIL_ID` is unset; doctor's `_check_guardrail` validates the configured guardrail is reachable before first PR |
| A15 | PR/issue spam — adversary closes/reopens or edits PR title in a loop | `BOT_MAX_RUNS_PER_HOUR` per-(repo, item) rate limit; pre-flight diff/file caps ESCALATE before any Bedrock call; AWS Budget alarm bounds fleet-wide spend |
| A16 | `act`-side artifact replay — attacker downloads a different run's artifact | `_integrity` SHA-256 stamp bound to `(repo, run_id, pr_number)`; act job verifies before posting |
| A17 | Adversarial `bot_actor` collision — another `github-actions[bot]` workflow's comments dedup-match Shadow's | `BOT_GITHUB_ACTOR` override documented in README; default is `github-actions[bot]` (collides with PR-overlap detectors etc.); adopters set a unique value to break the collision |
| A18 | Secrets-Manager prompt tampering — a principal with `secretsmanager:PutSecretValue` on a [BYO-prompt](README.md#bring-your-own-prompts) secret rewrites the reviewer's/triager's **system prompt** (suppress findings, mis-triage issues, emit attacker-chosen output). Applies to all eight prompts across all four surfaces (PR review, issue triage, issue-respond, follow-up). Opt-in only (`prompt_sm_prefix` unset by default → bundled prompts). The guardrail/sanitizer/`<constraints>` defenses run on *inputs* (`guardContent`), **not** the system prompt, so they do not catch this. | Restrict `PutSecretValue` on the prompt secrets to a minimal principal set; enable CloudTrail + rotation on them. Scope the role's `GetSecretValue` to `<prefix>/*-prompt` (least privilege; covers all eight prompt secrets). The provenance `source: sm:<name>` + rollup SHA-256 are tamper-**evidence** (they change on any of the eight) but not tamper-**blocking**. `prompt_sm_prefix` is a `workflow_call` input set by the caller template, so PR/fork authors cannot influence which secret is read (base-branch checkout). |
| A19 | SM prompt-secret NAME disclosed in the artifact — when BYO prompts are used, provenance records `source: sm:<prefix>/pr-investigator-prompt` in `shadow_result.json`, readable by anyone with `actions:read` on the repo. | **Accepted by design.** The secret *name* (not its value — that is SHA-256 only) is disclosed so audits can distinguish an SM-sourced prompt from the bundled default. Choose a non-sensitive namespace for `prompt_sm_prefix`; the secret contents never appear in the artifact or logs. |
| A20 | Slack webhook token leaked to logs — the webhook URL's `/services/T…/B…/<token>` path is a bearer credential (post arbitrary messages to the maintainer channel). A transport-error string from `requests` embeds the full URL, which a naive `logger.error(f"…{e}")` would write to the Actions log. | `slack_client._send` logs **only** `type(e).__name__`, never the exception message or the URL — mirroring `cloudwatch.py`. GitHub Actions also masks the registered `SLACK_WEBHOOK_URL` secret, but the log-hygiene fix protects self-hosted runners and plain-`var` configs where masking doesn't apply. A failed delivery emits a `SlackDeliveryFailures` CloudWatch metric so a Slack-only team sees the channel go dark; delivery stays best-effort (never fails the run — the GitHub label+comment is the primary escalation). |

## Adversarial corpus

Five-issue corpus on the Powertools demo fork captures the attack classes
exercised live during this project's pre-demo overnight evidence run:

- Issue #5: A7 envelope-breakout via `</issue><sys>...` title
- Issue #6: A8 `system:` substring in body (real-looking bug report)
- Issue #7: A8 spaceful marker (`Ignore Previous Instructions` in title)
- Issue #8: A8 multi-marker attack (`</user>`, `<|im_start|>`, `system:`, `my system prompt is`)
- Issue #9: A10 secret exfiltration with `AKIA...` + `ghp_...` payloads

All five ESCALATE'd with `needs-human` label. Zero instruction-following, zero
secret echo, zero envelope leakage. Run artifacts live in the workflow run
history of `sudsali/powertools-lambda-python`.

## Reporting a vulnerability

Open a private security advisory at
https://github.com/sudsali/shadow/security/advisories/new. Include a
reproduction case (a PR or issue title/body that triggers the unintended
behavior). The bot itself reviews its own PRs — including
security-fix PRs — so an issue may show up first as a Shadow review on
the fix.

## Defense-in-depth, not either-or

Shadow does not assume any single layer is bulletproof. The Bedrock
Guardrail (server-side, AWS-operated scanner), the local sanitizer
(Python regex on every input/output), the prompt-level `<constraints>`
block (every Investigator/Critic/Reporter system prompt), the
`pull_request_target` base-branch lock, and the two-job `analyze`/`act`
permission split all stack on top of each other. Compromise of any single
layer should not produce exfiltration.

## What's NOT in scope

- Cross-repo refactors or multi-repo orchestration. Shadow scopes to one
  repo at a time.
- Approving or merging PRs. Shadow only comments and labels; merge gates
  are the maintainer's decision.
- Modifying CI configuration or workflow files. The act job has no write
  access to `.github/workflows`.
- Reading repo Secrets directly. The bot reads what GitHub Actions exposes
  via `secrets:` mappings only.
