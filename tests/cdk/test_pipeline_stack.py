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
