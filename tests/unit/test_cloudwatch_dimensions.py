"""CloudWatch Reason dimension and Throttled metric.

emit_metrics now accepts a `reason` kwarg that becomes a Reason dimension
on every metric. This lets operator dashboards group SKIP/ESCALATE counts
by cause (rate_limited, throttled, bedrock_unavailable). Throttled metric
fires only when action=SKIP and reason contains "throttled".
"""
from datetime import datetime
from unittest import mock

import pytest

from shadow import cloudwatch as cw


def test_reason_dimension_attached_when_provided(monkeypatch):
    """reason kwarg becomes a Reason dimension on every metric so dashboards
    can graph SKIP-by-reason and ESCALATE-by-reason."""
    fake_client = mock.Mock()
    monkeypatch.setenv("GITHUB_REPOSITORY", "x/y")
    with mock.patch("boto3.client", return_value=fake_client):
        cw.emit_metrics(
            repository="org/repo",
            metrics={"totals": {}},
            action="ESCALATE",
            reason="rate_limited",
        )
    args, kwargs = fake_client.put_metric_data.call_args
    data = kwargs["MetricData"]
    for d in data:
        names = [dim["Name"] for dim in d["Dimensions"]]
        assert "Reason" in names
        reason_dim = next(dim for dim in d["Dimensions"] if dim["Name"] == "Reason")
        assert reason_dim["Value"] == "rate_limited"


def test_reason_dimension_omitted_when_not_provided(monkeypatch):
    """No reason kwarg → only Repository + Pipeline dimensions, preserving
    backward compat with existing dashboards keyed on those two."""
    fake_client = mock.Mock()
    monkeypatch.setenv("GITHUB_REPOSITORY", "x/y")
    with mock.patch("boto3.client", return_value=fake_client):
        cw.emit_metrics(
            repository="org/repo",
            metrics={"totals": {"input_tokens": 1, "output_tokens": 1}},
            action="RESPOND",
        )
    args, kwargs = fake_client.put_metric_data.call_args
    data = kwargs["MetricData"]
    for d in data:
        names = [dim["Name"] for dim in d["Dimensions"]]
        assert "Reason" not in names


def test_reason_truncated_to_dimension_limit():
    """CloudWatch dimension value limit is 256 chars; we cap at 250."""
    dims = [{"Name": "Repository", "Value": "x"}, {"Name": "Pipeline", "Value": "agentic"}]
    long_reason = "a" * 500
    dims.append({"Name": "Reason", "Value": long_reason[:250]})
    # Ensure the truncation logic in emit_metrics produces a value < 256.
    fake_client = mock.Mock()
    with mock.patch("boto3.client", return_value=fake_client):
        cw.emit_metrics(
            repository="org/repo",
            metrics={"totals": {}},
            action="SKIP",
            reason=long_reason,
        )
    _args, kwargs = fake_client.put_metric_data.call_args
    for d in kwargs["MetricData"]:
        for dim in d["Dimensions"]:
            if dim["Name"] == "Reason":
                assert len(dim["Value"]) <= 256


def test_throttled_metric_fires_on_skip_throttled():
    """SKIP with reason containing 'throttled' emits the dedicated Throttled
    metric so dashboards can graph capacity-induced SKIPs separately."""
    dims = [
        {"Name": "Repository", "Value": "org/repo"},
        {"Name": "Pipeline", "Value": "agentic"},
        {"Name": "Reason", "Value": "throttled"},
    ]
    data = list(cw._build_metric_data(
        {"totals": {}}, "SKIP", dims, datetime.utcnow(),
    ))
    names = {d["MetricName"] for d in data}
    assert "Throttled" in names


def test_throttled_metric_does_not_fire_on_escalate():
    """ESCALATE shouldn't emit Throttled even with the reason set; the metric
    is meant to track the SKIP-rerun-later flow specifically."""
    dims = [
        {"Name": "Repository", "Value": "x"},
        {"Name": "Pipeline", "Value": "agentic"},
        {"Name": "Reason", "Value": "throttled"},
    ]
    data = list(cw._build_metric_data({"totals": {}}, "ESCALATE", dims, datetime.utcnow()))
    names = {d["MetricName"] for d in data}
    assert "Throttled" not in names


def test_throttled_metric_does_not_fire_without_throttled_reason():
    """SKIP with another reason (already_commented, etc.) skips Throttled."""
    dims = [
        {"Name": "Repository", "Value": "x"},
        {"Name": "Pipeline", "Value": "agentic"},
        {"Name": "Reason", "Value": "already_commented"},
    ]
    data = list(cw._build_metric_data({"totals": {}}, "SKIP", dims, datetime.utcnow()))
    names = {d["MetricName"] for d in data}
    assert "Throttled" not in names


def test_invocations_emitted_with_reason_dimension():
    """Invocations is the per-artifact counter — must always emit, regardless
    of action, so dashboards undercount no flow."""
    dims = [
        {"Name": "Repository", "Value": "x"},
        {"Name": "Pipeline", "Value": "agentic"},
        {"Name": "Reason", "Value": "author_is_bot"},
    ]
    data = list(cw._build_metric_data({"totals": {}}, "SKIP", dims, datetime.utcnow()))
    names = {d["MetricName"] for d in data}
    assert "Invocations" in names


def test_emit_metrics_swallows_throttled_emission_failure(monkeypatch):
    """Even when reason='throttled' triggers an extra Throttled metric,
    a put_metric_data failure must still be swallowed so observability
    can never break a posting flow."""
    from botocore.exceptions import ClientError
    fake_client = mock.Mock()
    fake_client.put_metric_data.side_effect = ClientError(
        {"Error": {"Code": "Throttled", "Message": "x"}}, "PutMetricData",
    )
    with mock.patch("boto3.client", return_value=fake_client):
        ok, _ = cw.emit_metrics(
            repository="org/repo", metrics={"totals": {}},
            action="SKIP", reason="throttled",
        )
    assert ok is False
