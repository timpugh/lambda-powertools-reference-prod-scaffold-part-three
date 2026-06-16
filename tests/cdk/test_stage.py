"""Stage-level assertion tests: environment naming, stack tags, and the cdk-nag gate.

The deployment-environment dimension (``env_name``) namespaces stack names so
ephemeral per-developer/per-branch deployments never collide with the
long-lived ``prod`` stacks in a shared account. These tests pin three
contracts:

1. **prod keeps the legacy names byte-for-byte** — any drift would orphan the
   currently deployed stacks (CloudFormation matches stacks by name).
2. **Non-prod names embed the env name and disable alarm paging.**
3. **Every stack synthesizes with zero unsuppressed cdk-nag errors.** This is
   a near-equivalent of the CLI ``cdk synth '**'`` gate that runs without
   Docker: ``Template.from_stack`` never *raises* on Aspect errors, but the
   findings are present as error-level annotations, so asserting the
   annotation list is empty catches an unsuppressed finding locally instead
   of in the CI cdk-check job. The CLI synth remains the authoritative gate
   (it also covers asset bundling); see CLAUDE.md.
"""

import json

import pytest

aws_cdk = pytest.importorskip("aws_cdk", reason="aws_cdk not installed — skipping CDK stage tests")

import aws_cdk as cdk
from aws_cdk.assertions import Annotations, Match, Template

from infrastructure.app_stage import DEFAULT_ENV_NAME, AppStage, validate_env_name

# Skip Docker bundling so these tests run without Docker (same key the CLI honours).
_NO_BUNDLING = {"aws:cdk:bundling-stacks": []}


@pytest.fixture(scope="module")
def prod_stage() -> AppStage:
    """Synthesize the default (prod) stage for us-east-1."""
    app = cdk.App(context=_NO_BUNDLING)
    return AppStage(app, "ServerlessApp-us-east-1-stage", region="us-east-1")


@pytest.fixture(scope="module")
def dev_stage() -> AppStage:
    """Synthesize an ephemeral developer stage."""
    app = cdk.App(context=_NO_BUNDLING)
    return AppStage(
        app,
        "ServerlessApp-alice-feature-x-us-east-1-stage",
        region="us-east-1",
        env_name="alice-feature-x",
    )


class TestEnvironmentNaming:
    def test_prod_default_keeps_legacy_stack_names(self, prod_stage: AppStage) -> None:
        # Byte-for-byte: a rename here orphans the deployed prod stacks.
        assert prod_stage.waf.stack_name == "ServerlessAppWaf-us-east-1"
        assert prod_stage.data.stack_name == "ServerlessAppData-us-east-1"
        assert prod_stage.backend.stack_name == "ServerlessAppBackend-us-east-1"
        assert prod_stage.frontend.stack_name == "ServerlessAppFrontend-us-east-1"
        assert prod_stage.audit.stack_name == "ServerlessAppAudit-us-east-1"

    def test_ephemeral_env_namespaces_every_stack(self, dev_stage: AppStage) -> None:
        assert dev_stage.waf.stack_name == "ServerlessAppWaf-alice-feature-x-us-east-1"
        assert dev_stage.data.stack_name == "ServerlessAppData-alice-feature-x-us-east-1"
        assert dev_stage.backend.stack_name == "ServerlessAppBackend-alice-feature-x-us-east-1"
        assert dev_stage.frontend.stack_name == "ServerlessAppFrontend-alice-feature-x-us-east-1"
        assert dev_stage.audit.stack_name == "ServerlessAppAudit-alice-feature-x-us-east-1"

    def test_ephemeral_env_disables_alarm_paging(self, dev_stage: AppStage) -> None:
        # Non-prod environments must not create the SNS alarm topic — an
        # ephemeral branch stack should never page an operator.
        template = Template.from_stack(dev_stage.backend)
        template.resource_count_is("AWS::SNS::Topic", 0)

    def test_prod_env_routes_alarms_to_topic(self, prod_stage: AppStage) -> None:
        template = Template.from_stack(prod_stage.backend)
        template.resource_count_is("AWS::SNS::Topic", 1)

    @pytest.mark.parametrize("bad_name", ["", "has space", "has/slash", "-leading-hyphen", "x" * 40])
    def test_invalid_env_names_fail_at_synth(self, bad_name: str) -> None:
        # Stack names embed the env name; reject illegal values with a clear
        # message at synth instead of an opaque CloudFormation error at deploy.
        with pytest.raises(ValueError, match="Invalid deployment environment name"):
            validate_env_name(bad_name)

    def test_default_env_name_is_prod(self) -> None:
        # app.py and the Makefile lean on this default; changing it silently
        # re-points bare `cdk deploy` away from the long-lived stacks.
        assert DEFAULT_ENV_NAME == "prod"


