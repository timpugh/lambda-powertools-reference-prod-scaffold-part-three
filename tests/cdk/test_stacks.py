"""CDK stack assertion tests.

These tests synthesize each CDK stack in-process using ``aws_cdk.assertions.Template``
and verify that key security properties are correctly configured. They serve as a
regression guard — if a construct property is accidentally removed or changed (e.g.,
KMS encryption dropped from DynamoDB, PITR disabled, CloudFront TLS downgraded),
the test fails immediately at synthesis time rather than silently deploying an
insecure template.

These tests run the cdk-nag Aspects (all five rule packs wired by
``nag_utils.apply_compliance_aspects``: AwsSolutions, Serverless, NIST 800-53 R5,
HIPAA Security, PCI DSS 3.2.1) because each stack constructor attaches them — but
they assert only on resource properties and logical IDs. ``Template.from_stack``
does **not** raise on cdk-nag Aspect errors, so a clean ``pytest tests/cdk`` run
is NOT a guarantee that cdk-nag passes. The hard nag gate is the CLI
``cdk synth '**'`` step in the ``cdk-check`` CI job (and ``make cdk-synth``
locally), which does fail on unsuppressed findings. See CLAUDE.md
"Critical local-vs-CI gap" for the full explanation.

Asset bundling (Docker) is skipped via the ``aws:cdk:bundling-stacks`` context key
so these tests run without Docker.

The ``aws_cdk`` package is only installed in the CDK check CI job, not in the
regular unit-test environment. All tests in this module are skipped automatically
when ``aws_cdk`` is not importable, so the standard ``pytest tests/unit`` run stays
clean.
"""

import json

import pytest

aws_cdk = pytest.importorskip("aws_cdk", reason="aws_cdk not installed — skipping CDK stack tests")

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from hello_world.hello_world_frontend_stack import HelloWorldFrontendStack
from hello_world.hello_world_stack import HelloWorldStack
from hello_world.hello_world_waf_stack import HelloWorldWafStack

# Fake account/region — synthesis does not make live AWS API calls
_TEST_ACCOUNT = "123456789012"
_TEST_REGION = "us-east-1"
_TEST_ENV = cdk.Environment(account=_TEST_ACCOUNT, region=_TEST_REGION)
_WAF_ENV = cdk.Environment(account=_TEST_ACCOUNT, region="us-east-1")

# Skip Docker bundling so these tests run without Docker.
# The CDK CLI and Python SDK both honour this context key during synthesis.
_NO_BUNDLING = {"aws:cdk:bundling-stacks": []}


# ── Module-scoped fixtures ────────────────────────────────────────────────────
# Each stack is synthesized once per test module (scope="module") to keep the
# suite fast — all assertions in this file share one synthesis per stack.


@pytest.fixture(scope="module")
def waf_template() -> Template:
    """Synthesize HelloWorldWafStack and return its CloudFormation template."""
    app = cdk.App(context=_NO_BUNDLING)
    stack = HelloWorldWafStack(app, "TestWafStack", env=_WAF_ENV)
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def backend_template() -> Template:
    """Synthesize HelloWorldStack and return its CloudFormation template."""
    app = cdk.App(context=_NO_BUNDLING)
    stack = HelloWorldStack(app, "TestBackendStack", env=_TEST_ENV)
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def frontend_template() -> Template:
    """Synthesize HelloWorldFrontendStack and return its CloudFormation template."""
    app = cdk.App(context=_NO_BUNDLING)
    waf = HelloWorldWafStack(app, "TestFrontendWaf", env=_WAF_ENV)
    backend = HelloWorldStack(app, "TestFrontendBackend", env=_TEST_ENV)
    stack = HelloWorldFrontendStack(
        app,
        "TestFrontendStack",
        api_url=backend.api_url,
        api_id=backend.api_id,
        waf_acl_arn=waf.web_acl_arn,
        env=_TEST_ENV,
        cross_region_references=True,
    )
    return Template.from_stack(stack)


# ── WAF stack ─────────────────────────────────────────────────────────────────


