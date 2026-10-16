"""Assertion tests for the CI/CD pipeline stack (spec: 2026-07-10-ci-cd-pipeline-design).

Synthesizes the pipeline shape the same way app.py does with -c pipeline=true
(nag packs attached at the App root, Docker bundling skipped). The pipeline
needs an explicit account+region (CDK Pipelines deploys concrete
environments), so fixtures pin a dummy account.
"""

from typing import cast

import pytest

aws_cdk = pytest.importorskip("aws_cdk", reason="aws_cdk not installed — skipping CDK pipeline tests")

import aws_cdk as cdk
from aws_cdk.assertions import Annotations, Match, Template

from infrastructure.app_stage import AppStage
from infrastructure.nag_utils import attach_nag_packs
from infrastructure.pipeline_stack import PipelineStack
from tests.cdk.test_stage import _unacknowledged_findings

# Same key tests/cdk/test_stage.py uses to skip Docker bundling.
_NO_BUNDLING = {"aws:cdk:bundling-stacks": []}

ACCOUNT = "111111111111"
REGION = "us-east-1"
CONNECTION_ARN = f"arn:aws:codeconnections:{REGION}:{ACCOUNT}:connection/12345678-abcd-4ef0-9876-0123456789ab"


def _pipeline_stack() -> PipelineStack:
    app = cdk.App(context=_NO_BUNDLING)
    return PipelineStack(
        app,
        "ServerlessAppPipeline",
        code_connection_arn=CONNECTION_ARN,
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )


@pytest.fixture(scope="module")
def pipeline_template() -> Template:
    return Template.from_stack(_pipeline_stack())


class TestPipelineCore:
    def test_source_is_the_codeconnections_repo(self, pipeline_template: Template) -> None:
        pipeline_template.has_resource_properties(
            "AWS::CodePipeline::Pipeline",
            Match.object_like(
                {
                    "Stages": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Name": "Source",
                                    "Actions": [
                                        Match.object_like(
                                            {
                                                "Configuration": Match.object_like(
                                                    {
                                                        "ConnectionArn": CONNECTION_ARN,
                                                        "FullRepositoryId": "timpugh/lambda-powertools-reference-prod-scaffold-part-three",
                                                        "BranchName": "main",
                                                        # Must stay false while the explicit V2 git
                                                        # trigger exists — legacy change detection
                                                        # left true suppressed WebhookV2 routing
                                                        # entirely (pushes started nothing).
                                                        "DetectChanges": False,
                                                    }
                                                )
                                            }
                                        )
                                    ],
                                }
                            )
                        ]
                    )
                }
            ),
        )

    def test_push_trigger_is_explicitly_declared(self, pipeline_template: Template) -> None:
        # CDK synthesizes a V2 pipeline with no Triggers block, and V2 does
        # NOT inherit V1's connection-based change detection — live-proven:
        # pushes started zero executions until this block existed. If this
        # assertion fails, push-to-deploy is broken even though every stage
        # still works when started by hand.
        pipeline_template.has_resource_properties(
            "AWS::CodePipeline::Pipeline",
            Match.object_like(
                {
                    "Triggers": [
                        Match.object_like(
                            {
                                "ProviderType": "CodeStarSourceConnection",
                                "GitConfiguration": Match.object_like(
                                    {
                                        "SourceActionName": "timpugh_lambda-powertools-reference-prod-scaffold-part-three",
                                        "Push": [{"Branches": {"Includes": ["main"]}}],
                                    }
                                ),
                            }
                        )
                    ]
                }
            ),
        )

    def test_synth_codebuild_is_docker_privileged(self, pipeline_template: Template) -> None:
        pipeline_template.has_resource_properties(
            "AWS::CodeBuild::Project",
            Match.object_like({"Environment": Match.object_like({"PrivilegedMode": True})}),
        )

    def test_every_codebuild_project_runs_on_arm(self, pipeline_template: Template) -> None:
        # The app's Lambdas are ARM64, so PythonFunction bundling docker-builds
        # arm64 images — which an x86 CodeBuild host cannot execute (exec
        # format error, live-proven on the pipeline's first Synth). Every
        # generated project must stay on the Graviton fleet.
        projects = pipeline_template.find_resources("AWS::CodeBuild::Project")
        assert projects, "pipeline should generate CodeBuild projects"
        non_arm = [
            logical_id
            for logical_id, project in projects.items()
            if project["Properties"]["Environment"]["Type"] != "ARM_CONTAINER"
        ]
        assert not non_arm, f"CodeBuild projects not on the ARM fleet: {non_arm}"

    def test_synth_carries_the_connection_arn_env_var(self, pipeline_template: Template) -> None:
        # The ARN is deliberately never committed (public repo, embeds the
        # account id) — the pipeline's own definition is its home. app.py
        # falls back to CODE_CONNECTION_ARN when the context key is absent,
        # so this env var is what makes every self-mutation synth
        # self-sufficient. If it disappears, the pipeline's first run after
        # the change fails at Synth with the validator's fail-loud error.
        pipeline_template.has_resource_properties(
            "AWS::CodeBuild::Project",
            Match.object_like(
                {
                    "Environment": Match.object_like(
                        {
                            "EnvironmentVariables": Match.array_with(
                                [
                                    Match.object_like(
                                        {
                                            "Name": "CODE_CONNECTION_ARN",
                                            "Value": CONNECTION_ARN,
                                        }
                                    )
                                ]
                            )
                        }
                    )
                }
            ),
        )

    def test_codebuild_log_group_is_explicit_with_retention(self, pipeline_template: Template) -> None:
        pipeline_template.has_resource_properties("AWS::Logs::LogGroup", Match.object_like({"RetentionInDays": 90}))

    def test_artifact_bucket_is_cmk_encrypted(self, pipeline_template: Template) -> None:
        pipeline_template.has_resource_properties(
            "AWS::S3::Bucket",
            Match.object_like(
                {
                    "BucketEncryption": {
                        "ServerSideEncryptionConfiguration": [
                            Match.object_like(
                                {"ServerSideEncryptionByDefault": Match.object_like({"SSEAlgorithm": "aws:kms"})}
                            )
                        ]
                    }
                }
            ),
        )

    def test_every_pipeline_role_carries_the_boundary(self, pipeline_template: Template) -> None:
        roles = pipeline_template.find_resources("AWS::IAM::Role")
        assert roles, "pipeline stack should create roles"
        unbounded = [
            logical_id for logical_id, role in roles.items() if "PermissionsBoundary" not in role.get("Properties", {})
        ]
        assert not unbounded, f"roles without the permissions boundary: {unbounded}"


