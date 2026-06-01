# Replay bench

Regression tests for the prompt-output → posted-comment pipeline.

## Why a fixture replay, not live Bedrock

The disprove pattern is a stack of three model calls. Bedrock costs $2-3/PR
and varies turn-to-turn. CI cannot afford to actually call the model on
every commit. So the bench captures **real Critic and Reporter outputs from
past runs** as JSON fixtures, then replays them through Shadow's
deterministic post-processing layer.

This catches:

- Reporter JSON parser regressions (`_parse_reporter_output`)
- Critic verdict regex regressions (`_VERDICT_RE`)
- Comment formatter regressions (`_filter_and_format_inline_comments`)
- Refutation trail rendering regressions (`_format_refutation_trail`)
- Sanitizer regressions on real model output
- Inline-comment filtering / dedup / severity-handling regressions

This does **not** catch:

- Prompt changes that produce different model output (would need a
  live-Bedrock eval harness, which the bench deliberately doesn't run)
- Anthropic model upgrades changing behavior
- Bedrock guardrail changes

## Fixture format

Each fixture is a JSON file under `fixtures/` named after the source PR:

```
fixtures/deequ-722-critic.txt        — raw Critic stdout for PR #722
fixtures/deequ-722-reporter.json     — raw Reporter JSON output for PR #722
fixtures/deequ-722-expected.json     — expected after _filter_and_format
```

When a bug surfaces in a real run, capture the input that broke it as a
new fixture so the regression can't happen twice.
