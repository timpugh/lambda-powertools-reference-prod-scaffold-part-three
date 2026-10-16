#!/usr/bin/env python3
"""CDK application entry point.

Synthesizes a :class:`AppStage` per target region and deployment
environment. Each Stage groups the five stacks that make up one regional
deployment (data, WAF, backend, frontend, audit) so ``cdk deploy`` treats them
as a single unit:

  ServerlessAppWaf-{region}      — WAF WebACL, physically in us-east-1
                                (CloudFront constraint), but named per region
                                so each Stage is fully independent and can
                                be destroyed separately.
  ServerlessAppData-{region}     — stateful layer: DynamoDB idempotency table and
                                its dedicated CMK, isolated so its lifecycle is
                                independent of the stateless compute stack
                                (RETAIN/protection togglable via retain_data).
  ServerlessAppBackend-{region}         — Lambda, API Gateway, SSM, AppConfig
  ServerlessAppFrontend-{region} — S3, CloudFront (references WAF ARN cross-region
                                via SSM when target region differs from us-east-1)
  ServerlessAppAudit-{region}    — stateful audit layer: the CloudTrail object-level
                                S3 data-event trail, its log bucket, and a
                                dedicated CMK. Audits the frontend buckets
                                one-way (RETAIN/protection togglable via
                                retain_data).

The target region is controlled by the ``region`` CDK context key.
Defaults to us-east-1 if not specified. Any region in the pinned
``APPCONFIG_EXTENSION_ARM64_LAYER_ARNS`` map (``infrastructure/appconfig_extension.py``
— every region AWS publishes the arm64 AppConfig Agent Lambda extension to)
synthesizes. For a region the map lacks — or to trial a newer layer before
bumping the pin — override the layer ARN with the
``appconfig_extension_layer_arn`` CDK context key
(``-c appconfig_extension_layer_arn=<arn>``), falling back to the
``APPCONFIG_EXTENSION_LAYER_ARN`` environment variable; the override must be in
the target region (layers are regional). With neither, an unmapped region fails
synth with a ``ValueError`` naming both mechanisms.

The stateful stacks' retention posture is controlled by the ``retain_data`` CDK
context key (``-c retain_data=true``). It defaults to ``false`` so the template
tears down cleanly; production forks set it true to flip the data and audit
stacks (tables, buckets, CMKs) to RETAIN with deletion/termination protection.

The AppConfig feature-flag rollout posture is controlled by the
``appconfig_monitor`` CDK context key (``-c appconfig_monitor=true``). It defaults
to ``false`` (all-at-once flag deployment, no monitor) so the cold/first deploy
always succeeds; enable it only after a first deploy to get a gradual rollout with
alarm-driven auto-rollback for ongoing flag changes (see README "Deployment safety").

The greeting SSM parameter's name is optionally overridden by the
``ssm_param_path`` CDK context key (``-c ssm_param_path=/org/app/greeting``). It
defaults to ``None``, which keeps CDK's auto-generated name (the Lambda reads
whichever name is live via the ``GREETING_PARAM_NAME`` env var either way).
**Caution:** set it before the first deploy, or accept parameter replacement —
changing ``parameter_name`` on a deployed stack replaces the parameter and
resets its value to "hello world".

The deployment environment is controlled by the ``env`` CDK context key,
falling back to the ``ENVIRONMENT`` variable, defaulting to ``prod``.
``prod`` keeps the un-suffixed stack names above (so the long-lived
deployment is unaffected) and routes alarms to the SNS topic. Any other
value namespaces every stack (``ServerlessAppBackend-{env}-{region}``), which makes
ephemeral per-developer or per-branch deployments collision-free in a
shared account, and disables alarm paging for them.

The ``pipeline`` CDK context key (``-c pipeline=true``) switches this file to
synthesize :class:`PipelineStack`, the self-mutating CD pipeline, instead of a
directly-deployable :class:`AppStage`. It defaults to ``false``, keeping
``make deploy`` and ephemeral ``env`` deploys on the legacy direct-Stage
shape. Pipeline mode requires the CodeConnections connection ARN (from the
one-time console handshake — see
``infrastructure.app_stage.validate_code_connection_arn``), resolved from the
``code_connection_arn`` context key or, failing that, the
``CODE_CONNECTION_ARN`` environment variable. The ARN is deliberately NOT
committed (it embeds the account id; the repo is public): ``make
deploy-pipeline`` auto-discovers it from the account for the workstation
birth deploy, and the pipeline's own synth step reads it from an env var
baked into its CodeBuild definition (see ``pipeline_stack.py``). Pipeline
mode also rejects any ``env`` context value outright: the pipeline owns its
own ``dev`` and ``prod`` environments end to end, so an ``-c env`` override
would silently do nothing.

Usage:
    cdk deploy --all                            # prod stage in us-east-1 (default)
    cdk deploy --all -c region=ap-southeast-1   # separate Singapore Stage (mapped)
    cdk deploy --all -c region=<unmapped> \
        -c appconfig_extension_layer_arn=<arn>  # region the pinned map lacks
    cdk deploy --all -c env=alice-feature-x     # ephemeral developer stage
    make deploy ENV=alice-feature-x             # same, via the Makefile

Each Stage is fully independent — destroying one does not affect any other.
All five stacks for a given region+environment are destroyed together
(the region example below is a mapped region):

    cdk destroy --all -c region=ap-southeast-1
    cdk destroy --all -c env=alice-feature-x
"""

