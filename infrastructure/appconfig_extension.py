"""AWS AppConfig Agent Lambda extension layer: the pinned per-region map + resolver.

Split out of :mod:`infrastructure.backend_app` (which attaches the layer in
``BackendApp._attach_appconfig_extension_layer``) so the 39-entry ARN table —
data refreshed by hand from the AWS docs — lives apart from construct logic.
"""

import re

from aws_cdk import Token

# AWS-published AppConfig Agent Lambda extension layer (ARM64), per deploy
# region. Each ARN is region- and version-pinned by AWS (the publishing account
# differs per region and partition — aws / aws-cn / aws-us-gov / aws-eusc), so
# take entries verbatim from the AppConfig user guide's ARM64 table, never
# compose them. Fail-loud mapping: an unmapped region (with no override — see
# ``resolve_extension_layer_arn``) must break synth, not silently
# deploy a Lambda whose flag store points at a dead localhost port.
#
# Pinned deliberately rather than resolved from AWS's public SSM parameter
# (/aws/service/aws-appconfig/lambda-extension/arm64/latest): a pin keeps synth
# reproducible, makes every layer bump a visible ``cdk diff``, and rolls the new
# layer out through the CodeDeploy canary like any other function change. The
# SSM parameter is the source of truth for CHECKING freshness — e.g.
# ``aws ssm get-parameter --name /aws/service/aws-appconfig/lambda-extension/arm64/latest``.
#
# Extension version 2.0.22365 (launched 08/24/2026), except where noted. Refresh
# from https://docs.aws.amazon.com/appconfig/latest/userguide/appconfig-integration-lambda-extensions-versions.html
# (ARM64 table; accessed 2026-10-16).
APPCONFIG_EXTENSION_ARM64_LAYER_ARNS: dict[str, str] = {
    "us-east-1": "arn:aws:lambda:us-east-1:027255383542:layer:AWS-AppConfig-Extension-Arm64:265",
    "us-east-2": "arn:aws:lambda:us-east-2:728743619870:layer:AWS-AppConfig-Extension-Arm64:239",
    "us-west-1": "arn:aws:lambda:us-west-1:958113053741:layer:AWS-AppConfig-Extension-Arm64:285",
    "us-west-2": "arn:aws:lambda:us-west-2:359756378197:layer:AWS-AppConfig-Extension-Arm64:283",
    "ca-central-1": "arn:aws:lambda:ca-central-1:039592058896:layer:AWS-AppConfig-Extension-Arm64:188",
    "ca-west-1": "arn:aws:lambda:ca-west-1:436199621743:layer:AWS-AppConfig-Extension-Arm64:168",
    "eu-central-1": "arn:aws:lambda:eu-central-1:066940009817:layer:AWS-AppConfig-Extension-Arm64:244",
    "eu-central-2": "arn:aws:lambda:eu-central-2:758369105281:layer:AWS-AppConfig-Extension-Arm64:186",
    "eu-west-1": "arn:aws:lambda:eu-west-1:434848589818:layer:AWS-AppConfig-Extension-Arm64:247",
    "eu-west-2": "arn:aws:lambda:eu-west-2:282860088358:layer:AWS-AppConfig-Extension-Arm64:204",
    "eu-west-3": "arn:aws:lambda:eu-west-3:493207061005:layer:AWS-AppConfig-Extension-Arm64:198",
    "eu-north-1": "arn:aws:lambda:eu-north-1:646970417810:layer:AWS-AppConfig-Extension-Arm64:232",
    "eu-south-1": "arn:aws:lambda:eu-south-1:203683718741:layer:AWS-AppConfig-Extension-Arm64:187",
    "eu-south-2": "arn:aws:lambda:eu-south-2:586093569114:layer:AWS-AppConfig-Extension-Arm64:182",
    "cn-north-1": "arn:aws-cn:lambda:cn-north-1:615057806174:layer:AWS-AppConfig-Extension-Arm64:171",
    "cn-northwest-1": "arn:aws-cn:lambda:cn-northwest-1:615084187847:layer:AWS-AppConfig-Extension-Arm64:169",
    "ap-east-1": "arn:aws:lambda:ap-east-1:630222743974:layer:AWS-AppConfig-Extension-Arm64:191",
    "ap-east-2": "arn:aws:lambda:ap-east-2:730335625313:layer:AWS-AppConfig-Extension-Arm64:127",
    "ap-northeast-1": "arn:aws:lambda:ap-northeast-1:980059726660:layer:AWS-AppConfig-Extension-Arm64:231",
    "ap-northeast-2": "arn:aws:lambda:ap-northeast-2:826293736237:layer:AWS-AppConfig-Extension-Arm64:190",
    "ap-northeast-3": "arn:aws:lambda:ap-northeast-3:706869817123:layer:AWS-AppConfig-Extension-Arm64:195",
    "ap-southeast-1": "arn:aws:lambda:ap-southeast-1:421114256042:layer:AWS-AppConfig-Extension-Arm64:216",
    "ap-southeast-2": "arn:aws:lambda:ap-southeast-2:080788657173:layer:AWS-AppConfig-Extension-Arm64:262",
    "ap-southeast-3": "arn:aws:lambda:ap-southeast-3:418787028745:layer:AWS-AppConfig-Extension-Arm64:199",
    "ap-southeast-4": "arn:aws:lambda:ap-southeast-4:307021474294:layer:AWS-AppConfig-Extension-Arm64:183",
    "ap-southeast-5": "arn:aws:lambda:ap-southeast-5:631746059939:layer:AWS-AppConfig-Extension-Arm64:145",
    "ap-southeast-6": "arn:aws:lambda:ap-southeast-6:381491832265:layer:AWS-AppConfig-Extension-Arm64:93",
    "ap-southeast-7": "arn:aws:lambda:ap-southeast-7:851725616657:layer:AWS-AppConfig-Extension-Arm64:139",
    "ap-south-1": "arn:aws:lambda:ap-south-1:554480029851:layer:AWS-AppConfig-Extension-Arm64:239",
    "ap-south-2": "arn:aws:lambda:ap-south-2:489524808438:layer:AWS-AppConfig-Extension-Arm64:181",
    "sa-east-1": "arn:aws:lambda:sa-east-1:000010852771:layer:AWS-AppConfig-Extension-Arm64:226",
    "mx-central-1": "arn:aws:lambda:mx-central-1:891376990304:layer:AWS-AppConfig-Extension-Arm64:147",
    "af-south-1": "arn:aws:lambda:af-south-1:574348263942:layer:AWS-AppConfig-Extension-Arm64:194",
    "il-central-1": "arn:aws:lambda:il-central-1:895787185223:layer:AWS-AppConfig-Extension-Arm64:177",
    "us-gov-east-1": "arn:aws-us-gov:lambda:us-gov-east-1:946561847325:layer:AWS-AppConfig-Extension-Arm64:173",
    "us-gov-west-1": "arn:aws-us-gov:lambda:us-gov-west-1:946746059096:layer:AWS-AppConfig-Extension-Arm64:172",
    # Verbatim, not a typo: eusc's current layer version (48) is LOWER than its
    # 2.0.18836 one (49) in AWS's own tables — layer versions are per-region
    # publish counters, not extension versions.
    "eusc-de-east-1": "arn:aws-eusc:lambda:eusc-de-east-1:426221636601:layer:AWS-AppConfig-Extension-Arm64:48",
    # me-central-1 / me-south-1 are absent from the 2.0.20159 and 2.0.22365
    # tables (AWS gives no reason), so they stay pinned to 2.0.18836 — the last
    # version AWS documents for those regions.
    "me-central-1": "arn:aws:lambda:me-central-1:662846165436:layer:AWS-AppConfig-Extension-Arm64:168",
    "me-south-1": "arn:aws:lambda:me-south-1:559955524753:layer:AWS-AppConfig-Extension-Arm64:182",
}

