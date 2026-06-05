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
    assert set(doc["Resources"].keys()) == {
        "GitHubOidcProvider", "ShadowBotRole", "ShadowMonthlyBudget",
        "ShadowGuardrail", "ShadowGuardrailVersion",
    }


def test_required_parameters_present(doc):
    # These names appear in adopter docs / one-click URL query string;
    # renaming breaks every existing Launch Stack link.
    expected = {"GitHubOrg", "GitHubRepo", "ShadowSourceRepo",
                "ShadowWorkflowRef", "BedrockRegion",
                "ExistingOidcProviderArn",
                "MonthlyBudgetLimit", "BudgetEmailAddress",
                "ProvisionGuardrail"}
    assert set(doc["Parameters"].keys()) == expected


def test_required_outputs_present(doc):
    # README points adopters at these output names.
    expected = {"ShadowRoleArn", "OidcProviderArn", "TrustPolicyScope",
                "BedrockRegion", "NextSteps",
                "GuardrailId", "GuardrailVersion"}
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


def test_monthly_budget_parameter_present(doc):
    """Cost-protection parameters must keep the same names + types so a
    re-launch of the stack with the same template URL is idempotent."""
    params = doc["Parameters"]
    assert params["MonthlyBudgetLimit"]["Type"] == "Number"
    assert params["MonthlyBudgetLimit"]["Default"] == 0
    # MinValue prevents adopters from passing -1 to "disable" — that path
    # would create a Budget with a negative limit, which AWS silently
    # accepts as "alert immediately on every dollar spent".
    assert params["MonthlyBudgetLimit"]["MinValue"] == 0
    assert params["BudgetEmailAddress"]["Type"] == "String"
    assert params["BudgetEmailAddress"]["Default"] == ""


def test_monthly_budget_resource_present_when_set(doc):
    """The Budget resource must filter on Bedrock spend only — a missing
    CostFilter would alert on the entire account's spend, masking what
    Shadow itself costs."""
    budget = doc["Resources"]["ShadowMonthlyBudget"]
    assert budget["Type"] == "AWS::Budgets::Budget"
    # Conditional creation: stack with limit=0 must NOT create a Budget,
    # because AWS Budgets bills $0.02/budget/day and a no-op budget is
    # money on fire.
    assert budget["Condition"] == "CreateBudget"
    props = budget["Properties"]
    assert props["Budget"]["BudgetType"] == "COST"
    assert props["Budget"]["TimeUnit"] == "MONTHLY"
    assert props["Budget"]["CostFilters"]["Service"] == ["Amazon Bedrock"]
    notifications = props["NotificationsWithSubscribers"]
    # At least 80% + 100% thresholds — the 80% gives breathing room before
    # the budget blows; the 100% is the ground-truth "we hit the limit".
    thresholds = sorted(n["Notification"]["Threshold"] for n in notifications)
    assert thresholds == [80, 100]
    for n in notifications:
        assert n["Notification"]["NotificationType"] == "ACTUAL"
        assert n["Notification"]["ComparisonOperator"] == "GREATER_THAN"
        for sub in n["Subscribers"]:
            assert sub["SubscriptionType"] == "EMAIL"


def test_shadow_source_repo_default_and_threading(doc):
    """ShadowSourceRepo defaults to upstream sudsali/shadow but threads through
    job_workflow_ref so a fork can stand up its own role without editing the
    template. Locks both the default and the substitution to prevent regression
    to a hardcoded `sudsali/shadow` in the trust policy."""
    params = doc["Parameters"]
    assert params["ShadowSourceRepo"]["Default"] == "sudsali/shadow"
    role = doc["Resources"]["ShadowBotRole"]["Properties"]
    statement = role["AssumeRolePolicyDocument"]["Statement"][0]
    job_ref = statement["Condition"]["StringLike"][
        "token.actions.githubusercontent.com:job_workflow_ref"
    ]
    assert "${ShadowSourceRepo}" in job_ref
    assert "sudsali/shadow/" not in job_ref


def test_guardrail_provisioned_by_default(doc):
    """ProvisionGuardrail defaults to "true" so adopters get a default
    Bedrock Guardrail without an explicit opt-in. Flipping this default
    means new adopters silently lose prompt-injection defense — flag at
    test time."""
    assert doc["Parameters"]["ProvisionGuardrail"]["Default"] == "true"
    assert doc["Conditions"]["CreateGuardrail"]


def test_guardrail_blocks_prompt_attack_at_high_strength(doc):
    """The guardrail's PROMPT_ATTACK input filter is the headline defense
    against jailbreak / instruction-override attempts. HIGH input strength
    is the strongest setting; downgrading silently weakens defense."""
    guardrail = doc["Resources"]["ShadowGuardrail"]["Properties"]
    filters = guardrail["ContentPolicyConfig"]["FiltersConfig"]
    prompt_attack = next(f for f in filters if f["Type"] == "PROMPT_ATTACK")
    assert prompt_attack["InputStrength"] == "HIGH"


def test_guardrail_denies_system_prompt_extraction(doc):
    """The denied-topic list must include attempts to exfiltrate the bot's
    system prompt — that's the PR-review-bot-specific attack surface."""
    guardrail = doc["Resources"]["ShadowGuardrail"]["Properties"]
    topics = guardrail["TopicPolicyConfig"]["TopicsConfig"]
    names = {t["Name"] for t in topics}
    assert "SystemPromptExtraction" in names
    assert "InstructionOverride" in names
    for topic in topics:
        assert topic["Type"] == "DENY"


def test_guardrail_blocks_aws_keys(doc):
    """AWS access keys must be blocked at the guardrail layer in addition
    to the local sanitizer. Belt-and-suspenders: a model paraphrase that
    elides the literal AKIA prefix could slip past the local regex but
    not the Bedrock-server-side scanner."""
    guardrail = doc["Resources"]["ShadowGuardrail"]["Properties"]
    pii = guardrail["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]
    aws_keys = [p for p in pii if p["Type"] in ("AWS_ACCESS_KEY", "AWS_SECRET_KEY")]
    assert len(aws_keys) == 2
    for entry in aws_keys:
        assert entry["Action"] == "BLOCK"


def test_apply_guardrail_permission_granted_when_provisioned(doc):
    """The role's BedrockInvoke policy must include bedrock:ApplyGuardrail
    when CreateGuardrail is true — otherwise Converse calls referencing the
    guardrail get AccessDenied at runtime, breaking every PR review."""
    role = doc["Resources"]["ShadowBotRole"]["Properties"]
    bedrock_policy = next(
        p for p in role["Policies"] if p["PolicyName"] == "BedrockInvoke"
    )
    statements = bedrock_policy["PolicyDocument"]["Statement"]
    # Find the conditional ApplyGuardrail statement (rendered as !If).
    found = False
    for s in statements:
        # When parsed, !If renders as a 3-element sequence; look for a
        # statement whose Action contains ApplyGuardrail in any branch.
        if isinstance(s, list):
            for branch in s:
                if isinstance(branch, dict) and branch.get("Action") == "bedrock:ApplyGuardrail":
                    found = True
        elif isinstance(s, dict) and s.get("Action") == "bedrock:ApplyGuardrail":
            found = True
    assert found, "bedrock:ApplyGuardrail permission missing from BedrockInvoke policy"


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
