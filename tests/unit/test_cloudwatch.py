"""CloudWatch metrics emitter. boto3 calls are mocked at the function
boundary; we verify the metric-data shape, not the AWS call itself."""
from datetime import datetime
from unittest import mock

import pytest

from shadow import cloudwatch as cw


def test_disabled_via_env(monkeypatch):
    monkeypatch.setenv("SHADOW_CLOUDWATCH_DISABLED", "true")
    ok, reason = cw.emit_metrics(
        repository="org/repo", metrics={}, action="RESPOND",
    )
    assert not ok
    assert "disabled" in reason


def test_no_repository_returns_error():
    ok, reason = cw.emit_metrics(
        repository="", metrics={"cost_usd": {"total": 0.5}},
        action="RESPOND",
    )
    assert not ok


def test_namespace_override_logs_error(monkeypatch, caplog):
    """Namespace override produces AccessDenied at PutMetricData time
    because the CFN policy hardcodes 'Shadow'. Surface it in logs at
    emit time so adopters notice instead of silently losing metrics."""
    import logging
    monkeypatch.setenv("SHADOW_CLOUDWATCH_NAMESPACE", "Acme/Shadow")
    with caplog.at_level(logging.ERROR, logger="shadow"):
        cw.emit_metrics(repository="", metrics={}, action="RESPOND")
    assert any("CFN-pinned" in r.message for r in caplog.records)


def test_metric_data_shape_includes_cost_and_overturn():
    metrics = {
        "cost_usd": {"total": 1.23, "by_stage": {}},
        "critic_overturn_rate": 0.65,
        "totals": {"input_tokens": 10000, "output_tokens": 200},
    }
    dims = [
        {"Name": "Repository", "Value": "org/repo"},
        {"Name": "Pipeline", "Value": "agentic"},
    ]
    data = list(cw._build_metric_data(metrics, "RESPOND", dims, datetime.utcnow()))
    names = {d["MetricName"] for d in data}
    assert "CostPerPR" in names
    assert "CriticOverturnRate" in names
    assert "InputTokens" in names
    assert "OutputTokens" in names
    assert "Invocations" in names
    # ESCALATE-only metric should not appear on a RESPOND.
    assert "Escalations" not in names


def test_escalate_emits_escalations_metric():
    metrics = {"cost_usd": {"total": 0.0}, "totals": {}}
    dims = [{"Name": "Repository", "Value": "x"}, {"Name": "Pipeline", "Value": "agentic"}]
    data = list(cw._build_metric_data(metrics, "ESCALATE", dims, datetime.utcnow()))
    assert any(d["MetricName"] == "Escalations" for d in data)


def test_overturn_rate_reported_as_percent():
    metrics = {"critic_overturn_rate": 0.5, "totals": {}, "cost_usd": {"total": 0.0}}
    dims = [{"Name": "Repository", "Value": "x"}, {"Name": "Pipeline", "Value": "agentic"}]
    data = list(cw._build_metric_data(metrics, "RESPOND", dims, datetime.utcnow()))
    overturn = next(d for d in data if d["MetricName"] == "CriticOverturnRate")
    assert overturn["Value"] == 50.0
    assert overturn["Unit"] == "Percent"


def test_dimensions_attached_to_every_metric():
    metrics = {"cost_usd": {"total": 1.0}, "totals": {"input_tokens": 1, "output_tokens": 1}}
    dims = [{"Name": "Repository", "Value": "org/repo"}, {"Name": "Pipeline", "Value": "agentic"}]
    data = list(cw._build_metric_data(metrics, "RESPOND", dims, datetime.utcnow()))
    for d in data:
        assert d["Dimensions"] == dims


def test_emit_swallows_boto_failure():
    """Observability must never block the bot from posting findings."""
    import boto3
    from botocore.exceptions import ClientError

    fake_client = mock.Mock()
    err = ClientError({"Error": {"Code": "Throttled", "Message": "x"}}, "PutMetricData")
    fake_client.put_metric_data.side_effect = err
    with mock.patch.object(boto3, "client", return_value=fake_client):
        ok, reason = cw.emit_metrics(
            repository="org/repo",
            metrics={"cost_usd": {"total": 1.0}, "totals": {"input_tokens": 1, "output_tokens": 1}},
            action="RESPOND",
        )
    assert not ok
    assert "put_metric_data" in reason