import os

import aws_cdk as cdk

from infrastructure.app_stage import (
    DEFAULT_ENV_NAME,
    AppStage,
    parse_context_flag,
    resolve_appconfig_extension_layer_arn,
    stage_id,
    validate_code_connection_arn,
    validate_ssm_param_path,
)
from infrastructure.nag_utils import attach_nag_packs
from infrastructure.pipeline_stack import PipelineStack

app = cdk.App()

# cdk-nag v3: the five rule packs are policy-validation plugins evaluated over
# the synthesized assembly during app.synth() — attached ONCE here at the App
# root, not per stack (per-stack Aspects carry only the project's own
# TemplateConventionChecks; see nag_utils.attach_nag_packs). Unacknowledged
# findings fail the synth itself, so `cdk synth` (CLI and in-process alike)
# remains the hard gate.
attach_nag_packs(app)

# Target region for the backend and frontend stacks. Defaults to us-east-1
# when no context value is provided. WAF is always pinned to us-east-1
# inside the Stage regardless of this value.
target_region: str = app.node.try_get_context("region") or "us-east-1"

# Deployment environment. Context key wins (explicit per-invocation intent);
# the ENVIRONMENT variable serves pipelines that export it once per job;
# the default keeps `cdk deploy` pointing at the long-lived prod stacks.
env_name: str = app.node.try_get_context("env") or os.environ.get("ENVIRONMENT") or DEFAULT_ENV_NAME

# Retention switch for the stateful data stack. Context values arrive as
# strings on the CLI (`-c retain_data=true`) or as a native bool from cdk.json;
# parse_context_flag normalises both and FAILS SYNTH on anything else — a typo
# like `-c retain_data=yes` silently coercing to False would strip retention
# and deletion protection from a deployment the operator believes is protected.
# Default (flag absent) is False, keeping the template destroy-friendly.
retain_data: bool = parse_context_flag(app.node.try_get_context("retain_data"), "retain_data")

# Opt-in AppConfig gradual rollout + alarm rollback monitor (`-c appconfig_monitor=true`).
# Default False so the cold/first deploy always succeeds: a CFN-managed AppConfig
# deployment with a monitor rolls back when its alarm is INSUFFICIENT_DATA, which a
# fresh stack's metric always is — see backend_app._attach_appconfig_rollback_monitor
# and README "Deployment safety". Turn it on only AFTER a first all-at-once deploy has
# produced metric data, to protect ongoing flag changes.
appconfig_monitor: bool = parse_context_flag(app.node.try_get_context("appconfig_monitor"), "appconfig_monitor")

