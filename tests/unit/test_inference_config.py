"""Sampling-param handling for Bedrock Converse.

Newer Claude families (Opus 4.7+, Sonnet 5+) reject `temperature` — sending it
is a hard ValidationException. _build_inference_config drops it for those via a
forward-safe pattern, and _converse_with_retry backstops any family missing
from that pattern by retrying once without sampling params. These tests exist
because the original pattern matched only opus-4-7 and silently broke opus-4-8.
"""
from unittest import mock

import pytest
from botocore.exceptions import ClientError

from shadow import bedrock_client as bc
from shadow.bedrock_client import (
    _build_inference_config,
    _is_sampling_deprecated_error,
)


@pytest.mark.parametrize("model_id, keeps_temperature", [
    ("us.anthropic.claude-opus-4-7", False),
    ("us.anthropic.claude-opus-4-8", False),            # the bug: was sent temperature
    ("us.anthropic.claude-opus-4-9", False),
    ("global.anthropic.claude-opus-4-8", False),
    ("us.anthropic.claude-sonnet-5", False),
    ("us.anthropic.claude-opus-5", False),
    ("us.anthropic.claude-sonnet-4-6", True),            # accepts sampling
    ("us.anthropic.claude-haiku-4-5-20251001-v1:0", True),
    # Legacy Claude 3 carries an 8-digit date; must NOT be caught by sonnet-[5-9]
    # (a prior pattern matched `sonnet-20...` and wrongly stripped temperature).
    ("us.anthropic.claude-3-sonnet-20240229-v1:0", True),
    ("us.anthropic.claude-opus-4-1-20250805-v1:0", True),
    ("us.anthropic.claude-opus-4-70-fake", True),        # boundary: not 4-7/4-8
    ("us.anthropic.claude-opus-50", True),               # must NOT substring-match opus-5
    ("us.anthropic.claude-sonnet-50", True),
    ("us.anthropic.claude-opus-10", True),               # two-digit major → retry backstops
    ("", True),
    (None, True),
])
def test_temperature_dropped_only_for_no_sampling_families(model_id, keeps_temperature):
    cfg = _build_inference_config(4096, 0.3, model_id)
    assert cfg["maxTokens"] == 4096
    assert ("temperature" in cfg) is keeps_temperature


def _client():
    with mock.patch.object(bc.boto3, "client"):
        class _Cfg:
            bedrock_model_id = "us.anthropic.claude-opus-4-8"
            bedrock_timeout = 240
            guardrail_id = ""
            guardrail_version = "DRAFT"
        return bc.BedrockClient(_Cfg())


def _temp_deprecated_error():
    return ClientError(
        {"Error": {"Code": "ValidationException",
                   "Message": "`temperature` is deprecated for this model."}},
        "Converse",
    )


@pytest.mark.parametrize("msg, expected", [
    ("`temperature` is deprecated for this model.", True),
    ("temperature is not supported", True),
    ("unsupported parameter: top_p", True),
    ("temperature is not allowed for this model", True),   # wording variant
    ("ThrottlingException: slow down", False),
    ("access denied", False),
    ("model deprecated", False),                            # 'deprecated' but not a sampling param
])
def test_is_sampling_deprecated_error(msg, expected):
    err = ClientError({"Error": {"Code": "ValidationException", "Message": msg}}, "Converse")
    assert _is_sampling_deprecated_error(err) is expected


def test_retry_strips_temperature_and_succeeds():
    # A model not in the pattern still gets temperature; the first call fails
    # deprecated, the retry (no temperature) succeeds.
    c = _client()
    calls = []

    def fake_converse(**kwargs):
        calls.append(kwargs["inferenceConfig"])
        if "temperature" in kwargs["inferenceConfig"]:
            raise _temp_deprecated_error()
        return {"ok": True}

    fake_client = mock.Mock()
    fake_client.converse.side_effect = fake_converse
    kwargs = {"modelId": "us.anthropic.claude-future-9",
              "messages": [], "inferenceConfig": {"maxTokens": 10, "temperature": 0.3}}
    resp = c._converse_with_retry(fake_client, kwargs)
    assert resp == {"ok": True}
    assert len(calls) == 2                       # first + retry
    assert "temperature" in calls[0]
    assert "temperature" not in calls[1]         # stripped on retry


def test_retry_not_triggered_for_unrelated_error():
    # A non-sampling error must propagate, not silently retry.
    c = _client()
    fake_client = mock.Mock()
    fake_client.converse.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "Converse")
    with pytest.raises(ClientError):
        c._converse_with_retry(fake_client, {
            "modelId": "m", "messages": [], "inferenceConfig": {"maxTokens": 10, "temperature": 0.3}})
    assert fake_client.converse.call_count == 1  # no retry
