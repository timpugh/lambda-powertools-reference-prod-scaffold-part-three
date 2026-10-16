"""Stage-level assertion tests: environment naming, stack tags, and the cdk-nag gate.

The deployment-environment dimension (``env_name``) namespaces stack names so
ephemeral per-developer/per-branch deployments never collide with the
long-lived ``prod`` stacks in a shared account. These tests pin three
contracts:

1. **prod keeps the legacy names byte-for-byte** — any drift would orphan the
   currently deployed stacks (CloudFormation matches stacks by name).
2. **Non-prod names embed the env name and disable alarm paging.**
3. **Every shipped shape synthesizes with zero unacknowledged cdk-nag
   findings.** cdk-nag v3 packs are policy-validation plugins evaluated over
   the synthesized assembly; the gate here synthesizes each deployment shape
   (fixtures attach the packs the same way ``app.py`` does) and parses the
   assembly's ``validation-report.json`` — neither ``app.synth()`` nor the
   CLI fails natively for Python apps (see ``_unacknowledged_findings``).
   The project's own ``TemplateConventionChecks`` Aspect still surfaces
   error-level annotations, asserted separately. The CLI path stays gated by
   ``scripts/check_validation_report.py`` in ``make cdk-synth`` and CI.
"""

import json
from pathlib import Path
from typing import cast

import pytest

aws_cdk = pytest.importorskip("aws_cdk", reason="aws_cdk not installed — skipping CDK stage tests")

import aws_cdk as cdk
from aws_cdk import aws_s3 as s3
from aws_cdk.assertions import Annotations, Match, Template

from infrastructure.app_stage import (
    APPCONFIG_EXTENSION_LAYER_ARN_ENV_VAR,
    DEFAULT_ENV_NAME,
    AppStage,
    parse_context_flag,
    resolve_appconfig_extension_layer_arn,
    validate_appconfig_extension_layer_arn,
    validate_code_connection_arn,
    validate_env_name,
    validate_ssm_param_path,
)
from infrastructure.appconfig_extension import (
    APPCONFIG_EXTENSION_ARM64_LAYER_ARNS,
    APPCONFIG_EXTENSION_DOCS_URL,
    partition_for_region,
    resolve_extension_layer_arn,
)
from infrastructure.backend_stack import BackendStack
from infrastructure.data_stack import DataStack
from infrastructure.nag_utils import attach_nag_packs
from infrastructure.validation_aspects import TemplateConventionChecks

# Skip Docker bundling so these tests run without Docker (same key the CLI honours).
_NO_BUNDLING = {"aws:cdk:bundling-stacks": []}


def _nag_app() -> cdk.App:
    """A test App with the five rule packs attached, mirroring app.py."""
    app = cdk.App(context=_NO_BUNDLING)
    attach_nag_packs(app)
    return app


def _unacknowledged_findings(root: cdk.App) -> list[str]:
    """Synthesize an App and return its unacknowledged cdk-nag v3 findings.

    Policy-validation plugins evaluate the produced assembly during
    ``App.synth()``, but the in-process synth does NOT raise on findings
    (observed against cdk-nag 3.0.1) — it prints the report and writes
    ``validation-report.json`` into the cloud assembly. Reading that file is
    therefore the reliable in-process gate. The CLI synth doesn't fail for
    Python apps either (CDK sets process.exitCode in jsii's throwaway Node
    kernel), which is why make cdk-synth / CI run
    scripts/check_validation_report.py over cdk.out after synthesizing.
    """
    assembly = root.synth()
    report_path = Path(assembly.directory) / "validation-report.json"
    if not report_path.exists():
        return []
    report = json.loads(report_path.read_text())
    return [
        f"{violation.get('ruleName')} @ "
        + ", ".join(r.get("resourceLogicalId", "?") for r in violation.get("violatingResources", []))
        for plugin_report in report.get("pluginReports", [])
        for violation in plugin_report.get("violations", [])
    ]


def _assert_nag_clean(stage: AppStage) -> None:
    findings = _unacknowledged_findings(cast(cdk.App, stage.node.root))
    details = "\n".join(f"  {f}" for f in findings)
    assert not findings, (
        f"unacknowledged cdk-nag findings — fix the resource or add a scoped "
        f"acknowledgment with a reason (see CLAUDE.md):\n{details}"
    )


