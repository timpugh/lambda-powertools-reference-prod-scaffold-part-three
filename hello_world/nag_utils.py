"""Shared cdk-nag helpers.

``apply_compliance_aspects`` applies the full available rule-pack set to a
stack so every stack exercises the same compliance gauntlet. NIST 800-53 R4
is intentionally omitted — R5 supersedes it and running both would duplicate
findings on overlapping controls.

``CDK_LAMBDA_SUPPRESSIONS`` is the canonical suppression list for CDK-managed
singleton Lambdas (AwsCustomResource provider, BucketDeployment, S3AutoDeleteObjects).
Their runtime, memory, tracing, DLQ, VPC, and IAM policies are all managed by
CDK and cannot be configured by the caller. Import it and pass it to
``NagSuppressions.add_resource_suppressions_by_path`` or
``NagSuppressions.add_resource_suppressions`` with ``apply_to_children=True``.
"""

from collections.abc import Iterable
from typing import cast

from aws_cdk import Aspects, Duration, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_lambda_destinations as destinations
from aws_cdk import aws_sqs as sqs
from cdk_nag import (
    AwsSolutionsChecks,
    HIPAASecurityChecks,
    NagSuppressions,
    NIST80053R5Checks,
    PCIDSS321Checks,
    ServerlessChecks,
)
from constructs import Construct, IConstruct


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

    singleton.configure_async_invoke(on_failure=destinations.SqsDestination(dlq))
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
