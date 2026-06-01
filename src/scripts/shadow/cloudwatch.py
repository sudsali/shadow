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


def emit_metrics(*, repository, metrics, action, pipeline="agentic", region=None):
    """Push a small batch of metrics to CloudWatch.

    `metrics` is the artifact's `metrics` dict (post-_finalize_metrics).
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
    data = list(_build_metric_data(metrics, action, dims, timestamp))
    if not data:
        return False, "no metrics to emit"

    try:
        client.put_metric_data(Namespace=namespace, MetricData=data)
    except (ClientError, BotoCoreError, NoCredentialsError) as e:
        # Never fail the bot for an observability hiccup.
        logger.warning("CloudWatch emit failed (%s); continuing", type(e).__name__)
        return False, f"put_metric_data failed: {type(e).__name__}"
    return True, f"emitted {len(data)} metrics"


def _build_metric_data(metrics, action, dims, timestamp):
    if not isinstance(metrics, dict):
        return

    def _datum(name, value, unit="None"):
        return {
            "MetricName": name,
            "Dimensions": dims,
            "Timestamp": timestamp,
            "Unit": unit,
            "Value": float(value),
        }

    cost_total = (metrics.get("cost_usd") or {}).get("total")
    if isinstance(cost_total, (int, float)):
        yield _datum("CostPerPR", cost_total, unit="None")

    overturn = metrics.get("critic_overturn_rate")
    if isinstance(overturn, (int, float)):
        # Reported as a percentage so CloudWatch graphs are readable.
        yield _datum("CriticOverturnRate", overturn * 100.0, unit="Percent")

    totals = metrics.get("totals") or {}
    in_tok = totals.get("input_tokens") or 0
    out_tok = totals.get("output_tokens") or 0
    yield _datum("InputTokens", in_tok, unit="Count")
    yield _datum("OutputTokens", out_tok, unit="Count")

    # 1 = the bot ran to completion on this PR; helps graph fleet-wide
    # invocation rate against errors.
    yield _datum("Invocations", 1, unit="Count")
    if action == "ESCALATE":
        yield _datum("Escalations", 1, unit="Count")
