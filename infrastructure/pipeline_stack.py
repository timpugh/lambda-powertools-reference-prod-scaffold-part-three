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

from typing import Any, cast

import aws_cdk as cdk
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_codepipeline as codepipeline
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import pipelines
from constructs import Construct

from infrastructure.app_stage import SCAFFOLD_PERMISSIONS_BOUNDARY, AppStage
from infrastructure.nag_utils import (
    LOG_SINK_SUPPRESSION_RULES,
    acknowledge_rules,
    apply_compliance_aspects,
    create_auto_delete_objects_log_group,
    grant_logs_service_to_key,
)

GITHUB_REPO = "timpugh/lambda-powertools-reference-prod-scaffold-part-three"
GITHUB_BRANCH = "main"

# The pipeline owns this env name end to end (deploys it, tests against it,
# never tears it down). Manual `make deploy ENV=dev` would fight the pipeline
# over the same stacks — documented as reserved in the README.
DEV_ENV_NAME = "dev"


def _with_literal_partition(finding_id: str) -> list[str]:
    """Return both partition renderings of an ``applies_to`` finding id.

    The in-process nag gate (``tests/cdk/test_pipeline_stack.py`` and every
    other cdk-nag test fixture in this repo) synthesizes without cdk.json
    context, so an unresolved partition renders as the ``<AWS::Partition>``
    placeholder. The pipeline's own Synth step — and any CLI synth — runs
    with cdk.json's ``@aws-cdk/core:enablePartitionLiterals: true`` +
    ``target-partitions: ["aws"]``, which resolves the same ARN to a literal
    ``arn:aws:...`` instead: a different exact finding id. Passing an id
    through unchanged if it carries no placeholder keeps this safe to map
    over every entry in an ``applies_to`` list, not just the ARN ones.
    """
    if "<AWS::Partition>" not in finding_id:
        return [finding_id]
    return [finding_id, finding_id.replace("<AWS::Partition>", "aws")]


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
        super().__init__(scope, construct_id, permissions_boundary=SCAFFOLD_PERMISSIONS_BOUNDARY, **kwargs)
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
                # False = DetectChanges:false on the source action. Legacy
                # connection-based change detection and the explicit V2 git
                # trigger (_declare_push_trigger) are mutually exclusive
                # routing modes: with DetectChanges left true, WebhookV2
                # routing never registered and pushes started NOTHING —
                # live-proven via CloudTrail (every execution was
                # CloudFormation's restart_execution_on_update; a push-timing
                # race made one look webhook-triggered). The console's V2
                # trigger flow sets exactly this combination: DetectChanges
                # false + a Triggers block.
                trigger_on_push=False,
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
            # The connection ARN is deliberately NOT committed (it embeds the
            # account id; the repo is public). The pipeline stores it in its
            # own definition instead: this env var is how the self-mutation
            # synth above resolves it — app.py falls back to
            # CODE_CONNECTION_ARN when the context key is absent. The loop is
            # self-consistent: the workstation birth deploy injects the value
            # once (make deploy-pipeline auto-discovers it from the account),
            # and every pipeline synth re-embeds what its own environment
            # carries. Rotation = rerun make deploy-pipeline.
            env={"CODE_CONNECTION_ARN": code_connection_arn},
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
                    # ARM (Graviton) fleet, matching the app's ARM64 Lambdas:
                    # PythonFunction bundling runs `docker build --platform
                    # linux/arm64` from public.ecr.aws/sam/build-python*, and
                    # an x86 CodeBuild host cannot execute the arm64 image —
                    # `exec /bin/sh: exec format error`, live-proven on the
                    # pipeline's first Synth (2026-07-14). CodeBuild has no
                    # QEMU binfmt, so native ARM is the fix (and it worked
                    # locally all along only because dev machines are ARM
                    # Macs). Graviton is also the cheaper fleet.
                    build_image=codebuild.LinuxArmBuildImage.AMAZON_LINUX_2023_STANDARD_3_0,
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
        self._declare_push_trigger()
        self._acknowledge_pipeline_findings()

    def _declare_push_trigger(self) -> None:
        """Explicitly declare the on-push trigger the V2 pipeline needs.

        CDK synthesizes this pipeline as type V2 with NO ``Triggers`` block,
        and V2 pipelines do NOT inherit V1's connection-based change
        detection — live-proven here: pushes to ``main`` started zero
        executions; every run was CreatePipeline or the
        ``restart_execution_on_update`` restart, which masked the gap because
        early pushes coincided with workstation redeploys. The explicit git
        push trigger below is what makes push-to-deploy actually work.

        Applied as a CFN property override because ``pipelines.CodePipeline``
        creates the source action internally — the L2 ``triggers=`` prop
        needs the action object, which doesn't exist until
        ``build_pipeline()`` has run (hence this method's call-site ordering).
        The source action's name is CDK-derived from the repo slug
        (slashes -> underscores).
        """
        cfn_pipeline = self.pipeline.pipeline.node.default_child
        if not isinstance(cfn_pipeline, codepipeline.CfnPipeline):
            raise TypeError(
                f"Expected the pipeline's default child to be CfnPipeline, got "
                f"{type(cfn_pipeline).__name__} — the CDK Pipelines internals changed; "
                "re-anchor _declare_push_trigger's escape hatch."
            )
        cfn_pipeline.add_property_override(
            "Triggers",
            [
                {
                    "ProviderType": "CodeStarSourceConnection",
                    "GitConfiguration": {
                        "SourceActionName": GITHUB_REPO.replace("/", "_"),
                        "Push": [{"Branches": {"Includes": [GITHUB_BRANCH]}}],
                    },
                }
            ],
        )

    def _add_stages(
        self,
        *,
        retain_data: bool,
        appconfig_monitor: bool,
        ssm_param_path: str | None,
    ) -> None:
        env = cdk.Environment(account=self.account, region=self.region)

        # Persistent dev environment, updated in place each run. retain_data
        # is pinned False (dev data is regenerable by definition) and
        # ssm_param_path is NOT forwarded — a fixed parameter name would
        # collide with prod's in the shared account. appconfig_monitor IS
        # forwarded: once flipped in cdk.json (after both cold deploys —
        # README runbook), dev exercises the same rollout machinery prod uses.
        dev = AppStage(
            self,
            "Dev",
            region=self.region,
            env_name=DEV_ENV_NAME,
            retain_data=False,
            appconfig_monitor=appconfig_monitor,
            ssm_param_path=None,
            env=env,
        )

        integration_test = pipelines.CodeBuildStep(
            "IntegrationTest",
            install_commands=["pip install uv"],
            commands=[
                "make install-lambda",
                "make test-integration",
            ],
            env={
                # pytest-env's D:-prefixed defaults yield to these (the
                # exported-stack-name override fix — see pyproject.toml).
                "AWS_BACKEND_STACK_NAME": f"ServerlessAppBackend-{DEV_ENV_NAME}-{self.region}",
                "AWS_FRONTEND_STACK_NAME": f"ServerlessAppFrontend-{DEV_ENV_NAME}-{self.region}",
            },
            role_policy_statements=[
                iam.PolicyStatement(
                    actions=["cloudformation:DescribeStacks"],
                    resources=[
                        self.format_arn(
                            service="cloudformation",
                            resource="stack",
                            resource_name=f"ServerlessAppBackend-{DEV_ENV_NAME}-{self.region}/*",
                        ),
                        self.format_arn(
                            service="cloudformation",
                            resource="stack",
                            resource_name=f"ServerlessAppFrontend-{DEV_ENV_NAME}-{self.region}/*",
                        ),
                    ],
                )
            ],
        )
        self.pipeline.add_stage(dev, post=[integration_test])

        # Prod reuses the legacy stack names (env_name="prod" is AppStage's
        # default naming), so the pipeline updates the existing prod stacks
        # in place. The manual approval is the only gate between a green
        # integration run and prod.
        prod = AppStage(
            self,
            "Prod",
            region=self.region,
            retain_data=retain_data,
            appconfig_monitor=appconfig_monitor,
            ssm_param_path=ssm_param_path,
            env=env,
        )
        self.pipeline.add_stage(prod, pre=[pipelines.ManualApprovalStep("PromoteToProd")])

    def _acknowledge_pipeline_findings(self) -> None:
        """Acknowledge cdk-nag findings inherent to CDK Pipelines' generated shape.

        Every ``applies_to`` id below was read verbatim from a real gate run
        (``tests/cdk/test_pipeline_stack.py::TestPipelineNagCompliance`` — cdk-nag
        v3 matches IAM4/IAM5 findings by their exact granular ``Rule[Finding]``
        id, so a bare rule-id acknowledgment matches nothing; see CLAUDE.md).
        Region/account are interpolated from this stack's own ``self.region`` /
        ``self.account`` rather than hardcoded: CDK Pipelines requires a
        concrete environment (see the ``PipelineStack`` construction site), so
        these are never unresolved tokens here, and the acknowledgment stays
        correct once a real account replaces the test fixture's dummy one.

        Two forward-looking fragility caveats baked into these literals:

        1. The CDK-generated logical-id hash suffixes embedded in a few
           CodeBuild log-group/report-group ARNs (e.g.
           ``<CdBuildSynthCdkBuildProjectEB9D3AEF>``) are pasted as printed —
           they're derived from the construct tree, not from account/region,
           so they're stable across account/region but NOT across a
           reshaping of this stack's construct tree (renaming/reordering the
           pipeline's stages or CodeBuild steps regenerates the hash and
           silently un-acknowledges these findings).
        2. Every ``<AWS::Partition>`` entry is paired with its literal-``aws``
           sibling via ``_with_literal_partition`` below. The in-process test
           gate (``TestPipelineNagCompliance``, and every other cdk-nag test
           fixture in this repo) synthesizes without cdk.json context, so
           cdk-nag renders the unresolved partition pseudo-parameter as the
           ``<AWS::Partition>`` placeholder. The pipeline's own Synth step —
           and any CLI synth — runs with cdk.json's
           ``@aws-cdk/core:enablePartitionLiterals: true`` +
           ``target-partitions: ["aws"]``, which resolves the same ARNs to a
           literal ``arn:aws:...`` instead, a different exact finding id the
           placeholder-only acknowledgment would NOT match — surfacing these
           findings as unacknowledged and failing
           ``scripts/check_validation_report.py`` on the pipeline's very
           first live run. Carrying both renderings keeps the CLI gate and
           the test gate in lockstep; whichever rendering a given synth
           doesn't produce simply matches nothing and suppresses nothing, so
           it's harmless in both gates (see ``frontend_stack.py``'s RUM
           cleanup / BucketDeployment acknowledgments for the same pattern).
        """
        region = self.region
        account = self.account
        artifact_bucket_logical_id = self.get_logical_id(cast(s3.CfnBucket, self.artifact_bucket.node.default_child))

        # CDK Pipelines generates its own least-possible roles for every stage
        # of the self-mutating pipeline: the underlying CodePipeline's own
        # role, the CodeConnections source action role, the synth /
        # self-mutate / integration-test CodeBuild projects, and the
        # cdk-assets file-publishing role. Every wildcard below is that
        # construct's documented shape, not hand-written policy: S3
        # object/bucket actions plus the artifact-bucket ARN (every pipeline
        # action reads/writes the shared artifact bucket), CodeBuild's own
        # report-group/log-group ARNs (provisioned per project), the two
        # dev-stack CloudFormation ARNs the integration-test step's
        # DescribeStacks grant is scoped to, and the self-mutation step's
        # broad iam:PassRole / "any resource" wildcards (it must be able to
        # redeploy a future, not-yet-known pipeline definition — the
        # self-mutating capability CDK Pipelines documents as inherently
        # broad).
        iam5_reason = (
            "CDK Pipelines-generated roles: artifact-bucket object access, CodeBuild report "
            "groups and log groups, the dev-stack DescribeStacks scope, and the self-mutation "
            "step's redeploy-anything wildcards are the construct's documented shape, not "
            "hand-written policy."
        )
        inline_policy_reason = (
            "CDK Pipelines attaches a tightly-scoped inline DefaultPolicy to every generated "
            "role — its only policy-attachment mechanism; there is no managed-policy "
            "alternative to swap in."
        )
        source_repo_url_reason = (
            "Every CodeBuild project here (synth, integration test, self-mutation, and the "
            "FileAsset publishers) sources from CODEPIPELINE, not a directly configured "
            "GitHub/BitBucket URL — the real GitHub auth is the CodeConnections connection "
            "wired into the pipeline's Source stage, which IS the OAuth-based mechanism this "
            "rule requires; it just isn't visible at the CodeBuild-project level the rule checks."
        )
        acknowledge_rules(
            self,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": iam5_reason,
                    "applies_to": [
                        entry
                        for raw in [
                            "Action::s3:GetObject*",
                            "Action::s3:GetBucket*",
                            "Action::s3:List*",
                            "Action::s3:DeleteObject*",
                            "Action::s3:Abort*",
                            f"Resource::<{artifact_bucket_logical_id}.Arn>/*",
                            "Action::kms:ReEncrypt*",
                            "Action::kms:GenerateDataKey*",
                            f"Resource::arn:<AWS::Partition>:logs:{region}:{account}:"
                            "log-group:/aws/codebuild/<CdBuildSynthCdkBuildProjectEB9D3AEF>:*",
                            f"Resource::arn:<AWS::Partition>:codebuild:{region}:{account}:"
                            "report-group/<CdBuildSynthCdkBuildProjectEB9D3AEF>-*",
                            f"Resource::arn:<AWS::Partition>:logs:{region}:{account}:"
                            "log-group:/aws/codebuild/<CdDevIntegrationTestE94219AC>:*",
                            f"Resource::arn:<AWS::Partition>:codebuild:{region}:{account}:"
                            "report-group/<CdDevIntegrationTestE94219AC>-*",
                            f"Resource::arn:<AWS::Partition>:cloudformation:{region}:{account}:"
                            f"stack/ServerlessAppBackend-{DEV_ENV_NAME}-{region}/*",
                            f"Resource::arn:<AWS::Partition>:cloudformation:{region}:{account}:"
                            f"stack/ServerlessAppFrontend-{DEV_ENV_NAME}-{region}/*",
                            f"Resource::arn:<AWS::Partition>:logs:{region}:{account}:"
                            "log-group:/aws/codebuild/<PipelineUpdatePipelineSelfMutationDAA41400>:*",
                            f"Resource::arn:<AWS::Partition>:codebuild:{region}:{account}:"
                            "report-group/<PipelineUpdatePipelineSelfMutationDAA41400>-*",
                            f"Resource::arn:*:iam::{account}:role/*",
                            "Resource::*",
                            f"Resource::arn:<AWS::Partition>:logs:{region}:{account}:log-group:/aws/codebuild/*",
                            f"Resource::arn:<AWS::Partition>:codebuild:{region}:{account}:report-group/*",
                        ]
                        for entry in _with_literal_partition(raw)
                    ],
                },
                {"id": "NIST.800.53.R5-IAMNoInlinePolicy", "reason": inline_policy_reason},
                {"id": "HIPAA.Security-IAMNoInlinePolicy", "reason": inline_policy_reason},
                {"id": "PCI.DSS.321-IAMNoInlinePolicy", "reason": inline_policy_reason},
                {"id": "HIPAA.Security-CodeBuildProjectSourceRepoUrl", "reason": source_repo_url_reason},
                {"id": "PCI.DSS.321-CodeBuildProjectSourceRepoUrl", "reason": source_repo_url_reason},
            ],
        )

        # The artifact bucket holds transient CD build output (CodeBuild
        # synth/build artifacts, cdk.out) with a 90-day expiry — access
        # logging, versioning, and replication add no compliance value for
        # redeployable build output. Same rationale as the SSE-S3 log-sink
        # buckets elsewhere in this project, so the rule-name list is shared
        # (nag_utils.LOG_SINK_SUPPRESSION_RULES) rather than re-typed; the
        # S3DefaultEncryptionKMS entries are filtered out because this bucket
        # is KMS- not SSE-S3-encrypted (create_sse_s3_log_bucket doesn't
        # apply here) and never triggers them.
        artifact_bucket_reason = (
            "Transient CD pipeline build artifacts (CodeBuild synth/build output) with a "
            "90-day expiry — access logging, versioning, and replication add no compliance "
            "value for redeployable build output."
        )
        acknowledge_rules(
            self.artifact_bucket,
            [
                {"id": rule, "reason": artifact_bucket_reason}
                for rule in LOG_SINK_SUPPRESSION_RULES
                if "DefaultEncryptionKMS" not in rule
            ],
        )
