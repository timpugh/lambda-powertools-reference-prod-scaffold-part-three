"""Shared cdk-nag helpers.

``apply_compliance_aspects`` applies the full available rule-pack set to a
stack so every stack exercises the same compliance gauntlet. NIST 800-53 R4
is intentionally omitted — R5 supersedes it and running both would duplicate
findings on overlapping controls.

``CDK_LAMBDA_SUPPRESSIONS`` is the canonical suppression list for CDK-managed
singleton Lambdas (AwsCustomResource provider, BucketDeployment, S3AutoDeleteObjects).
Their runtime, memory, tracing, DLQ, VPC, and IAM policies are all managed by
CDK and cannot be configured by the caller. Import it and pass it to
``NagSuppressions.add_resource_suppressions`` with ``apply_to_children=True``,
or use the ``suppress_cdk_singletons`` helper. Absolute-path suppression
(``add_resource_suppressions_by_path``) is intentionally avoided throughout this
project: the singletons are resolved via ``node.try_find_child`` so suppressions
keep working when the stacks are nested under a ``cdk.Stage`` (a path string
would break on the added Stage prefix).
"""

from collections.abc import Iterable
from typing import cast

from aws_cdk import Aspects, CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_lambda_destinations as destinations
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_wafv2 as wafv2
from cdk_nag import (
    AwsSolutionsChecks,
    HIPAASecurityChecks,
    NagSuppressions,
    NIST80053R5Checks,
    PCIDSS321Checks,
    ServerlessChecks,
)
from constructs import Construct, IConstruct

# CDK-managed singleton Lambda construct ID for the AwsCustomResource provider.
# Derived from CDK's own source hash; stable for years and unaffected by rescoping
# stacks under a cdk.Stage. Shared here so the backend and frontend stacks — which
# both suppress it AND attach an async DLQ to it via try_find_child — provably
# reference the same construct ID rather than re-typing the literal at each site.
AWS_CUSTOM_RESOURCE_PROVIDER_ID = "AWS679f53fac002430cb0da5b7982bd2287"


def apply_compliance_aspects(stack: Stack) -> None:
    """Attach every cdk-nag rule pack this project runs to ``stack``."""
    Aspects.of(stack).add(AwsSolutionsChecks(verbose=True))
    Aspects.of(stack).add(ServerlessChecks(verbose=True))
    Aspects.of(stack).add(NIST80053R5Checks(verbose=True))
    Aspects.of(stack).add(HIPAASecurityChecks(verbose=True))
    Aspects.of(stack).add(PCIDSS321Checks(verbose=True))


def grant_logs_service_to_key(key: kms.Key, *, region: str, account: str, partition: str) -> None:
    """Add the standard CloudWatch Logs service-principal grant to a CMK.

    Three CMKs in this project (backend, frontend, WAF) need the same statement:
    a grant to ``logs.{region}.amazonaws.com`` for symmetric encrypt/decrypt
    operations, conditioned via ``kms:EncryptionContext:aws:logs:arn`` so only
    log groups in this account+region can request key operations. Defining it
    in one place keeps the three call sites in lockstep — pylint's R0801
    duplicate-code check correctly flags any drift between them, and the
    confused-deputy condition is exactly the kind of thing that's harmful to
    forget on one of the three CMKs.
    """
    key.add_to_resource_policy(
        iam.PolicyStatement(
            actions=["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"],
            principals=[iam.ServicePrincipal(f"logs.{region}.amazonaws.com")],
            resources=["*"],
            conditions={
                "ArnLike": {
                    "kms:EncryptionContext:aws:logs:arn": f"arn:{partition}:logs:{region}:{account}:log-group:*",
                },
            },
        )
    )


def grant_guardduty_service_to_key(key: kms.Key, *, region: str, account: str, partition: str) -> None:
    """Grant GuardDuty ``kms:Decrypt`` on a CMK so Lambda Protection can introspect.

    GuardDuty Lambda Protection (and similar foundational-detection features)
    needs to read Lambda function configuration — including env vars encrypted
    with a customer-managed key. Without this grant the assumed
    ``AWSServiceRoleForAmazonGuardDuty`` role is denied ``kms:Decrypt`` against
    the CMK, leaving GuardDuty's coverage of CMK-encrypted resources incomplete
    (the original CloudTrail finding that motivated this grant).

    Scoped to GuardDuty detectors in this account+region only via
    ``aws:SourceAccount`` and ``aws:SourceArn`` — the cross-account
    confused-deputy guard AWS documents for service-principal grants.
    """
    key.add_to_resource_policy(
        iam.PolicyStatement(
            sid="AllowGuardDutyDecrypt",
            actions=["kms:Decrypt"],
            principals=[iam.ServicePrincipal("guardduty.amazonaws.com")],
            resources=["*"],
            conditions={
                "StringEquals": {"aws:SourceAccount": account},
                "ArnLike": {"aws:SourceArn": f"arn:{partition}:guardduty:{region}:{account}:detector/*"},
            },
        )
    )


