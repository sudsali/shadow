"""Contract tests for the reusable workflow adopters call."""
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/shadow-review.yml"
MODEL_VARS = ("BEDROCK_MODEL_ID", "BEDROCK_REPORTER_MODEL_ID", "BEDROCK_CRITIC_MODEL_ID", "BEDROCK_ISSUE_MODEL_ID")


def test_model_env_is_a_plain_secret_passthrough():
    doc = yaml.safe_load(WORKFLOW.read_text())
    seen = {}
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            for name in MODEL_VARS:
                if name in (step.get("env") or {}):
                    seen[name] = step["env"][name]
    # Any fallback makes the env non-empty, which silently overrides .shadow.yml `models:`.
    assert seen == {name: "${{ secrets.%s }}" % name for name in MODEL_VARS}