def _pipeline_stages(template: Template) -> list[dict]:
    pipelines_found = template.find_resources("AWS::CodePipeline::Pipeline")
    assert len(pipelines_found) == 1
    return next(iter(pipelines_found.values()))["Properties"]["Stages"]


def _flatten_fn_join(resource: dict) -> str:
    """Flatten an ``Fn::Join`` ARN resource to its literal string content.

    Ref-dicts (e.g. ``{"Ref": "AWS::Partition"}``) are skipped, so the result
    is just the concatenated string parts — enough to substring-match the
    stack name baked into the ARN.
    """
    _, parts = resource["Fn::Join"]
    return "".join(part for part in parts if isinstance(part, str))


class TestStageLadder:
    def test_dev_deploys_before_prod(self, pipeline_template: Template) -> None:
        names = [s["Name"] for s in _pipeline_stages(pipeline_template)]
        assert "Dev" in names
        assert "Prod" in names
        assert names.index("Dev") < names.index("Prod")

    def test_prod_gates_on_manual_approval(self, pipeline_template: Template) -> None:
        prod = next(s for s in _pipeline_stages(pipeline_template) if s["Name"] == "Prod")
        approvals = [a for a in prod["Actions"] if a["ActionTypeId"]["Category"] == "Approval"]
        assert len(approvals) == 1
        assert approvals[0]["Name"] == "PromoteToProd"
        # RunOrder 1 = the approval blocks every deploy action in the stage.
        assert approvals[0]["RunOrder"] == 1

    def test_dev_stage_runs_the_integration_gate(self, pipeline_template: Template) -> None:
        dev = next(s for s in _pipeline_stages(pipeline_template) if s["Name"] == "Dev")
        action_names = [a["Name"] for a in dev["Actions"]]
        assert "IntegrationTest" in action_names

    def test_integration_gate_can_read_only_the_dev_stacks(self, pipeline_template: Template) -> None:
        # The test step's role may DescribeStacks on the two dev stacks and
        # nothing broader — the prod stacks are deliberately out of reach.
        #
        # Match.object_like({}) matches ANY dict, so a shape-only assertion
        # here would pass even if both ARNs pointed at prod. Instead, pin the
        # literal ARN content: find the DescribeStacks statement whose
        # Resource is a list (the CDK-Pipelines self-mutation role also
        # grants this action, but on the scalar "Resource": "*" — excluded
        # below by the list check, and asserted to be the only other one),
        # then flatten each Fn::Join resource and match the exact stack name.
        policies = pipeline_template.find_resources("AWS::IAM::Policy")
        list_resource_statements = [
            statement
            for policy in policies.values()
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]
            for action in [statement.get("Action")]
            if "cloudformation:DescribeStacks" in (action if isinstance(action, list) else [action])
            if isinstance(statement.get("Resource"), list)
        ]
        assert len(list_resource_statements) == 1, (
            "expected exactly one list-scoped cloudformation:DescribeStacks "
            f"statement (the dev-stacks grant); found {len(list_resource_statements)}"
        )

        resources = list_resource_statements[0]["Resource"]
        assert len(resources) == 2
        flattened = [_flatten_fn_join(resource) for resource in resources]
        assert any(arn.endswith("stack/ServerlessAppBackend-dev-us-east-1/*") for arn in flattened)
        assert any(arn.endswith("stack/ServerlessAppFrontend-dev-us-east-1/*") for arn in flattened)