def build_managed_threat_rules(metric_prefix: str) -> list[wafv2.CfnWebACL.RuleProperty]:
    """Build the four AWS managed rule groups shared by every WebACL in this project.

    Two WebACLs use these: the CLOUDFRONT-scoped ACL in ``HelloWorldWafStack``
    (browser traffic at the edge) and the REGIONAL-scoped ACL on API Gateway in
    ``HelloWorldApp`` (closes the ``execute-api`` CloudFront-bypass window). Both
    need the identical IP-reputation / common / known-bad-inputs / anonymous-IP
    protections, so the list is defined once here — pylint's R0801 duplicate-code
    check would otherwise (correctly) flag two ~60-line copies drifting apart, and
    a managed rule group that's added to one ACL but forgotten on the other is
    exactly the kind of asymmetry this consolidation prevents.

    The rate-based rule is intentionally NOT part of this shared set. It belongs
    only on the CLOUDFRONT ACL, where it aggregates by ``FORWARDED_IP`` because
    CloudFront is the only client the edge sees. On the regional ACL guarding the
    origin, every request also arrives from a CloudFront edge IP, so an
    IP-aggregated limit would penalise legitimate funnelled traffic; origin-side
    volume is bounded instead by API Gateway stage throttling and the function's
    reserved concurrency.

    Args:
        metric_prefix: Prefix for each rule's CloudWatch metric name (the caller
            passes its stack name so metrics stay unique across deployments).

    Returns:
        The four managed-rule-group ``RuleProperty`` objects at priorities 0-3.
        Callers that add a rate rule place it at priority 4.
    """

    def _managed_group(name: str, priority: int, metric_suffix: str) -> wafv2.CfnWebACL.RuleProperty:
        return wafv2.CfnWebACL.RuleProperty(
            name=name,
            priority=priority,
            statement=wafv2.CfnWebACL.StatementProperty(
                managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                    vendor_name="AWS",
                    name=name,
                )
            ),
            override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name=f"{metric_prefix}-{metric_suffix}",
                sampled_requests_enabled=True,
            ),
        )

    return [
        # Blocks IPs with a poor reputation (scanners, botnets, TOR exits)
        _managed_group("AWSManagedRulesAmazonIpReputationList", 0, "IpReputationList"),
        # Core rule set — protects against OWASP Top 10 web exploits
        _managed_group("AWSManagedRulesCommonRuleSet", 1, "CommonRuleSet"),
        # Blocks requests containing known malicious inputs (SQLi, XSS patterns)
        _managed_group("AWSManagedRulesKnownBadInputsRuleSet", 2, "KnownBadInputs"),
        # Blocks requests from anonymizing services (VPN, Tor exits, hosting providers)
        _managed_group("AWSManagedRulesAnonymousIpList", 3, "AnonymousIpList"),
    ]


def attach_async_failure_destination(
    scope: IConstruct,
    singleton_id: str,
    *,
    encryption_key: kms.Key,
    queue_id: str,
) -> sqs.Queue | None:
    """Wire an SQS DLQ to a CDK-managed async singleton Lambda.

    AwsCustomResource provider Lambdas are invoked asynchronously by
    CloudFormation during stack lifecycle events. Without an on_failure
    destination, a provider crash that exhausts Lambda's two automatic
    async retries is silently dropped — the stack rollback still surfaces
    a CFN error, but the *cause* (Python traceback, AWS API error response)
    is gone unless someone catches it in CloudWatch within the retention
    window. SQS as the on_failure destination preserves the failed-event
    envelope (full request payload + responseContext) for post-mortem.

    The queue uses the same CMK as the surrounding stack, with 14-day
    retention (Lambda's max meaningful window — events older than that
    have already aged past most rollback investigations).

    Returns the created queue so callers can attach alarms or outputs;
    returns None if the singleton isn't present under ``scope`` (which
    happens when no AwsCustomResource has been instantiated in this stack).
    """
    singleton = scope.node.try_find_child(singleton_id)
    # IFunction is a JSII protocol that isn't runtime-checkable, so we check
    # the concrete Function class. SingletonFunction is a subclass, so the
    # isinstance check covers both.
    if not isinstance(singleton, _lambda.Function):
        return None

    dlq = sqs.Queue(
        cast(Construct, scope),
        queue_id,
        encryption=sqs.QueueEncryption.KMS,
        encryption_master_key=encryption_key,
        retention_period=Duration.days(14),
        enforce_ssl=True,
        removal_policy=RemovalPolicy.DESTROY,
    )

    # This queue IS the dead-letter destination. cdk-nag flags any SQS queue
    # without a DLQ or a redrive policy, but recursing DLQs into more DLQs
    # makes no sense — when this terminal queue's consumer fails, manual
    # inspection of the queue content is the recovery path, not another DLQ.
    dlq_terminal_reason = (
        "Terminal DLQ: this queue IS the dead-letter destination — recursing into another DLQ has no recovery value"
    )
    NagSuppressions.add_resource_suppressions(
        dlq,
        [
            {"id": "AwsSolutions-SQS3", "reason": dlq_terminal_reason},
            {"id": "Serverless-SQSRedrivePolicy", "reason": dlq_terminal_reason},
        ],
    )

    singleton.configure_async_invoke(on_failure=destinations.SqsDestination(dlq))

    # configure_async_invoke + SqsDestination + KMS-encrypted queue adds
    # kms:GenerateDataKey* and kms:ReEncrypt* wildcards to the singleton's
    # auto-generated default policy so it can encrypt messages to the DLQ.
    # These are granular IAM5 findings that need applies_to scoping rather
    # than the blanket suppression in CDK_LAMBDA_SUPPRESSIONS. Also
    # re-applies the inline-policy suppressions because the DefaultPolicy
    # resource only materialized when configure_async_invoke modified the
    # role above, after the initial suppress_cdk_singletons run.
    kms_wildcard_reason = (
        "KMS wildcards required by configure_async_invoke to encrypt messages to the CMK-encrypted DLQ"
    )
    NagSuppressions.add_resource_suppressions(
        cast(Construct, singleton),
        [
            {
                "id": "AwsSolutions-IAM5",
                "applies_to": ["Action::kms:GenerateDataKey*", "Action::kms:ReEncrypt*"],
                "reason": kms_wildcard_reason,
            },
            {
                "id": "NIST.800.53.R5-IAMNoInlinePolicy",
                "reason": "CDK-generated inline policy on singleton service role",
            },
            {
                "id": "HIPAA.Security-IAMNoInlinePolicy",
                "reason": "CDK-generated inline policy on singleton service role",
            },
            {"id": "PCI.DSS.321-IAMNoInlinePolicy", "reason": "CDK-generated inline policy on singleton service role"},
        ],
        apply_to_children=True,
    )

    # Surface the DLQ URL so operators can find captured provider failures. Emitted
    # here (rather than in each calling stack) so the two call sites don't duplicate
    # the same CfnOutput block — keeping the queue genuinely consumed, not just bound.
    CfnOutput(
        cast(Construct, scope),
        f"{queue_id}Url",
        description="SQS DLQ capturing failed async invocations of the AwsCustomResource provider Lambda",
        value=dlq.queue_url,
    )

    return dlq


