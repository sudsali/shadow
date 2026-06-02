# Head-to-head: Shadow vs CodeRabbit on `Significant-Gravitas/AutoGPT`

This bench compares Shadow against an existing AI review bot (CodeRabbit) on the *same* PR diff. Two cases.

---

## Case 1 (smoking gun): Shadow caught a bug CodeRabbit MISSED

**Upstream PR:** [Significant-Gravitas/AutoGPT#13236](https://github.com/Significant-Gravitas/AutoGPT/pull/13236) — `refactor(backend): plumb ExecutionContext at direct-block-execute boundary`. Merged.

**What CodeRabbit said on #13236:**
- Posted "**Actionable comments: 0**"
- Only one inline comment, on a midnight-rollover wall-clock test assertion (minor)
- Did **not** flag the autopilot/copilot scope gap

**What the upstream maintainers had to do later:** ship a separate fix PR — [#13247](https://github.com/Significant-Gravitas/AutoGPT/pull/13247) — to address what was missing. From #13247's description (verbatim, emphasis added):

> The same logical bug existed — and **still exists post-#13236** — at the copilot/autopilot `run_block` tool boundary: `backend/copilot/tools/helpers.py::prepare_block_for_execution` constructs an `ExecutionContext` for every block invoked by autopilot, but never set `user_timezone`. **Time blocks called from autopilot with `use_user_timezone=true` therefore returned UTC time regardless of the user's profile.**

This is the gap CodeRabbit missed.

**What Shadow found on the equivalent diff** ([fork bench PR](https://github.com/sudsali/AutoGPT/pull/3); [workflow run](https://github.com/sudsali/AutoGPT/actions/runs/26734397922)):

> **BUG** — `autogpt_platform/backend/backend/copilot/tools/helpers.py:244`
>
> Diff removes `user_timezone=user_timezone` from `ExecutionContext` construction in copilot's `execute_block`. Since `ExecutionContext.user_timezone` defaults to `"UTC"`, time blocks executed via copilot/autopilot tool boundary now always use UTC regardless of user's configured timezone. A user in America/New_York with `use_user_timezone=True` on a time block will get UTC results when invoked through copilot, but correct local results via REST — silent data divergence by invocation path.

**Evidence Shadow attached** (full Refutation Trail in the artifact):

- `backend/data/execution.py:102` — `user_timezone: str = "UTC"` (the default that takes over when omitted)
- `backend/blocks/time_blocks.py:193, 303, 409` — three call sites that read `execution_context.user_timezone`
- `backend/blocks/time_blocks.py:79-80` — the conditional `if format_type.use_user_timezone and user_timezone:` that branches on the now-default-UTC value

Result: **same root cause as upstream PR #13247 identified, same trigger, same affected files. Found in 1 inline finding, 0% Critic overturn rate, $4.79 on `c0b4297` (was $2.38 pre-hardening).**

### Side-by-side

| | CodeRabbit on #13236 | Shadow on equivalent diff |
|---|---|---|
| Found the autopilot/copilot scope gap? | ❌ "Actionable comments: 0" | ✅ 1 BUG finding |
| Cited the right file? | n/a | `helpers.py:244` (matches PR #13247's fix location exactly) |
| Cited downstream call sites? | n/a | `time_blocks.py:193, 303, 409` |
| Identified the divergence path? | n/a | "REST returns correct local time, copilot returns UTC — silent data divergence by invocation path" |
| Cost (per the bot's metrics) | not disclosed | $4.79 (artifact-stamped, c0b4297) |

### Why the maintainer is the source of truth on this miss

The maintainers themselves stated #13247 was a follow-up to the same logical bug as #13236 (`#13247` description, verbatim). CR reviewed #13236 and missed it; the maintainers shipped a second PR to fix it. Shadow, given the same kind of diff, surfaced the gap.

---

## Case 2 (parity): Shadow rediscovered the same bug CodeRabbit caught

**Upstream PR:** [Significant-Gravitas/AutoGPT#13239](https://github.com/Significant-Gravitas/AutoGPT/pull/13239) — `fix(platform): drop running balance from user transaction history`. Merged.

**What CodeRabbit found** (1 actionable inline comment on `AdminUserGrantHistory.tsx:136`):

> _Avoid fabricating balances when `running_balance` is missing. Line 133 and Line 145 currently coalesce missing `running_balance` to `0`, which can display incorrect financial values (e.g., `$0` ending balance). For admin billing data, it's safer to surface that data is unavailable rather than fabricate a `$0`._

**Bench replication:** the fix commit was reverted onto a fork ([sudsali/AutoGPT#1](https://github.com/sudsali/AutoGPT/pull/1)). Shadow reviewed the diff with no knowledge of upstream PR #13239 or CR's review.

**What Shadow found** ([artifact](https://github.com/sudsali/AutoGPT/actions/runs/26733174920)):

> **BUG** — `credit.py:1254`
>
> When `CreditTransaction.runningBalance` is NULL in the DB, the new Balance column shows `$0.00` instead of indicating unknown. This is misleading to users and admins reviewing history and is the exact concern CodeRabbit raised. Either keep `running_balance: int | None` end-to-end and render a placeholder when missing, or backfill/guarantee non-null at write time before exposing the column.

### Side-by-side

| | CodeRabbit | Shadow |
|---|---|---|
| Found the issue? | ✅ | ✅ |
| File flagged | `AdminUserGrantHistory.tsx` (frontend symptom) | `credit.py:1254` (backend root cause) |
| Evidence cited | None — assertion-style | `schema.prisma:927` (column nullable), 4 prior call sites (`credit.py:348, 933, 3080, 3195` already use defensive `or 0`), SQL path uses `COALESCE`, types.ts changed `running_balance?: number → running_balance: number` |
| Concrete fix recommendation | Add a guard for `null` | Two named alternatives: `int \| None` end-to-end OR backfill-at-write |
| Refutation trail | None | Yes — Investigator hypothesis + Critic disprove attempt visible in the comment |

Both bots flagged the same issue; Shadow chose the backend root cause, CR chose the frontend symptom site. Shadow's finding is grep-derived (every claim citable), CR's is assertion-style.

---

## Method differences (across both cases)

- **CodeRabbit prompts a model with the diff and posts what comes back.** No re-derivation step. No tool-driven verification before posting. Findings are assertion-style.
- **Shadow's pipeline is generator–verifier.** The Critic does not see the Investigator's tool trace; it re-reads the cited code with its own tool calls and either UPHOLDs or OVERTURNs each finding. Only what survives posts. Each surviving finding carries a Refutation Trail (`<details>` block) showing both agents' work.
- **Net effect:** Shadow's posted comments include independently-verified evidence. CR's posted comments are model output. On Case 1, the difference shows up as recall — Shadow caught what CR missed. On Case 2, the difference shows up as evidence quality — same finding, but Shadow attaches schema citations and 4+ prior call-site references that the maintainer can scan in seconds.

## Caveats

- **n=2.** This is two PRs on one repo. Don't extrapolate the absolute miss rate.
- **CodeRabbit's review is free-tier on a public OSS project.** Their paid tier may produce richer output we didn't observe.
- **Shadow's cost is real.** Case 1 was $4.79 on `c0b4297` (was $2.38 pre-hardening). A team that values parsable evidence over assertions will pay it; a team that wants free-tier scan-the-diff coverage may not.
- **Both bots could be wrong.** The maintainer accepted both reviews; the actual production bug-recurrence rate is unknown.

## Reproducibility

| Bench | PR | Workflow run |
|---|---|---|
| Case 1: smoking gun | [sudsali/AutoGPT#3](https://github.com/sudsali/AutoGPT/pull/3) | [26734397922](https://github.com/sudsali/AutoGPT/actions/runs/26734397922) |
| Case 2: parity | [sudsali/AutoGPT#1](https://github.com/sudsali/AutoGPT/pull/1) | [26733174920](https://github.com/sudsali/AutoGPT/actions/runs/26733174920) |
| Upstream #13236 (CR review) | [Significant-Gravitas/AutoGPT#13236](https://github.com/Significant-Gravitas/AutoGPT/pull/13236) | — |
| Upstream #13239 (CR review) | [Significant-Gravitas/AutoGPT#13239](https://github.com/Significant-Gravitas/AutoGPT/pull/13239) | — |
| Upstream #13247 (proves CR missed it on #13236) | [Significant-Gravitas/AutoGPT#13247](https://github.com/Significant-Gravitas/AutoGPT/pull/13247) | — |

Every workflow run uploads `shadow_result.json` with full provenance: per-prompt SHA-256 fingerprint, per-stage cost / token / model breakdown, sanitizer events histogram, full tool trace (with secrets redacted), SHA-256 integrity stamp.
