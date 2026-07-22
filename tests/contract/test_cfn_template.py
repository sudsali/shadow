"""The CFN Launch Stack is the install path for adopters. Its parameter
names, resource names, and output names are a public contract — adopter
docs reference them, and a downstream `Launch Stack` URL would break if
they change."""
import re
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
        "GitHubOidcProvider", "ShadowBotRole", "ShadowMetricsRole",
        "ShadowMonthlyBudget",
        "ShadowGuardrail", "ShadowGuardrailVersion",
        "ShadowAlarmTopic", "ShadowEscalationSpikeAlarm",
        "ShadowInvocationSpikeAlarm", "ShadowApprovalSpikeAlarm",
        "ShadowHighRiskApprovalAlarm",
    }


def test_required_parameters_present(doc):
    # These names appear in adopter docs / one-click URL query string;
    # renaming breaks every existing Launch Stack link.
    expected = {"GitHubOrg", "GitHubRepo", "ShadowSourceRepo",
                "ShadowWorkflowRef", "BedrockRegion",
                "ExistingOidcProviderArn",
                "MonthlyBudgetLimit", "BudgetEmailAddress",
                "EscalationSpikeThreshold", "InvocationSpikeThreshold",
                "ApprovalSpikeThreshold", "HighRiskApprovalThreshold",
                "ProvisionGuardrail"}
    assert set(doc["Parameters"].keys()) == expected


def test_required_outputs_present(doc):
    # README points adopters at these output names.
    expected = {"ShadowRoleArn", "MetricsRoleArn", "OidcProviderArn",
                "TrustPolicyScope", "BedrockRegion", "NextSteps",
                "GuardrailId", "GuardrailVersion", "AlarmTopicArn"}
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


def test_metrics_role_is_putmetricdata_only(doc):
    """The emit job's role must be least-privilege: only cloudwatch:PutMetricData
    scoped to namespace Shadow, and NOTHING else (no Bedrock/Secrets/S3). This is
    a distinct trust surface from shadow-review.yml, so a leak here would hand the
    approval workflow the full engine role."""
    role = doc["Resources"]["ShadowMetricsRole"]["Properties"]
    policies = role["Policies"]
    assert len(policies) == 1, "metrics role must carry exactly one policy"
    statements = policies[0]["PolicyDocument"]["Statement"]
    assert len(statements) == 1
    stmt = statements[0]
    assert stmt["Effect"] == "Allow"
    assert stmt["Action"] == "cloudwatch:PutMetricData", \
        "metrics role must grant ONLY PutMetricData"
    assert stmt["Condition"]["StringEquals"]["cloudwatch:namespace"] == "Shadow"
    # No engine permission anywhere in THIS role's policies.
    blob = str(policies)
    for forbidden in ("bedrock:", "secretsmanager:", "s3:"):
        assert forbidden not in blob, f"metrics role leaked {forbidden}"
    # Trust must pin to auto-approve.yml, not shadow-review.yml.
    cond = role["AssumeRolePolicyDocument"]["Statement"][0]["Condition"]["StringLike"]
    jwr = cond["token.actions.githubusercontent.com:job_workflow_ref"]
    assert "auto-approve.yml" in jwr and "shadow-review.yml" not in jwr


def test_approval_alarm_threshold_defaults(doc):
    """HighRiskApprovalThreshold=0 is security-load-bearing: with
    GreaterThanThreshold it pages on the first high-risk auto-approval. Pin the
    defaults so a bump can't silently disable that."""
    params = doc["Parameters"]
    assert params["ApprovalSpikeThreshold"]["Type"] == "Number"
    assert params["ApprovalSpikeThreshold"]["Default"] == 5
    assert params["ApprovalSpikeThreshold"]["MinValue"] == 1
    assert params["HighRiskApprovalThreshold"]["Type"] == "Number"
    assert params["HighRiskApprovalThreshold"]["Default"] == 0
    assert params["HighRiskApprovalThreshold"]["MinValue"] == 0
    # Default 0 only means "page on the first" under GreaterThanThreshold; pin
    # the operator + notBreaching or the semantics silently change.
    for name in ("ShadowApprovalSpikeAlarm", "ShadowHighRiskApprovalAlarm"):
        props = doc["Resources"][name]["Properties"]
        assert props["ComparisonOperator"] == "GreaterThanThreshold"
        assert props["TreatMissingData"] == "notBreaching"


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


