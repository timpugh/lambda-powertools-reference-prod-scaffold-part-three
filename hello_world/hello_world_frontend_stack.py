from typing import Any

from aws_cdk import (
    Annotations,
    CfnOutput,
    CustomResourceProviderBase,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
)
from aws_cdk import (
    aws_athena as athena,
)
from aws_cdk import (
    aws_cloudfront as cloudfront,
)
from aws_cdk import (
    aws_cloudfront_origins as origins,
)
from aws_cdk import (
    aws_cloudtrail as cloudtrail,
)
from aws_cdk import (
    aws_cognito as cognito,
)
from aws_cdk import (
    aws_glue as glue,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_kms as kms,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_rum as rum,
)
from aws_cdk import (
    aws_s3 as s3,
)
from aws_cdk import (
    aws_s3_deployment as s3deploy,
)
from aws_cdk import (
    custom_resources as cr,
)
from cdk_nag import NagSuppressions
from constructs import Construct

from hello_world.nag_utils import (
    AWS_CUSTOM_RESOURCE_PROVIDER_ID,
    CDK_LAMBDA_SUPPRESSIONS,
    apply_compliance_aspects,
    attach_async_failure_destination,
    grant_logs_service_to_key,
    suppress_cdk_singletons,
)


class HelloWorldFrontendStack(Stack):
    """CDK stack for the Hello World frontend.

    Provisions a private S3 bucket for static assets and a CloudFront
    distribution with OAC, HTTPS-only enforcement, and security response
    headers. WAF protection is provided by a WebACL ARN passed in from
    HelloWorldWafStack, which is always deployed in us-east-1.

    This stack can be deployed to any region. When the target region differs
    from us-east-1, CDK bridges the WAF ARN cross-region automatically via
    SSM Parameter Store (enabled by cross_region_references=True in app.py).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        api_url: str,
        api_id: str,
        waf_acl_arn: str,
        **kwargs: Any,
    ) -> None:
        """Provision all frontend AWS resources.

        Args:
            scope: The CDK construct scope.
            construct_id: The unique identifier for this stack.
            api_url: The backend API Gateway URL, injected into config.json at deploy time.
            api_id: The backend API Gateway REST API ID, used to pin the CSP connect-src
                to the exact execute-api host instead of a region-wide wildcard.
            waf_acl_arn: ARN of the WAF WebACL from HelloWorldWafStack (always in us-east-1).
            **kwargs: Additional keyword arguments passed to the parent Stack.
        """
        super().__init__(scope, construct_id, **kwargs)
        self._api_id = api_id

        apply_compliance_aspects(self)

        # ── KMS key ──────────────────────────────────────────────────────────
        # Used to encrypt the frontend S3 bucket and CloudWatch log group.
        # CloudWatch Logs requires the Logs service principal in the key policy.
        frontend_encryption_key = kms.Key(
            self,
            "FrontendEncryptionKey",
            description=f"KMS key for {self.stack_name} S3 bucket and log groups",
            enable_key_rotation=True,
            # See HelloWorldApp.encryption_key for the rationale — automated
            # rotation, no dependent redeploys, 90-day compliance baseline.
            rotation_period=Duration.days(90),
            removal_policy=RemovalPolicy.DESTROY,
        )
        # Confused-deputy guard on the CMK's CloudWatch Logs service grant.
        # See ``grant_logs_service_to_key`` in ``nag_utils.py``.
        grant_logs_service_to_key(
            frontend_encryption_key,
            region=self.region,
            account=self.account,
            partition=self.partition,
        )

        # ── S3 access logging bucket ─────────────────────────────────────────
        # Receives both S3 server access logs and CloudFront standard access
        # logs. Must use SSE-S3 (not SSE-KMS) because neither the S3 log
        # delivery service nor CloudFront standard logging support KMS-encrypted
        # target buckets. This bucket itself does not need access logging (that
        # would be circular), versioning, or replication.
        access_log_bucket = s3.Bucket(
            self,
            "FrontendAccessLogBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            # CloudFront standard logging requires ACL-based delivery — the bucket owner
            # needs FULL_CONTROL on delivered log objects. BUCKET_OWNER_PREFERRED keeps
            # Object Ownership set so ACLs remain usable for CloudFront log delivery.
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
            versioned=False,
            # 7-day expiration cap on every prefix in this bucket (S3 access logs,
            # CloudFront standard logs, Athena query results). Tunable: extend
            # the duration, swap to a tiered transition (Standard-IA at 30d,
            # Glacier Instant Retrieval at 90d, Glacier Deep Archive at 180d),
            # or layer per-prefix rules if logs and Athena results need
            # different retention.
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireAfter7Days",
                    enabled=True,
                    expiration=Duration.days(7),
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                ),
            ],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        access_log_bucket_suppressions = [
            ("AwsSolutions-S1", "This IS the access log bucket — logging to itself would be circular"),
            (
                "NIST.800.53.R5-S3BucketLoggingEnabled",
                "This IS the access log bucket — logging to itself would be circular",
            ),
            (
                "NIST.800.53.R5-S3DefaultEncryptionKMS",
                "S3 log delivery service does not support KMS-encrypted target buckets; SSE-S3 is used instead",
            ),
            (
                "HIPAA.Security-S3DefaultEncryptionKMS",
                "S3 log delivery service does not support KMS-encrypted target buckets; SSE-S3 is used instead",
            ),
            (
                "PCI.DSS.321-S3DefaultEncryptionKMS",
                "S3 log delivery service does not support KMS-encrypted target buckets; SSE-S3 is used instead",
            ),
            (
                "NIST.800.53.R5-S3BucketVersioningEnabled",
                "Versioning not needed for log bucket — logs are append-only and transient",
            ),
            (
                "HIPAA.Security-S3BucketVersioningEnabled",
                "Versioning not needed for log bucket — logs are append-only and transient",
            ),
            (
                "PCI.DSS.321-S3BucketVersioningEnabled",
                "Versioning not needed for log bucket — logs are append-only and transient",
            ),
            ("NIST.800.53.R5-S3BucketReplicationEnabled", "Replication not needed for log bucket in sample app"),
            ("HIPAA.Security-S3BucketReplicationEnabled", "Replication not needed for log bucket in sample app"),
            ("PCI.DSS.321-S3BucketReplicationEnabled", "Replication not needed for log bucket in sample app"),
        ]
        NagSuppressions.add_resource_suppressions(
            access_log_bucket,
            [{"id": rule, "reason": reason} for rule, reason in access_log_bucket_suppressions],
        )

        # ── S3 bucket ────────────────────────────────────────────────────────
        # Fully private — CloudFront OAC is the only allowed reader.
        # KMS-encrypted with server access logging to access_log_bucket.
        bucket = s3.Bucket(
            self,
            "FrontendBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=frontend_encryption_key,
            enforce_ssl=True,
            server_access_logs_bucket=access_log_bucket,
            server_access_logs_prefix="s3-access-logs/",
            versioned=False,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        self._create_s3_audit_trail(audited_buckets=[bucket, access_log_bucket], encryption_key=frontend_encryption_key)

        # ── CloudFront response headers ──────────────────────────────────────
        # Custom ResponseHeadersPolicy (replaces the AWS-managed SECURITY_HEADERS)
        # adding HSTS + CSP on top of the four headers that policy provided.
        # See _build_response_headers_policy for the full rationale.
        response_headers_policy = self._build_response_headers_policy()

        # ── CloudFront distribution ──────────────────────────────────────────
        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=response_headers_policy,
            ),
            default_root_object="index.html",
            error_responses=[
                # Return index.html for 403/404 so SPA client-side routing works
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
            ],
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            web_acl_id=waf_acl_arn,
            enable_logging=True,
            log_bucket=access_log_bucket,
            log_file_prefix="cloudfront/",
        )

        # ── CloudWatch RUM + X-Ray ───────────────────────────────────────────
        # RUM collects browser telemetry (page loads, JS errors, fetch latency)
        # and — with enable_x_ray — emits a client-side trace segment that joins
        # the backend Lambda/API Gateway segments into a single X-Ray trace.
        # Guest (unauthenticated) browsers authenticate via Cognito Identity
        # Pool → STS AssumeRoleWithWebIdentity → scoped rum:PutRumEvents role.
        # The monitor ARN is constructed from the known monitor name so the
        # IAM role can reference it without a circular dependency on the
        # CfnAppMonitor resource.
        rum_identity_pool = cognito.CfnIdentityPool(
            self,
            "RumIdentityPool",
            allow_unauthenticated_identities=True,
            identity_pool_name=f"{self.stack_name}-rum",
        )
        rum_monitor_name = f"{self.stack_name}-rum"
        rum_monitor_arn = f"arn:{self.partition}:rum:{self.region}:{self.account}:appmonitor/{rum_monitor_name}"
        rum_unauth_role = iam.Role(
            self,
            "RumUnauthenticatedRole",
            assumed_by=iam.FederatedPrincipal(
                "cognito-identity.amazonaws.com",
                conditions={
                    "StringEquals": {"cognito-identity.amazonaws.com:aud": rum_identity_pool.ref},
                    "ForAnyValue:StringLike": {"cognito-identity.amazonaws.com:amr": "unauthenticated"},
                },
                assume_role_action="sts:AssumeRoleWithWebIdentity",
            ),
            description=f"Guest role assumed by browser RUM clients for {rum_monitor_name}",
            inline_policies={
                "AllowPutRumEvents": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["rum:PutRumEvents"],
                            resources=[rum_monitor_arn],
                        )
                    ]
                )
            },
        )
        cognito.CfnIdentityPoolRoleAttachment(
            self,
            "RumIdentityPoolRoleAttachment",
            identity_pool_id=rum_identity_pool.ref,
            roles={"unauthenticated": rum_unauth_role.role_arn},
        )
        rum_app_monitor = rum.CfnAppMonitor(
            self,
            "RumAppMonitor",
            name=rum_monitor_name,
            domain=distribution.distribution_domain_name,
            cw_log_enabled=True,
            # Enable custom events so the frontend can call cwr('recordEvent', ...)
            # for domain telemetry. Without this set to ENABLED, custom event
            # uploads are silently dropped at the data plane.
            custom_events=rum.CfnAppMonitor.CustomEventsProperty(status="ENABLED"),
            app_monitor_configuration=rum.CfnAppMonitor.AppMonitorConfigurationProperty(
                allow_cookies=True,
                enable_x_ray=True,
                session_sample_rate=1.0,
                # CloudFormation's schema only accepts ["errors", "performance", "http"] here —
                # "interaction" is rejected as an invalid enum value despite being a real RUM
                # plugin. This server-side list is metadata used by the AWS-generated snippet,
                # not the live plugin loader. The actual plugin set is controlled by the
                # client-side `telemetries` array in frontend/index.html, which DOES include
                # "interaction" alongside the http tuple form. Keep these two lists divergent
                # on purpose; do not "sync" them.
                telemetries=["errors", "performance", "http"],
                identity_pool_id=rum_identity_pool.ref,
                guest_role_arn=rum_unauth_role.role_arn,
            ),
        )

        # CMK-encrypted log group for the BucketDeployment provider Lambda.
        # Passing log_group= here (instead of log_retention=) avoids the legacy
        # LogRetention singleton path and keeps every log group encrypted with
        # this stack's CMK.
        bucket_deployment_log_group = logs.LogGroup(
            self,
            "BucketDeploymentLogGroup",
            encryption_key=frontend_encryption_key,
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Shared CMK-encrypted log group for all AwsCustomResource singletons in
        # this stack (RumMetricsDestination, RumExtendedMetrics, InvalidateCloudFrontCache).
        # CDK reuses one provider Lambda across every AwsCustomResource in a stack,
        # so a single log group serves all three.
        custom_resource_log_group = logs.LogGroup(
            self,
            "AwsCustomResourceLogGroup",
            encryption_key=frontend_encryption_key,
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        rum_extended_metrics = self._wire_rum_metrics_extras(
            rum_app_monitor, rum_monitor_name, rum_monitor_arn, custom_resource_log_group
        )
        self._wire_rum_log_group_cleanup(rum_app_monitor, rum_monitor_name, custom_resource_log_group)

        # ── Deploy frontend assets ───────────────────────────────────────────
        # Uploads frontend/ to S3 and generates config.json with the API URL
        # and RUM client config injected at deploy time. Cache invalidation is
        # handled by a separate AwsCustomResource below — the BucketDeployment's
        # built-in `distribution=` parameter is intentionally not used because
        # its delete-time invalidation races with CloudFront's own deletion on
        # `cdk destroy` (aws/aws-cdk#15891).
        bucket_deployment = s3deploy.BucketDeployment(
            self,
            "DeployFrontend",
            sources=[
                s3deploy.Source.asset("frontend"),
                s3deploy.Source.json_data(
                    "config.json",
                    {
                        "apiUrl": api_url,
                        "rum": {
                            "appMonitorId": rum_app_monitor.attr_id,
                            "identityPoolId": rum_identity_pool.ref,
                            "region": self.region,
                            # Session attributes are attached to every RUM event
                            # in the session. Sourcing them from deploy-time
                            # config (rather than hardcoding in the HTML) lets
                            # multiple deploys feed the same dashboard while
                            # remaining filterable.
                            "sessionAttributes": {
                                "applicationName": self.stack_name,
                            },
                        },
                    },
                ),
            ],
            destination_bucket=bucket,
            log_group=bucket_deployment_log_group,
        )
        # Defer the slow asset deploy until after the RUM custom resources
        # have succeeded. If RumExtendedMetrics fails (it depends on IAM
        # propagation), the BucketDeployment never starts — saving the most
        # expensive single resource from being repeated on every retry until
        # the cheaper IAM dance settles.
        bucket_deployment.node.add_dependency(rum_extended_metrics)

        # CloudFront cache invalidation, decoupled from BucketDeployment.
        # Defines on_create and on_update only — no on_delete — so CFN simply
        # removes this resource from stack state during teardown without any
        # CloudFront API call to race with the distribution's own deletion.
        # This is the permanent fix for aws/aws-cdk#15891, replacing the
        # BucketDeployment's built-in invalidation hook.
        #
        # CallerReference is gated on the BucketDeployment's content-hashed S3
        # object key. Same assets → same key → CFN sees no change → no invalidation
        # fires (correct: nothing to invalidate). Different assets → different key →
        # CFN fires on_update → invalidation runs. Prevents backend-only deploys from
        # burning the 1000/month free invalidation quota. See README "Design
        # decisions" for the longer write-up.
        #
        # NOTE: CloudFront caps CallerReference at 128 characters, so we cannot fold
        # the api_url (or other identifiers) into it — the content-hashed object key
        # alone is already ~68 chars. If a backend-only API replacement changes
        # config.json's contents, that path is covered because Source.json_data
        # writes config.json as part of the BucketDeployment asset, so its hash —
        # and thus this object key — changes when api_url changes.
        # object_keys is a CDK list-token, not a Python list — use Fn.select.
        cf_invalidation_call = cr.AwsSdkCall(
            service="CloudFront",
            action="createInvalidation",
            parameters={
                "DistributionId": distribution.distribution_id,
                "InvalidationBatch": {
                    "Paths": {"Quantity": 1, "Items": ["/*"]},
                    "CallerReference": Fn.select(0, bucket_deployment.object_keys),
                },
            },
            physical_resource_id=cr.PhysicalResourceId.of(f"{self.stack_name}-cf-invalidation"),
        )
        invalidate_cf_cache = cr.AwsCustomResource(
            self,
            "InvalidateCloudFrontCache",
            on_create=cf_invalidation_call,
            on_update=cf_invalidation_call,
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=["cloudfront:CreateInvalidation"],
                        resources=[
                            f"arn:{Stack.of(self).partition}:cloudfront::{Stack.of(self).account}:distribution/{distribution.distribution_id}"
                        ],
                    ),
                ]
            ),
            log_group=custom_resource_log_group,
        )
        invalidate_cf_cache.node.add_dependency(bucket_deployment)
        # CDK generates an inline default policy on the AwsCustomResource's
        # auto-created role. Same constraint as the RUM custom resources;
        # apply the same IAMNoInlinePolicy suppressions.
        cf_invalidation_inline_reason = (
            "AwsCustomResource policy is a single least-privilege inline statement scoped to "
            "cloudfront:CreateInvalidation on this stack's distribution ARN — managed-policy "
            "reuse adds nothing"
        )
        NagSuppressions.add_resource_suppressions(
            invalidate_cf_cache,
            [
                {"id": "NIST.800.53.R5-IAMNoInlinePolicy", "reason": cf_invalidation_inline_reason},
                {"id": "HIPAA.Security-IAMNoInlinePolicy", "reason": cf_invalidation_inline_reason},
                {"id": "PCI.DSS.321-IAMNoInlinePolicy", "reason": cf_invalidation_inline_reason},
            ],
            apply_to_children=True,
        )

        CfnOutput(
            self,
            "CloudFrontDomainName",
            description="CloudFront distribution domain name — use this as your frontend URL",
            value=f"https://{distribution.distribution_domain_name}",
        )
        CfnOutput(
            self,
            "CloudFrontDistributionId",
            description="CloudFront distribution ID — needed for manual cache invalidations",
            value=distribution.distribution_id,
        )
        CfnOutput(
            self,
            "FrontendBucketName",
            description="S3 bucket storing the frontend static assets",
            value=bucket.bucket_name,
        )
        CfnOutput(
            self,
            "RumAppMonitorId",
            description="CloudWatch RUM app monitor ID — used by the browser RUM client",
            value=rum_app_monitor.attr_id,
        )
        CfnOutput(
            self,
            "RumIdentityPoolId",
            description="Cognito Identity Pool ID — used by the browser RUM client for guest credentials",
            value=rum_identity_pool.ref,
        )

        # ── RUM / Cognito cdk-nag suppressions ───────────────────────────────
        # Unauthenticated identities are intentional — browsers have no prior
        # identity and RUM's guest-credentials model is the standard pattern.
        # The role's only permission is rum:PutRumEvents on this monitor.
        NagSuppressions.add_resource_suppressions(
            rum_identity_pool,
            [
                {
                    "id": "AwsSolutions-COG7",
                    "reason": "RUM requires unauthenticated guest credentials for anonymous browser telemetry",
                },
            ],
        )
        # The guest role has a single least-privilege permission — rum:PutRumEvents
        # on exactly one monitor ARN — tightly bound to this role's one purpose.
        # A managed policy would add indirection without changing the security
        # posture, since the policy is used by nothing else and is scoped to a
        # resource that is itself one-to-one with the role.
        inline_policy_reason = (
            "Single least-privilege inline policy (rum:PutRumEvents on one monitor ARN) "
            "is tightly bound to this role's sole purpose — anonymous browser telemetry upload"
        )
        NagSuppressions.add_resource_suppressions(
            rum_unauth_role,
            [
                {"id": "NIST.800.53.R5-IAMNoInlinePolicy", "reason": inline_policy_reason},
                {"id": "HIPAA.Security-IAMNoInlinePolicy", "reason": inline_policy_reason},
                {"id": "PCI.DSS.321-IAMNoInlinePolicy", "reason": inline_policy_reason},
            ],
        )

        # Explicit CMK log group for the CDK auto-delete-objects singleton Lambda
        # (see _create_auto_delete_log_group). Returns the provider so the
        # suppression block below can target it.
        auto_delete_provider = self._create_auto_delete_log_group(frontend_encryption_key)

        self._create_athena_glue_resources(access_log_bucket, frontend_encryption_key)

        # ── Per-resource cdk-nag suppressions ──────────────────────────────────
        # All Lambdas in this stack are CDK-managed singletons. Their construct
        # IDs are stable (hashed from CDK's own source) but they are created as
        # stack-level siblings of the construct that requested them, so we look
        # them up with ``try_find_child`` rather than absolute path strings —
        # this keeps the suppression working regardless of whether the stack is
        # at the App root or nested inside a cdk.Stage.
        #
        # Stable singleton IDs:
        #   Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C — BucketDeployment provider
        #   Custom::S3AutoDeleteObjectsCustomResourceProvider — auto-delete provider
        #   AWS679f53fac002430cb0da5b7982bd2287 — AwsCustomResource provider Lambda
        #     (used by RumMetricsDestination, RumExtendedMetrics, InvalidateCloudFrontCache)
        suppress_cdk_singletons(
            self,
            (
                "Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C",
                AWS_CUSTOM_RESOURCE_PROVIDER_ID,
            ),
        )

        # ── Async failure destination for the AwsCustomResource provider ────────
        # See HelloWorldStack for the full rationale — CFN invokes the provider
        # async, and without on_failure a crashed provider's payload is lost.
        # attach_async_failure_destination also emits a CfnOutput of the DLQ URL,
        # so the returned queue is surfaced for operators rather than unused.
        self.cr_provider_dlq = attach_async_failure_destination(
            self,
            AWS_CUSTOM_RESOURCE_PROVIDER_ID,
            encryption_key=frontend_encryption_key,
            queue_id="AwsCustomResourceProviderDlq",
        )

        # minimizePolicies restructures the BucketDeployment handler's inline
        # policy into a separate resource under DeployFrontend/CustomResourceHandler.
        deploy_frontend = self.node.try_find_child("DeployFrontend")
        if deploy_frontend is not None:
            suppress_cdk_singletons(deploy_frontend, ("CustomResourceHandler",))
        if auto_delete_provider is not None:
            NagSuppressions.add_resource_suppressions(
                auto_delete_provider,
                CDK_LAMBDA_SUPPRESSIONS,
                apply_to_children=True,
            )

        # ── Stack-level cdk-nag suppressions (genuinely stack-wide) ─────────────
        replication_reason = "S3 replication not needed for sample app — static assets are redeployable"
        versioning_reason = "S3 versioning not needed for sample app — static assets are redeployable via cdk deploy"
        stack_suppressions = [
            ("AwsSolutions-CFR1", "Geo restriction not required for sample app"),
            ("AwsSolutions-CFR4", "Using default CloudFront certificate — no custom domain for sample app"),
            ("NIST.800.53.R5-S3BucketReplicationEnabled", replication_reason),
            ("NIST.800.53.R5-S3BucketVersioningEnabled", versioning_reason),
            ("HIPAA.Security-S3BucketReplicationEnabled", replication_reason),
            ("HIPAA.Security-S3BucketVersioningEnabled", versioning_reason),
            ("PCI.DSS.321-S3BucketReplicationEnabled", replication_reason),
            ("PCI.DSS.321-S3BucketVersioningEnabled", versioning_reason),
        ]
        NagSuppressions.add_stack_suppressions(
            self,
            [{"id": rule, "reason": reason} for rule, reason in stack_suppressions],
        )

    def _create_auto_delete_log_group(self, encryption_key: kms.Key) -> CustomResourceProviderBase | None:
        """Create an explicit CMK log group for the CDK auto-delete-objects singleton.

        CDK creates a singleton Lambda to empty the bucket before deletion. It is a
        CloudFormation-managed Lambda, but its log group is created implicitly by
        Lambda and has no retention — it would dangle after ``cdk destroy``. We find
        the provider via the construct tree and create an explicit log group so
        CloudFormation owns and deletes it.

        The lookup is type-checked at runtime instead of cast-asserted: if CDK ever
        swaps the provider out for an incompatible type the explicit isinstance()
        returns None and the log-group block is skipped, rather than letting a stale
        cast() lie its way into a service_token attribute access that would crash at
        synth time. We match ``CustomResourceProviderBase`` (not the narrower
        ``CustomResourceProvider`` subclass) because CDK 2.248's S3 auto-delete
        singleton synthesizes as the base type — matching only the subclass silently
        skipped the log group and let it dangle. Returns the provider (or None) so
        the caller can attach suppressions to it.
        """
        node = self.node.try_find_child("Custom::S3AutoDeleteObjectsCustomResourceProvider")
        provider = node if isinstance(node, CustomResourceProviderBase) else None
        # If the node exists but isn't a CustomResourceProvider, a CDK upgrade has
        # changed the provider's type and the log-group cleanup is silently skipped —
        # surface that loudly at synth instead of leaving a dangling /aws/lambda/ group.
        if node is not None and provider is None:
            Annotations.of(self).add_warning(
                "S3 auto-delete provider node found but is not a CustomResourceProviderBase — "
                "its log group will not be created and may dangle after cdk destroy. "
                "A CDK version bump likely changed the provider type; update AutoDeleteObjectsLogGroup wiring."
            )
        if provider is not None:
            # service_token is the Lambda ARN; index 6 of the colon-split is the function name
            fn_name = Fn.select(6, Fn.split(":", provider.service_token))
            logs.LogGroup(
                self,
                "AutoDeleteObjectsLogGroup",
                log_group_name=Fn.join("", ["/aws/lambda/", fn_name]),
                encryption_key=encryption_key,
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            )
        return provider

    def _build_response_headers_policy(self) -> cloudfront.ResponseHeadersPolicy:
        """Build the CloudFront ResponseHeadersPolicy (managed SECURITY_HEADERS + HSTS + CSP).

        A custom policy instead of the AWS-managed SECURITY_HEADERS policy, which
        omits two headers a production edge wants: Strict-Transport-Security (HSTS)
        and Content-Security-Policy (CSP). The four headers the managed policy
        already provided (X-Content-Type-Options, X-Frame-Options, Referrer-Policy,
        X-XSS-Protection) are reproduced here so nothing regresses.

        HSTS: 1-year max-age with includeSubDomains. ``preload`` is intentionally
        False — the HSTS preload list is keyed on the registrable domain, and a
        shared ``*.cloudfront.net`` host cannot (and should not) be submitted. A
        fork that wires a custom domain (see TODO "Enforce TLS 1.2+ minimum")
        should flip preload=True once ready to commit the domain to the list.

        CSP: the reference page ships inline ``<script>`` and ``<style>`` blocks
        (the RUM bootstrap in ``<head>`` shares ``window.__appConfigPromise`` with
        the body handler, relying on synchronous head execution), so
        ``'unsafe-inline'`` is required on script-src/style-src or the page's own
        scripts would be blocked. Everything else is locked down:
        default-src/object-src/base-uri/frame-ancestors are constrained, and
        script-src/connect-src are scoped to the exact AWS endpoints the page talks
        to (RUM client script + data plane, Cognito Identity for guest credentials,
        and this stack's specific execute-api host for the API call). To reach a
        strict (nonce/hash) CSP without ``'unsafe-inline'``, externalize the two
        inline scripts into ``'self'``-served .js assets first — CloudFront cannot
        mint per-response nonces, and static hashes would break on any edit.
        Tracked in TODO "CSP header on the static page".

        The RUM client script URL is hardcoded to us-east-1 in frontend/index.html
        regardless of deploy region, so script-src pins us-east-1 specifically
        while connect-src follows ``self.region`` for the data plane and Cognito.
        The execute-api host is pinned to this API's exact ID (``self._api_id``, a
        CDK token resolved to ``{id}.execute-api.{region}.amazonaws.com`` at synth)
        rather than a ``*.execute-api`` wildcard, so an injected script can only
        reach this app's own API — not every API in the region/account.
        """
        csp = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://client.rum.us-east-1.amazonaws.com; "
            "style-src 'self' 'unsafe-inline'; "
            "connect-src 'self' "
            f"https://dataplane.rum.{self.region}.amazonaws.com "
            f"https://cognito-identity.{self.region}.amazonaws.com "
            f"https://{self._api_id}.execute-api.{self.region}.amazonaws.com; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'none'"
        )
        return cloudfront.ResponseHeadersPolicy(
            self,
            "SecurityHeadersPolicy",
            response_headers_policy_name=f"{self.stack_name}-security-headers",
            comment="Security headers for the Hello World frontend (managed SECURITY_HEADERS + HSTS + CSP)",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY,
                    override=True,
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                    override=True,
                ),
                xss_protection=cloudfront.ResponseHeadersXSSProtection(
                    protection=True,
                    mode_block=True,
                    override=True,
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365),
                    include_subdomains=True,
                    preload=False,
                    override=True,
                ),
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=csp,
                    override=True,
                ),
            ),
        )

    def _create_s3_audit_trail(self, audited_buckets: list[s3.Bucket], encryption_key: kms.Key) -> None:
        """Create a CloudTrail Trail recording S3 object-level data events on the given buckets.

        Captures every Get/Put/Delete API call against the audited buckets. Object-level
        events aren't recorded by the default management-events trail and aren't
        reconstructible from S3 server access logs (those only cover successful reads/writes
        through the bucket interface, not failed authorization or DeleteObject calls).
        Trail logs are stored in a dedicated bucket so the audit destination isn't itself
        among the audited resources.
        """
        # CloudTrail needs explicit KMS grants on the encryption key to write
        # encrypted log files. CDK's auto-grants from passing encryption_key=
        # don't always extend to the cloudtrail service principal when the key
        # is shared with other services (CloudWatch Logs, CloudFront, etc.),
        # so add the principal explicitly here. Mirrors the existing logs/
        # CloudFront grants on the same key.
        # Confused-deputy guard: scope the CloudTrail principal grant to trails
        # in this account. The trail name is generated by CDK so we use a wildcard
        # ARN against the account; CloudTrail sets aws:SourceArn to the trail ARN
        # on every encrypt call. aws:SourceAccount is checked too as defense in
        # depth (some older trail integrations omit aws:SourceArn).
        encryption_key.add_to_resource_policy(
            iam.PolicyStatement(
                actions=["kms:GenerateDataKey*", "kms:DescribeKey"],
                principals=[iam.ServicePrincipal("cloudtrail.amazonaws.com")],
                resources=["*"],
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:{self.partition}:cloudtrail:{self.region}:{self.account}:trail/*",
                    },
                },
            )
        )
        cloudtrail_log_bucket = s3.Bucket(
            self,
            "CloudTrailLogsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=False,
            # Bound the audit trail's storage growth. S3 data events fire on
            # every Get/Put/Delete against the audited buckets and accumulate
            # forever otherwise. 30 days matches a typical short-retention
            # forensic window — production forks with compliance scope (HIPAA,
            # PCI) should extend or replace this with an AWS Backup plan, and
            # forks running CloudTrail Lake can drop the on-bucket trail
            # entirely.
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireAfter30Days",
                    enabled=True,
                    expiration=Duration.days(30),
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                ),
            ],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        # CloudTrail can't write to a bucket that has access logging or KMS-CMK
        # encryption enabled (delivery service limitations) — same constraints
        # that apply to access_log_bucket. Suppress the corresponding nag rules.
        bucket_suppressions = [
            ("AwsSolutions-S1", "CloudTrail log bucket — server access logging would create circular audit trails"),
            (
                "NIST.800.53.R5-S3BucketLoggingEnabled",
                "CloudTrail log bucket — server access logging would create circular audit trails",
            ),
            (
                "HIPAA.Security-S3BucketLoggingEnabled",
                "CloudTrail log bucket — server access logging would create circular audit trails",
            ),
            (
                "PCI.DSS.321-S3BucketLoggingEnabled",
                "CloudTrail log bucket — server access logging would create circular audit trails",
            ),
            (
                "NIST.800.53.R5-S3DefaultEncryptionKMS",
                "CloudTrail delivery service does not support KMS-CMK encrypted destination buckets",
            ),
            (
                "HIPAA.Security-S3DefaultEncryptionKMS",
                "CloudTrail delivery service does not support KMS-CMK encrypted destination buckets",
            ),
            (
                "PCI.DSS.321-S3DefaultEncryptionKMS",
                "CloudTrail delivery service does not support KMS-CMK encrypted destination buckets",
            ),
            (
                "NIST.800.53.R5-S3BucketVersioningEnabled",
                "Versioning not needed for CloudTrail log bucket — logs are append-only and integrity-validated by CloudTrail",
            ),
            (
                "HIPAA.Security-S3BucketVersioningEnabled",
                "Versioning not needed for CloudTrail log bucket — logs are append-only and integrity-validated by CloudTrail",
            ),
            (
                "PCI.DSS.321-S3BucketVersioningEnabled",
                "Versioning not needed for CloudTrail log bucket — logs are append-only and integrity-validated by CloudTrail",
            ),
            (
                "NIST.800.53.R5-S3BucketReplicationEnabled",
                "Replication not needed for CloudTrail log bucket in sample app",
            ),
            (
                "HIPAA.Security-S3BucketReplicationEnabled",
                "Replication not needed for CloudTrail log bucket in sample app",
            ),
            (
                "PCI.DSS.321-S3BucketReplicationEnabled",
                "Replication not needed for CloudTrail log bucket in sample app",
            ),
        ]
        NagSuppressions.add_resource_suppressions(
            cloudtrail_log_bucket,
            [{"id": rule, "reason": reason} for rule, reason in bucket_suppressions],
        )
        cloudtrail_log_group = logs.LogGroup(
            self,
            "S3DataEventsTrailLogs",
            encryption_key=encryption_key,
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        # Pin the trail name so its ARN is known *before* the trail resource is
        # created — needed to break the dependency cycle that would otherwise
        # form between the trail (which CDK auto-wires to depend on its bucket
        # policy) and the confused-deputy Deny statement on the bucket policy
        # (which references the trail ARN).
        trail_name = f"{self.stack_name}-S3DataEventsTrail"
        trail_arn = f"arn:{self.partition}:cloudtrail:{self.region}:{self.account}:trail/{trail_name}"

        # Confused-deputy guard on the CloudTrail bucket policy. CDK 2.248's
        # cloudtrail.Trail L2 grants the cloudtrail.amazonaws.com principal
        # s3:GetBucketAcl + s3:PutObject without an aws:SourceArn condition,
        # so any CloudTrail trail in any AWS account that ever discovered this
        # bucket name could in principle write to it. Adding two explicit Deny
        # statements (one per condition key) closes the gap on either mismatch
        # — if both keys lived in one StringNotEquals block IAM would AND them,
        # so a malicious trail in the same account with a different name would
        # match aws:SourceAccount and slip past. Splitting into two Denies gives
        # the OR semantics we actually want.
        ct_principals = [iam.ServicePrincipal("cloudtrail.amazonaws.com")]
        ct_resources = [cloudtrail_log_bucket.bucket_arn, cloudtrail_log_bucket.arn_for_objects("*")]
        cloudtrail_log_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                effect=iam.Effect.DENY,
                actions=["s3:GetBucketAcl", "s3:PutObject"],
                principals=ct_principals,
                resources=ct_resources,
                conditions={"StringNotEquals": {"aws:SourceArn": trail_arn}},
            )
        )
        cloudtrail_log_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                effect=iam.Effect.DENY,
                actions=["s3:GetBucketAcl", "s3:PutObject"],
                principals=ct_principals,
                resources=ct_resources,
                conditions={"StringNotEquals": {"aws:SourceAccount": self.account}},
            )
        )

        s3_data_events_trail = cloudtrail.Trail(
            self,
            "S3DataEventsTrail",
            trail_name=trail_name,
            bucket=cloudtrail_log_bucket,
            send_to_cloud_watch_logs=True,
            cloud_watch_log_group=cloudtrail_log_group,
            encryption_key=encryption_key,
            enable_file_validation=True,
            include_global_service_events=False,
            is_multi_region_trail=False,
        )
        s3_data_events_trail.add_s3_event_selector([cloudtrail.S3EventSelector(bucket=b) for b in audited_buckets])
        # CDK creates the trail's CloudWatch Logs delivery role with an inline
        # default policy — same pattern as the Lambda service role; not
        # directly configurable.
        inline_policy_reason = "CDK generates the trail's LogsRole default policy inline — not directly configurable"
        NagSuppressions.add_resource_suppressions(
            s3_data_events_trail,
            [
                {"id": "NIST.800.53.R5-IAMNoInlinePolicy", "reason": inline_policy_reason},
                {"id": "HIPAA.Security-IAMNoInlinePolicy", "reason": inline_policy_reason},
                {"id": "PCI.DSS.321-IAMNoInlinePolicy", "reason": inline_policy_reason},
            ],
            apply_to_children=True,
        )

    def _wire_rum_metrics_extras(
        self,
        rum_app_monitor: rum.CfnAppMonitor,
        rum_monitor_name: str,
        rum_monitor_arn: str,
        custom_resource_log_group: logs.LogGroup,
    ) -> cr.AwsCustomResource:
        """Wire CloudWatch metrics destination and dimensioned metric definitions to the AppMonitor.

        Returns the metric-definitions custom resource so callers can wire a dependency on it.

        Implementation notes (these are non-obvious — see README "CloudWatch RUM" section):
        - Each definition needs an explicit ``EventPattern``; the API rejects vended-metric
          submissions with just ``Name`` + ``DimensionKeys`` (returns 200 OK with an Errors[]
          body that AwsCustomResource treats as success).
        - All three ``rum:*`` actions are bundled on the destination CR's policy so the
          BatchCreate call benefits from a full putRumMetricsDestination round-trip of IAM
          propagation lead time. Splitting them per-CR loses the IAM race ~100% of the time.
        - ``on_update`` mirrors ``on_create``; without it AwsCustomResource no-ops on
          CloudFormation UPDATE events and changes to the metric list never reach AWS.
        - Http5xx omits the explicit numeric range filter that Http4xx requires (RUM applies
          the 5xx filter internally for that vended metric).
        """
        rum_metrics_destination = cr.AwsCustomResource(
            self,
            "RumMetricsDestination",
            on_create=cr.AwsSdkCall(
                service="rum",
                action="putRumMetricsDestination",
                parameters={"AppMonitorName": rum_monitor_name, "Destination": "CloudWatch"},
                physical_resource_id=cr.PhysicalResourceId.of(f"{rum_monitor_name}/CloudWatch"),
            ),
            on_delete=cr.AwsSdkCall(
                service="rum",
                action="deleteRumMetricsDestination",
                parameters={"AppMonitorName": rum_monitor_name, "Destination": "CloudWatch"},
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=[
                            "rum:PutRumMetricsDestination",
                            "rum:DeleteRumMetricsDestination",
                            "rum:BatchCreateRumMetricDefinitions",
                        ],
                        resources=[rum_monitor_arn],
                    ),
                ]
            ),
            log_group=custom_resource_log_group,
        )
        rum_metrics_destination.node.add_dependency(rum_app_monitor)

        js_pat = '{{"event_type":["com.amazon.rum.js_error_event"],"metadata":{{"{k}":[{{"exists":true}}]}}}}'
        http_pat = '{{"event_type":["com.amazon.rum.http_event"],"metadata":{{"browserName":[{{"exists":true}}]}}{s}}}'
        http4xx_status = ',"event_details":{"response":{"status":[{"numeric":[">=",400,"<",500]}]}}'
        page_pat = '{"event_type":["com.amazon.rum.page_view_event"],"metadata":{"pageId":[{"exists":true}]}}'
        defs: list[dict[str, Any]] = [
            {
                "Name": "JsErrorCount",
                "EventPattern": js_pat.format(k="browserName"),
                "DimensionKeys": {"metadata.browserName": "BrowserName"},
            },
            {
                "Name": "JsErrorCount",
                "EventPattern": js_pat.format(k="deviceType"),
                "DimensionKeys": {"metadata.deviceType": "DeviceType"},
            },
            {
                "Name": "JsErrorCount",
                "EventPattern": js_pat.format(k="countryCode"),
                "DimensionKeys": {"metadata.countryCode": "CountryCode"},
            },
            {
                "Name": "Http4xxCount",
                "EventPattern": http_pat.format(s=http4xx_status),
                "DimensionKeys": {"metadata.browserName": "BrowserName"},
            },
            {
                "Name": "Http5xxCount",
                "EventPattern": http_pat.format(s=""),
                "DimensionKeys": {"metadata.browserName": "BrowserName"},
            },
            {"Name": "PageViewCount", "EventPattern": page_pat, "DimensionKeys": {"metadata.pageId": "PageId"}},
        ]
        batch_create = cr.AwsSdkCall(
            service="rum",
            action="batchCreateRumMetricDefinitions",
            parameters={
                "AppMonitorName": rum_monitor_name,
                "Destination": "CloudWatch",
                "MetricDefinitions": defs,
            },
            physical_resource_id=cr.PhysicalResourceId.of(f"{rum_monitor_name}/extended-metrics"),
        )
        rum_extended_metrics = cr.AwsCustomResource(
            self,
            "RumExtendedMetrics",
            on_create=batch_create,
            on_update=batch_create,
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(resources=[rum_monitor_arn]),
            log_group=custom_resource_log_group,
        )
        rum_extended_metrics.node.add_dependency(rum_metrics_destination)

        # Same single-purpose, monitor-scoped justification as the RumUnauthenticatedRole
        # inline policy. Cdk-nag flags both per-construct CustomResourcePolicy resources.
        reason = (
            "Single least-privilege inline policy attached to the CDK AwsCustomResource handler — "
            "scoped to specific rum:* actions on one monitor ARN; managed-policy reuse adds nothing"
        )
        for construct in (rum_metrics_destination, rum_extended_metrics):
            NagSuppressions.add_resource_suppressions(
                construct,
                [
                    {"id": "NIST.800.53.R5-IAMNoInlinePolicy", "reason": reason},
                    {"id": "HIPAA.Security-IAMNoInlinePolicy", "reason": reason},
                    {"id": "PCI.DSS.321-IAMNoInlinePolicy", "reason": reason},
                ],
                apply_to_children=True,
            )
        return rum_extended_metrics

    def _wire_rum_log_group_cleanup(
        self,
        rum_app_monitor: rum.CfnAppMonitor,
        rum_monitor_name: str,
        custom_resource_log_group: logs.LogGroup,
    ) -> None:
        """Delete the RUM-auto-created CloudWatch Logs group at stack destroy.

        CloudWatch RUM creates a log group at
        ``/aws/vendedlogs/RUMService_{monitor-name}{first-8-hex-of-monitor-id}``
        the first time it ingests an event. That log group is owned by this
        account but created outside CloudFormation, so ``cdk destroy`` deletes
        the AppMonitor without touching the log group — same dangling-resource
        shape as the Application Insights dashboard that
        ``AppInsightsDashboardCleanup`` in the backend stack solves.

        ``ResourceNotFoundException`` is ignored so destroy succeeds even when
        no events were ever ingested (the log group only materializes on the
        first event — common in CI / dev / no-traffic deploys).
        """
        monitor_id_prefix = Fn.select(0, Fn.split("-", rum_app_monitor.attr_id))
        log_group_name = Fn.join("", [f"/aws/vendedlogs/RUMService_{rum_monitor_name}", monitor_id_prefix])
        log_group_arn = Fn.join(
            "",
            [
                f"arn:{self.partition}:logs:{self.region}:{self.account}:log-group:/aws/vendedlogs/RUMService_{rum_monitor_name}",
                monitor_id_prefix,
                ":*",
            ],
        )
        cleanup = cr.AwsCustomResource(
            self,
            "RumLogGroupCleanup",
            on_delete=cr.AwsSdkCall(
                service="CloudWatchLogs",
                action="deleteLogGroup",
                parameters={"logGroupName": log_group_name},
                physical_resource_id=cr.PhysicalResourceId.of("RumLogGroupCleanup"),
                ignore_error_codes_matching="ResourceNotFoundException",
            ),
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(resources=[log_group_arn]),
            install_latest_aws_sdk=False,
            log_group=custom_resource_log_group,
        )
        # The implicit attr_id reference already forces this dependency at the
        # CFN level; add_dependency makes the intent visible in code.
        cleanup.node.add_dependency(rum_app_monitor)

        # Matches the IAMNoInlinePolicy suppression pattern on the other RUM CRs
        # in this stack — CDK generates the handler's policy inline.
        reason = (
            "Single least-privilege inline policy attached to the CDK AwsCustomResource handler — "
            "scoped to logs:DeleteLogGroup on one log-group ARN; managed-policy reuse adds nothing"
        )
        # AwsSolutions-IAM5 fires because the log-group ARN ends in `:*`, which
        # is the standard CloudWatch Logs log-stream wildcard required by every
        # log-group resource ARN per the IAM docs — there is no way to grant
        # logs:DeleteLogGroup on a log group without the `:*` suffix. The
        # resource is otherwise fully scoped to one specific log group (path
        # built from this monitor's runtime-resolved ID prefix), so the
        # wildcard portion only authorizes log-stream-scope wildcards within
        # that one log group, not across log groups.
        iam5_reason = (
            "Log-group ARN includes the standard :* log-stream wildcard suffix — required for any "
            "CloudWatch Logs resource ARN per the IAM service authorization docs. The resource is "
            "otherwise scoped to one specific log group built from the monitor's runtime-resolved ID."
        )
        NagSuppressions.add_resource_suppressions(
            cleanup,
            [
                {"id": "NIST.800.53.R5-IAMNoInlinePolicy", "reason": reason},
                {"id": "HIPAA.Security-IAMNoInlinePolicy", "reason": reason},
                {"id": "PCI.DSS.321-IAMNoInlinePolicy", "reason": reason},
                {"id": "AwsSolutions-IAM5", "reason": iam5_reason, "applies_to": ["Resource::*"]},
            ],
            apply_to_children=True,
        )

    def _create_athena_glue_resources(self, access_log_bucket: s3.Bucket, encryption_key: kms.Key) -> None:
        """Create Glue catalog tables and Athena workgroup for CloudFront/S3 access log analytics."""
        # ── Glue Database ────────────────────────────────────────────────
        # Glue database names: lowercase, alphanumeric + underscores only.
        db_name = self.node.id.lower().replace("-", "_") + "_access_logs"

        glue_db = glue.CfnDatabase(
            self,
            "AccessLogsDatabase",
            catalog_id=self.account,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name=db_name,
                description="Glue catalog for CloudFront and S3 access logs",
            ),
        )

        # ── CloudFront Standard Logs Table ───────────────────────────────
        # 33-field tab-separated format; 2 header lines (#Version, #Fields).
        # All columns typed as string — CloudFront uses '-' for missing values.
        cf_table = glue.CfnTable(
            self,
            "CloudFrontLogsTable",
            catalog_id=self.account,
            database_name=db_name,
            table_input=glue.CfnTable.TableInputProperty(
                name="cloudfront_logs",
                description="CloudFront standard access logs",
                table_type="EXTERNAL_TABLE",
                parameters={"skip.header.line.count": "2", "EXTERNAL": "TRUE"},
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    location=f"s3://{access_log_bucket.bucket_name}/cloudfront/",
                    input_format="org.apache.hadoop.mapred.TextInputFormat",
                    output_format="org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library="org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe",
                        parameters={"field.delim": "\t", "serialization.null.format": "-"},
                    ),
                    columns=[
                        glue.CfnTable.ColumnProperty(name="log_date", type="string"),
                        glue.CfnTable.ColumnProperty(name="log_time", type="string"),
                        glue.CfnTable.ColumnProperty(name="x_edge_location", type="string"),
                        glue.CfnTable.ColumnProperty(name="sc_bytes", type="string"),
                        glue.CfnTable.ColumnProperty(name="c_ip", type="string"),
                        glue.CfnTable.ColumnProperty(name="cs_method", type="string"),
                        glue.CfnTable.ColumnProperty(name="cs_host", type="string"),
                        glue.CfnTable.ColumnProperty(name="cs_uri_stem", type="string"),
                        glue.CfnTable.ColumnProperty(name="sc_status", type="string"),
                        glue.CfnTable.ColumnProperty(name="cs_referer", type="string"),
                        glue.CfnTable.ColumnProperty(name="cs_user_agent", type="string"),
                        glue.CfnTable.ColumnProperty(name="cs_uri_query", type="string"),
                        glue.CfnTable.ColumnProperty(name="cs_cookie", type="string"),
                        glue.CfnTable.ColumnProperty(name="x_edge_result_type", type="string"),
                        glue.CfnTable.ColumnProperty(name="x_edge_request_id", type="string"),
                        glue.CfnTable.ColumnProperty(name="x_host_header", type="string"),
                        glue.CfnTable.ColumnProperty(name="cs_protocol", type="string"),
                        glue.CfnTable.ColumnProperty(name="cs_bytes", type="string"),
                        glue.CfnTable.ColumnProperty(name="time_taken", type="string"),
                        glue.CfnTable.ColumnProperty(name="x_forwarded_for", type="string"),
                        glue.CfnTable.ColumnProperty(name="ssl_protocol", type="string"),
                        glue.CfnTable.ColumnProperty(name="ssl_cipher", type="string"),
                        glue.CfnTable.ColumnProperty(name="x_edge_response_result_type", type="string"),
                        glue.CfnTable.ColumnProperty(name="cs_protocol_version", type="string"),
                        glue.CfnTable.ColumnProperty(name="fle_status", type="string"),
                        glue.CfnTable.ColumnProperty(name="fle_encrypted_fields", type="string"),
                        glue.CfnTable.ColumnProperty(name="c_port", type="string"),
                        glue.CfnTable.ColumnProperty(name="time_to_first_byte", type="string"),
                        glue.CfnTable.ColumnProperty(name="x_edge_detailed_result_type", type="string"),
                        glue.CfnTable.ColumnProperty(name="sc_content_type", type="string"),
                        glue.CfnTable.ColumnProperty(name="sc_content_len", type="string"),
                        glue.CfnTable.ColumnProperty(name="sc_range_start", type="string"),
                        glue.CfnTable.ColumnProperty(name="sc_range_end", type="string"),
                    ],
                ),
            ),
        )
        cf_table.add_dependency(glue_db)

        # ── S3 Server Access Logs Table ──────────────────────────────────
        # 26-field format with quoted strings and optional trailing fields.
        # RegexSerDe handles the complex delimiter pattern reliably.
        s3_log_regex = (
            r"([^ ]*) ([^ ]*) \[(.*?)\] ([^ ]*) ([^ ]*) ([^ ]*) ([^ ]*) ([^ ]*) "
            r'("[^"]*"|-) (-|[0-9]*) ([^ ]*) ([^ ]*) ([^ ]*) ([^ ]*) ([^ ]*) '
            r'([^ ]*) ("[^"]*"|-) ([^ ]*)(?: ([^ ]*) ([^ ]*) ([^ ]*) ([^ ]*) '
            r"([^ ]*) ([^ ]*) ([^ ]*) ([^ ]*))?.*$"
        )
        s3_table = glue.CfnTable(
            self,
            "S3AccessLogsTable",
            catalog_id=self.account,
            database_name=db_name,
            table_input=glue.CfnTable.TableInputProperty(
                name="s3_access_logs",
                description="S3 server access logs",
                table_type="EXTERNAL_TABLE",
                parameters={"EXTERNAL": "TRUE"},
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    location=f"s3://{access_log_bucket.bucket_name}/s3-access-logs/",
                    input_format="org.apache.hadoop.mapred.TextInputFormat",
                    output_format="org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library="org.apache.hadoop.hive.serde2.RegexSerDe",
                        parameters={"input.regex": s3_log_regex},
                    ),
                    columns=[
                        glue.CfnTable.ColumnProperty(name="bucket_owner", type="string"),
                        glue.CfnTable.ColumnProperty(name="bucket_name", type="string"),
                        glue.CfnTable.ColumnProperty(name="request_datetime", type="string"),
                        glue.CfnTable.ColumnProperty(name="remote_ip", type="string"),
                        glue.CfnTable.ColumnProperty(name="requester", type="string"),
                        glue.CfnTable.ColumnProperty(name="request_id", type="string"),
                        glue.CfnTable.ColumnProperty(name="operation", type="string"),
                        glue.CfnTable.ColumnProperty(name="key", type="string"),
                        glue.CfnTable.ColumnProperty(name="request_uri", type="string"),
                        glue.CfnTable.ColumnProperty(name="http_status", type="string"),
                        glue.CfnTable.ColumnProperty(name="error_code", type="string"),
                        glue.CfnTable.ColumnProperty(name="bytes_sent", type="string"),
                        glue.CfnTable.ColumnProperty(name="object_size", type="string"),
                        glue.CfnTable.ColumnProperty(name="total_time", type="string"),
                        glue.CfnTable.ColumnProperty(name="turn_around_time", type="string"),
                        glue.CfnTable.ColumnProperty(name="referrer", type="string"),
                        glue.CfnTable.ColumnProperty(name="user_agent", type="string"),
                        glue.CfnTable.ColumnProperty(name="version_id", type="string"),
                        glue.CfnTable.ColumnProperty(name="host_id", type="string"),
                        glue.CfnTable.ColumnProperty(name="signature_version", type="string"),
                        glue.CfnTable.ColumnProperty(name="cipher_suite", type="string"),
                        glue.CfnTable.ColumnProperty(name="authentication_type", type="string"),
                        glue.CfnTable.ColumnProperty(name="host_header", type="string"),
                        glue.CfnTable.ColumnProperty(name="tls_version", type="string"),
                        glue.CfnTable.ColumnProperty(name="access_point_arn", type="string"),
                        glue.CfnTable.ColumnProperty(name="acl_required", type="string"),
                    ],
                ),
            ),
        )
        s3_table.add_dependency(glue_db)

        # ── Athena WorkGroup ─────────────────────────────────────────────
        # Query results stored in the access log bucket under athena-results/
        # encrypted with this stack's CMK. The bucket itself uses SSE-S3
        # because S3/CloudFront log delivery cannot write to a KMS-encrypted
        # bucket, but Athena PutObject calls can override the bucket default
        # on a per-object basis to use SSE-KMS for the query results.
        workgroup_name = f"{self.node.id}-access-logs"
        workgroup = athena.CfnWorkGroup(
            self,
            "AccessLogsWorkGroup",
            name=workgroup_name,
            state="ENABLED",
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{access_log_bucket.bucket_name}/athena-results/",
                    encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                        encryption_option="SSE_KMS",
                        kms_key=encryption_key.key_arn,
                    ),
                ),
                enforce_work_group_configuration=True,
                publish_cloud_watch_metrics_enabled=True,
                # Hard ceiling on bytes scanned per query — Athena cancels any query
                # that would exceed it, capping the per-query cost (Athena bills
                # $5/TB scanned) against a forgotten WHERE clause or an accidental
                # full-table scan. 1 GiB is generous for these access-log tables at
                # sample-app volume and the 7-day log retention; raise it in a fork
                # with larger log volumes or wider date-range queries. AWS enforces a
                # 10 MB floor on this field.
                bytes_scanned_cutoff_per_query=1024 * 1024 * 1024,
            ),
        )

        # ── Athena Named Queries — CloudFront ────────────────────────────
        # Each named query must wait for the workgroup to exist.
        nq_cf_top_uris = athena.CfnNamedQuery(
            self,
            "CfTopRequestedUris",
            database=db_name,
            work_group=workgroup_name,
            name="CloudFront - Top Requested URIs",
            description="Most frequently requested URIs with error counts",
            query_string="""\
