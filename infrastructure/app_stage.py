"""AppStage — groups the data, WAF, backend, frontend, and audit stacks as one deploy unit.

The five stacks are always deployed together for a given region, so modelling
them as a :class:`cdk.Stage` makes that relationship structural rather than
conventional. A Stage also scopes the synthesised cloud assembly under its
own subdirectory (``cdk.out/assembly-{stage}/``), which keeps multi-region
synths from mixing their templates in the root of ``cdk.out/``.

Two stacks hold the stateful resources, kept separate from the stateless
compute so their lifecycles are independent: the **data** stack (the DynamoDB
idempotency table + its CMK — see :mod:`infrastructure.data_stack`) and
the **audit** stack (the CloudTrail data-event trail + its log bucket + a CMK —
see :mod:`infrastructure.audit_stack`). The audit stack is created last
because it *audits* the frontend buckets (a one-way dependency).

This change also paves the way for CDK Pipelines (each Stage is the natural
deployment unit) and for a future multi-environment layout (dev/staging/prod
as separate Stage instances under the same App).

Stack names are set explicitly via ``stack_name=`` so the CloudFormation
names stay unchanged (``ServerlessAppBackend-{region}`` etc.). Without the override,
wrapping in a Stage would prefix each stack name with the Stage ID, which
would orphan any currently deployed stacks.

Environment dimension
---------------------
``env_name`` adds a deployment-environment axis on top of the region axis.
The default, ``prod``, keeps the legacy stack names byte-for-byte so existing
deployments are not orphaned. Any other value is inserted into every stack
name (``ServerlessAppBackend-{env_name}-{region}``), which makes deployments
collision-free per environment — including *ephemeral* per-developer or
per-branch environments (``-c env=alice-feature-x``), where two people
iterating in one account must never fight over the same CloudFormation
stacks. Only the ``prod`` environment routes alarm notifications to the SNS
topic; ephemeral stacks keep dashboards and alarms but page nobody.
"""

import getpass
import re
from typing import Any

import aws_cdk as cdk
from constructs import Construct

from infrastructure.audit_stack import AuditStack
from infrastructure.backend_stack import BackendStack
from infrastructure.data_stack import DataStack
from infrastructure.frontend_stack import FrontendStack
from infrastructure.nag_utils import waf_logs_bucket_name
from infrastructure.waf_stack import WafStack

# The environment every deployment lands in unless overridden via
# `-c env=<name>` (or the ENVIRONMENT variable — see app.py). "prod" keeps
# the original un-suffixed stack names, so the default is always safe for
# the long-lived deployment.
DEFAULT_ENV_NAME = "prod"

# CloudFormation stack names allow only alphanumerics and hyphens, max 128
# chars; env names are embedded in stack names so they inherit the constraint.
_ENV_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")


def validate_env_name(env_name: str) -> str:
    """Validate a deployment-environment name at synth time.

    Stack names embed the env name, so an illegal character (or an
    over-long branch name pasted as-is) would otherwise surface as an
    opaque CloudFormation validation error at deploy time. 39 chars keeps
    the longest composed stack name (``ServerlessAppFrontend-{env}-{region}``)
    comfortably inside CloudFormation's 128-char limit.
    """
    if not _ENV_NAME_RE.match(env_name):
        raise ValueError(
            f"Invalid deployment environment name {env_name!r}: use 1-39 chars of [A-Za-z0-9-], "
            "starting alphanumeric (it is embedded in CloudFormation stack names). "
            "Tip: sanitize branch names, e.g. feature/foo -> feature-foo."
        )
    return env_name


def parse_context_flag(raw: object, key: str) -> bool:
    """Parse a boolean CDK context flag strictly, failing synth on junk values.

    Context values arrive as native bools from cdk.json but as strings from the
    CLI (``-c retain_data=true``). Anything other than true/false (any casing)
    is rejected at synth: silently coercing an unrecognized value
    (``-c retain_data=yes``, ``=1``, ``=on``) to False would deploy a
    production fork WITHOUT retention or deletion protection while the operator
    believes data is protected — the same fail-loud-at-synth rationale as
    :func:`validate_env_name`. ``None`` (flag not provided) is the documented
    default: False.
    """
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("true", "false"):
        return text == "true"
    raise ValueError(
        f"Invalid value for CDK context flag {key!r}: {raw!r}. "
        f"Use -c {key}=true or -c {key}=false (or a JSON boolean in cdk.json)."
    )


