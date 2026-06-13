"""Unit tests for the TemplateConventionChecks validation Aspect.

The Aspect enforces two project conventions no cdk-nag rule pack covers:
every log group declares a retention, and every stateful L1 resource declares
a removal policy. These tests drive deliberate violations through a throwaway
stack and assert the Aspect raises the matching error annotation — and that a
compliant resource raises nothing. The *absence* of violations on the real
stacks is already covered by ``tests/cdk/test_stage.py::TestNagCompliance``.
"""

import pytest

aws_cdk = pytest.importorskip("aws_cdk", reason="aws_cdk not installed — skipping CDK validation-aspect tests")

import aws_cdk as cdk
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk.assertions import Annotations, Match

from hello_world.validation_aspects import TemplateConventionChecks

_TEST_ENV = cdk.Environment(account="123456789012", region="us-east-1")


def _stack_with_aspect(stack_id: str) -> cdk.Stack:
    """A bare stack with only TemplateConventionChecks applied (no cdk-nag noise)."""
    app = cdk.App()
    stack = cdk.Stack(app, stack_id, env=_TEST_ENV)
    cdk.Aspects.of(stack).add(TemplateConventionChecks())
    return stack


class TestLogRetentionInvariant:
    def test_log_group_without_retention_errors(self) -> None:
        # A raw L1 CfnLogGroup carries no retention -> CloudWatch never-expires
        # it. (The L2 logs.LogGroup defaults retention to two years, so the
        # never-expire footgun only reaches synthesis via the L1 or the legacy
        # LogRetention path — exactly what this Aspect guards.)
        stack = _stack_with_aspect("NoRetentionStack")
        logs.CfnLogGroup(stack, "BadLogGroup")
        errors = Annotations.from_stack(stack).find_error("*", Match.string_like_regexp(".*no explicit retention.*"))
        assert errors, "expected a retention error for a log group with no retention"

    def test_log_group_with_retention_passes(self) -> None:
        stack = _stack_with_aspect("RetentionStack")
        logs.CfnLogGroup(stack, "GoodLogGroup", retention_in_days=7)
        errors = Annotations.from_stack(stack).find_error("*", Match.string_like_regexp(".*no explicit retention.*"))
        assert not errors, "a log group with retention must not raise the retention error"


class TestRemovalPolicyInvariant:
    def test_raw_l1_bucket_without_removal_policy_errors(self) -> None:
        stack = _stack_with_aspect("NoRemovalBucketStack")
        s3.CfnBucket(stack, "RawBucket")  # raw L1 carries no DeletionPolicy
        errors = Annotations.from_stack(stack).find_error(
            "*", Match.string_like_regexp(".*no explicit DeletionPolicy.*")
        )
        assert errors, "expected a removal-policy error for a raw L1 bucket"

    def test_l2_bucket_with_removal_policy_passes(self) -> None:
        stack = _stack_with_aspect("RemovalBucketStack")
        s3.Bucket(stack, "ManagedBucket", removal_policy=cdk.RemovalPolicy.DESTROY)
        errors = Annotations.from_stack(stack).find_error(
            "*", Match.string_like_regexp(".*no explicit DeletionPolicy.*")
        )
        assert not errors, "an L2 bucket with an explicit removal policy must not raise"

    def test_dynamodb_table_with_removal_policy_passes(self) -> None:
        stack = _stack_with_aspect("RemovalTableStack")
        dynamodb.TableV2(
            stack,
            "ManagedTable",
            partition_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        errors = Annotations.from_stack(stack).find_error(
            "*", Match.string_like_regexp(".*no explicit DeletionPolicy.*")
        )
        assert not errors, "a TableV2 with an explicit removal policy must not raise"