class TestStackTags:
    def test_all_stacks_carry_service_environment_owner_tags(self, prod_stage: AppStage) -> None:
        # Stack tags propagate to every taggable resource at deploy time —
        # cost allocation and console filtering for free. They live in the
        # deploy-time manifest (not the template body), so assert on the
        # stack's tag manager rather than template properties.
        for stack in (prod_stage.waf, prod_stage.data, prod_stage.backend, prod_stage.frontend, prod_stage.audit):
            tags = stack.tags.tag_values()
            assert tags.get("service") == "serverless-app"
            assert tags.get("environment") == "prod"
            assert tags.get("owner"), "owner tag must be non-empty (username or ci fallback)"

    def test_ephemeral_stage_tags_carry_env_name(self, dev_stage: AppStage) -> None:
        assert dev_stage.backend.tags.tag_values().get("environment") == "alice-feature-x"


class TestRetainDataPlumbing:
    """The Stage forwards retain_data to the stateful data stack."""

    def test_default_stage_data_table_is_destroyable(self, prod_stage: AppStage) -> None:
        # Default stage (retain_data omitted) → DESTROY, so the template tears
        # down cleanly.
        Template.from_stack(prod_stage.data).has_resource("AWS::DynamoDB::GlobalTable", {"DeletionPolicy": "Delete"})

    def test_retain_data_stage_data_table_is_retained(self) -> None:
        # retain_data=True flows Stage → DataStack → RETAIN.
        app = cdk.App(context=_NO_BUNDLING)
        stage = AppStage(app, "ServerlessApp-us-east-1-stage", region="us-east-1", retain_data=True)
        Template.from_stack(stage.data).has_resource("AWS::DynamoDB::GlobalTable", {"DeletionPolicy": "Retain"})


class TestAppConfigMonitorPlumbing:
    """The Stage forwards appconfig_monitor → backend → BackendApp.

    Default (off) = all-at-once, no monitor (asserted in test_stacks.py — the
    shape that must create a cold stack). On = gradual rollout + environment
    monitor, the opt-in production add-on for ongoing flag changes.
    """

    @staticmethod
    def _monitor_stage() -> AppStage:
        app = cdk.App(context=_NO_BUNDLING)
        return AppStage(app, "ServerlessApp-us-east-1-stage", region="us-east-1", appconfig_monitor=True)

    def test_monitor_on_uses_gradual_strategy(self) -> None:
        # appconfig_monitor=True flows Stage → BackendStack → BackendApp and
        # swaps the all-at-once strategy for a gradual one with a bake window.
        template = Template.from_stack(self._monitor_stage().backend)
        template.has_resource_properties(
            "AWS::AppConfig::DeploymentStrategy",
            {
                "GrowthType": "LINEAR",
                "GrowthFactor": 25,
                "DeploymentDurationInMinutes": 10,
                "FinalBakeTimeInMinutes": 5,
            },
        )

    def test_monitor_on_attaches_environment_monitor(self) -> None:
        # The environment carries a Monitors entry (alarm ARN + role ARN) so
        # AppConfig auto-rolls-back a bad flag rollout.
        template = Template.from_stack(self._monitor_stage().backend)
        template.has_resource_properties(
            "AWS::AppConfig::Environment",
            {
                "Monitors": Match.array_with(
                    [Match.object_like({"AlarmArn": Match.any_value(), "AlarmRoleArn": Match.any_value()})]
                )
            },
        )
        # The monitor role AppConfig assumes to read the alarm state exists.
        roles = template.find_resources("AWS::IAM::Role")
        assert any(
            "appconfig.amazonaws.com" in json.dumps(r["Properties"].get("AssumeRolePolicyDocument", {}))
            for r in roles.values()
        ), "expected an AppConfig-assumed monitor role when appconfig_monitor=True"


