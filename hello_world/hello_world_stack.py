from typing import Any, cast

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_iam as iam
from cdk_nag import NagSuppressions
from constructs import Construct

from hello_world.hello_world_app import HelloWorldApp
from hello_world.nag_utils import (
    AWS_CUSTOM_RESOURCE_PROVIDER_ID,
    apply_compliance_aspects,
    attach_async_failure_destination,
    suppress_cdk_singletons,
)

# CDK-managed singleton Lambda construct IDs to apply CDK_LAMBDA_SUPPRESSIONS to.
# The provider ID is shared (nag_utils) so the suppression pass and the async-DLQ
# attachment below provably target the same construct.
_CDK_SINGLETON_IDS = (AWS_CUSTOM_RESOURCE_PROVIDER_ID,)


class HelloWorldStack(Stack):
    """Thin wrapper stack composing the :class:`HelloWorldApp` construct.

    Per the CDK best practice "model with constructs, deploy with stacks",
    the domain logic lives in the ``HelloWorldApp`` construct; this stack only
    applies stack-wide compliance Aspects, wires CfnOutputs, and attaches the
    stack-level and singleton-scoped cdk-nag suppressions that cannot be
    expressed on individual resources.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, **kwargs)

        apply_compliance_aspects(self)

        self.app = HelloWorldApp(self, "App")

        # Expose API URL + ID for consumption by the frontend stack (api_id lets the
        # frontend CSP pin the exact execute-api host instead of a region wildcard).
        self.api_url = self.app.api_url
        self.api_id = self.app.api.rest_api_id

        CfnOutput(
            self,
            "HelloWorldApiOutput",
            description="API Gateway endpoint URL for Prod stage",
            value=f"{self.app.api.url}hello",
        )
        CfnOutput(
            self,
            "HelloWorldFunctionOutput",
            description="Hello World Lambda Function ARN",
            value=self.app.function.function_arn,
        )
        CfnOutput(
            self,
            "HelloWorldFunctionIamRoleOutput",
            description="IAM Role created for Hello World function",
            value=cast(iam.IRole, self.app.function.role).role_arn,
        )
        CfnOutput(
            self,
            "IdempotencyTableName",
            description="DynamoDB table used for Lambda idempotency",
            value=self.app.idempotency_table.table_name,
        )
        CfnOutput(
            self,
            "GreetingParameterName",
            description="SSM parameter name for the greeting message",
            value=self.app.greeting_param.parameter_name,
        )
        CfnOutput(
            self,
            "AppConfigAppName",
            description="AppConfig application name for feature flags",
            value=self.app.app_config_app.name,
        )
        CfnOutput(
            self,
            "CloudWatchDashboardUrl",
            description="CloudWatch dashboard URL for this stack",
            value=f"https://{self.region}.console.aws.amazon.com/cloudwatch/home#dashboards:name={self.stack_name}",
        )

        # ── Singleton-scoped cdk-nag suppressions ───────────────────────────────
        # CDK-managed singleton Lambdas (currently just the AwsCustomResource
        # provider) are created at the stack level as siblings of the construct
        # that requested them, not as children. ``suppress_cdk_singletons`` looks
        # them up via ``try_find_child`` so the suppressions keep working
        # regardless of whether the stack is at the App root or nested inside
        # a cdk.Stage. (The LogRetention singleton was eliminated when log
        # groups were made explicit via ``log_group=`` everywhere.)
        suppress_cdk_singletons(self, _CDK_SINGLETON_IDS)

        # ── Async failure destination for the AwsCustomResource provider ────────
        # CFN invokes the provider Lambda asynchronously; without an on_failure
        # destination, a crash that exhausts Lambda's two automatic retries is
        # silently dropped — only the CFN rollback error remains. Capturing the
        # failed event envelope to SQS preserves the AWS API response and full
        # request payload for post-mortem.
        # attach_async_failure_destination also emits a CfnOutput of the DLQ URL,
        # so the returned queue is surfaced for operators rather than unused.
        self.cr_provider_dlq = attach_async_failure_destination(
            self,
            AWS_CUSTOM_RESOURCE_PROVIDER_ID,
            encryption_key=self.app.encryption_key,
            queue_id="AwsCustomResourceProviderDlq",
        )

        # ── Stack-level cdk-nag suppressions (genuinely stack-wide) ─────────────
        NagSuppressions.add_stack_suppressions(
            self,
            [
                # ── AWS Solutions ────────────────────────────────────────────────
                {"id": "AwsSolutions-APIG2", "reason": "Request validation not needed for sample app"},
                # AwsSolutions-APIG3 (WAF on API Gateway) is no longer suppressed —
                # a REGIONAL WebACL is now associated with the Prod stage in
                # HelloWorldApp._attach_regional_waf, in addition to the
                # CloudFront-scoped ACL.
                {"id": "AwsSolutions-APIG4", "reason": "Authorization not needed for sample app"},
                {"id": "AwsSolutions-COG4", "reason": "Cognito authorizer not needed for sample app"},
                # ── Serverless ───────────────────────────────────────────────────
                # Serverless-APIGWDefaultThrottling is no longer suppressed —
                # stage-level throttling_rate_limit / throttling_burst_limit are
                # configured on the Prod stage in HelloWorldApp.
                {
                    "id": "CdkNagValidationFailure",
                    "reason": "Serverless-APIGWStructuredLogging validation fails due to intrinsic function reference in access log destination — structured JSON logging is configured via logging_format=JSON on the Lambda",
                },
                # ── NIST 800-53 R5 ──────────────────────────────────────────────
                # NIST.800.53.R5-APIGWAssociatedWithWAF is no longer suppressed —
                # a REGIONAL WebACL is associated with the Prod stage (see
                # HelloWorldApp._attach_regional_waf).
                {
                    "id": "NIST.800.53.R5-APIGWSSLEnabled",
                    "reason": "Client-side SSL certificates not required for sample app",
                },
                {
                    "id": "NIST.800.53.R5-APIGWCacheEnabledAndEncrypted",
                    "reason": (
                        "API Gateway cache cluster intentionally disabled for cost reasons — the smallest "
                        "0.5 GB cluster is ~$14/month for a sample app. Caching GET /hello would also serve "
                        "stale values across SSM parameter and AppConfig feature-flag changes."
                    ),
                },
                {
                    "id": "NIST.800.53.R5-DynamoDBInBackupPlan",
                    "reason": "AWS Backup plan not configured for sample app — PITR is enabled for point-in-time recovery",
                },
                # ── HIPAA Security ───────────────────────────────────────────────
                {
                    "id": "HIPAA.Security-APIGWSSLEnabled",
                    "reason": "Client-side SSL certificates not required for sample app",
                },
                {
                    "id": "HIPAA.Security-APIGWCacheEnabledAndEncrypted",
                    "reason": (
                        "API Gateway cache cluster intentionally disabled for cost reasons — "
                        "see NIST.800.53.R5-APIGWCacheEnabledAndEncrypted rationale above."
                    ),
                },
                {
                    "id": "HIPAA.Security-DynamoDBInBackupPlan",
                    "reason": "AWS Backup plan not configured for sample app — PITR is enabled for point-in-time recovery",
                },
                # ── PCI DSS 3.2.1 ────────────────────────────────────────────────
                # PCI.DSS.321-APIGWAssociatedWithWAF is no longer suppressed —
                # a REGIONAL WebACL is associated with the Prod stage (see
                # HelloWorldApp._attach_regional_waf).
                {
                    "id": "PCI.DSS.321-APIGWSSLEnabled",
                    "reason": "Client-side SSL certificates not required for sample app",
                },
                {
                    "id": "PCI.DSS.321-APIGWCacheEnabledAndEncrypted",
                    "reason": (
                        "API Gateway cache cluster intentionally disabled for cost reasons — "
                        "see NIST.800.53.R5-APIGWCacheEnabledAndEncrypted rationale above."
                    ),
                },
            ],
        )