class TestWafStack:
    def test_webacl_is_cloudfront_scoped(self, waf_template: Template) -> None:
        waf_template.has_resource_properties("AWS::WAFv2::WebACL", {"Scope": "CLOUDFRONT"})

    def test_logging_configuration_exists(self, waf_template: Template) -> None:
        waf_template.resource_count_is("AWS::WAFv2::LoggingConfiguration", 1)

    def test_kms_key_has_rotation_enabled(self, waf_template: Template) -> None:
        waf_template.has_resource_properties("AWS::KMS::Key", {"EnableKeyRotation": True})

    def test_log_group_has_kms_encryption(self, waf_template: Template) -> None:
        waf_template.has_resource_properties(
            "AWS::Logs::LogGroup",
            {"KmsKeyId": Match.any_value(), "RetentionInDays": Match.any_value()},
        )

    def test_webacl_has_rate_limiting_rule(self, waf_template: Template) -> None:
        # Assert the security-critical properties, not just the rule name: a
        # regression flipping FORWARDED_IP→IP, MATCH→NO_MATCH, or Block→Count would
        # silently weaken the per-client rate limit and cdk-nag does not check these.
        waf_template.has_resource_properties(
            "AWS::WAFv2::WebACL",
            {
                "Rules": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Name": "RateLimitPerIP",
                                "Action": {"Block": {}},
                                "Statement": {
                                    "RateBasedStatement": Match.object_like(
                                        {
                                            "Limit": 200,
                                            "AggregateKeyType": "FORWARDED_IP",
                                            "ForwardedIPConfig": {
                                                "HeaderName": "X-Forwarded-For",
                                                "FallbackBehavior": "MATCH",
                                            },
                                        }
                                    )
                                },
                            }
                        )
                    ]
                )
            },
        )

    def test_webacl_logging_targets_waf_log_group(self, waf_template: Template) -> None:
        # The LoggingConfiguration must reference an aws-waf-logs-* group and the WebACL.
        waf_template.resource_count_is("AWS::WAFv2::LoggingConfiguration", 1)
        waf_template.has_resource_properties(
            "AWS::WAFv2::LoggingConfiguration",
            {
                "LogDestinationConfigs": Match.any_value(),
                "ResourceArn": Match.any_value(),
            },
        )

    def test_webacl_has_managed_rule_sets(self, waf_template: Template) -> None:
        waf_template.has_resource_properties(
            "AWS::WAFv2::WebACL",
            {
                "Rules": Match.array_with(
                    [
                        Match.object_like({"Name": "AWSManagedRulesAmazonIpReputationList"}),
                        Match.object_like({"Name": "AWSManagedRulesCommonRuleSet"}),
                        Match.object_like({"Name": "AWSManagedRulesKnownBadInputsRuleSet"}),
                        Match.object_like({"Name": "AWSManagedRulesAnonymousIpList"}),
                    ]
                )
            },
        )

    def test_stack_outputs_exist(self, waf_template: Template) -> None:
        waf_template.has_output("WebAclArn", {})
        waf_template.has_output("WebAclId", {})
        waf_template.has_output("WafLogGroupName", {})


# ── Backend stack ─────────────────────────────────────────────────────────────