class TestDeploymentAggressivenessByEnv:
    """CodeDeploy canary aggressiveness is environment-gated: canary in prod, fast in dev.

    Prod-shape detail is asserted in test_stacks.py; this proves the *contrast* —
    the dev/ephemeral shape shifts the Lambda alias all-at-once so iterating
    doesn't wait out a canary window. (The AppConfig deployment is all-at-once in
    every environment — see test_stacks.py for why a CFN-managed cold deploy
    can't carry the alarm-monitored gradual rollout.)
    """

    def test_dev_codedeploy_is_all_at_once(self, dev_stage: AppStage) -> None:
        template = Template.from_stack(dev_stage.backend)
        groups = template.find_resources("AWS::CodeDeploy::DeploymentGroup")
        assert groups, "expected a CodeDeploy deployment group even in dev"
        config_names = [g["Properties"].get("DeploymentConfigName", "") for g in groups.values()]
        assert all("AllAtOnce" in name for name in config_names), config_names


class TestNagCompliance:
    """Zero error-level annotations per stack, asserted without Docker.

    Covers both cdk-nag rule-pack findings and the project's
    ``TemplateConventionChecks`` validation Aspect — both surface as
    error-level annotations through ``apply_compliance_aspects``.
    """

    @pytest.mark.parametrize("stack_attr", ["waf", "data", "backend", "frontend", "audit"])
    def test_no_unsuppressed_nag_errors(self, prod_stage: AppStage, stack_attr: str) -> None:
        stack = getattr(prod_stage, stack_attr)
        errors = Annotations.from_stack(stack).find_error("*", Match.string_like_regexp(".*"))
        details = "\n".join(
            f"  {e.id}: {(e.entry.data if isinstance(e.entry.data, str) else str(e.entry.data)).splitlines()[0]}"
            for e in errors
        )
        assert not errors, (
            f"unsuppressed cdk-nag findings on {stack.stack_name} — fix the resource or add a "
            f"scoped suppression with a reason (see CLAUDE.md):\n{details}"
        )

    def test_dev_stage_has_no_unsuppressed_nag_errors(self, dev_stage: AppStage) -> None:
        # The non-prod shape (no SNS topic) must be nag-clean too — otherwise
        # ephemeral stacks would fail the CI synth gate when env is overridden.
        errors = Annotations.from_stack(dev_stage.backend).find_error("*", Match.string_like_regexp(".*"))
        assert not errors

    def test_appconfig_monitor_shape_has_no_unsuppressed_nag_errors(self) -> None:
        # The opt-in monitor shape adds an alarm + a cloudwatch:DescribeAlarms
        # role; its suppressions (IAM5 wildcard, inline-policy, no-alarm-action)
        # must be complete or a fork enabling appconfig_monitor would fail synth.
        app = cdk.App(context=_NO_BUNDLING)
        stage = AppStage(app, "ServerlessApp-us-east-1-stage", region="us-east-1", appconfig_monitor=True)
        errors = Annotations.from_stack(stage.backend).find_error("*", Match.string_like_regexp(".*"))
        assert not errors
