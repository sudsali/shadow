"""action.yml is the GitHub Marketplace metadata. Renaming the inputs or
changing the branding silently breaks the Marketplace listing once
published. Lock the shape here."""
from pathlib import Path

import pytest
import yaml


ACTION_PATH = Path(__file__).resolve().parents[2] / "action.yml"


@pytest.fixture(scope="module")
def doc():
    with ACTION_PATH.open() as f:
        return yaml.safe_load(f)


def test_top_level_metadata(doc):
    assert doc["name"] == "Shadow"
    assert doc["author"] == "sudsali"
    assert "description" in doc and len(doc["description"]) > 50


def test_branding_present(doc):
    """Marketplace listings render a colored icon. Drop branding and the
    listing falls back to the default GitHub Actions chevron."""
    branding = doc.get("branding", {})
    assert branding.get("icon")
    assert branding.get("color")


def test_required_inputs_present(doc):
    """aws_role_arn is the only mandatory input; guardrail_id, pr_number,
    dry_run, shadow_ref are optional. Renaming aws_role_arn breaks every
    composite-shape adopter."""
    inputs = doc["inputs"]
    assert "aws_role_arn" in inputs
    assert inputs["aws_role_arn"]["required"] is True
    for opt in ("guardrail_id", "guardrail_version", "pr_number", "dry_run", "shadow_ref"):
        assert opt in inputs
        assert inputs[opt].get("required") is False or inputs[opt].get("required") is None


def test_runs_using_composite(doc):
    """Composite is the only viable shape for Marketplace listing of a
    repo that primarily exposes a reusable workflow."""
    assert doc["runs"]["using"] == "composite"


def test_composite_directs_to_reusable_workflow(doc):
    """The composite stub must point adopters at the reusable workflow
    shape because composite-to-reusable-workflow chaining isn't
    supported by GitHub today. If/when chaining ships, flip this test
    to a real-execution check."""
    steps = doc["runs"]["steps"]
    body = " ".join(s.get("run", "") for s in steps if s.get("shell") == "bash")
    assert "shadow-review.yml" in body
    assert "uses: sudsali/shadow/.github/workflows/shadow-review.yml" in body