def test_behavioral_alarms_aggregate_the_emitted_metrics(doc):
    """Each alarm must aggregate its metric across all dimension schemas via a
    Metrics Insights SQL query. A bare MetricName (no Dimensions) or a SEARCH
    expression is a dead/invalid alarm — the metric is emitted only WITH
    dimensions, and CloudWatch rejects SEARCH in an alarm. Metric names are
    cross-checked against cloudwatch.py so an emitter rename can't leave the
    alarm targeting a metric that's never published."""
    root = Path(__file__).resolve().parents[2]
    emitter_src = (root / "src/scripts/shadow/cloudwatch.py").read_text()
    # ApprovalGranted / HighRiskApproval are published by auto-approve.yml, not
    # the engine, so cross-check those two against the workflow.
    workflow_src = (root / "examples/auto-approve.yml").read_text()
    alarms = {
        "ShadowEscalationSpikeAlarm": ("Escalations", "EscalationSpikeThreshold", emitter_src, "engine"),
        "ShadowInvocationSpikeAlarm": ("Invocations", "InvocationSpikeThreshold", emitter_src, "engine"),
        "ShadowApprovalSpikeAlarm": ("ApprovalGranted", "ApprovalSpikeThreshold", workflow_src, "workflow"),
        "ShadowHighRiskApprovalAlarm": ("HighRiskApproval", "HighRiskApprovalThreshold", workflow_src, "workflow"),
    }
    for name, (metric, threshold_param, src, kind) in alarms.items():
        # The metric the alarm targets must actually be published by its emitter.
        if kind == "engine":
            # Match _datum(<quote><metric><quote>), tolerating whitespace/quotes.
            assert re.search(rf'_datum\(\s*[\'"]{metric}[\'"]', src), \
                f"{name} targets {metric}, which cloudwatch.py does not emit"
        else:
            # Workflow publishes via `MetricName=<metric>,` in an aws CLI call.
            # Trailing comma anchors the name so a superstring rename (e.g.
            # ApprovalGrantedV2) can't satisfy an alarm still on the old name.
            assert f"MetricName={metric}," in src, \
                f"{name} targets {metric}, which auto-approve.yml does not emit"
        alarm = doc["Resources"][name]
        assert alarm["Type"] == "AWS::CloudWatch::Alarm"
        assert alarm["Condition"] == "CreateAlarms"
        props = alarm["Properties"]
        # A top-level MetricName with no Dimensions is the dead-alarm footgun.
        assert "MetricName" not in props, f"{name} uses bare MetricName (dead-alarm risk)"
        exprs = [m.get("Expression", "") for m in props.get("Metrics", [])]
        joined = " ".join(exprs)
        assert "SEARCH(" not in joined, f"{name} uses SEARCH (illegal in a CloudWatch alarm)"
        assert "SELECT" in joined and "SUM(" in joined.upper(), \
            f"{name} is not a SELECT SUM(...) Metrics Insights query"
        assert f'"{metric}"' in joined, f"{name} does not aggregate {metric}"
        # Threshold must be wired to this alarm's own parameter (not swapped).
        assert props["Threshold"] == threshold_param, \
            f"{name} Threshold is not !Ref {threshold_param}"
        assert '"Shadow"' in joined, f"{name} not scoped to the Shadow namespace"
        # Wired to the SNS topic. (_CfnLoader renders !Ref as the bare scalar.)
        assert alarm["Properties"]["AlarmActions"] == ["ShadowAlarmTopic"]


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
