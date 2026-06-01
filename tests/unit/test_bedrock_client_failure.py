"""BedrockClient failure-attribution tests.

last_failed_model() and last_failure_was_throttle() let main.py embed the
specific model+exception in the artifact reason so operators can tell which
agent stage broke (Investigator/Critic/Reporter) instead of an opaque
"bedrock_unavailable".
"""
from unittest import mock

import pytest
from botocore.exceptions import ClientError


class _FakeCfg:
    bedrock_model_id = "us.anthropic.claude-opus-4-7"
    bedrock_timeout = 240
    guardrail_id = ""
    guardrail_version = "DRAFT"


@pytest.fixture
def client():
    from shadow import bedrock_client as bc
    with mock.patch.object(bc.boto3, "client"):
        return bc.BedrockClient(_FakeCfg())


def test_last_failed_model_none_initially(client):
    assert client.last_failed_model() is None
    assert client.last_failure_was_throttle() is False


def test_last_failed_model_records_model_and_exception(client):
    client._record_failure("model-a", ValueError("boom"))
    last = client.last_failed_model()
    assert last == ("model-a", "ValueError")


def test_last_failed_model_returns_most_recent(client):
    client._record_failure("model-a", ValueError("first"))
    client._record_failure("model-b", KeyError("second"))
    last = client.last_failed_model()
    assert last == ("model-b", "KeyError")


def test_last_failed_model_overwrites_same_model_with_new_exc(client):
    client._record_failure("model-a", ValueError("first"))
    client._record_failure("model-a", TypeError("second"))
    last = client.last_failed_model()
    assert last == ("model-a", "TypeError")


def test_throttling_exception_marked_as_throttle(client):
    err = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "Converse",
    )
    client._record_failure("model-a", err)
    assert client.last_failure_was_throttle() is True
    last = client.last_failed_model()
    assert last == ("model-a", "ClientError")


def test_too_many_requests_marked_as_throttle(client):
    err = ClientError(
        {"Error": {"Code": "TooManyRequestsException", "Message": "x"}},
        "Converse",
    )
    client._record_failure("model-a", err)
    assert client.last_failure_was_throttle() is True


def test_non_throttle_clienterror_not_throttle(client):
    err = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "bad input"}},
        "Converse",
    )
    client._record_failure("model-a", err)
    assert client.last_failure_was_throttle() is False
    last = client.last_failed_model()
    assert last == ("model-a", "ClientError")


def test_value_error_not_throttle(client):
    client._record_failure("model-a", ValueError("not aws"))
    assert client.last_failure_was_throttle() is False


def test_throttle_then_non_throttle_clears_throttle_state(client):
    err1 = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "x"}},
        "Converse",
    )
    client._record_failure("model-a", err1)
    assert client.last_failure_was_throttle() is True
    client._record_failure("model-a", ValueError("plain"))
    assert client.last_failure_was_throttle() is False