SELECT cs_uri_stem, cs_method,
       COUNT(*) as request_count,
       COUNT(CASE WHEN sc_status LIKE '4%' OR sc_status LIKE '5%' THEN 1 END) as errors
FROM cloudfront_logs
GROUP BY cs_uri_stem, cs_method
ORDER BY request_count DESC
LIMIT 25""",
        )
        nq_cf_top_uris.add_dependency(workgroup)
        nq_cf_errors = athena.CfnNamedQuery(
            self,
            "CfErrorResponses",
            database=db_name,
            work_group=workgroup_name,
            name="CloudFront - Error Responses",
            description="Recent 4xx/5xx error responses with client and edge details",
            query_string="""\
SELECT log_date, log_time, c_ip, cs_method, cs_uri_stem, sc_status,
       x_edge_result_type, x_edge_detailed_result_type
FROM cloudfront_logs
WHERE sc_status LIKE '4%' OR sc_status LIKE '5%'
ORDER BY log_date DESC, log_time DESC
LIMIT 50""",
        )
        nq_cf_errors.add_dependency(workgroup)
        nq_cf_top_ips = athena.CfnNamedQuery(
            self,
            "CfTopClientIps",
            database=db_name,
            work_group=workgroup_name,
            name="CloudFront - Top Client IPs",
            description="Highest-traffic client IPs with error counts",
            query_string="""\