class TestBackendStack:
    def test_kms_key_has_rotation_enabled(self, backend_template: Template) -> None:
        backend_template.has_resource_properties("AWS::KMS::Key", {"EnableKeyRotation": True})

    def test_dynamodb_has_pitr_enabled(self, backend_template: Template) -> None:
        backend_template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {"PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True}},
        )

    def test_dynamodb_has_kms_encryption(self, backend_template: Template) -> None:
        backend_template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {"SSESpecification": {"SSEEnabled": True}},
        )

    def test_lambda_has_active_tracing(self, backend_template: Template) -> None:
        backend_template.has_resource_properties(
            "AWS::Lambda::Function",
            {"TracingConfig": {"Mode": "Active"}, "MemorySize": 256},
        )

    def test_lambda_has_reserved_concurrency(self, backend_template: Template) -> None:
        # Reserved concurrency bounds blast radius and retires the
        # NIST/HIPAA LambdaConcurrency cdk-nag suppressions.
        backend_template.has_resource_properties(
            "AWS::Lambda::Function",
            {"ReservedConcurrentExecutions": 100},
        )

    def test_api_gateway_stage_has_throttling(self, backend_template: Template) -> None:
        # Stage-level throttling retires the Serverless-APIGWDefaultThrottling
        # suppression. Asserted via MethodSettings on the wildcard path/method.
        backend_template.has_resource_properties(
            "AWS::ApiGateway::Stage",
            {
                "MethodSettings": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "ThrottlingRateLimit": 100,
                                "ThrottlingBurstLimit": 200,
                                "HttpMethod": "*",
                                "ResourcePath": "/*",
                            }
                        )
                    ]
                )
            },
        )

    def test_api_gateway_has_regional_waf(self, backend_template: Template) -> None:
        # A REGIONAL WebACL is associated with the Prod stage to close the
        # execute-api CloudFront-bypass window. Retires the APIG3 /
        # APIGWAssociatedWithWAF (NIST/PCI) suppressions.
        backend_template.has_resource_properties("AWS::WAFv2::WebACL", {"Scope": "REGIONAL"})
        backend_template.resource_count_is("AWS::WAFv2::WebACLAssociation", 1)

    def test_regional_waf_has_logging(self, backend_template: Template) -> None:
        # WAFv2LoggingEnabled (NIST/HIPAA/PCI) requires logging on the ACL.
        # The regional ACL writes to a CMK-encrypted aws-waf-logs-* group.
        backend_template.resource_count_is("AWS::WAFv2::LoggingConfiguration", 1)
        backend_template.has_resource_properties(
            "AWS::Logs::LogGroup",
            {"LogGroupName": Match.string_like_regexp("^aws-waf-logs-"), "KmsKeyId": Match.any_value()},
        )

    def test_regional_waf_has_managed_rule_sets(self, backend_template: Template) -> None:
        # The regional ACL mirrors the four managed threat rule groups from the
        # CloudFront ACL (shared via build_managed_threat_rules), and omits the
        # rate-based rule by design.
        backend_template.has_resource_properties(
            "AWS::WAFv2::WebACL",
            {
                "Scope": "REGIONAL",
                "Rules": Match.array_with(
                    [
                        Match.object_like({"Name": "AWSManagedRulesAmazonIpReputationList"}),
                        Match.object_like({"Name": "AWSManagedRulesCommonRuleSet"}),
                        Match.object_like({"Name": "AWSManagedRulesKnownBadInputsRuleSet"}),
                        Match.object_like({"Name": "AWSManagedRulesAnonymousIpList"}),
                    ]
                ),
            },
        )

    def test_api_gateway_cache_cluster_disabled(self, backend_template: Template) -> None:
        # Cache cluster is intentionally disabled for cost (~$14/mo for the smallest size)
        # and to avoid serving stale values across SSM/AppConfig changes — see the
        # NIST.800.53.R5-APIGWCacheEnabledAndEncrypted suppression in HelloWorldStack.
        stages = backend_template.find_resources("AWS::ApiGateway::Stage")
        assert stages, "expected at least one API Gateway stage"
        for stage in stages.values():
            # Must be absent or explicitly False — `in (None, False)` also rejects a
            # CloudFormation token that could resolve truthy, which `is not True` would
            # have let slip through.
            assert stage["Properties"].get("CacheClusterEnabled") in (None, False)

    def test_log_groups_have_kms_encryption(self, backend_template: Template) -> None:
        backend_template.has_resource_properties(
            "AWS::Logs::LogGroup",
            {"KmsKeyId": Match.any_value(), "RetentionInDays": Match.any_value()},
        )

    def test_stack_outputs_exist(self, backend_template: Template) -> None:
        backend_template.has_output("HelloWorldApiOutput", {})
        backend_template.has_output("HelloWorldFunctionOutput", {})
        backend_template.has_output("IdempotencyTableName", {})
        backend_template.has_output("GreetingParameterName", {})
        backend_template.has_output("CloudWatchDashboardUrl", {})


# ── Frontend stack ────────────────────────────────────────────────────────────


