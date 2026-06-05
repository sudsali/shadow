# Shadow

**An adversarial code-review bot for OSS maintainers, on AWS Bedrock.**

You maintain a public repo. PRs land faster than you can read them. Issues pile up. Existing AI bots make it worse — they post every plausible finding from one LLM pass and you learn to scroll past them.

Shadow runs a **second agent that has to disprove the first** before anything reaches your repo. One calibrated comment per PR — not a thread of false positives.

- ✅ Reviews PRs with inline comments + a refutation trail you can audit
- ✅ Triages new issues (responds, escalates, or closes)
- ✅ Replies to follow-up comments on issues until you tell it to stop
- ✅ Optional Slack ping on every escalation
- ✅ Runs in **your** AWS account — no third-party SaaS, no data leaving your perimeter
- ✅ Mean ~$0.61/PR across 9 OSS PRs in 3 languages ([bench](bench/RESULTS.md))
- ✅ Caught a real bug [CodeRabbit missed on AutoGPT](bench/HEAD_TO_HEAD.md) — same diff, same PR

Apache 2.0. Five lines of YAML to install. No fork required.

---

## What Shadow does for your repo

Shadow has four surfaces. Pick the ones you want — they're independent.

### 1. PR review

When a PR is opened or pushed, Shadow reads the diff, investigates with `read_file` / `grep_codebase` / `find_callers` / `find_tests_for`, and posts inline comments on findings that survive a second agent's disprove pass.

Each comment carries a collapsed `<details>` block — the **Refutation Trail** — showing the Investigator's hypothesis and the Critic's failed counter-argument. You can read why each finding survived without leaving the PR.

If the Critic overturns every finding, the bot still posts a single `<!-- {bot.name}:clean -->`-marked confirmation so you can see the review ran. (Default `<!-- shadow:clean -->`; renamed if you set `bot.name` in `.shadow.yml`.)

### 2. Issue triage

When an issue is opened or reopened, Shadow classifies it into one of three actions:

- **RESPOND** — Shadow can answer from the codebase itself. Posts a comment citing specific files/lines, then escalates if the user pushes back.
- **ESCALATE** — Adds a configurable label (default `needs-human`) and pings Slack if configured. The bot doesn't engage further until you do.
- **CLOSE** — Spam, duplicate, or out-of-scope. Default is conservative — when in doubt, ESCALATE.

The bot is told to default to ESCALATE; maintainer attention is the scarce resource it's protecting.

### 3. Follow-up replies on `issue_comment`

When someone replies to an issue Shadow already commented on, Shadow can reply once more if the comment looks answerable from the codebase, or escalate if the conversation isn't converging. Capped at 2 replies per issue/PR (`BOT_MAX_REPLIES`) before every subsequent comment escalates instead.

### 4. Slack escalation (optional)

Set `SLACK_WEBHOOK_URL` and Shadow posts a one-line summary to a Slack channel on every ESCALATE — title, labels, and a link back to GitHub. Off by default.

### What Shadow does NOT do