@pytest.fixture(scope="module")
def prod_stage() -> AppStage:
    """Synthesize the default (prod) stage for us-east-1."""
    return AppStage(_nag_app(), "ServerlessApp-us-east-1-stage", region="us-east-1")


@pytest.fixture(scope="module")
def dev_stage() -> AppStage:
    """Synthesize an ephemeral developer stage."""
    return AppStage(
        _nag_app(),
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


class TestContextFlagParsing:
    """app.py parses retain_data / appconfig_monitor via parse_context_flag."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, False),  # flag absent — the documented destroy-friendly default
            (True, True),  # native bool from cdk.json
            (False, False),
            ("true", True),  # CLI string forms, any casing
            ("TRUE", True),
            ("false", False),
            ("False", False),
        ],
    )
    def test_recognized_values(self, raw: object, expected: bool) -> None:
        assert parse_context_flag(raw, "retain_data") is expected

    @pytest.mark.parametrize("raw", ["yes", "1", "on", "retain", ""])
    def test_unrecognized_values_fail_at_synth(self, raw: str) -> None:
        # `-c retain_data=yes` silently coercing to False would deploy DESTROY
        # policies with no deletion protection while the operator believes
        # production data is retained — fail the synth loudly instead.
        with pytest.raises(ValueError, match="retain_data"):
            parse_context_flag(raw, "retain_data")


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


class TestSsmParamPathPlumbing:
    """`-c ssm_param_path=/my/path` overrides the greeting parameter name."""

    def test_default_keeps_autogenerated_name(self, prod_stage: AppStage) -> None:
        # No Name property when the context key is absent — CDK auto-generates,
        # exactly as before this feature existed (no prod template churn).
        params = Template.from_stack(prod_stage.backend).find_resources("AWS::SSM::Parameter")
        assert all("Name" not in p["Properties"] for p in params.values())

    def test_context_path_sets_parameter_name(self) -> None:
        app = cdk.App(context=_NO_BUNDLING)
        stage = AppStage(
            app, "ServerlessApp-us-east-1-stage", region="us-east-1", ssm_param_path="/serverless-app/greeting"
        )
        Template.from_stack(stage.backend).has_resource_properties(
            "AWS::SSM::Parameter", {"Name": "/serverless-app/greeting"}
        )

    @pytest.mark.parametrize("bad", ["no-leading-slash", "/trailing/", "/has space", "/", ""])
    def test_invalid_paths_fail_at_synth(self, bad: str) -> None:
        with pytest.raises(ValueError, match="ssm_param_path"):
            validate_ssm_param_path(bad)

    def test_none_passes_through(self) -> None:
        assert validate_ssm_param_path(None) is None


def _function_layers(stage: AppStage) -> list[object]:
    """Every Lambda Layers entry in the backend stack, flattened."""
    functions = Template.from_stack(stage.backend).find_resources("AWS::Lambda::Function")
    return [layer for fn in functions.values() for layer in fn["Properties"].get("Layers", [])]


# Test-fixture override ARNs: well-formed but deliberately NOT real AWS layers
# (fake account), so they can never be mistaken for a pinned production value.
_FAKE_CONTEXT_ARN = "arn:aws:lambda:us-east-1:123456789012:layer:AppConfig-Extension-Context:7"
_FAKE_ENV_ARN = "arn:aws:lambda:us-east-1:123456789012:layer:AppConfig-Extension-Env:8"


class TestAppConfigExtensionLayerRegion:
    """The AppConfig extension layer resolves per deploy region.

    Resolution order: CDK context ``appconfig_extension_layer_arn`` → the
    ``APPCONFIG_EXTENSION_LAYER_ARN`` env var → the pinned
    ``APPCONFIG_EXTENSION_ARM64_LAYER_ARNS`` map (appconfig_extension.py) → a
    ``ValueError``. An unmapped region must break synth with an actionable error
    rather than deploy a Lambda whose flag store points at a dead localhost port.
    """

    def test_us_east_1_uses_pinned_2_0_22365_layer(self, prod_stage: AppStage) -> None:
        assert APPCONFIG_EXTENSION_ARM64_LAYER_ARNS["us-east-1"] in _function_layers(prod_stage)

    def test_mapped_non_us_east_1_region_uses_its_own_layer(self) -> None:
        app = cdk.App(context=_NO_BUNDLING)
        stage = AppStage(app, "ServerlessApp-ap-southeast-2-stage", region="ap-southeast-2")
        layers = _function_layers(stage)
        assert APPCONFIG_EXTENSION_ARM64_LAYER_ARNS["ap-southeast-2"] in layers
        assert APPCONFIG_EXTENSION_ARM64_LAYER_ARNS["us-east-1"] not in layers

    def test_every_mapped_arn_is_well_formed_and_in_its_own_region(self) -> None:
        # Guards copy/paste errors in the 39-entry table: each ARN must pass the
        # same validator an override does, carry its key's region, and sit in
        # that region's partition (the same helper the override check uses).
        for region, arn in APPCONFIG_EXTENSION_ARM64_LAYER_ARNS.items():
            assert validate_appconfig_extension_layer_arn(arn) == arn
            assert arn.split(":")[3] == region, (region, arn)
            assert arn.split(":")[1] == partition_for_region(region), (region, arn)

    @pytest.mark.parametrize(
        ("region", "partition"),
        [
            ("us-east-1", "aws"),
            ("cn-north-1", "aws-cn"),
            ("us-gov-west-1", "aws-us-gov"),
            ("eusc-de-east-1", "aws-eusc"),
        ],
    )
    def test_partition_for_region(self, region: str, partition: str) -> None:
        assert partition_for_region(region) == partition

    def test_override_partition_mismatch_raises(self) -> None:
        # Well-formed and region-matched, but cn-north-1 lives in aws-cn.
        bad = "arn:aws:lambda:cn-north-1:123456789012:layer:AppConfig-Extension-Context:7"
        with pytest.raises(ValueError, match=r"uses partition 'aws', but region 'cn-north-1' is in partition 'aws-cn'"):
            resolve_extension_layer_arn("cn-north-1", bad)

    def test_override_warns_on_backend_stack(self) -> None:
        stage = AppStage(
            cdk.App(context=_NO_BUNDLING),
            "ServerlessApp-us-east-1-stage",
            region="us-east-1",
            appconfig_extension_layer_arn=_FAKE_CONTEXT_ARN,
        )
        Annotations.from_stack(stage.backend).has_warning(
            "*", Match.string_like_regexp(f".*override active: {_FAKE_CONTEXT_ARN} overrides the pinned.*")
        )

    def test_no_override_no_warning(self, prod_stage: AppStage) -> None:
        Annotations.from_stack(prod_stage.backend).has_no_warning(
            "*", Match.string_like_regexp(".*AppConfig extension layer override.*")
        )

    @pytest.mark.parametrize("bad", ["arn:aws:lambda", "not-an-arn"])
    def test_malformed_override_bypassing_appstage_gets_friendly_error(self, bad: str) -> None:
        # Direct BackendStack use skips AppStage's validator; the resolver must
        # still give the friendly ValueError, not an IndexError from split().
        app = cdk.App(context=_NO_BUNDLING)
        env = cdk.Environment(region="us-east-1")
        data = DataStack(app, "BypassData", env=env)
        with pytest.raises(ValueError, match="appconfig_extension_layer_arn"):
            BackendStack(
                app,
                "BypassBackend",
                idempotency_table=data.idempotency_table,
                appconfig_extension_layer_arn=bad,
                env=env,
            )

    def test_unmapped_region_raises_clear_error(self) -> None:
        # us-iso-east-1 is a real region (aws-iso partition) that AWS does not
        # list in the extension's arm64 table, so it is genuinely unmapped. It is
        # exercised at the resolver, not via AppStage: a full Stage synth for it
        # fails earlier, in the Lambda Insights layer lookup (no Insights layer
        # published there), which would mask this error.
        assert "us-iso-east-1" not in APPCONFIG_EXTENSION_ARM64_LAYER_ARNS
        with pytest.raises(ValueError, match="us-iso-east-1") as excinfo:
            resolve_extension_layer_arn("us-iso-east-1", None)
        message = str(excinfo.value)
        assert "No AppConfig extension layer ARN mapped for region 'us-iso-east-1'" in message
        assert "-c appconfig_extension_layer_arn=" in message
        assert APPCONFIG_EXTENSION_LAYER_ARN_ENV_VAR in message
        assert APPCONFIG_EXTENSION_DOCS_URL in message

    def test_unmapped_region_fails_stage_synth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # End-to-end through AppStage: drop a mapped region from the map so the
        # whole synth path (Stage -> BackendStack -> BackendApp) hits the error.
        monkeypatch.delitem(APPCONFIG_EXTENSION_ARM64_LAYER_ARNS, "ap-southeast-1")
        app = cdk.App(context=_NO_BUNDLING)
        with pytest.raises(ValueError, match=r"No AppConfig extension layer ARN mapped for region 'ap-southeast-1'"):
            AppStage(app, "ServerlessApp-ap-southeast-1-stage", region="ap-southeast-1")

    def test_override_rescues_unmapped_region(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delitem(APPCONFIG_EXTENSION_ARM64_LAYER_ARNS, "ap-southeast-1")
        override = "arn:aws:lambda:ap-southeast-1:123456789012:layer:AppConfig-Extension-Context:7"
        app = cdk.App(context=_NO_BUNDLING)
        stage = AppStage(
            app, "ServerlessApp-ap-southeast-1-stage", region="ap-southeast-1", appconfig_extension_layer_arn=override
        )
        assert override in _function_layers(stage)

    def test_override_attaches_instead_of_map(self) -> None:
        app = cdk.App(context=_NO_BUNDLING)
        stage = AppStage(
            app, "ServerlessApp-us-east-1-stage", region="us-east-1", appconfig_extension_layer_arn=_FAKE_CONTEXT_ARN
        )
        layers = _function_layers(stage)
        assert _FAKE_CONTEXT_ARN in layers
        assert APPCONFIG_EXTENSION_ARM64_LAYER_ARNS["us-east-1"] not in layers

    def test_override_region_mismatch_raises(self) -> None:
        app = cdk.App(context=_NO_BUNDLING)
        with pytest.raises(ValueError, match=r"in region 'us-east-1', but the backend stack deploys to 'eu-west-1'"):
            AppStage(
                app,
                "ServerlessApp-eu-west-1-stage",
                region="eu-west-1",
                appconfig_extension_layer_arn=_FAKE_CONTEXT_ARN,
            )

    def test_unresolved_region_raises(self) -> None:
        with pytest.raises(ValueError, match="unresolved"):
            resolve_extension_layer_arn(cdk.Aws.REGION, None)


class TestAppConfigExtensionLayerOverrideResolution:
    """app.py's override resolution: context wins over env var; env var over the map."""

    def test_context_wins_over_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(APPCONFIG_EXTENSION_LAYER_ARN_ENV_VAR, _FAKE_ENV_ARN)
        assert resolve_appconfig_extension_layer_arn(_FAKE_CONTEXT_ARN) == _FAKE_CONTEXT_ARN

    def test_env_var_used_when_context_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(APPCONFIG_EXTENSION_LAYER_ARN_ENV_VAR, _FAKE_ENV_ARN)
        resolved = resolve_appconfig_extension_layer_arn(None)
        assert resolved == _FAKE_ENV_ARN
        # ...and the env-var override then wins over the pinned map in the synth.
        stage = AppStage(
            cdk.App(context=_NO_BUNDLING),
            "ServerlessApp-us-east-1-stage",
            region="us-east-1",
            appconfig_extension_layer_arn=resolved,
        )
        layers = _function_layers(stage)
        assert _FAKE_ENV_ARN in layers
        assert APPCONFIG_EXTENSION_ARM64_LAYER_ARNS["us-east-1"] not in layers

    def test_neither_set_falls_through_to_map(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(APPCONFIG_EXTENSION_LAYER_ARN_ENV_VAR, raising=False)
        assert resolve_appconfig_extension_layer_arn(None) is None

    def test_empty_env_var_treated_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(APPCONFIG_EXTENSION_LAYER_ARN_ENV_VAR, "")
        assert resolve_appconfig_extension_layer_arn(None) is None

    def test_malformed_env_var_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(APPCONFIG_EXTENSION_LAYER_ARN_ENV_VAR, "not-an-arn")
        with pytest.raises(ValueError, match="appconfig_extension_layer_arn"):
            resolve_appconfig_extension_layer_arn(None)

    @pytest.mark.parametrize(
        "bad",
        [
            "not-an-arn",
            # missing the mandatory :<version>
            "arn:aws:lambda:us-east-1:027255383542:layer:AWS-AppConfig-Extension-Arm64",
            # unsupported partition
            "arn:aws-iso:lambda:us-iso-east-1:123456789012:layer:AppConfig:1",
            # not a layer ARN
            "arn:aws:lambda:us-east-1:123456789012:function:my-fn",
            # account not 12 digits
            "arn:aws:lambda:us-east-1:12345:layer:AppConfig:1",
            "",
        ],
    )
    def test_validator_rejects_malformed_arns(self, bad: str) -> None:
        with pytest.raises(ValueError, match="appconfig_extension_layer_arn"):
            validate_appconfig_extension_layer_arn(bad)

    @pytest.mark.parametrize(
        "good",
        [
            "arn:aws-cn:lambda:cn-north-1:615057806174:layer:AWS-AppConfig-Extension-Arm64:171",
            "arn:aws-us-gov:lambda:us-gov-west-1:946746059096:layer:AWS-AppConfig-Extension-Arm64:172",
            "arn:aws-eusc:lambda:eusc-de-east-1:426221636601:layer:AWS-AppConfig-Extension-Arm64:48",
        ],
    )
    def test_validator_accepts_every_supported_partition(self, good: str) -> None:
        assert validate_appconfig_extension_layer_arn(good) == good

    def test_validator_none_passes_through(self) -> None:
        assert validate_appconfig_extension_layer_arn(None) is None

    @pytest.mark.parametrize("bad", [True, False, 0, 265, ["arn"]])
    def test_non_string_context_value_gets_friendly_error(self, bad: object, monkeypatch: pytest.MonkeyPatch) -> None:
        # cdk.json can hand over a native bool/number; it must not raise a
        # TypeError, nor (when falsy) silently fall through to the env var.
        monkeypatch.setenv(APPCONFIG_EXTENSION_LAYER_ARN_ENV_VAR, _FAKE_ENV_ARN)
        with pytest.raises(ValueError, match="appconfig_extension_layer_arn"):
            resolve_appconfig_extension_layer_arn(bad)


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
    """cdk-nag v3 gate: unacknowledged findings fail ``app.synth()`` itself.

    The five rule packs are policy-validation plugins on each fixture's test
    App (attached exactly the way ``app.py`` does), so one full-app synth per
    deployment shape is the compliance gate for every stack in that shape.
    The project's own ``TemplateConventionChecks`` Aspect still surfaces
    error-level annotations and is asserted separately.
    """

    def test_prod_stage_has_no_unacknowledged_findings(self, prod_stage: AppStage) -> None:
        _assert_nag_clean(prod_stage)

    def test_dev_stage_has_no_unacknowledged_findings(self, dev_stage: AppStage) -> None:
        # The non-prod shape (no SNS topic) must be nag-clean too — otherwise
        # ephemeral stacks would fail the CI synth gate when env is overridden.
        _assert_nag_clean(dev_stage)

    def test_appconfig_monitor_shape_has_no_unacknowledged_findings(self) -> None:
        # The opt-in monitor shape adds an alarm + a cloudwatch:DescribeAlarms
        # role; its acknowledgments (IAM5 wildcard, inline-policy,
        # no-alarm-action) must be complete or a fork enabling
        # appconfig_monitor would fail synth.
        stage = AppStage(_nag_app(), "ServerlessApp-us-east-1-stage", region="us-east-1", appconfig_monitor=True)
        _assert_nag_clean(stage)

    def test_retain_data_shape_has_no_unacknowledged_findings(self) -> None:
        # The production-fork shape (retain_data=True: RETAIN + deletion/
        # termination protection, retained buckets with no auto-delete
        # provider) is never synthesized by the CI CLI gate (default context) —
        # this is its only nag gate. A finding unique to the retained shape
        # would otherwise surface for the first time on a production fork's
        # own synth, after they flip the one switch the template tells them to.
        stage = AppStage(_nag_app(), "ServerlessApp-us-east-1-stage", region="us-east-1", retain_data=True)
        _assert_nag_clean(stage)

    def test_nag_gate_can_fail(self) -> None:
        # The assertion that the gate is not vacuous: a deliberately
        # non-compliant resource must produce findings in the validation
        # report. If the packs stop attaching (or the report location/shape
        # changes), every clean-gate test above would pass on nothing — this
        # canary is what catches that. A bare default Bucket violates several
        # pack rules (no access logs, no SSL, no KMS default encryption).
        app = _nag_app()
        stack = cdk.Stack(app, "NagCanaryStack")
        s3.Bucket(stack, "NonCompliantBucket")
        findings = _unacknowledged_findings(app)
        assert findings, "the nag gate reported nothing for a non-compliant bucket — the gate is vacuous"
        assert any("AwsSolutions-S" in f or "S3Bucket" in f for f in findings), findings

    @pytest.mark.parametrize("stack_attr", ["waf", "data", "backend", "frontend", "audit"])
    def test_convention_checks_have_no_error_annotations(self, prod_stage: AppStage, stack_attr: str) -> None:
        # TemplateConventionChecks (the project's own Aspect) still reports
        # via error-level annotations, independent of the v3 plugin packs.
        stack = getattr(prod_stage, stack_attr)
        errors = Annotations.from_stack(stack).find_error("*", Match.string_like_regexp(".*"))
        details = "\n".join(
            f"  {e.id}: {(e.entry.data if isinstance(e.entry.data, str) else str(e.entry.data)).splitlines()[0]}"
            for e in errors
        )
        assert not errors, f"error annotations on {stack.stack_name} (TemplateConventionChecks?):\n{details}"

    @pytest.mark.parametrize("stack_attr", ["waf", "data", "backend", "frontend", "audit"])
    def test_convention_aspect_attached_to_every_stack(self, prod_stage: AppStage, stack_attr: str) -> None:
        # The convention gate hangs on each stack constructor calling
        # apply_compliance_aspects; dropping the call would pass every other
        # gate vacuously. (The five rule packs are covered by
        # test_nag_gate_can_fail — they are App-level plugins now, not
        # per-stack Aspects.)
        aspects = cdk.Aspects.of(getattr(prod_stage, stack_attr)).all
        assert any(isinstance(aspect, TemplateConventionChecks) for aspect in aspects), (
            f"TemplateConventionChecks is not attached to the {stack_attr} stack — "
            "apply_compliance_aspects was likely dropped from its constructor"
        )


class TestPermissionsBoundary:
    """Every role the app creates must carry the cdk-scaffold-boundary.

    The boundary policy's DenyRoleCreationWithoutBoundary statement makes
    an unbounded role a deploy-time failure once the CFN exec role is
    bounded — this test moves that failure to synth time.
    """

    @pytest.mark.parametrize("stack_attr", ["waf", "data", "backend", "frontend", "audit"])
    def test_every_role_carries_the_boundary(self, prod_stage: AppStage, stack_attr: str) -> None:
        template = Template.from_stack(getattr(prod_stage, stack_attr))
        roles = template.find_resources("AWS::IAM::Role")
        unbounded = [
            logical_id for logical_id, role in roles.items() if "PermissionsBoundary" not in role.get("Properties", {})
        ]
        assert not unbounded, f"roles without the permissions boundary in {stack_attr}: {unbounded}"

    def test_backend_actually_has_roles(self, prod_stage: AppStage) -> None:
        # Guard against the parametrized test passing vacuously.
        assert Template.from_stack(prod_stage.backend).find_resources("AWS::IAM::Role")


class TestCodeConnectionArnValidation:
    """Pipeline mode requires a well-formed CodeConnections ARN, fail-loud at synth."""

    VALID = "arn:aws:codeconnections:us-east-1:111111111111:connection/12345678-abcd-4ef0-9876-0123456789ab"
    VALID_LEGACY = "arn:aws:codestar-connections:us-east-1:111111111111:connection/12345678-abcd-4ef0-9876-0123456789ab"

    def test_valid_arn_passes_through(self) -> None:
        assert validate_code_connection_arn(self.VALID) == self.VALID

    def test_legacy_codestar_service_name_accepted(self) -> None:
        # Connections created before the 2024 rename still carry the old service name.
        assert validate_code_connection_arn(self.VALID_LEGACY) == self.VALID_LEGACY

    def test_missing_arn_fails_with_handshake_hint(self) -> None:
        with pytest.raises(ValueError, match="CodeConnections"):
            validate_code_connection_arn(None)

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "not-an-arn",
            "arn:aws:s3:::bucket",
            "arn:aws:codeconnections:us-east-1:111111111111:host/12345678-abcd-4ef0-9876-0123456789ab",
        ],
    )
    def test_malformed_arns_fail_at_synth(self, bad: str) -> None:
        with pytest.raises(ValueError, match="code_connection_arn"):
            validate_code_connection_arn(bad)
