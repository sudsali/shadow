"""Cost computation. Feeds the Shadow/CostPerPR CloudWatch metric (cost
data is observability, not part of the posted comment body). Bedrock list
prices change periodically; bench the math against the table in main.py."""
import pytest

from shadow.main import (
    _MODEL_PRICING_PER_M_TOKENS,
    _compute_cost_usd,
    _stage_cost,
)


def test_zero_tokens_zero_cost():
    stage = {
        "skipped": False, "model_id": "us.anthropic.claude-opus-4-7",
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    }
    assert _stage_cost(stage) == 0.0


def test_skipped_stage_costs_zero():
    stage = {
        "skipped": True, "model_id": "us.anthropic.claude-opus-4-7",
        "input_tokens": 100_000, "output_tokens": 100_000,
    }
    assert _stage_cost(stage) == 0.0


def test_opus_input_pricing_per_million():
    stage = {
        "skipped": False, "model_id": "us.anthropic.claude-opus-4-7",
        "input_tokens": 1_000_000, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    }
    assert _stage_cost(stage) == 5.00


def test_opus_output_pricing_per_million():
    stage = {
        "skipped": False, "model_id": "us.anthropic.claude-opus-4-7",
        "input_tokens": 0, "output_tokens": 1_000_000,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    }
    assert _stage_cost(stage) == 25.00


def test_haiku_pricing_5x_cheaper_than_opus():
    haiku = {
        "skipped": False,
        "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "input_tokens": 1_000_000, "output_tokens": 1_000_000,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    }
    opus = dict(haiku); opus["model_id"] = "us.anthropic.claude-opus-4-7"
    assert _stage_cost(opus) / _stage_cost(haiku) == pytest.approx(5.0, rel=0.001)


def test_cache_reads_are_one_tenth_input_price():
    """Anthropic via Bedrock: cache reads bill at 0.1× input price."""
    stage = {
        "skipped": False, "model_id": "us.anthropic.claude-opus-4-7",
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 1_000_000, "cache_write_tokens": 0,
    }
    # 1M cache_read at Opus = 1M * $5 * 0.1 = $0.50
    assert _stage_cost(stage) == 0.50


def test_cache_writes_are_one_point_two_five_input_price():
    stage = {
        "skipped": False, "model_id": "us.anthropic.claude-opus-4-7",
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 1_000_000,
    }
    # 1M cache_write at Opus = 1M * $5 * 1.25 = $6.25
    assert _stage_cost(stage) == 6.25


def test_unknown_model_falls_back_to_opus_pricing():
    """Adopter who overrides BEDROCK_MODEL_ID to something unrecognized still
    gets a cost number — pricing table fallback to Opus 4.7."""
    stage = {
        "skipped": False, "model_id": "anthropic.claude-9000-not-yet",
        "input_tokens": 1_000_000, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    }
    assert _stage_cost(stage) == 5.00


def test_compute_cost_usd_aggregates_three_stages():
    metrics = {
        "investigator": {
            "skipped": False, "model_id": "us.anthropic.claude-opus-4-7",
            "input_tokens": 100_000, "output_tokens": 10_000,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
        },
        "critic": {
            "skipped": False, "model_id": "us.anthropic.claude-opus-4-7",
            "input_tokens": 50_000, "output_tokens": 5_000,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
        },
        "reporter": {
            "skipped": False,
            "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "input_tokens": 20_000, "output_tokens": 2_000,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
        },
    }
    cost = _compute_cost_usd(metrics)
    assert "by_stage" in cost
    assert "total" in cost
    assert cost["total"] == round(
        cost["by_stage"]["investigator"]
        + cost["by_stage"]["critic"]
        + cost["by_stage"]["reporter"],
        4,
    )


def test_pricing_table_covers_default_models():
    """If the bot ships with new defaults, the pricing table must keep up."""
    assert "us.anthropic.claude-opus-4-7" in _MODEL_PRICING_PER_M_TOKENS
    assert "us.anthropic.claude-haiku-4-5-20251001-v1:0" in _MODEL_PRICING_PER_M_TOKENS