- **Auto-close PRs.** Shadow only comments and labels. You decide what merges.
- **Auto-merge or auto-approve.** Same.
- **Cross-repo refactors.** Shadow scopes to one repo at a time.
- **Check out PR head code.** The workflow uses `pull_request_target` and reads only the base-branch checkout, defending against the [pwn-request attack](https://securitylab.github.com/research/github-actions-preventing-pwn-requests/) where untrusted PR code runs in privileged context.

---

## How it works

```
   Investigator  ──▶  Critic  ──▶  Reporter
   (finds)            (disproves)   (posts only what survives)
```

Three agents. The Investigator emits structured findings with hypothesis, evidence, and confidence. The Critic — running with **independent context, no shared tool trace** — re-reads the cited code with its own tool calls and tries to overturn each finding. The Reporter posts only findings the Critic upheld AND with confidence ≥ 8.

This is generator–verifier as the **core architecture**, not a re-rank step. A single agent can't disprove itself; the pipeline forces independent verification before anything reaches your repo.

**Cost-aware model split:** Investigator and Critic run on Opus 4.7 (deep search + independent verification). Reporter runs on Haiku 4.5 (cheap structured-JSON formatting). The two reasoning stages dominate token spend; the Reporter is a thin formatting pass.

---

## Install (5 minutes)

No fork required. You'll install three things:

### 1. Caller workflow — `.github/workflows/shadow.yml`

Copy [`examples/caller-workflow.yml`](examples/caller-workflow.yml) verbatim. Subscribes to PRs, issues, and issue comments. Templates the run-name so the rate-limit gate works for issue events. The minimal shape:

```yaml
name: Shadow
run-name: "Shadow #${{ github.event.pull_request.number || github.event.issue.number || inputs.pr_number }}"
on:
  pull_request_target:
    types: [opened, reopened, synchronize]
  issues:
    types: [opened, reopened]
  issue_comment:
    types: [created]    # remove this trigger if you don't want followup replies
  workflow_dispatch:
    inputs:
      pr_number:
        required: true
      dry_run:
        type: boolean
        default: true
permissions:
  contents: read
  id-token: write
  pull-requests: write
  issues: write
jobs:
  shadow:
    uses: sudsali/shadow/.github/workflows/shadow-review.yml@v0
    secrets:
      AWS_ROLE_ARN: ${{ secrets.AWS_ROLE_ARN }}
```

### 2. Config — `.shadow.yml` at repo root

Only `codebase.src_dir` is required. See [`examples/shadow.example.yml`](examples/shadow.example.yml) for everything you can tune.

```yaml
codebase:
  src_dir: src/    # required — your primary source directory
```

### 3. AWS role — `AWS_ROLE_ARN` secret

[Click the Launch Stack button](#aws-setup) (one click), or [follow the manual setup](#manual-setup-alternative). Either way you end up with a `arn:aws:iam::...:role/shadow-bot-ci` to paste into your repo's `AWS_ROLE_ARN` secret (Settings → Secrets and variables → Actions).

### 4. Open a PR

Or reopen an issue, or comment on a tracked issue. Shadow runs.

> **Preflight (recommended).** After AWS setup but before your first real PR, validate the wiring locally:
> ```sh
> git clone https://github.com/sudsali/shadow && cd shadow
> pip install -r requirements.txt
> python -m shadow.doctor --role-arn $AWS_ROLE_ARN --region us-east-1
> ```
> The doctor validates the role ARN shape, calls Bedrock with a 1-token Converse to confirm model access, parses your `.shadow.yml` (point `--repo-root` at it if you run doctor from a different directory), and verifies prompts load. Each failure prints a fix link.

---

## What it costs

**~$0.61/PR mean** across the [9-PR bench corpus](bench/RESULTS.md), with most PRs landing between $0.23 and $1.11. The deliberately-large head-to-head PR against CodeRabbit on AutoGPT runs ~$4.79 — large-diff PRs are where the Critic does its most expensive work re-deriving findings independently.

That's higher than single-call review bots in the $0.05–$0.30 range. The cost buys verification: the Critic's job is to **overturn false positives so you don't pay attention to noise**. The calculus most adopters land on is "is N false positives more expensive than a dollar?"

You pay the bill — Bedrock is invoked from your AWS account on your credentials. Pin `shadow_ref` to a specific SHA, set the cost levers below, and watch [bench/RESULTS.md](bench/RESULTS.md) to forecast monthly spend on your traffic.

**Cost levers, in order of impact:**

| Lever | Effect | Trade-off |
|---|---|---|
| `BOT_INVESTIGATOR_MAX_TOOL_CALLS=5` + `BOT_CRITIC_MAX_TOOL_CALLS=4` | ~50% cost cut | Quality cost on complex PRs |
| Override `BEDROCK_MODEL_ID` to Haiku 4.5 across all stages | ~80% cost cut | Quality cost — bench before adopting |
| `paths-ignore: ['**/*.md', 'docs/**', '.github/**']` in caller workflow | Zero cost on docs PRs | Docs PRs unreviewed |
| Run only on labelled PRs: `if: contains(github.event.pull_request.labels.*.name, 'needs-shadow')` | Linear with label use | Maintainer must label |
| AWS Budgets alarm at $50/month with auto-action `SHADOW_DISABLED=true` | Hard cap | Bot stops mid-month if blown |

The CloudFormation Launch Stack provisions the Budget for you; see [Cost protection](#cost-protection).

---

## Configuration

### `.shadow.yml` reference

```yaml
codebase:
  src_dir: aws_lambda_powertools    # required — your primary source directory
  file_ext: .py                     # optional — narrows tool searches
  test_dir: tests                   # optional — bot infers from src_dir if omitted
  language: python                  # optional — used by issue-respond prompt only

bot:
  name: shadow                      # used in marker comments; alphanumeric + '-_' only
  escalate_label: needs-human       # must already exist in your repo's Labels
  max_replies: 2                    # 0..100; cap on followup replies before next user comment ESCALATEs
  max_runs_per_hour: 20             # 0..100; per-(repo, item) rate limit; 0 disables

# Override per-stage models if you want a different cost/quality trade-off.
# Env vars BEDROCK_MODEL_ID / BEDROCK_REPORTER_MODEL_ID / BEDROCK_CRITIC_MODEL_ID take precedence.
models:
  investigator: us.anthropic.claude-opus-4-7
  critic: us.anthropic.claude-opus-4-7
  reporter: us.anthropic.claude-haiku-4-5-20251001-v1:0
```

### Env vars (set in your caller workflow's `env:` block)

| Env var | Default | Purpose |
|---|---|---|
| `BOT_AGENT_PIPELINE` | `1` | Set `0`/`false`/`no`/`off` to disable PR review (PR events ESCALATE with `pipeline_disabled`). **Issue/issue_comment events still run** — use `SHADOW_DISABLED=true` for a fleet-wide kill-switch. |
| `BOT_INVESTIGATOR_MAX_TOOL_CALLS` | `10` | Investigator tool budget. Lower = cheaper, less depth. |
| `BOT_CRITIC_MAX_TOOL_CALLS` | `8` | Critic tool budget. |
| `BOT_INVESTIGATOR_MAX_TURNS` | `15` | Hard cap on Investigator agent loop turns. |
| `BOT_CRITIC_MAX_TURNS` | `10` | Hard cap on Critic agent loop turns. |
| `BOT_AGENT_MAX_DIFF_CHARS` | `200000` | Per-agent diff truncation limit. |
| `BOT_PIPELINE_WALL_CLOCK_S` | `480` | Total agent-pipeline wall-clock budget. |
| `BOT_REPORTER_MIN_REMAINING_S` | `60` | Reporter is pre-empted if less than this remains. |
| `BOT_MAX_DIFF_FOR_REVIEW_CHARS` | `100000` | Pre-flight diff cap. Diff above this → ESCALATE before any Bedrock call. |
| `BOT_MAX_FILES_FOR_REVIEW` | `50` | Pre-flight file-count cap. Same shape as above. |
| `BOT_MAX_RUNS_PER_HOUR` | `20` | Per-(repo, item) hourly run cap. `0` disables. Capped at `100`; values above clamp with a warning. Negative values fall back to default. Hit → ESCALATE with `shadow:rate-limited` label. |
| `BOT_MAX_REPLIES` | `2` | Followup-reply cap per (issue, PR). Capped at `100`; negative falls back to default. |
| `BOT_GITHUB_ACTOR` | `github-actions[bot]` | GitHub login Shadow's comments appear under. **Set this to a unique value** if your repo has other workflows that also post as `github-actions[bot]` (e.g., PR-overlap detectors, claim-checkers). Otherwise Shadow's `already_commented` dedup matches their comments and silently SKIPs every PR. |
| `DRY_RUN` | `false` | When `true`, Shadow writes the artifact but doesn't post comments. |
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-opus-4-7` | Investigator model. |
| `BEDROCK_REPORTER_MODEL_ID` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Reporter model. Default is Haiku because Bedrock's `outputConfig.textFormat` is Haiku-only over Converse today; Opus 4.7 rejects it. |
| `BEDROCK_CRITIC_MODEL_ID` | falls back to `BEDROCK_MODEL_ID` | Critic model. |
| `GUARDRAIL_ID` / `GUARDRAIL_VERSION` | unset | Bedrock Guardrail ID + version. **The CFN Launch Stack provisions one by default**; copy the `GuardrailId`/`GuardrailVersion` outputs into these repo secrets after stack deploy. Without them set, the bot still works but skips the server-side prompt-injection scanner — local sanitizer + prompt constraints are the fallback. |
| `KB_S3_BUCKET` / `KB_S3_KEY` | unset | Optional S3-hosted knowledge base appended to the Investigator's system prompt. Useful for project-specific conventions. The IAM role needs `s3:GetObject` on that bucket; CFN doesn't grant this — extend the role yourself. |
| `SLACK_WEBHOOK_URL` | unset | Slack channel webhook for escalation pings. Set to a [Slack incoming webhook URL](https://api.slack.com/messaging/webhooks); the bot posts a one-line summary to that channel on every ESCALATE. |
| `SHADOW_DISABLED` | unset | Set as a repo or org **variable** (Settings → Variables, NOT Secrets) to fleet-wide-disable. |
| `SHADOW_VERIFY_ARTIFACT` | `true` | Verifies analyze→act artifact integrity. Set `false` only for combined-job flows. |
| `SHADOW_CLOUDWATCH_DISABLED` | `false` | Disables CloudWatch metric emission. |
| `SHADOW_CLOUDWATCH_NAMESPACE` | `Shadow` | CloudWatch namespace override (the CFN policy is scoped to `Shadow` — change both together). |

---

## AWS setup

Shadow is BYO-AWS today. The bot calls Bedrock from **your** account, you get the bill, you own the audit trail.

### Recommended: one-click CloudFormation Launch Stack

[![Launch Stack](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)](https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/quickcreate?templateURL=https://raw.githubusercontent.com/sudsali/shadow/v0/infrastructure/shadow-iam-stack.yaml&stackName=shadow-bot)

The button opens AWS Console with [`infrastructure/shadow-iam-stack.yaml`](infrastructure/shadow-iam-stack.yaml) pre-loaded. Fill in:

| Parameter | What to enter |
|---|---|
| **GitHubOrg** | Your GitHub org or username |
| **GitHubRepo** | Repo name. No default — pick one. Pass `*` only if you've audited every repo in the org. |
| **ShadowSourceRepo** | `sudsali/shadow` (default) — or your fork's `owner/repo` if you maintain a hardened private copy |
| **ShadowWorkflowRef** | `*` for quick start, or a 40-char SHA to pin trust to one audited revision |
| **BedrockRegion** | Where Bedrock will be invoked. `us-east-1` / `us-west-2` / `us-east-2` are the validated combinations; other regions work if both Opus 4.7 and Haiku 4.5 are available there ([model-region matrix](https://docs.aws.amazon.com/bedrock/latest/userguide/models-regions.html)). The region you pick here must match where you enable model access in the next step. |
| **ExistingOidcProviderArn** | Leave blank if your account has no GitHub OIDC provider yet. **If your account already uses GitHub Actions OIDC, paste the existing provider ARN** (`aws iam list-open-id-connect-providers`). Leaving blank when one exists fails with `EntityAlreadyExists`. |
| **MonthlyBudgetLimit** + **BudgetEmailAddress** | Optional. Set both to enable an AWS Budget that emails at 80% / 100% of the cap. `0` / blank skips the alarm. |
| **ProvisionGuardrail** | Default `true`. Provisions a Bedrock Guardrail with prompt-attack defense + PII blocks (see [Security model](#security-model)). Set to `false` only if you maintain a custom guardrail and want to point Shadow at it via the `GUARDRAIL_ID`/`GUARDRAIL_VERSION` secrets. |

The stack creates the OIDC provider (if needed), an IAM role with the canonical `job_workflow_ref`-pinned trust policy, a Bedrock-invoke permission scoped to Anthropic models only, AND (by default) a Bedrock Guardrail with prompt-attack + PII filters. After deploy, copy these outputs into repo secrets:

- `ShadowRoleArn` → `AWS_ROLE_ARN` (always required)
- `GuardrailId` → `GUARDRAIL_ID` (when ProvisionGuardrail=true)
- `GuardrailVersion` → `GUARDRAIL_VERSION` (when ProvisionGuardrail=true)

You still need to **enable Bedrock model access** (the stack can't do this for you):

> AWS Console → Bedrock (in your `BedrockRegion`) → Model access → enable `anthropic.claude-opus-4-7` AND `anthropic.claude-haiku-4-5`. Auto-subscribes in ≤ 15 min.

After the stack is up:

```sh
python -m shadow.doctor --role-arn $ARN --region $REGION
```

> **Heads-up on the template URL:** the Launch Stack button resolves `…/sudsali/shadow/v0/infrastructure/shadow-iam-stack.yaml` at click time. It tracks the moving `v0` tag. If you re-launch the stack later, AWS fetches whatever is at `v0` *then* — not what you saw before. For reproducible IAM provisioning, download the YAML at a specific SHA and upload it manually.

### Manual setup (alternative)

If you prefer not to run CloudFormation:

1. **Bedrock model access** — same Console step as above.

2. **GitHub OIDC provider** in IAM (idempotent):
   - Provider URL: `https://token.actions.githubusercontent.com`
   - Audience: `sts.amazonaws.com`

3. **IAM role** `shadow-bot-ci`. Trust policy:
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Principal": {"Federated": "arn:aws:iam::ACCT:oidc-provider/token.actions.githubusercontent.com"},
       "Action": "sts:AssumeRoleWithWebIdentity",
       "Condition": {
         "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
         "StringLike": {
           "token.actions.githubusercontent.com:sub": "repo:YOUR_ORG/YOUR_REPO:*",
           "token.actions.githubusercontent.com:job_workflow_ref": "sudsali/shadow/.github/workflows/shadow-review.yml@*"
         }
       }
     }]
   }
   ```
   Replace `ACCT` with your account ID and `YOUR_ORG/YOUR_REPO` with your GitHub repo. **If you forked Shadow**, replace `sudsali/shadow` in `job_workflow_ref` with your fork. The two `StringLike` claims combine with **AND**: `sub` limits which repo can assume the role; `job_workflow_ref` pins to Shadow's workflow file. For monorepo installs use `repo:YOUR_ORG/*:*` only if you've audited every repo.

   Permission policy. Replace `REGION` with your Bedrock region (`us-east-1`, `us-west-2`, etc.). For multi-region setups add a Statement per region — Bedrock ARNs are region-scoped:
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": ["bedrock:InvokeModel", "bedrock:Converse", "bedrock:InvokeModelWithResponseStream"],
       "Resource": [
         "arn:aws:bedrock:REGION::foundation-model/anthropic.*",
         "arn:aws:bedrock:REGION:*:inference-profile/us.anthropic.*"
       ]
     }]
   }
   ```

4. **Set `AWS_ROLE_ARN` repo secret** to the role's ARN (e.g., `arn:aws:iam::123456789012:role/shadow-bot-ci`).

---

## Security model

You're letting a bot read your repo and post on your behalf. Here's the trust boundary.

- **Base-branch lock.** The workflow uses `pull_request_target` with the **base branch** checked out — never the PR head. `.shadow.yml` and Shadow code execute from the locked baseline, not from untrusted PR content. This is the documented mitigation for the [pull_request_target attack pattern](https://securitylab.github.com/research/github-actions-preventing-pwn-requests/).
- **Two-job permission split.** The `analyze` job has `id-token: write` (calls Bedrock) but **cannot post to GitHub**. The `act` job has `pull-requests: write` + `issues: write` but **cannot call AWS**. Compromise of either job has reduced blast radius.
- **OIDC trust scoping.** The `job_workflow_ref` claim pins your AWS role to **this workflow file at this version**. A fork that copies the workflow path can't assume your role — the claim is checked by AWS STS, not by Shadow.
- **`.shadow.yml` path validation.** Absolute paths and `..` segments in `codebase.src_dir`/`test_dir` are rejected at load time; the field falls back to default with a workflow-log warning.
- **`bot.name` is sanitized.** Names that would render an HTML-comment-breaking marker (`shadow--evil`) or one matching the prompt-injection sanitizer (`system`) are rejected and fall back to the default.
- **Comment marker.** Clean reviews carry `<!-- {bot.name}:clean -->` for grep-ability — `<!-- shadow:clean -->` by default. Re-runs post a new review; edit-in-place is not currently implemented.
- **Bedrock data privacy.** Per [AWS Bedrock data protection](https://docs.aws.amazon.com/bedrock/latest/userguide/data-protection.html): Bedrock does not use your inputs/outputs to train base models, and Anthropic has no access to your prompts or completions. **Caveat:** if your AWS account has [CloudWatch model invocation logging](https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html) enabled, full request/response payloads (including PR diffs) land in your CloudWatch logs.
- **Bedrock Guardrail provisioned by default.** The CloudFormation Launch Stack creates a Shadow-owned Bedrock Guardrail (`ProvisionGuardrail=true` is the default) with **PROMPT_ATTACK at HIGH input strength** (Bedrock's headline jailbreak/instruction-override scanner), denied topics for system-prompt extraction and instruction-override (matched by example), `AWS_ACCESS_KEY` and `AWS_SECRET_KEY` BLOCK on the PII filter, and custom regex blocks for GitHub PATs (`ghp_`/`gho_`/`ghs_`/`github_pat_`) and Anthropic API keys (`sk-ant-`). After the stack deploys, paste the `GuardrailId` and `GuardrailVersion` outputs into your repo secrets (alongside `AWS_ROLE_ARN`) so the bot wires the guardrail into every Converse call. This stacks on top of the local sanitizer (`tests/security/`) — defense in depth, not either-or. Set `ProvisionGuardrail=false` only if you maintain a custom guardrail and want to point Shadow at it via the same secrets. Filter strengths are conservative defaults; tune in the AWS Console if the guardrail false-positives on legitimate PR text.

### Supply-chain pinning

Pick the trade-off:

- **`@v0`** (moving tag) — auto-updates to whatever upstream tags as `v0` next. **Lowest friction; you don't control which version reviews your code.** Suitable for trying the bot.
- **`@<40-char SHA>`** — frozen at the version you audited. Manual update required. **Recommended for production.** Add a Dependabot config so SHA bumps land as PRs you can review:
  ```yaml
  # .github/dependabot.yml — bumps @<SHA> pins; silently inert for @v0
  version: 2
  updates:
    - package-ecosystem: github-actions
      directory: "/"
      schedule: { interval: weekly }
  ```
- **Fork into your org and pin to your fork's SHA** — full org control. Recommended when your trust boundary is the org, not an individual GitHub account.

> **`v0` tag stability.** The `v0` tag is currently force-pushed as the project iterates — adopters pinning `@v0` get the latest revision on every workflow run. The first numbered release (`v1.0`) will freeze the tag-version contract: from then on, `v0` will not move and follow-on changes ship as semver releases (`v1.1`, `v1.2`, …). Until that release, treat `@v0` as "latest" semantics. SHA-pinned adopters are insulated from any tag movement.

---

## Audit trail

Every analyze run writes a `shadow_result.json` artifact, retained 7 days by GitHub. Read these blocks if you need to audit what Shadow did on your PR:

- **`provenance`** — per-stage prompt fingerprints (SHA-256), a rollup hash, the Shadow git ref, and the model IDs that ran. Tampering with `prompts/*.txt` upstream changes the rollup. An inline `*_PROMPT` env var override flips the per-prompt `source` field from `file:prompts/...` to `env:VAR_NAME`.
- **`security_events`** — per-PR histogram of sanitizer blocks during the analyze stage. Categories like `aws_access_key`, `github_pat`, `jwt`, `ignore previous instructions` — never the matched value itself. The `by_category` list shows which categories fired and how often. (Sanitizer blocks during the act stage land in workflow logs, not the artifact.)
- **`_integrity`** — SHA-256 of the artifact body, bound to `(repo, run_id, pr_number)`. The `act` job verifies this before posting; a replayed or tampered artifact is rejected. `RUN_ATTEMPT` is intentionally excluded so `gh run rerun --failed-only` still verifies. Set `SHADOW_VERIFY_ARTIFACT=false` to opt out (combined-job flows only).
- **Refutation Trail in posted comments** — UPHELD findings include a collapsed `<details>` block showing the Investigator's hypothesis and the Critic's disprove attempt when either is non-empty. The disprove pattern is *auditable* because adopters can read why each surviving finding survived.
- **CloudWatch custom metrics** — `Shadow/CostPerPR`, `Shadow/CriticOverturnRate`, `Shadow/InputTokens`, `Shadow/OutputTokens`, `Shadow/Invocations`, `Shadow/Escalations` per analyze run. Dimensions: `Repository`, `Pipeline`. Set `SHADOW_CLOUDWATCH_DISABLED=true` to opt out. CloudFormation grants `cloudwatch:PutMetricData` scoped to the `Shadow` namespace.

---

## Cost protection

The per-PR levers (under [What it costs](#what-it-costs)) bound a single review. These three guards bound **fleet-wide** spend, defending against PR/issue spam and runaway traffic:

- **Per-(repo, item) hourly rate limit** (`BOT_MAX_RUNS_PER_HOUR`, default `20`). Caps how many times a single PR or issue can trigger Shadow per rolling hour. Beyond the limit, the bot ESCALATES with the `shadow:rate-limited` label instead of running the agent pipeline. Defends against an adversary closing/reopening or editing a PR title in a loop. Set to `0` to disable. **Issue/issue_comment events** require `run-name: "Shadow #${{ github.event.issue.number || ... }}"` in your caller workflow so the rate-limit gate can match prior runs (see [`examples/caller-workflow.yml`](examples/caller-workflow.yml)).
- **Pre-flight diff/file caps** (`BOT_MAX_DIFF_FOR_REVIEW_CHARS`, `BOT_MAX_FILES_FOR_REVIEW`, defaults `100000` / `50`). A 50-file PR makes the Investigator read 5+ files, the Critic re-reads, the Reporter formats — costs multiply. Diff or file count above the cap → ESCALATE before any Bedrock call. Pre-flight escalation is ~$0; a runaway pipeline on a giant PR is $5+.
- **AWS Budgets opt-in via CFN** (`MonthlyBudgetLimit` parameter on `shadow-iam-stack.yaml`). Set a positive USD amount + a `BudgetEmailAddress` and the stack creates an `AWS::Budgets::Budget` filtered to Amazon Bedrock spend, with email alerts at 80% and 100%. `0` skips Budget creation (default — AWS Budgets bills $0.02/budget/day, so opt-in only). Email-only today; auto-shutdown via `SHADOW_DISABLED` is a planned upgrade.

---

## Removing Shadow

| Scope | How |
|---|---|
| **Single repo** | Delete `.github/workflows/shadow.yml`. |
| **Org-wide pause** | Set repo or org **variable** `SHADOW_DISABLED=true` (Settings → Secrets and variables → Actions → **Variables tab**, NOT Secrets — GitHub doesn't allow `secrets.*` in `workflow_call` job-level `if:`). Both jobs skip. Faster than deleting workflow files across many repos. |
| **AWS cleanup** | Delete or revoke the `shadow-bot-ci` IAM role to drop the Bedrock-invoke permission. |

If you previously tried to set `SHADOW_DISABLED` as a Secret rather than a Variable: move it to Variables — the secret form silently never gated the workflow.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Could not assume role` from configure-aws-credentials | OIDC trust policy mismatch | Verify `sub` matches `repo:YOUR_ORG/YOUR_REPO:*` and `job_workflow_ref` includes the Shadow workflow path. Run `aws sts get-caller-identity` from a minimal workflow first. |
| `AccessDeniedException` on Bedrock | Model access not enabled in region | AWS Console → Bedrock → Model access → enable Opus 4.7 + Haiku 4.5. |
| `ValidationException` on Bedrock call | Wrong model ID format | Check `BEDROCK_MODEL_ID` is `us.anthropic.claude-opus-4-7` (no `-v1` suffix on 4.7). |
| `Required prompt missing: prompts/pr-investigator.txt` | `@v0` (or pinned SHA) doesn't include `prompts/` | Verify the ref in `sudsali/shadow` includes the `prompts/` directory. If you forked, ensure your tag does too. |
| No comments posted, workflow green | `dry_run: true` | Set `dry_run: false` in caller-workflow inputs. |
| Every PR escalates with `prompt_load_failed` | `prompts/` not present at the pinned `shadow_ref` | Same as above. |
| `existing_feedback` always empty | Caller workflow missing `pull-requests: read` | Reusable workflow declares this; if you customized the caller, ensure permissions include `pull-requests: read`. |
| `Artifact integrity check failed: artifact from different workflow_run` | `act` downloaded an artifact from a different run (manual replay, race) | Re-trigger from latest commit. For combined-job flows set `SHADOW_VERIFY_ARTIFACT=false`. |
| CFN stack fails with `EntityAlreadyExists: GitHubOidcProvider` | Account already has the GitHub OIDC provider | Re-run with `ExistingOidcProviderArn` set to the existing ARN (`aws iam list-open-id-connect-providers`). |
| `bot.name=… would render a marker tripping the sanitizer` warning | `bot.name` ends in `system` (or matches a future-reserved suffix) | Pick a different `bot.name` — it'd otherwise null every clean response to ESCALATE. |

---

## Roadmap

**Implemented** (shipped, covered by tests + CI):

- BYO-AWS reusable workflow with two-job security split (`analyze` / `act`)
- One-click CloudFormation Launch Stack for IAM, OIDC trust, AWS Budget, **and a default Bedrock Guardrail** with prompt-attack + PII filters (set `ProvisionGuardrail=false` to skip)
- `shadow doctor` preflight CLI (verifies role, Bedrock access, prompts)
- Audit trail in artifact: prompt-hash provenance, security-events histogram, SHA-256 integrity stamp bound to `(repo, run_id, pr_number)`
- Refutation Trail rendered into posted comments (`<details>` block per finding)
- CloudWatch custom metrics (cost, overturn rate, tokens, invocations, escalations)
- `.shadow.yml` config: per-stage model overrides, custom marker name, escalate label, reply/run caps
- Multi-PR bench results across Python / Java / Go forks ([`bench/RESULTS.md`](bench/RESULTS.md))
- Head-to-head bench vs CodeRabbit on AutoGPT ([`bench/HEAD_TO_HEAD.md`](bench/HEAD_TO_HEAD.md))
- Issue triage (RESPOND / ESCALATE / CLOSE) + followup replies on `issue_comment`
- Slack escalation pings (optional via `SLACK_WEBHOOK_URL`)

**In progress** (not shipped):

- Eval harness gating prompt changes against a fixture corpus
- False-positive rate measurement on a clean-PR corpus (we have an [adversarial-corpus pass](bench/RESULTS.md) and a 9-PR known-bug bench; the FP rate on PRs with no bugs is the missing data point)

**Under discussion** (no commitment):

- Composite-action wrapper for GitHub Marketplace listing
- Haiku-first model split for cost reduction
- Hosted variant where Anthropic / a third party operates the AWS account, eliminating BYO-AWS setup
- Auto-shutdown action when AWS Budget alarm fires (currently email-only)

---

## License

Apache 2.0. See [LICENSE](LICENSE).

## Contributing

Issues and PRs welcome. The bot reviews its own PRs.

```sh
pip install -r requirements.txt pytest
python -m pytest tests/
```

The suite has three tiers: `tests/unit/` (pure helpers), `tests/integration/` (yaml + filesystem), `tests/contract/` (response schema + prompt presence). CI runs all three plus `actionlint` on every PR.
