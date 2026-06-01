"""CloudWatch custom-metrics emitter.

Emits a small batch of Shadow operational metrics into the adopter's AWS
account so they can graph Shadow as part of their existing observability.
Best-effort: a CloudWatch failure NEVER blocks the bot from posting findings.

Dimensions: Repository (GITHUB_REPOSITORY), Pipeline (agentic).
Namespace: Shadow (the CFN Launch Stack policy is scoped to this exact
namespace via a `cloudwatch:namespace` condition; setting
SHADOW_CLOUDWATCH_NAMESPACE to anything else requires updating the IAM
policy too, otherwise PutMetricData returns AccessDenied).
"""
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("shadow")


_DEFAULT_NAMESPACE = "Shadow"


def emit_metrics(*, repository, metrics, action, pipeline="agentic", region=None, reason=None,
                  base_metrics=True, post_failure_count=0):
    """Push a small batch of metrics to CloudWatch.

    `metrics` is the artifact's `metrics` dict (post-_finalize_metrics).
    `reason` (optional) is the artifact's reason field — attached as a
    `Reason` dimension so operator dashboards can group SKIP/ESCALATE
    counts by cause. The Reason value is keyed on the prefix before the
    first `:` to bound dimension cardinality: every (model_id, exception)
    combo would otherwise mint a separate stream (~$0.30/month each).
    `base_metrics=False` lets the act() post-failure path emit only the
    PostFailures metric without re-counting Invocations against analyze's.

    Returns (ok, reason). Caller logs the reason but never fails on it —
    metrics are observability, not a posting prerequisite."""
    if os.getenv("SHADOW_CLOUDWATCH_DISABLED", "").lower() == "true":
        return False, "disabled by env"
    namespace = os.getenv("SHADOW_CLOUDWATCH_NAMESPACE", _DEFAULT_NAMESPACE)
    if namespace != _DEFAULT_NAMESPACE:
        # CFN policy hardcodes namespace=Shadow; an override produces
        # AccessDenied and silently drops every metric. Log loud once so
        # the misconfig surfaces in workflow logs instead of cratering
        # observability without a trace.
        logger.error(
            "SHADOW_CLOUDWATCH_NAMESPACE=%r differs from CFN-pinned %r; "
            "PutMetricData will fail with AccessDenied. Update the IAM "
            "policy condition or unset the env var.",
            namespace, _DEFAULT_NAMESPACE,
        )
    if not repository:
        return False, "no repository dimension"

    try:
        import boto3
        from botocore.exceptions import ClientError, BotoCoreError, NoCredentialsError
    except ImportError:
        return False, "boto3 unavailable"

    try:
        client = boto3.client("cloudwatch", region_name=region or os.getenv("AWS_REGION") or "us-east-1")
    except (ClientError, BotoCoreError, NoCredentialsError) as e:
        return False, f"cloudwatch client init failed: {type(e).__name__}"

    timestamp = datetime.now(tz=timezone.utc)
    dims = [
        {"Name": "Repository", "Value": repository},
        {"Name": "Pipeline", "Value": pipeline},
    ]
    if reason:
        # Bound dimension cardinality to the documented finite reason set
        # (rate_limited, throttled, bedrock_unavailable, investigator_empty,
        # critic_failed, reporter_failed, post_failure, …). Compound reasons
        # like "bedrock_unavailable: model:Exc" would otherwise mint one
        # unique stream per model+exception combo at ~$0.30/month each.
        reason_str = str(reason).strip().split(":", 1)[0].strip()
        # Truncate to dimension-value limit; CloudWatch rejects above 256.
        dims.append({"Name": "Reason", "Value": reason_str[:250] or "unknown"})
    data = list(_build_metric_data(metrics, action, dims, timestamp,
                                    base_metrics=base_metrics,
                                    post_failure_count=post_failure_count))
    if not data:
        return False, "no metrics to emit"

    try:
        client.put_metric_data(Namespace=namespace, MetricData=data)
    except (ClientError, BotoCoreError, NoCredentialsError) as e:
        # Never fail the bot for an observability hiccup.
        logger.warning("CloudWatch emit failed (%s); continuing", type(e).__name__)
        return False, f"put_metric_data failed: {type(e).__name__}"
    return True, f"emitted {len(data)} metrics"


def _build_metric_data(metrics, action, dims, timestamp,
                        base_metrics=True, post_failure_count=0):
    if not isinstance(metrics, dict):
        return

    def _is_real_number(v):
        # bool is an int subclass; True passing through as 1.0 silently emits
        # CostPerPR=1.0 / CriticOverturnRate=100% from a model that returned
        # a boolean where a number belonged.
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    def _datum(name, value, unit="None"):
        return {
            "MetricName": name,
            "Dimensions": dims,
            "Timestamp": timestamp,
            "Unit": unit,
            "Value": float(value),
        }

    if base_metrics:
        cost_total = (metrics.get("cost_usd") or {}).get("total")
        if _is_real_number(cost_total):
            yield _datum("CostPerPR", cost_total, unit="None")

        overturn = metrics.get("critic_overturn_rate")
        if _is_real_number(overturn):
            # Reported as a percentage so CloudWatch graphs are readable.
            yield _datum("CriticOverturnRate", overturn * 100.0, unit="Percent")

        totals = metrics.get("totals") or {}
        in_tok = totals.get("input_tokens") or 0
        out_tok = totals.get("output_tokens") or 0
        yield _datum("InputTokens", in_tok, unit="Count")
        yield _datum("OutputTokens", out_tok, unit="Count")

        # 1 = analyze() reached _write_artifact for this PR; helps graph
        # fleet-wide invocation rate against errors. Only emitted from the
        # analyze artifact path — act()'s post-failure path passes
        # base_metrics=False so PostFailures rides on a dedicated metric
        # instead of double-counting the divisor.
        yield _datum("Invocations", 1, unit="Count")
        if action == "ESCALATE":
            yield _datum("Escalations", 1, unit="Count")
        # SKIP=throttled = Bedrock capacity hit, not a triage decision.
        # Operators distinguish these from triage SKIPs (already_commented,
        # author_is_bot, …) — a 200-PR throttle storm would otherwise hide
        # in normal SKIP volume.
        if action == "SKIP":
            for d in dims:
                if d.get("Name") == "Reason" and "throttled" in str(d.get("Value", "")):
                    yield _datum("Throttled", 1, unit="Count")
                    break

    if post_failure_count > 0:
        yield _datum("PostFailures", post_failure_count, unit="Count")