# Environment-variable fallback for the layer override (context key wins; see
# app_stage.resolve_appconfig_extension_layer_arn).
APPCONFIG_EXTENSION_LAYER_ARN_ENV_VAR = "APPCONFIG_EXTENSION_LAYER_ARN"

# Lambda layer-version ARNs in every partition the AppConfig extension is
# published to (aws, aws-cn, aws-us-gov, aws-eusc). The region segment is
# captured loosely (resolve_extension_layer_arn checks it equals the stack
# region, and that the partition matches it); the trailing :<version> is
# mandatory — a version-less layer ARN is not attachable.
_LAYER_VERSION_ARN_RE = re.compile(
    r"^arn:(aws|aws-cn|aws-us-gov|aws-eusc):lambda:[a-z0-9-]+:\d{12}:layer:[A-Za-z0-9_-]+:\d+$"
)

APPCONFIG_EXTENSION_DOCS_URL = (
    "https://docs.aws.amazon.com/appconfig/latest/userguide/appconfig-integration-lambda-extensions-versions.html"
)


def partition_for_region(region: str) -> str:
    """Return the AWS partition a region name belongs to (single source of truth).

    Covers the partitions the extension is published in; anything else is the
    commercial ``aws`` partition. (ISO partitions are deliberately not mapped —
    AWS publishes no AppConfig extension there, so the validator already
    rejects their ARNs.)
    """
    for prefix, partition in (("cn-", "aws-cn"), ("us-gov-", "aws-us-gov"), ("eusc-", "aws-eusc")):
        if region.startswith(prefix):
            return partition
    return "aws"