SELECT c_ip, COUNT(*) as request_count,
       COUNT(CASE WHEN sc_status LIKE '4%' OR sc_status LIKE '5%' THEN 1 END) as errors
FROM cloudfront_logs
GROUP BY c_ip
ORDER BY request_count DESC
LIMIT 25""",
        )
        nq_cf_top_ips.add_dependency(workgroup)
        nq_cf_bandwidth = athena.CfnNamedQuery(
            self,
            "CfBandwidthByEdge",
            database=db_name,
            work_group=workgroup_name,
            name="CloudFront - Bandwidth by Edge Location",
            description="Total bytes transferred per edge location",
            query_string="""\
SELECT x_edge_location, COUNT(*) as requests,
       SUM(CAST(sc_bytes AS bigint)) as bytes_out,
       SUM(CAST(cs_bytes AS bigint)) as bytes_in
FROM cloudfront_logs
GROUP BY x_edge_location
ORDER BY bytes_out DESC
LIMIT 25""",
        )
        nq_cf_bandwidth.add_dependency(workgroup)
        nq_cf_cache = athena.CfnNamedQuery(
            self,
            "CfCacheHitRatio",
            database=db_name,
            work_group=workgroup_name,
            name="CloudFront - Cache Hit Ratio",
            description="Request counts and percentages by edge result type (Hit/Miss/Error)",
            query_string="""\
