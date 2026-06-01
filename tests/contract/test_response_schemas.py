"""The bot ships strict JSON schemas the model is asked to produce against.

These tests pin the field shape downstream code reads from the artifact.
Renaming `comment`, `severity`, or `evidence` would break act() silently;
the test makes the rename a hard failure at PR time.
"""
import json
from pathlib import Path

SCHEMAS = Path(__file__).resolve().parents[2] / "src/scripts/shadow/schemas"


def _load(name):
    return json.loads((SCHEMAS / name).read_text())


def test_pr_review_schema_is_well_formed():
    s = _load("pr_review_response.json")
    assert s["type"] == "object"
    assert "analysis" in s["required"]


def test_issue_response_schema_parses():
    s = _load("issue_response.json")
    assert s["type"] == "object"


def test_followup_response_schema_parses():
    s = _load("followup_response.json")
    assert s["type"] == "object"


def test_pr_review_schema_pins_finding_shape():
    """act() reads severity/comment/evidence by these exact names."""
    s = _load("pr_review_response.json")
    finding = (
        s["properties"]["analysis"]["items"]["properties"]["finding"]
    )
    assert "severity" in finding["properties"]
    assert "comment" in finding["properties"]
    assert "evidence" in finding["properties"]
    # severity values are mapped to colors / filtering — locking the enum.
    assert finding["properties"]["severity"]["enum"] == [
        "BUG", "EDGE_CASE", "MISSING_TEST", "DESIGN", "NIT",
    ]


def test_pr_review_schema_pins_outer_analysis_shape():
    s = _load("pr_review_response.json")
    item = s["properties"]["analysis"]["items"]["properties"]
    assert "file" in item
    assert "line" in item
    assert "hypothesis" in item
    assert "disproved" in item
