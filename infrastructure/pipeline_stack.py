"""PipelineStack — the self-mutating CD pipeline (CDK Pipelines).

Synthesized ONLY in pipeline mode (``-c pipeline=true`` — see app.py); the
default shape keeps the direct-AppStage layout for manual `make deploy` and
ephemeral ENV deploys. Sourced from GitHub ``main`` via a CodeConnections
connection (one-time console handshake; the ARN arrives via the
``code_connection_arn`` context key, validated fail-loud in app_stage).

Stage ladder (spec 2026-07-10-ci-cd-pipeline-design): a persistent ``dev``
environment (pipeline-reserved env name), live integration tests against it,
a manual approval, then prod — which reuses the legacy stack names, so the
pipeline updates the existing prod stacks in place.

Encryption posture: per-stack CMK (matches every other stack), encrypting
the artifact bucket and the CodeBuild log group. The log group is CFN-owned
and handed to every generated CodeBuild project — CodeBuild otherwise
auto-creates never-expire log groups outside CloudFormation (the
dangling-resource problem this repo's cleanup patterns exist for).
"""

from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_codepipeline as codepipeline
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import pipelines
from constructs import Construct

from infrastructure.app_stage import BOUNDARY_POLICY_NAME
from infrastructure.nag_utils import (
    apply_compliance_aspects,
    create_auto_delete_objects_log_group,
    grant_logs_service_to_key,
)

GITHUB_REPO = "timpugh/lambda-powertools-reference-prod-scaffold-part-one"
GITHUB_BRANCH = "main"

# The pipeline owns this env name end to end (deploys it, tests against it,
# never tears it down). Manual `make deploy ENV=dev` would fight the pipeline
# over the same stacks — documented as reserved in the README.
DEV_ENV_NAME = "dev"


class PipelineStack(cdk.Stack):
    """CodePipeline (dev → integration tests → approval → prod), self-mutating."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        code_connection_arn: str,
        retain_data: bool = False,
        appconfig_monitor: bool = False,
        ssm_param_path: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            scope,
            construct_id,
            permissions_boundary=cdk.PermissionsBoundary.from_name(BOUNDARY_POLICY_NAME),
            **kwargs,
        )
        apply_compliance_aspects(self)

        # Per-stack CMK, same pattern as every other stack in the app.
        self.encryption_key = kms.Key(
            self,
            "PipelineKey",
            description="CMK for the CD pipeline's artifact bucket and CodeBuild logs",
            enable_key_rotation=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        grant_logs_service_to_key(
            self.encryption_key,
            region=self.region,
            account=self.account,
            partition=self.partition,
        )

        # One CFN-owned log group for every generated CodeBuild project
        # (synth, self-mutate, asset publishing, integration tests) —
        # explicit retention per TemplateConventionChecks, CMK-encrypted,
        # and destroyed with the stack instead of dangling.
        self.build_log_group = logs.LogGroup(
            self,
            "PipelineBuildLogs",
            encryption_key=self.encryption_key,
            retention=logs.RetentionDays.THREE_MONTHS,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # Artifact bucket: transient build artifacts only — destroy-friendly,
        # CMK-encrypted, 90-day expiry so failed-run leftovers don't accrete.
        self.artifact_bucket = s3.Bucket(
            self,
            "PipelineArtifacts",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.encryption_key,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            auto_delete_objects=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            lifecycle_rules=[s3.LifecycleRule(expiration=cdk.Duration.days(90))],
        )
        create_auto_delete_objects_log_group(self, self.encryption_key)

        underlying = codepipeline.Pipeline(
            self,
            "Cd",
            pipeline_name="ServerlessAppPipeline",
            artifact_bucket=self.artifact_bucket,
            restart_execution_on_update=True,
        )

        synth = pipelines.CodeBuildStep(
            "Synth",
            input=pipelines.CodePipelineSource.connection(
                GITHUB_REPO,
                GITHUB_BRANCH,
                connection_arn=code_connection_arn,
            ),
            install_commands=[
                "npm ci",
                "pip install uv",
            ],
            commands=[
                # Same pair as `make cdk-synth` / the CI cdk-check job, plus
                # pipeline mode so the assembly contains this stack (required
                # for self-mutation). '**' descends into the Stage-nested
                # stacks so asset bundling runs against the real stacks.
                "npx cdk synth -c pipeline=true '**'",
                "uv run python scripts/check_validation_report.py cdk.out",
            ],
            primary_output_directory="cdk.out",
        )

        self.pipeline = pipelines.CodePipeline(
            self,
            "Pipeline",
            code_pipeline=underlying,
            synth=synth,
            # PythonFunction asset bundling runs Docker during `cdk synth`.
            docker_enabled_for_synth=True,
            code_build_defaults=pipelines.CodeBuildOptions(
                build_environment=codebuild.BuildEnvironment(
                    build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                ),
                logging=codebuild.LoggingOptions(
                    cloud_watch=codebuild.CloudWatchLoggingOptions(
                        log_group=self.build_log_group,
                    )
                ),
            ),
        )

        self._add_stages(
            retain_data=retain_data,
            appconfig_monitor=appconfig_monitor,
            ssm_param_path=ssm_param_path,
        )

        # Force role/project generation now so the acknowledgments below see
        # the final construct tree (build_pipeline is otherwise deferred to
        # synth, after which acknowledgments can no longer be attached).
        self.pipeline.build_pipeline()
        self._acknowledge_pipeline_findings()

    def _add_stages(
        self,
        *,
        retain_data: bool,
        appconfig_monitor: bool,
        ssm_param_path: str | None,
    ) -> None:
        # Filled in by Task 5.
        pass

    def _acknowledge_pipeline_findings(self) -> None:
        # Filled in by Task 6 from the actual gate output.
        pass