def _nag_pipeline_stack() -> PipelineStack:
    app = cdk.App(context=_NO_BUNDLING)
    attach_nag_packs(app)
    return PipelineStack(
        app,
        "ServerlessAppPipeline",
        code_connection_arn=CONNECTION_ARN,
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )


class TestPipelineNagCompliance:
    def test_pipeline_shape_has_no_unacknowledged_findings(self) -> None:
        stack = _nag_pipeline_stack()
        findings = _unacknowledged_findings(stack.node.root)
        details = "\n".join(f"  {f}" for f in findings)
        assert not findings, (
            f"unacknowledged cdk-nag findings in the pipeline shape — fix the resource or "
            f"add a scoped acknowledgment with a reason (see CLAUDE.md):\n{details}"
        )

    def test_convention_checks_have_no_error_annotations(self) -> None:
        stack = _nag_pipeline_stack()
        Annotations.from_stack(stack).has_no_error("*", Match.any_value())


# Test-fixture override: well-formed, in the pipeline's region, fake account —
# never mistakable for a pinned production layer.
_FAKE_LAYER_OVERRIDE = f"arn:aws:lambda:{REGION}:123456789012:layer:AppConfig-Extension-Override:9"
_LAYER_ENV_VAR = "APPCONFIG_EXTENSION_LAYER_ARN"


def _projects_by_role(template: Template) -> dict[str, list[dict]]:
    """Split CodeBuild projects into Synth / IntegrationTest / other by buildspec content."""
    roles: dict[str, list[dict]] = {"synth": [], "integration": [], "other": []}
    for project in template.find_resources("AWS::CodeBuild::Project").values():
        spec = str(project["Properties"]["Source"].get("BuildSpec", ""))
        if "npx cdk synth" in spec:
            roles["synth"].append(project)
        elif "make test-integration" in spec:
            roles["integration"].append(project)
        else:
            roles["other"].append(project)
    return roles


def _env_var_names(project: dict) -> set[str]:
    return {v["Name"] for v in project["Properties"]["Environment"].get("EnvironmentVariables", [])}


def _backend_layers(pipeline: PipelineStack, stage_id: str) -> list[object]:
    stage = cast(AppStage, pipeline.node.find_child(stage_id))
    functions = Template.from_stack(stage.backend).find_resources("AWS::Lambda::Function")
    return [layer for fn in functions.values() for layer in fn["Properties"].get("Layers", [])]


class TestPipelineLayerOverride:
    """The AppConfig extension layer override in pipeline mode: forwarded + baked into Synth only."""

    @staticmethod
    def _override_stack() -> PipelineStack:
        return PipelineStack(
            cdk.App(context=_NO_BUNDLING),
            "ServerlessAppPipeline",
            code_connection_arn=CONNECTION_ARN,
            appconfig_extension_layer_arn=_FAKE_LAYER_OVERRIDE,
            env=cdk.Environment(account=ACCOUNT, region=REGION),
        )

    def test_override_is_baked_into_synth_only(self) -> None:
        roles = _projects_by_role(Template.from_stack(self._override_stack()))
        assert len(roles["synth"]) == 1
        synth_vars = {
            v["Name"]: v["Value"] for v in roles["synth"][0]["Properties"]["Environment"]["EnvironmentVariables"]
        }
        assert synth_vars[_LAYER_ENV_VAR] == _FAKE_LAYER_OVERRIDE
        assert roles["integration"], "expected the IntegrationTest CodeBuild project"
        for project in roles["integration"] + roles["other"]:
            assert _LAYER_ENV_VAR not in _env_var_names(project)

    def test_override_reaches_dev_and_prod_backends(self) -> None:
        stack = self._override_stack()
        for stage_id in ("Dev", "Prod"):
            assert _FAKE_LAYER_OVERRIDE in _backend_layers(stack, stage_id), stage_id

    def test_override_emits_sticky_warning_on_pipeline_stack(self) -> None:
        Annotations.from_stack(self._override_stack()).has_warning(
            "*", Match.string_like_regexp(f".*{_FAKE_LAYER_OVERRIDE}.*make deploy-pipeline.*")
        )

    def test_no_override_means_no_env_var_and_no_warning(self, pipeline_template: Template) -> None:
        for projects in _projects_by_role(pipeline_template).values():
            for project in projects:
                assert _LAYER_ENV_VAR not in _env_var_names(project)
        stack = _pipeline_stack()
        Annotations.from_stack(stack).has_no_warning(
            "*", Match.string_like_regexp(".*AppConfig extension layer override.*")
        )
        for stage_id in ("Dev", "Prod"):
            assert _FAKE_LAYER_OVERRIDE not in _backend_layers(stack, stage_id)
