#!/usr/bin/env python3
"""CDK application entry point.

Synthesizes a :class:`HelloWorldStage` per target region and deployment
environment. Each Stage groups the five stacks that make up one regional
deployment (data, WAF, backend, frontend, audit) so ``cdk deploy`` treats them
as a single unit:

  HelloWorldWaf-{region}      — WAF WebACL, physically in us-east-1
                                (CloudFront constraint), but named per region
                                so each Stage is fully independent and can
                                be destroyed separately.
  HelloWorldData-{region}     — stateful layer: DynamoDB idempotency table and
                                its dedicated CMK, isolated so its lifecycle is
                                independent of the stateless compute stack
                                (RETAIN/protection togglable via retain_data).
  HelloWorld-{region}         — Lambda, API Gateway, SSM, AppConfig
  HelloWorldFrontend-{region} — S3, CloudFront (references WAF ARN cross-region
                                via SSM when target region differs from us-east-1)
  HelloWorldAudit-{region}    — stateful audit layer: the CloudTrail object-level
                                S3 data-event trail, its log bucket, and a
                                dedicated CMK. Audits the frontend buckets
                                one-way (RETAIN/protection togglable via
                                retain_data).

The target region is controlled by the ``region`` CDK context key.
Defaults to us-east-1 if not specified.

The stateful stacks' retention posture is controlled by the ``retain_data`` CDK
context key (``-c retain_data=true``). It defaults to ``false`` so the template
tears down cleanly; production forks set it true to flip the data and audit
stacks (tables, buckets, CMKs) to RETAIN with deletion/termination protection.

The deployment environment is controlled by the ``env`` CDK context key,
falling back to the ``ENVIRONMENT`` variable, defaulting to ``prod``.
``prod`` keeps the un-suffixed stack names above (so the long-lived
deployment is unaffected) and routes alarms to the SNS topic. Any other
value namespaces every stack (``HelloWorld-{env}-{region}``), which makes
ephemeral per-developer or per-branch deployments collision-free in a
shared account, and disables alarm paging for them.

Usage:
    cdk deploy --all                            # prod stage in us-east-1 (default)
    cdk deploy --all -c region=ap-southeast-1   # separate Singapore Stage
    cdk deploy --all -c env=alice-feature-x     # ephemeral developer stage
    make deploy ENV=alice-feature-x             # same, via the Makefile

Each Stage is fully independent — destroying one does not affect any other.
All five stacks for a given region+environment are destroyed together:

    cdk destroy --all -c region=ap-southeast-1
    cdk destroy --all -c env=alice-feature-x
"""

import os

import aws_cdk as cdk

from hello_world.hello_world_stage import DEFAULT_ENV_NAME, HelloWorldStage, stage_id

app = cdk.App()

# Target region for the backend and frontend stacks. Defaults to us-east-1
# when no context value is provided. WAF is always pinned to us-east-1
# inside the Stage regardless of this value.
target_region: str = app.node.try_get_context("region") or "us-east-1"

# Deployment environment. Context key wins (explicit per-invocation intent);
# the ENVIRONMENT variable serves pipelines that export it once per job;
# the default keeps `cdk deploy` pointing at the long-lived prod stacks.
env_name: str = app.node.try_get_context("env") or os.environ.get("ENVIRONMENT") or DEFAULT_ENV_NAME

# Retention switch for the stateful data stack. Context values arrive as
# strings on the CLI (`-c retain_data=true`) or as a native bool from
# cdk.json, so normalise both. Default False keeps the template destroy-friendly.
_retain_ctx = app.node.try_get_context("retain_data")
retain_data: bool = _retain_ctx if isinstance(_retain_ctx, bool) else str(_retain_ctx).lower() == "true"

# Stage id composition lives next to the Stage (hello_world_stage.stage_id):
# prod keeps the legacy id so existing cdk.out assembly paths and tooling
# keyed on the stage name stay stable; other envs get their own id.
HelloWorldStage(
    app,
    stage_id(env_name, target_region),
    region=target_region,
    env_name=env_name,
    retain_data=retain_data,
)

app.synth()