# SSM parameter paths: slash-anchored hierarchy, no trailing slash. SSM itself
# allows [a-zA-Z0-9_.-/]; the anchor keeps `-c ssm_param_path=greeting` (no
# leading /) from silently creating a non-hierarchical parameter.
_SSM_PARAM_PATH_RE = re.compile(r"^(/[a-zA-Z0-9_.-]+)+$")


def validate_ssm_param_path(raw: str | None) -> str | None:
    """Validate the optional `ssm_param_path` context override at synth time.

    ``None`` (context key absent) means "keep CDK's auto-generated parameter
    name" — the default that leaves existing deployments untouched. Anything
    else must be a well-formed hierarchical SSM path; failing synth loudly
    beats an opaque CloudFormation validation error at deploy (the same
    rationale as :func:`validate_env_name`).
    """
    if raw is None:
        return None
    if not _SSM_PARAM_PATH_RE.match(raw):
        raise ValueError(
            f"Invalid value for CDK context key 'ssm_param_path': {raw!r}. "
            "Use a slash-anchored SSM path like /serverless-app/greeting "
            "(chars [a-zA-Z0-9_.-] per segment, no trailing slash)."
        )
    return raw


def stage_id(env_name: str, region: str) -> str:
    """Compose the Stage construct id for an environment + region pair.

    prod keeps the legacy id (``ServerlessApp-{region}-stage``) so existing
    cdk.out assembly paths and any tooling keyed on the stage name stay
    stable; every other environment gets its own namespaced id.
    """
    if env_name == DEFAULT_ENV_NAME:
        return f"ServerlessApp-{region}-stage"
    return f"ServerlessApp-{env_name}-{region}-stage"


def _owner_tag_value() -> str:
    """Resolve the owner tag from the local user, falling back for CI runners.

    Tag values are sanitized the same way env names are — getpass can return
    usernames with dots or other characters that are awkward downstream.
    """
    try:
        user = getpass.getuser()
    except OSError:
        return "ci"
    return re.sub(r"[^A-Za-z0-9-]", "-", user) or "ci"