SELECT x_edge_result_type, COUNT(*) as request_count,
       ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(), 2) as pct
FROM cloudfront_logs
GROUP BY x_edge_result_type
ORDER BY request_count DESC""",
        )
        nq_cf_cache.add_dependency(workgroup)

        # ── Athena Named Queries — S3 ────────────────────────────────────
        nq_s3_ops = athena.CfnNamedQuery(
            self,
            "S3TopOperations",
            database=db_name,
            work_group=workgroup_name,
            name="S3 - Top Operations",
            description="Most common S3 operations with error counts",
            query_string="""\
SELECT operation, COUNT(*) as op_count,
       COUNT(CASE WHEN http_status NOT IN ('200','204','206','304') THEN 1 END) as errors
FROM s3_access_logs
GROUP BY operation
ORDER BY op_count DESC
LIMIT 25""",
        )
        nq_s3_ops.add_dependency(workgroup)
        nq_s3_errors = athena.CfnNamedQuery(
            self,
            "S3ErrorRequests",
            database=db_name,
            work_group=workgroup_name,
            name="S3 - Error Requests",
            description="Recent failed S3 requests with error details",
            query_string="""\
SELECT request_datetime, remote_ip, requester, operation, key,
       request_uri, http_status, error_code
FROM s3_access_logs
WHERE http_status NOT IN ('200', '204', '206', '304', '-')
ORDER BY request_datetime DESC
LIMIT 50""",
        )
        nq_s3_errors.add_dependency(workgroup)
        nq_s3_requesters = athena.CfnNamedQuery(
            self,
            "S3TopRequesters",
            database=db_name,
            work_group=workgroup_name,
            name="S3 - Top Requesters",
            description="Highest-traffic S3 requesters with error counts",
            query_string="""\