class TestFrontendStack:
    def test_kms_key_has_rotation_enabled(self, frontend_template: Template) -> None:
        frontend_template.has_resource_properties("AWS::KMS::Key", {"EnableKeyRotation": True})

    def test_frontend_bucket_has_kms_encryption(self, frontend_template: Template) -> None:
        frontend_template.has_resource_properties(
            "AWS::S3::Bucket",
            {
                "BucketEncryption": {
                    "ServerSideEncryptionConfiguration": Match.array_with(
                        [Match.object_like({"ServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"}})]
                    )
                }
            },
        )

    def test_access_log_bucket_uses_s3_managed_encryption(self, frontend_template: Template) -> None:
        # Access log bucket must use SSE-S3 (S3 log delivery cannot write to KMS-encrypted targets)
        frontend_template.has_resource_properties(
            "AWS::S3::Bucket",
            {
                "BucketEncryption": {
                    "ServerSideEncryptionConfiguration": Match.array_with(
                        [Match.object_like({"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}})]
                    )
                }
            },
        )

    def test_cloudfront_has_waf_attached(self, frontend_template: Template) -> None:
        # WebACLId is set from the WAF stack — confirms cross-stack wiring is intact
        frontend_template.has_resource_properties(
            "AWS::CloudFront::Distribution",
            {"DistributionConfig": {"WebACLId": Match.any_value()}},
        )

    def test_cloudfront_redirects_http_to_https(self, frontend_template: Template) -> None:
        frontend_template.has_resource_properties(
            "AWS::CloudFront::Distribution",
            {"DistributionConfig": {"DefaultCacheBehavior": {"ViewerProtocolPolicy": "redirect-to-https"}}},
        )

    def test_cf_invalidation_caller_reference_is_single_object_key(self, frontend_template: Template) -> None:
        # CloudFront caps CallerReference at 128 chars. The content-hashed object key
        # alone (~68 chars) fits; folding in api_url or other ids blows the limit and
        # fails at deploy time (CFN custom-resource CREATE_FAILED) — something synth /
        # Template.from_stack does NOT catch. Pin it to the single Fn::Select on the
        # BucketDeployment object keys so a regression is caught here.
        #
        # The Create property is a JSON string OR a dict (Fn::Join, since it embeds
        # the distribution Ref), so serialize defensively (default=str tolerates
        # CDK tokens) and assert on the CallerReference fragment within it.
        crs = frontend_template.find_resources("Custom::AWS")
        creates = [json.dumps(r["Properties"].get("Create"), default=str) for r in crs.values()]
        invalidations = [c for c in creates if "createInvalidation" in c]
        assert len(invalidations) == 1, "expected exactly one CloudFront invalidation custom resource"
        blob = invalidations[0]
        # The invalidation Create blob is itself an Fn::Join (distribution Ref), so we
        # check the CallerReference value specifically: it must come from SourceObjectKeys
        # via Fn::Select, and must NOT introduce additional joined string parts beyond the
        # object key (which would happen if api_url etc. were concatenated in).
        assert "SourceObjectKeys" in blob, "CallerReference should derive from the BucketDeployment object keys"
        # Guard against the 128-char regression: api_url is an execute-api URL; it must
        # not appear anywhere in the invalidation call (it was wrongly folded into
        # CallerReference once and overflowed the limit).
        assert "execute-api" not in blob, (
            "api_url must not be concatenated into the invalidation CallerReference (128-char CloudFront limit)"
        )

    def test_response_headers_policy_sets_hsts_and_csp(self, frontend_template: Template) -> None:
        # Custom ResponseHeadersPolicy adds HSTS + CSP on top of the four headers
        # the AWS-managed SECURITY_HEADERS policy provided. Assert both security
        # headers the managed policy omitted are present and overriding. The CSP
        # value is an Fn::Join (it interpolates the API Gateway id token to pin the
        # exact execute-api host), so assert the joined fragments rather than a
        # plain string: the leading fragment carries default-src + script-src, and
        # a later fragment carries the pinned execute-api host (no `*` wildcard).
        frontend_template.has_resource_properties(
            "AWS::CloudFront::ResponseHeadersPolicy",
            {
                "ResponseHeadersPolicyConfig": {
                    "SecurityHeadersConfig": Match.object_like(
                        {
                            "StrictTransportSecurity": Match.object_like(
                                {
                                    "AccessControlMaxAgeSec": 31536000,
                                    "IncludeSubdomains": True,
                                    "Override": True,
                                }
                            ),
                            "ContentSecurityPolicy": Match.object_like(
                                {
                                    "ContentSecurityPolicy": {
                                        "Fn::Join": [
                                            "",
                                            Match.array_with(
                                                [
                                                    Match.string_like_regexp("default-src 'self'"),
                                                    Match.string_like_regexp(r"\.execute-api\."),
                                                ]
                                            ),
                                        ]
                                    },
                                    "Override": True,
                                }
                            ),
                        }
                    )
                }
            },
        )

    def test_csp_pins_exact_api_host_not_wildcard(self, frontend_template: Template) -> None:
        # F90: connect-src must target this API's exact host ({id}.execute-api...),
        # not `*.execute-api...` which would match every API in the region/account.
        policies = frontend_template.find_resources("AWS::CloudFront::ResponseHeadersPolicy")
        (policy,) = policies.values()
        csp = policy["Properties"]["ResponseHeadersPolicyConfig"]["SecurityHeadersConfig"]["ContentSecurityPolicy"][
            "ContentSecurityPolicy"
        ]
        # csp is an Fn::Join; flatten its string fragments and assert no wildcard host.
        fragments = [p for p in csp["Fn::Join"][1] if isinstance(p, str)]
        joined = " ".join(fragments)
        assert "*.execute-api." not in joined, "CSP connect-src must pin the exact API host, not a wildcard"
        assert ".execute-api." in joined

    def test_distribution_uses_custom_response_headers_policy(self, frontend_template: Template) -> None:
        # The distribution must reference our policy, not the AWS-managed one.
        policies = frontend_template.find_resources("AWS::CloudFront::ResponseHeadersPolicy")
        assert len(policies) == 1, "expected exactly one custom ResponseHeadersPolicy"
        frontend_template.has_resource_properties(
            "AWS::CloudFront::Distribution",
            {"DistributionConfig": {"DefaultCacheBehavior": {"ResponseHeadersPolicyId": Match.any_value()}}},
        )

    def test_rum_server_side_telemetries_pinned(self, frontend_template: Template) -> None:
        # The server-side RUM telemetries list intentionally diverges from the
        # client-side list in frontend/index.html (CloudFormation rejects
        # "interaction"). Pin the server list so an accidental edit — e.g. dropping
        # "http", which silently degrades vended HTTP metrics — is caught in CI.
        # See the "do not sync these lists" comment in _wire_rum_metrics / the RUM
        # AppMonitor block of hello_world_frontend_stack.py.
        frontend_template.has_resource_properties(
            "AWS::RUM::AppMonitor",
            {"AppMonitorConfiguration": Match.object_like({"Telemetries": ["errors", "performance", "http"]})},
        )

    def test_auto_delete_log_group_is_created(self, frontend_template: Template) -> None:
        # Regression guard: the S3 auto-delete singleton's log group must be owned
        # by CDK so it doesn't dangle after cdk destroy. The provider synthesizes as
        # CustomResourceProviderBase (not the narrower CustomResourceProvider), and
        # matching only the subclass previously skipped this group silently. The
        # group is the one whose name is built from the provider's service_token
        # (/aws/lambda/<fn-name> via Fn::Join), so assert exactly one such group
        # exists with CMK encryption + retention.
        log_groups = frontend_template.find_resources("AWS::Logs::LogGroup")
        auto_delete_groups = [
            lg
            for lg in log_groups.values()
            if isinstance(lg["Properties"].get("LogGroupName"), dict) and "Fn::Join" in lg["Properties"]["LogGroupName"]
        ]
        assert len(auto_delete_groups) == 1, (
            "expected exactly one auto-delete provider log group (name built via Fn::Join from the "
            "provider service_token) — the type guard in _create_auto_delete_log_group likely no "
            "longer matches the CDK provider class, so the group is being skipped"
        )
        props = auto_delete_groups[0]["Properties"]
        assert "KmsKeyId" in props
        assert "RetentionInDays" in props

    def test_three_s3_buckets_exist(self, frontend_template: Template) -> None:
        # FrontendBucket + FrontendAccessLogBucket + CloudTrailLogsBucket
        frontend_template.resource_count_is("AWS::S3::Bucket", 3)

    def test_stack_outputs_exist(self, frontend_template: Template) -> None:
        frontend_template.has_output("CloudFrontDomainName", {})
        frontend_template.has_output("CloudFrontDistributionId", {})
        frontend_template.has_output("FrontendBucketName", {})


# ── Logical ID stability for stateful resources ───────────────────────────────
# CDK best practice: never let the logical ID of a stateful resource drift.
# A changed logical ID makes CloudFormation replace the resource — which for
# a DynamoDB table, S3 bucket, KMS key, or CloudFront distribution means data
# loss, downtime, or both. These tests lock in the current logical IDs so any
# refactor that would silently rename one (e.g., moving a construct, renaming
# a variable) fails at test time instead of at deploy time.
#
# If you genuinely need to change one of these IDs, use ``CfnResource.overrideLogicalId``
# to preserve the old name, or accept replacement and update this test in the
# same commit so the intent is reviewable.


class TestLogicalIdStability:
    """Lock in logical IDs of stateful resources — changing one replaces the resource."""

    # ── Backend ────────────────────────────────────────────────────────────────

    def test_backend_dynamodb_table_id(self, backend_template: Template) -> None:
        assert "AppIdempotencyTable7A3F72D5" in backend_template.find_resources("AWS::DynamoDB::Table")

    def test_backend_kms_key_id(self, backend_template: Template) -> None:
        assert "AppEncryptionKey7F644894" in backend_template.find_resources("AWS::KMS::Key")

    def test_backend_ssm_parameter_id(self, backend_template: Template) -> None:
        assert "AppGreetingParameterD5E6E64F" in backend_template.find_resources("AWS::SSM::Parameter")

    def test_backend_appconfig_application_id(self, backend_template: Template) -> None:
        assert "AppFeatureFlagsAppD0EAAC11" in backend_template.find_resources("AWS::AppConfig::Application")

    def test_backend_appconfig_environment_id(self, backend_template: Template) -> None:
        assert "AppFeatureFlagsEnvBF21F0D3" in backend_template.find_resources("AWS::AppConfig::Environment")

    def test_backend_appconfig_profile_id(self, backend_template: Template) -> None:
        assert "AppFeatureFlagsProfile324F0464" in backend_template.find_resources(
            "AWS::AppConfig::ConfigurationProfile"
        )

    def test_backend_log_group_ids(self, backend_template: Template) -> None:
        log_groups = backend_template.find_resources("AWS::Logs::LogGroup")
        assert "AppHelloWorldFunctionLogGroupD773BE34" in log_groups
        assert "AppHelloWorldApiAccessLogsBAD11F8B" in log_groups
        assert "AppHelloWorldApiExecutionLogsA5806940" in log_groups

    # ── Frontend ───────────────────────────────────────────────────────────────

    def test_frontend_kms_key_id(self, frontend_template: Template) -> None:
        assert "FrontendEncryptionKey272BB0CA" in frontend_template.find_resources("AWS::KMS::Key")

    def test_frontend_bucket_ids(self, frontend_template: Template) -> None:
        buckets = frontend_template.find_resources("AWS::S3::Bucket")
        assert "FrontendBucketEFE2E19C" in buckets
        assert "FrontendAccessLogBucketD05E8E55" in buckets

    def test_frontend_cloudfront_distribution_id(self, frontend_template: Template) -> None:
        assert "Distribution830FAC52" in frontend_template.find_resources("AWS::CloudFront::Distribution")

    # ── WAF ────────────────────────────────────────────────────────────────────

    def test_waf_kms_key_id(self, waf_template: Template) -> None:
        assert "WafEncryptionKeyB025E51A" in waf_template.find_resources("AWS::KMS::Key")

    def test_waf_log_group_id(self, waf_template: Template) -> None:
        assert "WafLogGroupDFDE65B0" in waf_template.find_resources("AWS::Logs::LogGroup")

    def test_waf_webacl_id(self, waf_template: Template) -> None:
        # L1 CfnWebACL — its logical ID is the construct_id with no hash suffix.
        assert "WebACL" in waf_template.find_resources("AWS::WAFv2::WebACL")