class AppStage(cdk.Stage):
    """All five stacks (data, WAF, backend, frontend, audit) for a single regional deployment.

    The WAF stack is always pinned to ``us-east-1`` (CloudFront-scoped WebACLs
    must live there). The data, backend, frontend, and audit stacks deploy to
    ``region``. When ``region`` differs from ``us-east-1``,
    ``cross_region_references=True`` on the frontend stack bridges the WAF ARN
    through SSM automatically.

    ``env_name`` selects the deployment environment (see module docstring):
    ``prod`` (default) keeps the legacy stack names and routes alarms to SNS;
    anything else gets env-suffixed, collision-free stack names with alarm
    paging disabled.

    ``retain_data`` is the production switch for the stateful data stack: when
    ``True`` the idempotency table and its CMK flip to ``RemovalPolicy.RETAIN``
    with DynamoDB deletion protection and stack termination protection. It
    defaults to ``False`` so the template (and ephemeral environments) tear
    down cleanly; production forks set it via ``-c retain_data=true``.

    ``appconfig_monitor`` is the opt-in production switch for AppConfig feature-
    flag rollouts: when ``True`` the flag deployment uses a gradual strategy and
    the environment carries a CloudWatch alarm monitor that auto-rolls-back a bad
    flag config. It defaults to ``False`` (all-at-once, no monitor) because a
    monitored CFN-managed deployment cannot create a cold stack — see
    :meth:`BackendApp._attach_appconfig_rollback_monitor`. Set it via
    ``-c appconfig_monitor=true`` only AFTER a first all-at-once deploy.

    ``ssm_param_path`` optionally overrides the auto-generated name of the
    greeting SSM parameter. ``None`` (the default) leaves CDK's auto-naming
    untouched — the Lambda reads whichever name is live via the
    ``GREETING_PARAM_NAME`` env var either way, so this is purely cosmetic
    unless a fork wants the parameter at a specific, predictable path. Set it
    via ``-c ssm_param_path=/org/app/greeting`` before the first deploy:
    changing it afterwards replaces the parameter (the value resets to
    ``"hello world"``).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        region: str,
        env_name: str = DEFAULT_ENV_NAME,
        retain_data: bool = False,
        appconfig_monitor: bool = False,
        ssm_param_path: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        validate_env_name(env_name)
        validate_ssm_param_path(ssm_param_path)
        is_production_env = env_name == DEFAULT_ENV_NAME

        # prod keeps the legacy names (no env segment) so the long-lived
        # deployment is never orphaned by a rename; every other env gets its
        # own namespaced set of stacks.
        env_segment = "" if is_production_env else f"-{env_name}"
        waf_stack_name = f"ServerlessAppWaf{env_segment}-{region}"
        data_stack_name = f"ServerlessAppData{env_segment}-{region}"
        backend_stack_name = f"ServerlessAppBackend{env_segment}-{region}"
        frontend_stack_name = f"ServerlessAppFrontend{env_segment}-{region}"
        audit_stack_name = f"ServerlessAppAudit{env_segment}-{region}"

        waf_env = cdk.Environment(region="us-east-1")
        target_env = cdk.Environment(region=region)

        # Stack tags: CloudFormation propagates them to every resource that
        # supports tagging at create time, giving cost allocation and console
        # filtering for free. Passed explicitly per stack (rather than via a
        # Tags.of() aspect) to match the @aws-cdk/core:explicitStackTags flag
        # enabled in cdk.json.
        stack_tags = {
            "service": "serverless-app",
            "environment": env_name,
            "owner": _owner_tag_value(),
        }

        self.waf = WafStack(
            self,
            waf_stack_name,
            stack_name=waf_stack_name,
            env=waf_env,
            tags=stack_tags,
        )

        # Stateful data layer (DynamoDB + its own CMK). Created before the
        # backend so its idempotency table can be handed to the compute stack
        # cross-stack. retain_data governs RETAIN/DESTROY + deletion/termination
        # protection (see DataStack).
        self.data = DataStack(
            self,
            data_stack_name,
            stack_name=data_stack_name,
            retain_data=retain_data,
            env=target_env,
            tags=stack_tags,
        )

        self.backend = BackendStack(
            self,
            backend_stack_name,
            stack_name=backend_stack_name,
            idempotency_table=self.data.idempotency_table,
            is_production_env=is_production_env,
            appconfig_monitor=appconfig_monitor,
            ssm_param_path=ssm_param_path,
            # By-name metric addressing, same no-cross-stack-ref technique as
            # the WAF log locations below — the CloudFront WebACL's
            # VisibilityConfig.MetricName is a deterministic string (see
            # WafStack), so it can be passed as a plain string rather than a
            # cross-stack/cross-region construct reference.
            cf_web_acl_metric_name=f"{waf_stack_name}WebACL",
            env=target_env,
            tags=stack_tags,
        )

        # WAF→S3 log locations for the frontend's Athena Glue tables. Computed
        # here (the Stage knows every stack name) from the shared
        # waf_logs_bucket_name formula + the AWS-fixed WAF log key layout
        # (AWSLogs/{account}/WAFLogs/{cloudfront|region}/{web-acl-name}/) + the
        # explicit WebACL names set in the WAF/backend stacks. Passing the
        # resolved strings avoids a cross-stack (and cross-region) reference —
        # they use only the account pseudo-param, which resolves identically in
        # every stack of this account.
        account = cdk.Aws.ACCOUNT_ID
        cf_waf_logs_location = (
            f"s3://{waf_logs_bucket_name(account=account, stack_name=waf_stack_name, suffix='cf')}"
            f"/AWSLogs/{account}/WAFLogs/cloudfront/{waf_stack_name}-cf/"
        )
        regional_waf_logs_location = (
            f"s3://{waf_logs_bucket_name(account=account, stack_name=backend_stack_name, suffix='api')}"
            f"/AWSLogs/{account}/WAFLogs/{region}/{backend_stack_name}-api/"
        )

        self.frontend = FrontendStack(
            self,
            frontend_stack_name,
            stack_name=frontend_stack_name,
            api_url=self.backend.api_url,
            api_id=self.backend.api_id,
            waf_acl_arn=self.waf.web_acl_arn,
            cf_waf_logs_location=cf_waf_logs_location,
            regional_waf_logs_location=regional_waf_logs_location,
            env=target_env,
            tags=stack_tags,
            # Enables CDK's SSM-based cross-region reference bridging.
            # When region == us-east-1 this is a no-op.
            # When region differs, CDK writes the WAF ARN into SSM in us-east-1
            # and reads it back in the target region — all managed automatically.
            cross_region_references=True,
        )

        # Audit data store: the CloudTrail object-level S3 data-event trail, its
        # log bucket, and a dedicated CMK. Created last because it *audits* the
        # frontend buckets — a one-way dependency (audit -> frontend; the frontend
        # never references the audit stack). retain_data governs RETAIN/DESTROY +
        # termination protection (see AuditStack).
        self.audit = AuditStack(
            self,
            audit_stack_name,
            stack_name=audit_stack_name,
            audited_buckets=[self.frontend.bucket, self.frontend.access_log_bucket],
            retain_data=retain_data,
            env=target_env,
            tags=stack_tags,
        )