SELECT remote_ip, requester, COUNT(*) as request_count,
       COUNT(CASE WHEN http_status NOT IN ('200','204','206','304') THEN 1 END) as errors
FROM s3_access_logs
GROUP BY remote_ip, requester
ORDER BY request_count DESC
LIMIT 25""",
        )
        nq_s3_requesters.add_dependency(workgroup)
        nq_s3_slow = athena.CfnNamedQuery(
            self,
            "S3SlowRequests",
            database=db_name,
            work_group=workgroup_name,
            name="S3 - Slow Requests",
            description="Highest-latency S3 requests by total_time (ms)",
            query_string="""\
SELECT request_datetime, remote_ip, operation, key, http_status,
       total_time, turn_around_time, bytes_sent
FROM s3_access_logs
WHERE total_time != '-'
ORDER BY CAST(total_time AS integer) DESC
LIMIT 50""",
        )
        nq_s3_slow.add_dependency(workgroup)
        nq_s3_access_denied = athena.CfnNamedQuery(
            self,
            "S3AccessDenied",
            database=db_name,
            work_group=workgroup_name,
            name="S3 - Access Denied (403)",
            description="Recent 403 AccessDenied responses with requester and operation details",
            query_string="""\
SELECT request_datetime, remote_ip, requester, operation, key,
       request_uri, error_code
FROM s3_access_logs
WHERE http_status = '403'
ORDER BY request_datetime DESC
LIMIT 50""",
        )
        nq_s3_access_denied.add_dependency(workgroup)
        nq_s3_object_reads = athena.CfnNamedQuery(
            self,
            "S3ObjectReads",
            database=db_name,
            work_group=workgroup_name,
            name="S3 - Object Read Audit",
            description="Who read which object (GET.OBJECT operations) with status and bytes",
            query_string="""\
SELECT request_datetime, remote_ip, requester, key,
       http_status, bytes_sent, user_agent
FROM s3_access_logs
WHERE operation LIKE '%GET.OBJECT%'
ORDER BY request_datetime DESC
LIMIT 100""",
        )
        nq_s3_object_reads.add_dependency(workgroup)

        # ── Outputs ──────────────────────────────────────────────────────
        CfnOutput(
            self,
            "GlueDatabaseName",
            description="Glue catalog database for CloudFront and S3 access log analytics",
            value=db_name,
        )
        CfnOutput(
            self,
            "AthenaWorkGroupName",
            description="Athena workgroup for querying access logs",
            value=workgroup_name,
        )