def validate_appconfig_extension_layer_arn(raw: object) -> str | None:
    """Validate the optional `appconfig_extension_layer_arn` override's shape.

    ``None`` (neither the context key nor the env var set) means "use the
    pinned per-region map ``APPCONFIG_EXTENSION_ARM64_LAYER_ARNS``". Anything
    else must be a full, versioned Lambda layer ARN string — a non-string from
    cdk.json (``true``, a number) or a paste error fails synth here with an
    actionable message rather than as a TypeError or an opaque Lambda error at
    deploy (the same rationale as ``app_stage.validate_ssm_param_path``).
    Region/partition-vs-stack matching is left to
    :func:`resolve_extension_layer_arn`, which knows the stack's region.
    """
    if raw is None:
        return None
    if not isinstance(raw, str) or not _LAYER_VERSION_ARN_RE.match(raw):
        raise ValueError(
            f"Invalid value for CDK context key 'appconfig_extension_layer_arn' "
            f"(or ${APPCONFIG_EXTENSION_LAYER_ARN_ENV_VAR}): {raw!r}. Expected a versioned layer ARN string like "
            "arn:aws:lambda:us-east-1:027255383542:layer:AWS-AppConfig-Extension-Arm64:265 "
            "(partition aws, aws-cn, aws-us-gov, or aws-eusc)."
        )
    return raw


def resolve_extension_layer_arn(region: str, override: str | None) -> str:
    """Pick the AppConfig extension layer ARN for ``region``, failing synth loudly.

    Resolution order: ``override`` (already resolved by app.py as CDK context
    ``appconfig_extension_layer_arn`` → env var ``APPCONFIG_EXTENSION_LAYER_ARN``,
    and shape-validated by :func:`validate_appconfig_extension_layer_arn`)
    → the pinned ``APPCONFIG_EXTENSION_ARM64_LAYER_ARNS`` map → ``ValueError``.

    Lambda layers are regional — a function can only attach a layer version
    from its own region — so an override whose region segment differs from
    the stack's would otherwise fail only at deploy, after CloudFormation has
    started mutating the stack. Checked here, at synth, instead — along with
    the partition (a ``cn-*`` region needs an ``aws-cn`` ARN, and so on).
    """
    if Token.is_unresolved(region):
        # An env-agnostic stack (no region given) has a token region; the map
        # is keyed by concrete region names, so there is nothing to look up.
        raise ValueError(
            "Cannot resolve the AppConfig extension layer for an unresolved (token) stack region — "
            "deploy the backend stack with an explicit region (app.py's -c region / us-east-1 default "
            "via AppStage), not an environment-agnostic stack."
        )
    if override is not None:
        # Re-validate the shape: a caller that bypassed AppStage (direct
        # BackendStack/BackendApp use) must get the friendly error, not an
        # IndexError from the split below.
        validate_appconfig_extension_layer_arn(override)
        # arn:<partition>:lambda:<region>:<account>:layer:<name>:<version>
        override_partition, override_region = override.split(":")[1], override.split(":")[3]
        if override_region != region:
            raise ValueError(
                f"AppConfig extension layer override {override!r} is in region {override_region!r}, "
                f"but the backend stack deploys to {region!r} — Lambda layers are regional; use that "
                f"region's ARN from {APPCONFIG_EXTENSION_DOCS_URL}"
            )
        expected_partition = partition_for_region(region)
        if override_partition != expected_partition:
            # e.g. arn:aws:lambda:cn-north-1:... — well-formed and region-matched,
            # but no such layer can exist: cn-* regions live in aws-cn.
            raise ValueError(
                f"AppConfig extension layer override {override!r} uses partition {override_partition!r}, "
                f"but region {region!r} is in partition {expected_partition!r} — use that region's ARN "
                f"from {APPCONFIG_EXTENSION_DOCS_URL}"
            )
        return override
    mapped = APPCONFIG_EXTENSION_ARM64_LAYER_ARNS.get(region)
    if mapped is None:
        raise ValueError(
            f"No AppConfig extension layer ARN mapped for region {region!r}. Either pass that region's "
            "arm64 layer ARN via -c appconfig_extension_layer_arn=<arn> (or the "
            "APPCONFIG_EXTENSION_LAYER_ARN environment variable), or add it to "
            "APPCONFIG_EXTENSION_ARM64_LAYER_ARNS in infrastructure/appconfig_extension.py — take it verbatim "
            f"from {APPCONFIG_EXTENSION_DOCS_URL}"
        )
    return mapped
