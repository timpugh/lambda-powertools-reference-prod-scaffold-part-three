"""Assertion tests for the CI/CD pipeline stack (spec: 2026-07-10-ci-cd-pipeline-design).

Synthesizes the pipeline shape the same way app.py does with -c pipeline=true
(nag packs attached at the App root, Docker bundling skipped). The pipeline
needs an explicit account+region (CDK Pipelines deploys concrete
environments), so fixtures pin a dummy account.
"""

import pytest

aws_cdk = pytest.importorskip("aws_cdk", reason="aws_cdk not installed — skipping CDK pipeline tests")

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from infrastructure.pipeline_stack import PipelineStack

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
                                                        "FullRepositoryId": "timpugh/lambda-powertools-reference-prod-scaffold-part-one",
                                                        "BranchName": "main",
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

    def test_synth_codebuild_is_docker_privileged(self, pipeline_template: Template) -> None:
        pipeline_template.has_resource_properties(
            "AWS::CodeBuild::Project",
            Match.object_like({"Environment": Match.object_like({"PrivilegedMode": True})}),
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
        pipeline_template.has_resource_properties(
            "AWS::IAM::Policy",
            Match.object_like(
                {
                    "PolicyDocument": Match.object_like(
                        {
                            "Statement": Match.array_with(
                                [
                                    Match.object_like(
                                        {
                                            "Action": "cloudformation:DescribeStacks",
                                            "Resource": [
                                                Match.object_like({}),
                                                Match.object_like({}),
                                            ],
                                        }
                                    )
                                ]
                            )
                        }
                    )
                }
            ),
        )