# Optional SSM path override for the greeting parameter (`-c ssm_param_path=/my/app/greeting`).
# Default None keeps CDK's auto-generated name; validated at synth (fail-loud like retain_data).
ssm_param_path: str | None = validate_ssm_param_path(app.node.try_get_context("ssm_param_path"))

# Optional AppConfig Agent Lambda extension layer override. Context key wins
# (`-c appconfig_extension_layer_arn=<arn>`), then the APPCONFIG_EXTENSION_LAYER_ARN
# env var; None falls through to the pinned per-region map in appconfig_extension.py.
# Shape-validated here (fail-loud like ssm_param_path); BackendApp additionally
# rejects an override whose region differs from the stack's (layers are regional).
appconfig_extension_layer_arn: str | None = resolve_appconfig_extension_layer_arn(
    app.node.try_get_context("appconfig_extension_layer_arn")
)

# Pipeline mode (`-c pipeline=true`): synthesize the self-mutating CD
# pipeline instead of a directly-deployable stage. The pipeline embeds
# AppStage twice (dev + prod) — see infrastructure/pipeline_stack.py and
# README "CI/CD pipeline". Default False keeps this file's legacy shape:
# `make deploy` and ephemeral ENV deploys are untouched.
pipeline_mode: bool = parse_context_flag(app.node.try_get_context("pipeline"), "pipeline")

if pipeline_mode:
    # The pipeline owns its environments (dev + prod); an -c env override
    # here would silently do nothing, so fail loud instead.
    if app.node.try_get_context("env") is not None:
        raise ValueError(
            "The 'env' context key has no effect with -c pipeline=true — the pipeline "
            "deploys its own 'dev' and 'prod' environments. Drop -c env, or drop "
            "-c pipeline=true for a direct ephemeral deploy."
        )
    # CDK Pipelines deploys concrete environments, so the pipeline stack
    # needs an explicit account. The CDK CLI resolves CDK_DEFAULT_ACCOUNT
    # from the active AWS credentials at synth.
    account = os.environ.get("CDK_DEFAULT_ACCOUNT")
    if not account:
        raise ValueError(
            "CDK_DEFAULT_ACCOUNT is not set — pipeline mode needs AWS credentials at "
            "synth so the pipeline stack gets a concrete account (run via the cdk CLI "
            "with credentials, e.g. `make deploy-pipeline`)."
        )
    PipelineStack(
        app,
        "ServerlessAppPipeline",
        # Context key wins (the workstation birth deploy — make deploy-pipeline
        # resolves the ARN from CONN=/.env/auto-discovery and passes it as -c);
        # the CODE_CONNECTION_ARN env var is how the pipeline's own synth step
        # resolves it (baked into its CodeBuild definition — pipeline_stack.py).
        # Neither home is the repo: the ARN is deliberately never committed.
        code_connection_arn=validate_code_connection_arn(
            app.node.try_get_context("code_connection_arn") or os.environ.get("CODE_CONNECTION_ARN")
        ),
        retain_data=retain_data,
        appconfig_monitor=appconfig_monitor,
        ssm_param_path=ssm_param_path,
        appconfig_extension_layer_arn=appconfig_extension_layer_arn,
        env=cdk.Environment(account=account, region=target_region),
    )
else:
    # Stage id composition lives next to the Stage (app_stage.stage_id):
    # prod keeps the legacy id so existing cdk.out assembly paths and tooling
    # keyed on the stage name stay stable; other envs get their own id.
    AppStage(
        app,
        stage_id(env_name, target_region),
        region=target_region,
        env_name=env_name,
        retain_data=retain_data,
        appconfig_monitor=appconfig_monitor,
        ssm_param_path=ssm_param_path,
        appconfig_extension_layer_arn=appconfig_extension_layer_arn,
    )

app.synth()