def suppress_cdk_singletons(scope: IConstruct, singleton_ids: Iterable[str]) -> None:
    """Apply ``CDK_LAMBDA_SUPPRESSIONS`` to any CDK-managed singletons present under ``scope``.

    Resolves each ID via ``node.try_find_child`` rather than an absolute path
    string so suppressions survive being nested in a ``cdk.Stage``. Missing IDs
    are tolerated — some singletons only appear when the construct that needs
    them is instantiated.
    """
    for singleton_id in singleton_ids:
        singleton = scope.node.try_find_child(singleton_id)
        if singleton is not None:
            NagSuppressions.add_resource_suppressions(
                cast(Construct, singleton),
                CDK_LAMBDA_SUPPRESSIONS,
                apply_to_children=True,
            )


CDK_LAMBDA_SUPPRESSIONS = [
    {"id": "AwsSolutions-IAM4", "reason": "CDK-managed singleton Lambda uses AWS managed execution role"},
    {"id": "AwsSolutions-IAM5", "reason": "CDK-managed singleton Lambda uses wildcard in auto-generated policy"},
    {"id": "AwsSolutions-L1", "reason": "CDK-managed singleton Lambda runtime is not configurable"},
    {"id": "Serverless-LambdaTracing", "reason": "CDK-managed singleton Lambda — tracing is not configurable"},
    {"id": "Serverless-LambdaDLQ", "reason": "CDK-managed singleton Lambda — DLQ is not configurable"},
    {"id": "Serverless-LambdaDefaultMemorySize", "reason": "CDK-managed singleton Lambda — memory is not configurable"},
    {"id": "Serverless-LambdaLatestVersion", "reason": "CDK-managed singleton Lambda runtime is not configurable"},
    {"id": "NIST.800.53.R5-IAMNoInlinePolicy", "reason": "CDK-generated inline policy on singleton service role"},
    {"id": "NIST.800.53.R5-LambdaDLQ", "reason": "CDK-managed singleton Lambda — DLQ is not configurable"},
    {
        "id": "NIST.800.53.R5-LambdaConcurrency",
        "reason": "CDK-managed singleton Lambda — concurrency is not configurable",
    },
    {"id": "NIST.800.53.R5-LambdaInsideVPC", "reason": "CDK-managed singleton Lambda — VPC is not configurable"},
    {"id": "HIPAA.Security-IAMNoInlinePolicy", "reason": "CDK-generated inline policy on singleton service role"},
    {"id": "HIPAA.Security-LambdaDLQ", "reason": "CDK-managed singleton Lambda — DLQ is not configurable"},
    {
        "id": "HIPAA.Security-LambdaConcurrency",
        "reason": "CDK-managed singleton Lambda — concurrency is not configurable",
    },
    {"id": "HIPAA.Security-LambdaInsideVPC", "reason": "CDK-managed singleton Lambda — VPC is not configurable"},
    {"id": "PCI.DSS.321-IAMNoInlinePolicy", "reason": "CDK-generated inline policy on singleton service role"},
    {"id": "PCI.DSS.321-LambdaInsideVPC", "reason": "CDK-managed singleton Lambda — VPC is not configurable"},
]
