"""The CFN Launch Stack is the install path for adopters. Its parameter
names, resource names, and output names are a public contract — adopter
docs reference them, and a downstream `Launch Stack` URL would break if
they change."""
from pathlib import Path

import pytest
import yaml

CFN_PATH = Path(__file__).resolve().parents[2] / "infrastructure/shadow-iam-stack.yaml"


class _CfnLoader(yaml.SafeLoader):
    """PyYAML loader that tolerates CloudFormation short-form tags."""


def _cfn_tag(loader, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return None


for _tag in (
    "!Ref", "!Sub", "!If", "!GetAtt", "!Equals", "!Not", "!And", "!Or", "!Join",
):
    _CfnLoader.add_constructor(_tag, _cfn_tag)


@pytest.fixture(scope="module")
def doc():
    with CFN_PATH.open() as f:
        return yaml.load(f, Loader=_CfnLoader)


def test_template_top_level_shape(doc):
    assert doc["AWSTemplateFormatVersion"] == "2010-09-09"
    assert "Description" in doc
    assert set(doc["Resources"].keys()) == {"GitHubOidcProvider", "ShadowBotRole"}


def test_required_parameters_present(doc):
    # These names appear in adopter docs / one-click URL query string;
    # renaming breaks every existing Launch Stack link.
    expected = {"GitHubOrg", "GitHubRepo", "ShadowWorkflowRef",
                "BedrockRegion", "ExistingOidcProviderArn"}
    assert set(doc["Parameters"].keys()) == expected


def test_required_outputs_present(doc):
    # README points adopters at these output names.
    expected = {"ShadowRoleArn", "OidcProviderArn", "TrustPolicyScope",
                "BedrockRegion", "NextSteps"}
    assert set(doc["Outputs"].keys()) == expected


def test_role_has_canonical_name(doc):
    role = doc["Resources"]["ShadowBotRole"]["Properties"]
    assert role["RoleName"] == "shadow-bot-ci"


def test_trust_policy_uses_job_workflow_ref(doc):
    role = doc["Resources"]["ShadowBotRole"]["Properties"]
    statement = role["AssumeRolePolicyDocument"]["Statement"][0]
    string_like = statement["Condition"]["StringLike"]
    # Both sub and job_workflow_ref must be enforced — that's the whole
    # security model. Don't drop one without auditing the threat model.
    assert "token.actions.githubusercontent.com:sub" in string_like
    assert "token.actions.githubusercontent.com:job_workflow_ref" in string_like


def test_bedrock_actions_are_allow_invoke_only(doc):
    role = doc["Resources"]["ShadowBotRole"]["Properties"]
    bedrock_policy = next(
        p for p in role["Policies"] if p["PolicyName"] == "BedrockInvoke"
    )
    statement = bedrock_policy["PolicyDocument"]["Statement"][0]
    assert statement["Effect"] == "Allow"
    # Invoke-only — no model management, no marketplace ops, no logging
    # admin. Tightening Bedrock permissions is the easiest way to limit
    # blast radius if the role is compromised.
    assert set(statement["Action"]) == {
        "bedrock:InvokeModel",
        "bedrock:Converse",
        "bedrock:InvokeModelWithResponseStream",
    }


def test_bedrock_resources_scope_to_anthropic_only(doc):
    role = doc["Resources"]["ShadowBotRole"]["Properties"]
    bedrock_policy = next(
        p for p in role["Policies"] if p["PolicyName"] == "BedrockInvoke"
    )
    resources = bedrock_policy["PolicyDocument"]["Statement"][0]["Resource"]
    # Every resource ARN must end in `anthropic.*` — Shadow only invokes
    # Claude. A wildcard model resource would let a compromised role
    # invoke arbitrary providers.
    for arn in resources:
        assert "anthropic." in arn, arn


def test_bedrock_region_parameter_is_constrained(doc):
    region = doc["Parameters"]["BedrockRegion"]
    # Only the 3 cross-region-inference backends are allowed; an open
    # parameter would silently let adopters request access in regions
    # where Anthropic models are not yet available.
    assert set(region["AllowedValues"]) == {"us-east-1", "us-west-2", "us-east-2"}


def test_cloudwatch_policy_scoped_to_shadow_namespace(doc):
    """CloudWatch metrics must only land in the Shadow namespace.
    Renaming the namespace requires updating BOTH this policy AND the
    adopter's CFN deployment — flag a divergence at test time."""
    role = doc["Resources"]["ShadowBotRole"]["Properties"]
    cw_policy = next(
        (p for p in role["Policies"] if p["PolicyName"] == "CloudWatchMetrics"),
        None,
    )
    assert cw_policy is not None, "CloudWatchMetrics policy missing"
    statement = cw_policy["PolicyDocument"]["Statement"][0]
    assert statement["Action"] == "cloudwatch:PutMetricData"
    assert statement["Effect"] == "Allow"
    cond = statement["Condition"]["StringEquals"]
    assert cond["cloudwatch:namespace"] == "Shadow"
