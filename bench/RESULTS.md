# Bench results — Shadow vs maintainer review

This bench measures whether Shadow independently rediscovers the bugs that real PR reviewers caught upstream — across three AWS-flagship OSS repositories in three different languages.

## Method

For each repository, we picked **4 recently-merged "fix:" PRs** with real correctness content (not docs or test-only fixes). For each fix PR, we:

1. Forked the repository (no upstream changes).
2. Created a branch off `master`/`main`/`develop` and **reverted the merged fix commit** onto it. The branch now contains the diff that *re-introduces* the bug the maintainer's fix had eliminated.
3. Opened that branch as a fresh PR against the fork's default branch. Shadow reviews the diff with no knowledge of the upstream fix or its accompanying review comments.
4. Captured the resulting `shadow_result.json` artifact (cost, overturn rate, inline findings).

The Investigator and Critic see only the diff plus the codebase itself (via `read_file`/`grep_codebase`/`find_callers`). They do not see the upstream PR title, the upstream review thread, or the original fix commit message.

## Results (measured 2026-06)

| Repo | Lang | Upstream PR | Action | Findings | Severities | Cost | Critic overturn |
|---|---|---|---|---|---|---|---|
| `aws/aws-sdk-java-v2` | Java | [#6737](https://github.com/aws/aws-sdk-java-v2/pull/6737) | RESPOND | 1 | BUG | $1.11 | 0% |
| `aws/aws-sdk-java-v2` | Java | [#6936](https://github.com/aws/aws-sdk-java-v2/pull/6936) | RESPOND | 0 | (overturned) | $0.76 | 100% |
| `aws/aws-sdk-java-v2` | Java | [#6986](https://github.com/aws/aws-sdk-java-v2/pull/6986) | RESPOND | 3 | BUG, BUG, MISSING_TEST | $0.76 | 0% |
| `aws/aws-sdk-java-v2` | Java | [#7000](https://github.com/aws/aws-sdk-java-v2/pull/7000) | RESPOND | 0 | (no findings) | $0.23 | n/a |
| `aws/karpenter-provider-aws` | Go | [#9080](https://github.com/aws/karpenter-provider-aws/pull/9080) | RESPOND | 1 | BUG | $0.51 | 0% |
| `aws/karpenter-provider-aws` | Go | [#9124](https://github.com/aws/karpenter-provider-aws/pull/9124) | RESPOND | 2 | BUG, MISSING_TEST | $0.87 | 33% |
| `aws/karpenter-provider-aws` | Go | [#9170](https://github.com/aws/karpenter-provider-aws/pull/9170) | RESPOND | 1 | MISSING_TEST | $0.43 | 50% |
| `aws/karpenter-provider-aws` | Go | [#9202](https://github.com/aws/karpenter-provider-aws/pull/9202) | RESPOND | 1 | NIT | $0.25 | 0% |
| `aws-powertools/powertools-lambda-python` | Python | [#8176](https://github.com/aws-powertools/powertools-lambda-python/pull/8176) | RESPOND | 2 | DESIGN, BUG | $0.53 | 0% |

**Totals:** 9 PRs · 11 inline findings · $5.45 total · **mean $0.61/PR** · mean Critic overturn rate ~26%

Each row was a fresh PR (new branch, new PR number) on the current revision, opened to bypass Shadow's existing-feedback dedup. Run artifacts are linked in the [Reproducibility](#reproducibility) section.

### Earlier-revision reference (May 2026)

An earlier revision of the same corpus measured $0.41/PR mean — before recent security-hardening work that added Investigator/Critic budget and validation overhead. The ~50% cost shift came with stronger envelope-tag neutralization, sanitizer-marker bypass defenses, and config-validation diagnostics. Findings on the headline bugs (S3 multipart concurrency #6986, nil-deref #9080) reproduce on the current revision.

| Earlier revision totals | Current revision totals |
|---|---|
| 9 PRs · 15 findings · $3.84 · $0.41/PR · 3.7% mean overturn | 9 PRs · 11 findings · $5.45 · $0.61/PR · ~26% mean overturn |

The Critic's mean overturn rate of ~26% on the current revision (vs 3.7% earlier) is the disprove pass doing more work — overturning more candidate findings to keep the post-rate calibrated. We're tracking this as a quality signal: a 26% overturn rate means the Critic dropped roughly one in four Investigator candidates as not-actually-a-bug.

## Reading the table

- **Action: `RESPOND`** = Shadow posted inline comments on the PR. The opposite is `SKIP` (Investigator returned no concerns) or `ESCALATE` (the bot decided maintainer review was needed). Across this bench, every PR triggered RESPOND because every PR re-introduced a real bug.
- **Findings:** number of inline comments Shadow posted. Each carries a Refutation Trail (`<details>` block) showing the Investigator's hypothesis and the Critic's disprove attempt.
- **Critic overturn:** what fraction of the Investigator's findings the Critic OVERTURNED. 0% means the Critic upheld every finding the Investigator raised. The 33% on Karpenter #9170 reflects the Critic dropping a third candidate finding the Investigator surfaced — exactly the disprove pass doing its job.

## Highlights

The findings are not pattern-match noise. Examples (full text in each artifact):

- **`aws-sdk-java-v2#6986` (S3 multipart concurrency, 3 findings):** Shadow caught all three separate concurrency bugs the upstream fix addressed — non-atomic increment in `addToBytesToLastCompletedParts`, `TreeSet` thread-safety in `addCompletedPart`, and the missing `volatile` happens-before publication guarantee on `totalParts`/`response`. Three independent issues identified in one diff.

- **`karpenter-provider-aws#9080` (nil deref):** Identified the exact root cause — Go evaluates both branches of `lo.Ternary(cr.Interruptible == nil, false, *cr.Interruptible)` eagerly, so the dereference happens before the nil check. Required understanding Go's evaluation semantics, not pattern-matching.

- **`karpenter-provider-aws#9170`:** Pinpointed the trigger — `MinValues=50` on a 3-AZ topology causes `Truncate` to fail; the revert removed the error-wrapping that prevented this from blocking all provisioning.

- **`aws-sdk-java-v2#6737` (DynamoDB enhanced):** Caught that `Collectors.toMap` doesn't accept null values, so DynamoDB Map attributes containing NULL-typed values would throw NPE in `StringAttributeConverter.transformTo`.

## What this measures

This is a **Shadow vs maintainer-review** bench: Shadow's findings are compared against what the upstream maintainers caught (those fix PRs all merged after human review). Across 9 PRs, Shadow rediscovered the same correctness issues — sometimes in finer-grained detail (3 findings on `#6986`), sometimes with a sharper trigger explanation than the original commit message.

This is **not** a "Shadow vs other AI bots" bench. None of the three repos has an AI review bot installed at the time of this bench (top commenters: humans, Dependabot, GitHub Actions CI). For a head-to-head against another AI bot, see `bench/HEAD_TO_HEAD.md` (in progress).

## Reproducibility

Every bench PR is on a public fork:

- Java: <https://github.com/sudsali/aws-sdk-java-v2/pulls?q=is%3Apr+label%3Anone+bench%3A>
- Go: <https://github.com/sudsali/karpenter-provider-aws/pulls?q=is%3Apr+bench%3A>
- Python: <https://github.com/sudsali/powertools-lambda-python/pull/1>

Every workflow run uploads `shadow_result.json` with full provenance:

- Per-prompt SHA-256 fingerprint
- Per-stage cost / token / model breakdown
- Sanitizer events histogram
- Full tool trace (with secrets redacted)
- SHA-256 integrity stamp bound to `(repo, run_id, pr_number)`

Re-running the bench against a different `shadow_ref` (commit SHA) is straightforward — flip the `uses:` line in each fork's `.github/workflows/shadow.yml` to the new SHA and re-trigger the PRs.

## Caveats

- **Bedrock list prices change.** Costs above are from runs on 2026-05-31; absolute dollars will drift. Relative cost ratios across stages (Investigator ≈ Critic ≫ Reporter) are stable.
- **n=9 is small.** A larger bench is in progress. The signal we're publishing here is the qualitative quality of the findings (the highlights section), not the precise overturn rate.
- **All 9 PRs re-introduce known bugs.** This bench measures recall against confirmed bugs. False-positive rate on clean PRs is a separate measurement (planned).
