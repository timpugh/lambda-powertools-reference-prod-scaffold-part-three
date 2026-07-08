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

from infrastructure.audit_stack import AuditStack
from infrastructure.backend_stack import BackendStack
from infrastructure.data_stack import DataStack
from infrastructure.frontend_stack import FrontendStack
from infrastructure.waf_stack import WafStack

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
    """Synthesize WafStack and return its CloudFormation template."""
    app = cdk.App(context=_NO_BUNDLING)
    stack = WafStack(app, "TestWafStack", env=_WAF_ENV)
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def data_template() -> Template:
    """Synthesize DataStack (default destroy-friendly shape)."""
    app = cdk.App(context=_NO_BUNDLING)
    stack = DataStack(app, "TestDataStack", env=_TEST_ENV)
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def data_template_retained() -> Template:
    """Synthesize DataStack with retain_data=True (production shape)."""
    app = cdk.App(context=_NO_BUNDLING)
    stack = DataStack(app, "TestDataStackRetained", retain_data=True, env=_TEST_ENV)
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def backend_template() -> Template:
    """Synthesize BackendStack and return its CloudFormation template."""
    app = cdk.App(context=_NO_BUNDLING)
    data = DataStack(app, "TestBackendData", env=_TEST_ENV)
    stack = BackendStack(app, "TestBackendStack", idempotency_table=data.idempotency_table, env=_TEST_ENV)
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def frontend_template() -> Template:
    """Synthesize FrontendStack and return its CloudFormation template."""
    app = cdk.App(context=_NO_BUNDLING)
    waf = WafStack(app, "TestFrontendWaf", env=_WAF_ENV)
    data = DataStack(app, "TestFrontendData", env=_TEST_ENV)
    backend = BackendStack(app, "TestFrontendBackend", idempotency_table=data.idempotency_table, env=_TEST_ENV)
    stack = FrontendStack(
        app,
        "TestFrontendStack",
        api_url=backend.api_url,
        api_id=backend.api_id,
        waf_acl_arn=waf.web_acl_arn,
        cf_waf_logs_location="s3://aws-waf-logs-123456789012-deadbeef0001-cf/AWSLogs/123456789012/WAFLogs/cloudfront/test-cf/",
        regional_waf_logs_location="s3://aws-waf-logs-123456789012-deadbeef0002-api/AWSLogs/123456789012/WAFLogs/us-east-1/test-api/",
        env=_TEST_ENV,
        cross_region_references=True,
    )
    return Template.from_stack(stack)


def _build_frontend(app: cdk.App, stack_suffix: str) -> FrontendStack:
    """Build the waf->data->backend->frontend chain and return the frontend stack.

    The audit stack audits the frontend buckets, so its tests need a real
    frontend whose ``bucket`` / ``access_log_bucket`` they can pass in.
    """
    waf = WafStack(app, f"{stack_suffix}Waf", env=_WAF_ENV)
    data = DataStack(app, f"{stack_suffix}Data", env=_TEST_ENV)
    backend = BackendStack(app, f"{stack_suffix}Backend", idempotency_table=data.idempotency_table, env=_TEST_ENV)
    return FrontendStack(
        app,
        f"{stack_suffix}Frontend",
        api_url=backend.api_url,
        api_id=backend.api_id,
        waf_acl_arn=waf.web_acl_arn,
        cf_waf_logs_location="s3://aws-waf-logs-123456789012-deadbeef0001-cf/AWSLogs/123456789012/WAFLogs/cloudfront/test-cf/",
        regional_waf_logs_location="s3://aws-waf-logs-123456789012-deadbeef0002-api/AWSLogs/123456789012/WAFLogs/us-east-1/test-api/",
        env=_TEST_ENV,
        cross_region_references=True,
    )


@pytest.fixture(scope="module")
def audit_template() -> Template:
    """Synthesize AuditStack (default destroy-friendly shape)."""
    app = cdk.App(context=_NO_BUNDLING)
    frontend = _build_frontend(app, "TestAudit")
    stack = AuditStack(
        app,
        "TestAuditStack",
        audited_buckets=[frontend.bucket, frontend.access_log_bucket],
        env=_TEST_ENV,
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def audit_template_retained() -> Template:
    """Synthesize AuditStack with retain_data=True (production shape)."""
    app = cdk.App(context=_NO_BUNDLING)
    frontend = _build_frontend(app, "TestAuditRetained")
    stack = AuditStack(
        app,
        "TestAuditStackRetained",
        audited_buckets=[frontend.bucket, frontend.access_log_bucket],
        retain_data=True,
        env=_TEST_ENV,
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
        # regression flipping IP→FORWARDED_IP or Block→Count would silently
        # disable the per-client rate limit and cdk-nag does not check these.
        # A CLOUDFRONT-scoped ACL inspects the *viewer* request, where the
        # source IP already is the real client IP and X-Forwarded-For is
        # normally absent — and per the WAF docs a missing header means the
        # rule is skipped entirely (fallback behavior never fires), so a
        # FORWARDED_IP aggregation here would make the rule a no-op.
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
                                            "AggregateKeyType": "IP",
                                        }
                                    )
                                },
                            }
                        )
                    ]
                )
            },
        )
        # Belt-and-braces: no rule in the edge ACL may carry a ForwardedIPConfig.
        for acl in waf_template.find_resources("AWS::WAFv2::WebACL").values():
            assert "ForwardedIPConfig" not in json.dumps(acl["Properties"].get("Rules", []), default=str)

    def test_webacl_logging_targets_s3_bucket(self, waf_template: Template) -> None:
        # WAF logs go to an aws-waf-logs-* S3 bucket (not CloudWatch). The
        # LoggingConfiguration destination must be the bucket ARN, and the bucket
        # must carry the delivery.logs.amazonaws.com write policy.
        waf_template.resource_count_is("AWS::WAFv2::LoggingConfiguration", 1)
        configs = waf_template.find_resources("AWS::WAFv2::LoggingConfiguration")
        (config,) = configs.values()
        destination = json.dumps(config["Properties"]["LogDestinationConfigs"][0], default=str)
        assert "WafLogsBucket" in destination, "WAF logging destination must be the S3 bucket ARN"
        # Bucket name must start with aws-waf-logs- (AWS requirement).
        buckets = waf_template.find_resources("AWS::S3::Bucket")
        names = json.dumps([b["Properties"].get("BucketName") for b in buckets.values()], default=str)
        assert "aws-waf-logs-" in names, "WAF log bucket name must start with aws-waf-logs-"

    def test_waf_log_bucket_has_delivery_policy(self, waf_template: Template) -> None:
        # WAF→S3 needs the delivery service principal granted write + ACL-check.
        waf_template.has_resource_properties(
            "AWS::S3::BucketPolicy",
            {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Principal": {"Service": "delivery.logs.amazonaws.com"},
                                    "Action": "s3:PutObject",
                                }
                            )
                        ]
                    )
                }
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
        waf_template.has_output("WafLogsBucketName", {})


# ── Backend stack ─────────────────────────────────────────────────────────────


class TestDataStack:
    """Stateful data layer: DynamoDB idempotency table + its dedicated CMK.

    These properties moved out of the backend stack when the stateful
    resources were isolated into DataStack (CDK best practice:
    keep stateful resources in their own stack).
    """

    def test_kms_key_has_rotation_enabled(self, data_template: Template) -> None:
        data_template.has_resource_properties("AWS::KMS::Key", {"EnableKeyRotation": True})

    def test_dynamodb_has_pitr_enabled(self, data_template: Template) -> None:
        # TableV2 synthesizes AWS::DynamoDB::GlobalTable, where PITR (and the
        # 1-day recovery window — shortest allowed; the cache's records TTL out
        # after an hour, so longer PITR retention is pure storage cost) lives
        # per-replica rather than at the table level.
        data_template.has_resource_properties(
            "AWS::DynamoDB::GlobalTable",
            {
                "Replicas": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "PointInTimeRecoverySpecification": {
                                    "PointInTimeRecoveryEnabled": True,
                                    "RecoveryPeriodInDays": 1,
                                }
                            }
                        )
                    ]
                )
            },
        )

    def test_dynamodb_has_kms_encryption(self, data_template: Template) -> None:
        # GlobalTable splits encryption across a table-level SSESpecification
        # (algorithm) and a per-replica KMSMasterKeyId (the CMK). Assert both —
        # SSEType KMS alone would also pass with an AWS-owned key.
        data_template.has_resource_properties(
            "AWS::DynamoDB::GlobalTable",
            {
                "SSESpecification": {"SSEEnabled": True, "SSEType": "KMS"},
                "Replicas": Match.array_with(
                    [Match.object_like({"SSESpecification": {"KMSMasterKeyId": Match.any_value()}})]
                ),
            },
        )

    def test_dynamodb_contributor_insights_throttled_keys_mode(self, data_template: Template) -> None:
        # THROTTLED_KEYS records insights only for throttled keys — the signal
        # this cache needs — at a fraction of full-table insights cost. Pinned
        # so a refactor back to the bool flag (full mode) is a visible change.
        data_template.has_resource_properties(
            "AWS::DynamoDB::GlobalTable",
            {
                "Replicas": Match.array_with(
                    [
                        Match.object_like(
                            {"ContributorInsightsSpecification": {"Enabled": True, "Mode": "THROTTLED_KEYS"}}
                        )
                    ]
                )
            },
        )

    def test_stack_output_exists(self, data_template: Template) -> None:
        data_template.has_output("IdempotencyTableName", {})

    # ── Default (destroy-friendly) retention posture ─────────────────────────────
    # The template ships retain_data=False so dev/ephemeral environments tear
    # down cleanly: table + CMK are DESTROY, deletion protection off.

    def test_default_table_is_destroyable(self, data_template: Template) -> None:
        data_template.has_resource(
            "AWS::DynamoDB::GlobalTable",
            {"DeletionPolicy": "Delete", "UpdateReplacePolicy": "Delete"},
        )

    def test_default_table_deletion_protection_off(self, data_template: Template) -> None:
        # DeletionProtectionEnabled lives per-replica on a GlobalTable.
        data_template.has_resource_properties(
            "AWS::DynamoDB::GlobalTable",
            {"Replicas": Match.array_with([Match.object_like({"DeletionProtectionEnabled": False})])},
        )

    def test_default_key_is_destroyable(self, data_template: Template) -> None:
        data_template.has_resource("AWS::KMS::Key", {"DeletionPolicy": "Delete"})

    # ── Production (retain_data=True) retention posture ──────────────────────────
    # A production fork flips one flag; the table + CMK become RETAIN and
    # DynamoDB deletion protection turns on.

    def test_retained_table_is_retained(self, data_template_retained: Template) -> None:
        data_template_retained.has_resource(
            "AWS::DynamoDB::GlobalTable",
            {"DeletionPolicy": "Retain", "UpdateReplacePolicy": "Retain"},
        )

    def test_retained_table_deletion_protection_on(self, data_template_retained: Template) -> None:
        data_template_retained.has_resource_properties(
            "AWS::DynamoDB::GlobalTable",
            {"Replicas": Match.array_with([Match.object_like({"DeletionProtectionEnabled": True})])},
        )

    def test_retained_key_is_retained(self, data_template_retained: Template) -> None:
        data_template_retained.has_resource("AWS::KMS::Key", {"DeletionPolicy": "Retain"})


class TestBackendStack:
    def test_kms_key_has_rotation_enabled(self, backend_template: Template) -> None:
        backend_template.has_resource_properties("AWS::KMS::Key", {"EnableKeyRotation": True})

    def test_lambda_has_active_tracing(self, backend_template: Template) -> None:
        backend_template.has_resource_properties(
            "AWS::Lambda::Function",
            {"TracingConfig": {"Mode": "Active"}, "MemorySize": 256},
        )

    def test_lambda_runtime_and_system_log_level(self, backend_template: Template) -> None:
        # Runtime currency retires the AwsSolutions-L1 / Serverless-LambdaLatestVersion
        # suppressions; SystemLogLevel pins the platform-log posture in code.
        # The SlowInvocations saved query reads platform.report records, which
        # are emitted at INFO — raising this level would silently blind it.
        backend_template.has_resource_properties(
            "AWS::Lambda::Function",
            {
                "Runtime": "python3.14",
                "LoggingConfig": Match.object_like({"LogFormat": "JSON", "SystemLogLevel": "INFO"}),
            },
        )

    def test_lambda_async_retries_pinned_to_zero(self, backend_template: Template) -> None:
        # retry_attempts=0 documents that no async invocation path exists (the
        # function is API Gateway-synchronous only). The application function's
        # EventInvokeConfig must carry the explicit 0 — the custom-resource
        # provider's EventInvokeConfig (DLQ wiring) intentionally leaves
        # retries at the service default, so match on the qualified function.
        configs = backend_template.find_resources("AWS::Lambda::EventInvokeConfig")
        app_configs = [
            c for c in configs.values() if "ApiFunction" in json.dumps(c["Properties"].get("FunctionName"), default=str)
        ]
        assert len(app_configs) == 1, "expected exactly one EventInvokeConfig on the application function"
        assert app_configs[0]["Properties"]["MaximumRetryAttempts"] == 0

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

    def test_regional_waf_logs_to_s3(self, backend_template: Template) -> None:
        # WAFv2LoggingEnabled (NIST/HIPAA/PCI) requires logging on the ACL. The
        # regional ACL writes to an aws-waf-logs-* S3 bucket (same-region
        # requirement → the bucket lives in this stack), with the delivery
        # service principal granted write on it.
        backend_template.resource_count_is("AWS::WAFv2::LoggingConfiguration", 1)
        buckets = backend_template.find_resources("AWS::S3::Bucket")
        names = json.dumps([b["Properties"].get("BucketName") for b in buckets.values()], default=str)
        assert "aws-waf-logs-" in names, "regional WAF log bucket name must start with aws-waf-logs-"
        backend_template.has_resource_properties(
            "AWS::S3::BucketPolicy",
            {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [Match.object_like({"Principal": {"Service": "delivery.logs.amazonaws.com"}})]
                    )
                }
            },
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

    def test_regional_waf_rate_limits_direct_callers_only(self, backend_template: Template) -> None:
        # The regional ACL rate-limits only requests WITHOUT X-Forwarded-For —
        # direct execute-api callers, the CloudFront-bypass path. Funnelled
        # traffic always carries the XFF header CloudFront appends toward the
        # origin, so it must never match: the scope-down NotStatement is the
        # security-critical part, not just the rule's presence. Without this
        # rule one direct caller could exhaust the shared stage throttle and
        # starve legitimate funnelled users (see _attach_regional_waf).
        acls = backend_template.find_resources("AWS::WAFv2::WebACL")
        regional = [a for a in acls.values() if a["Properties"]["Scope"] == "REGIONAL"]
        assert len(regional) == 1, "expected exactly one REGIONAL WebACL"
        rules = {r["Name"]: r for r in regional[0]["Properties"]["Rules"]}
        rule = rules.get("RateLimitDirectCallers")
        assert rule is not None, "expected the direct-caller rate rule on the regional ACL"
        assert rule["Action"] == {"Block": {}}
        stmt = rule["Statement"]["RateBasedStatement"]
        assert stmt["AggregateKeyType"] == "IP"
        scope_down = stmt["ScopeDownStatement"]["NotStatement"]["Statement"]["SizeConstraintStatement"]
        assert scope_down["FieldToMatch"]["SingleHeader"]["Name"] == "x-forwarded-for"
        assert scope_down["ComparisonOperator"] == "GT"
        assert scope_down["Size"] == 0

    def test_appinsights_dashboard_cleanup_targets_real_dashboard_name(self, backend_template: Template) -> None:
        # Application Insights names its auto-created dashboard
        # "ApplicationInsights-{resource-group-name}", and the resource group
        # name itself already starts with "ApplicationInsights-" — so the real
        # dashboard name is the DOUBLED prefix. Deleting resource_group.name
        # verbatim silently deletes nothing (found as a dangling dashboard
        # after a live teardown). Both the SDK-call parameter and the scoped
        # IAM resource must use the doubled-prefix name.
        crs = backend_template.find_resources("Custom::AWS")
        deletes = [json.dumps(r["Properties"].get("Delete"), default=str) for r in crs.values()]
        dashboard_deletes = [d for d in deletes if "deleteDashboards" in d]
        assert len(dashboard_deletes) == 1, "expected exactly one DeleteDashboards custom resource"
        assert "ApplicationInsights-ApplicationInsights-TestBackendStack" in dashboard_deletes[0], (
            "cleanup must target the doubled-prefix dashboard name Application Insights actually creates"
        )

    def test_appconfig_deletion_protection_bypassed(self, backend_template: Template) -> None:
        # The account-level AppConfig deletion-protection window refuses to
        # delete environments/profiles a client polled recently — which is
        # *always* true for this Lambda. Without BYPASS, every `cdk destroy`
        # of a recently used stack fails mid-teardown, recreating the class of
        # dangling-teardown problem `make destroy-clean` exists to solve.
        backend_template.has_resource_properties("AWS::AppConfig::Environment", {"DeletionProtectionCheck": "BYPASS"})
        backend_template.has_resource_properties(
            "AWS::AppConfig::ConfigurationProfile", {"DeletionProtectionCheck": "BYPASS"}
        )

    def test_alarm_topic_is_encrypted_and_locked_down(self, backend_template: Template) -> None:
        # The default (production) environment routes alarms to one SNS topic.
        # It must be CMK-encrypted (same project key as every other resource)
        # and its policy must both deny plaintext publishes and admit only
        # CloudWatch acting for this account's alarms.
        backend_template.resource_count_is("AWS::SNS::Topic", 1)
        backend_template.has_resource_properties("AWS::SNS::Topic", {"KmsMasterKeyId": Match.any_value()})
        backend_template.has_resource_properties(
            "AWS::SNS::TopicPolicy",
            {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Effect": "Deny",
                                    "Action": "sns:Publish",
                                    "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                                }
                            ),
                            Match.object_like(
                                {
                                    "Sid": "AllowCloudWatchAlarmsPublish",
                                    "Effect": "Allow",
                                    "Principal": {"Service": "cloudwatch.amazonaws.com"},
                                    "Condition": Match.object_like(
                                        {"StringEquals": {"aws:SourceAccount": Match.any_value()}}
                                    ),
                                }
                            ),
                        ]
                    )
                }
            },
        )

    def test_operational_alarms_publish_to_topic(self, backend_template: Template) -> None:
        # An operational alarm with no action is a dashboard widget, not an
        # alert. Every operational alarm (Lambda p90 latency + API Gateway 5xx
        # fault rate) must carry an AlarmAction referencing the SNS topic. The
        # the CodeDeploy canary rollback alarm is excluded: it is polled by
        # CodeDeploy, not a paging channel — see
        # test_deployment_control_alarm_has_no_sns_action.
        alarms = backend_template.find_resources("AWS::CloudWatch::Alarm")
        assert len(alarms) >= 2, "expected at least the p90 latency and 5xx fault-rate alarms"
        names = json.dumps([a["Properties"].get("AlarmName") for a in alarms.values()])
        assert "Latency-P90" in names
        assert "Fault-Rate" in names
        for logical_id, alarm in alarms.items():
            if "rollback" in alarm["Properties"].get("AlarmDescription", "").lower():
                continue  # deployment-control alarm — asserted separately
            actions = alarm["Properties"].get("AlarmActions", [])
            assert actions, f"{logical_id} has no AlarmActions — it would fire silently"
            assert "AlarmTopic" in json.dumps(actions), f"{logical_id} must publish to the alarm topic"

    def test_lambda_fault_rate_and_ddb_throttle_alarms_exist(self, backend_template: Template) -> None:
        # TODO.md "CloudWatch alarms — still open" items: Lambda error-rate and
        # DynamoDB throttle alarms via the existing MonitoringFacade calls.
        #
        # Kwarg-name note (verified against the installed cdk-monitoring-constructs
        # signatures): monitor_dynamo_table's add_read_throttled_events_count_alarm /
        # add_write_throttled_events_count_alarm take ThrottledEventsThreshold
        # (max_throttled_events_threshold=...), not ErrorCountThreshold as the task
        # brief assumed. monitor_lambda_function's add_fault_rate_alarm takes
        # ErrorRateThreshold as expected.
        alarms = backend_template.find_resources("AWS::CloudWatch::Alarm")
        names = json.dumps([a["Properties"].get("AlarmName") for a in alarms.values()])
        # "ApiFunction-" scopes the match to the Lambda's alarm — a bare
        # "Fault-Rate" would match the pre-existing RestApi 5xx fault-rate alarm.
        assert "ApiFunction-Fault-Rate" in names, "expected a Lambda fault-rate alarm"
        descriptions = " ".join(json.dumps(a["Properties"]) for a in alarms.values())
        assert "throttle" in descriptions.lower(), "expected DynamoDB throttled-events alarms"

    def test_deployment_control_alarm_has_no_sns_action(self, backend_template: Template) -> None:
        # The CodeDeploy canary alarm is consumed by CodeDeploy, which polls its
        # state to decide on rollback — it's not a paging channel, so it carries
        # no SNS action (and the CloudWatchAlarmAction nag rule is suppressed).
        alarms = backend_template.find_resources("AWS::CloudWatch::Alarm")
        rollback = {
            lid: a for lid, a in alarms.items() if "rollback" in a["Properties"].get("AlarmDescription", "").lower()
        }
        assert len(rollback) == 1, "expected the CodeDeploy canary rollback alarm"
        for logical_id, alarm in rollback.items():
            assert not alarm["Properties"].get("AlarmActions"), (
                f"{logical_id} is a deployment-control alarm — it must carry no SNS action"
            )

    # ── CodeDeploy canary deployment (prod shape) ────────────────────────────────

    def test_lambda_alias_and_version_exist(self, backend_template: Template) -> None:
        # The API integrates with the alias, so a version + alias must exist for
        # CodeDeploy to shift traffic onto. The alias is named "live".
        backend_template.resource_count_is("AWS::Lambda::Version", 1)
        backend_template.has_resource_properties("AWS::Lambda::Alias", {"Name": "live"})

    def test_api_integration_targets_the_alias(self, backend_template: Template) -> None:
        # The GET method's integration URI must reference the alias (not the bare
        # function) — otherwise CodeDeploy traffic shifting wouldn't move real
        # traffic. The alias logical ID appears in the integration URI Fn::Join.
        aliases = backend_template.find_resources("AWS::Lambda::Alias")
        assert len(aliases) == 1
        alias_logical_id = next(iter(aliases))
        methods = backend_template.find_resources("AWS::ApiGateway::Method")
        get_methods = [m for m in methods.values() if m["Properties"].get("HttpMethod") == "GET"]
        assert get_methods, "expected a GET method"
        assert any(alias_logical_id in json.dumps(m["Properties"]["Integration"]["Uri"]) for m in get_methods), (
            "GET integration URI must reference the Lambda alias"
        )

    def test_codedeploy_canary_deployment_group(self, backend_template: Template) -> None:
        # Prod uses the canary config and rolls back on a failed deploy or an
        # alarm firing mid-shift.
        backend_template.has_resource_properties(
            "AWS::CodeDeploy::DeploymentGroup",
            {
                "DeploymentConfigName": "CodeDeployDefault.LambdaCanary10Percent5Minutes",
                "AlarmConfiguration": Match.object_like({"Enabled": True}),
                "AutoRollbackConfiguration": {
                    "Enabled": True,
                    "Events": Match.array_with(["DEPLOYMENT_FAILURE", "DEPLOYMENT_STOP_ON_ALARM"]),
                },
            },
        )

    # ── AppConfig deployment (all-at-once; no CFN-wired rollback monitor) ──────────

    def test_appconfig_strategy_is_all_at_once(self, backend_template: Template) -> None:
        # The CFN-managed AppConfig deployment runs during initial provisioning,
        # where a monitored alarm would be INSUFFICIENT_DATA (no traffic yet) and
        # AppConfig would auto-roll-back the cold deploy. So the strategy is
        # all-at-once and the environment carries no monitor — gradual + alarm
        # rollback is a documented production add-on for ongoing config changes.
        backend_template.has_resource_properties(
            "AWS::AppConfig::DeploymentStrategy",
            {"GrowthFactor": 100, "DeploymentDurationInMinutes": 0, "FinalBakeTimeInMinutes": 0},
        )

    def test_appconfig_environment_has_no_monitor(self, backend_template: Template) -> None:
        # No CloudWatch monitor on the environment (see test above for why).
        envs = backend_template.find_resources("AWS::AppConfig::Environment")
        (env,) = envs.values()
        assert not env["Properties"].get("Monitors"), "AppConfig env must not carry a rollback monitor on cold deploy"

    def test_kms_key_grants_cloudwatch_via_sns(self, backend_template: Template) -> None:
        # Without this key-policy statement the CMK-encrypted publish is denied
        # at KMS and alarm notifications vanish silently — the worst failure
        # mode for an alerting path. The grant must stay confused-deputy-guarded
        # (kms:ViaService pins it to SNS; aws:SourceAccount to this account).
        keys = backend_template.find_resources("AWS::KMS::Key")
        statements = []
        for key in keys.values():
            statements.extend(key["Properties"]["KeyPolicy"]["Statement"])
        cw_statements = [s for s in statements if s.get("Sid") == "AllowCloudWatchAlarmsViaSns"]
        assert len(cw_statements) == 1, "expected exactly one CloudWatch-via-SNS key grant"
        condition = cw_statements[0]["Condition"]["StringEquals"]
        assert condition["aws:SourceAccount"] == _TEST_ACCOUNT
        assert condition["kms:ViaService"] == f"sns.{_TEST_REGION}.amazonaws.com"

    def test_non_production_env_has_no_alarm_topic(self) -> None:
        # Ephemeral/dev environments keep the dashboards and alarms but must
        # not create the SNS topic — short-lived stacks never page anyone.
        app = cdk.App(context=_NO_BUNDLING)
        data = DataStack(app, "TestDevBackendData", env=_TEST_ENV)
        stack = BackendStack(
            app,
            "TestDevBackendStack",
            idempotency_table=data.idempotency_table,
            is_production_env=False,
            env=_TEST_ENV,
        )
        template = Template.from_stack(stack)
        template.resource_count_is("AWS::SNS::Topic", 0)
        assert len(template.find_resources("AWS::CloudWatch::Alarm")) >= 2, (
            "non-prod must keep its alarms (just without SNS routing)"
        )

    def test_appconfig_flags_are_deployed_to_environment(self, backend_template: Template) -> None:
        # A hosted configuration version alone is never served: the AppConfig
        # data plane (GetLatestConfiguration) only returns *deployed* config.
        # Without a Deployment resource the enhanced_greeting flag can never
        # evaluate true and every fetch takes the error-fallback path.
        backend_template.resource_count_is("AWS::AppConfig::DeploymentStrategy", 1)
        backend_template.resource_count_is("AWS::AppConfig::Deployment", 1)
        backend_template.has_resource_properties(
            "AWS::AppConfig::Deployment",
            {
                "ApplicationId": Match.any_value(),
                "EnvironmentId": Match.any_value(),
                "ConfigurationProfileId": Match.any_value(),
                "ConfigurationVersion": Match.any_value(),
                "DeploymentStrategyId": Match.any_value(),
            },
        )

    def test_appconfig_content_is_powertools_schema_in_freeform_profile(self, backend_template: Template) -> None:
        # Powertools FeatureFlags parses ITS OWN schema ({feature: {default:
        # bool, rules: ...}}), not AppConfig's native flag format. A profile of
        # type AWS.AppConfig.FeatureFlags serves the flattened
        # {feature: {enabled: bool}} form at the data plane, which Powertools
        # rejects with SchemaValidationError ("feature 'default' boolean key
        # must be present") — found only on a live deployment because the
        # handler's fallback masks it. The profile must be freeform and the
        # hosted content must be the Powertools schema.
        profiles = backend_template.find_resources("AWS::AppConfig::ConfigurationProfile")
        (profile,) = profiles.values()
        assert profile["Properties"].get("Type") in (None, "AWS.Freeform"), (
            "profile must be freeform — AWS.AppConfig.FeatureFlags serves a format Powertools cannot parse"
        )
        versions = backend_template.find_resources("AWS::AppConfig::HostedConfigurationVersion")
        (version,) = versions.values()
        content = json.loads(version["Properties"]["Content"])
        assert content["enhanced_greeting"]["default"] is False
        assert "flags" not in content
        assert "values" not in content

    def test_access_log_format_has_latency_and_no_message_string(self, backend_template: Template) -> None:
        # responseLatency feeds the SlowestRequests saved query (without it the
        # query can't sort by latency). $context.error.messageString must NOT
        # appear in a JSON format: absent access-log variables render as a bare
        # dash, so the unquoted form corrupts every success line and the quoted
        # form double-quotes every error line. The quoted raw
        # $context.error.message is the least-bad JSON-safe option.
        stages = backend_template.find_resources("AWS::ApiGateway::Stage")
        assert stages, "expected at least one API Gateway stage"
        for stage in stages.values():
            log_format = stage["Properties"]["AccessLogSetting"]["Format"]
            assert "$context.responseLatency" in log_format
            assert "$context.error.messageString" not in log_format, (
                "messageString is unusable in a JSON access-log format — see the comment in BackendApp"
            )
            assert '"$context.error.message"' in log_format

    def test_stage_depends_on_execution_log_group(self, backend_template: Template) -> None:
        # The execution log group is pre-created so CFN owns it; if the stage
        # goes live first, API Gateway auto-creates the group (unencrypted, no
        # retention) and the LogGroup CREATE then collides. The explicit
        # DependsOn makes the CMK-encrypted group win the race.
        stages = backend_template.find_resources("AWS::ApiGateway::Stage")
        assert stages, "expected at least one API Gateway stage"
        for stage in stages.values():
            assert "AppApiExecutionLogs0AE84813" in stage.get("DependsOn", []), (
                "Prod stage must depend on the pre-created execution log group"
            )

    def test_api_gateway_cache_cluster_disabled(self, backend_template: Template) -> None:
        # Cache cluster is intentionally disabled for cost (~$14/mo for the smallest size)
        # and to avoid serving stale values across SSM/AppConfig changes — see the
        # NIST.800.53.R5-APIGWCacheEnabledAndEncrypted suppression in BackendStack.
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

    def test_operational_log_groups_retain_90_days(self, backend_template: Template) -> None:
        # The operational app log groups — Lambda function, API Gateway access,
        # API Gateway execution — retain 90 days (enough debugging history + a
        # "3 months immediately available" window). Provider/CDK-singleton groups
        # stay short. Audit-relevant logs go to S3 for cheap long-term retention.
        log_groups = backend_template.find_resources("AWS::Logs::LogGroup")
        ninety_day = [lid for lid, lg in log_groups.items() if lg["Properties"].get("RetentionInDays") == 90]
        assert len(ninety_day) >= 3, f"expected ≥3 operational log groups at 90-day retention, found {len(ninety_day)}"

    def test_lambda_role_can_decrypt_appconfig_cmk(self, backend_template: Template) -> None:
        # The AppConfig hosted config is CMK-encrypted with the backend key, and
        # AppConfig evaluates kms:Decrypt against the *caller's* role on
        # GetLatestConfiguration. This grant used to ride the DynamoDB grant
        # (table shared the key); now that the table has its own key in the data
        # stack, the Lambda needs an explicit kms:Decrypt on the backend key.
        # Without it, GetLatestConfiguration fails at runtime with a KMS error —
        # which no synth-time check (cdk-nag included) would catch.
        backend_template.has_resource_properties(
            "AWS::IAM::Policy",
            {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": "kms:Decrypt",
                                    "Effect": "Allow",
                                    "Resource": {"Fn::GetAtt": ["AppEncryptionKey7F644894", "Arn"]},
                                }
                            )
                        ]
                    )
                }
            },
        )

    def test_stack_outputs_exist(self, backend_template: Template) -> None:
        backend_template.has_output("ApiUrlOutput", {})
        backend_template.has_output("FunctionArnOutput", {})
        backend_template.has_output("GreetingParameterName", {})
        backend_template.has_output("CloudWatchDashboardUrl", {})
        # IdempotencyTableName now lives on DataStack (the table owner).


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

    def test_frontend_bucket_versioned_with_noncurrent_expiry(self, frontend_template: Template) -> None:
        # Versioning gives in-bucket recovery for the deployed assets (git remains
        # the source of truth); the 30-day noncurrent expiry bounds version storage.
        frontend_template.has_resource_properties(
            "AWS::S3::Bucket",
            Match.object_like(
                {
                    "VersioningConfiguration": {"Status": "Enabled"},
                    "LifecycleConfiguration": Match.object_like(
                        {
                            "Rules": Match.array_with(
                                [
                                    Match.object_like(
                                        {
                                            "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
                                            "Status": "Enabled",
                                        }
                                    )
                                ]
                            )
                        }
                    ),
                }
            ),
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
        # AppMonitor block of frontend_stack.py.
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

    def test_async_providers_have_failure_destinations(self, frontend_template: Template) -> None:
        # CFN invokes custom-resource provider Lambdas asynchronously; a crash
        # that exhausts the two automatic retries is silently dropped without an
        # on_failure destination. Both stack-level Function-based providers
        # (AwsCustomResource provider + BucketDeployment handler) must carry an
        # EventInvokeConfig wiring their SQS DLQ.
        frontend_template.resource_count_is("AWS::Lambda::EventInvokeConfig", 2)

    def test_auto_delete_custom_resources_depend_on_log_group(self, frontend_template: Template) -> None:
        # The auto-delete provider logs on its CREATE invocation. If that
        # happens before CFN creates the explicit CMK log group, Lambda
        # implicitly creates the group and the LogGroup CREATE then fails with
        # "already exists". Each bucket's auto-delete custom resource must
        # depend on the log group so the CMK-encrypted group always wins.
        log_groups = frontend_template.find_resources("AWS::Logs::LogGroup")
        auto_delete_lg_ids = [
            logical_id
            for logical_id, lg in log_groups.items()
            if isinstance(lg["Properties"].get("LogGroupName"), dict) and "Fn::Join" in lg["Properties"]["LogGroupName"]
        ]
        assert len(auto_delete_lg_ids) == 1
        custom_resources = frontend_template.find_resources("Custom::S3AutoDeleteObjects")
        assert custom_resources, "expected auto-delete custom resources for the buckets"
        for logical_id, resource in custom_resources.items():
            assert auto_delete_lg_ids[0] in resource.get("DependsOn", []), (
                f"{logical_id} must depend on the auto-delete provider log group"
            )

    def test_two_s3_buckets_exist(self, frontend_template: Template) -> None:
        # FrontendBucket + FrontendAccessLogBucket. The CloudTrail-logs bucket
        # moved to AuditStack (see TestAuditStack).
        frontend_template.resource_count_is("AWS::S3::Bucket", 2)

    def test_stack_outputs_exist(self, frontend_template: Template) -> None:
        frontend_template.has_output("CloudFrontDomainName", {})
        frontend_template.has_output("CloudFrontDistributionId", {})
        frontend_template.has_output("FrontendBucketName", {})

    def test_waf_glue_tables_use_partition_projection(self, frontend_template: Template) -> None:
        # Two WAF Glue tables (CloudFront + regional) over the aws-waf-logs-* S3
        # data, each partition-projected on log_time so Athena needs no crawler /
        # ALTER TABLE ADD PARTITION. JSON SerDe maps WAF's records to the columns.
        # Granularity and range are pinned: Athena issues an S3 LIST per projected
        # partition in scope, so DAY x the bucket's own NOW-90DAYS lifecycle keeps
        # that at ~90 — a minute-granularity range anchored at a fixed date
        # projected ~800k partitions and one unfiltered query ran 6+ minutes
        # (found live; see _create_waf_glue_table).
        tables = frontend_template.find_resources("AWS::Glue::Table")
        waf_tables = {
            lid: t for lid, t in tables.items() if str(t["Properties"]["TableInput"].get("Name", "")).startswith("waf_")
        }
        assert len(waf_tables) == 2, "expected waf_cloudfront_logs + waf_regional_logs Glue tables"
        for table in waf_tables.values():
            ti = table["Properties"]["TableInput"]
            assert ti["Parameters"]["projection.enabled"] == "true"
            assert ti["Parameters"]["projection.log_time.type"] == "date"
            assert ti["Parameters"]["projection.log_time.format"] == "yyyy/MM/dd"
            assert ti["Parameters"]["projection.log_time.interval.unit"] == "DAYS"
            assert ti["Parameters"]["projection.log_time.range"] == "NOW-90DAYS,NOW"
            assert "storage.location.template" in ti["Parameters"]
            assert ti["PartitionKeys"] == [{"Name": "log_time", "Type": "string"}]
            assert ti["StorageDescriptor"]["SerdeInfo"]["SerializationLibrary"] == "org.openx.data.jsonserde.JsonSerDe"

    def test_waf_athena_named_queries_exist(self, frontend_template: Template) -> None:
        # 4 threat-triage queries per WAF table (8 total) restore the analysis the
        # retired CloudWatch Logs Insights saved queries provided.
        named_queries = frontend_template.find_resources("AWS::Athena::NamedQuery")
        waf_queries = [q for q in named_queries.values() if str(q["Properties"].get("Name", "")).startswith("WAF ")]
        assert len(waf_queries) == 8, f"expected 8 WAF named queries, found {len(waf_queries)}"

    def test_waf_named_queries_prune_partitions(self, frontend_template: Template) -> None:
        # The query half of the partition-projection contract: every WAF named
        # query must filter log_time in the projection's exact yyyy/MM/dd format
        # (slashes — Athena silently skips pruning on any other format) or the
        # query enumerates every projected partition, paying an S3 LIST each.
        named_queries = frontend_template.find_resources("AWS::Athena::NamedQuery")
        waf_queries = {
            lid: q for lid, q in named_queries.items() if str(q["Properties"].get("Name", "")).startswith("WAF ")
        }
        for logical_id, query in waf_queries.items():
            sql = query["Properties"]["QueryString"]
            assert "log_time >=" in sql, f"{logical_id} has no log_time partition filter — no pruning"
            assert "'%Y/%m/%d'" in sql, f"{logical_id} log_time filter must render the projection's slash format"

    def test_athena_workgroup_is_recursively_deletable(self, frontend_template: Template) -> None:
        # Running any of the shipped named queries leaves query-execution history
        # in the workgroup, and Athena refuses to delete a non-empty workgroup —
        # so without RecursiveDeleteOption `cdk destroy` fails once the queries
        # have been used (hit on a live teardown). The template default is
        # DESTROY-friendly, so the workgroup must clear itself on delete.
        frontend_template.has_resource(
            "AWS::Athena::WorkGroup",
            {"Properties": Match.object_like({"RecursiveDeleteOption": True})},
        )


class TestAuditStack:
    """CloudTrail S3 data-event trail + its log bucket + dedicated CMK (the audit data store)."""

    def test_cloudtrail_records_only_s3_data_events(self, audit_template: Template) -> None:
        # The trail exists for object-level S3 data events. CDK's defaults
        # (Trail.management_events=ALL, add_s3_event_selector include_management_
        # events=True) would additionally record every regional management event
        # — a billed second copy in any account that already has a trail.
        trails = audit_template.find_resources("AWS::CloudTrail::Trail")
        assert len(trails) == 1, "expected exactly one CloudTrail trail"
        (trail,) = trails.values()
        selectors = trail["Properties"]["EventSelectors"]
        assert selectors, "expected event selectors on the trail"
        for selector in selectors:
            assert selector.get("IncludeManagementEvents") is False, (
                "trail must not record management events — S3 data events only"
            )
        assert any("DataResources" in s for s in selectors), "expected an S3 data-event selector"

    def test_trail_has_file_validation_and_is_single_region(self, audit_template: Template) -> None:
        audit_template.has_resource_properties(
            "AWS::CloudTrail::Trail",
            {"EnableLogFileValidation": True, "IsMultiRegionTrail": False, "IncludeGlobalServiceEvents": False},
        )

    def test_cmk_has_rotation_enabled(self, audit_template: Template) -> None:
        audit_template.has_resource_properties("AWS::KMS::Key", {"EnableKeyRotation": True})

    def test_cloudtrail_key_grant_scoped_to_exact_trail_arn(self, audit_template: Template) -> None:
        # The trail name is pinned, so the CMK's CloudTrail service grant can
        # (and must) name the one trail allowed to use the key — a trail/*
        # wildcard would let any other trail in this account encrypt with the
        # audit CMK. aws:SourceAccount stays as defense in depth.
        keys = audit_template.find_resources("AWS::KMS::Key")
        statements = [s for k in keys.values() for s in k["Properties"]["KeyPolicy"]["Statement"]]
        ct_statements = [s for s in statements if s.get("Principal", {}).get("Service") == "cloudtrail.amazonaws.com"]
        assert len(ct_statements) == 1, "expected exactly one CloudTrail service grant on the audit CMK"
        condition = ct_statements[0]["Condition"]
        source_arn = json.dumps(condition["ArnLike"]["aws:SourceArn"], default=str)
        assert "trail/TestAuditStack-S3DataEventsTrail" in source_arn, (
            "CloudTrail key grant must pin aws:SourceArn to the exact pinned trail ARN, not trail/*"
        )
        assert "trail/*" not in source_arn
        assert condition["StringEquals"]["aws:SourceAccount"] == _TEST_ACCOUNT

    def test_one_s3_bucket_with_90_day_lifecycle(self, audit_template: Template) -> None:
        audit_template.resource_count_is("AWS::S3::Bucket", 1)
        audit_template.has_resource_properties(
            "AWS::S3::Bucket",
            {
                "LifecycleConfiguration": {
                    "Rules": Match.array_with([Match.object_like({"ExpirationInDays": 90, "Status": "Enabled"})])
                }
            },
        )

    def test_stack_output_exists(self, audit_template: Template) -> None:
        audit_template.has_output("CloudTrailLogsBucketName", {})

    # ── retention posture by retain_data ─────────────────────────────────────────

    def test_default_bucket_and_key_are_destroyable(self, audit_template: Template) -> None:
        audit_template.has_resource("AWS::S3::Bucket", {"DeletionPolicy": "Delete"})
        audit_template.has_resource("AWS::KMS::Key", {"DeletionPolicy": "Delete"})

    def test_retained_bucket_and_key_are_retained(self, audit_template_retained: Template) -> None:
        audit_template_retained.has_resource("AWS::S3::Bucket", {"DeletionPolicy": "Retain"})
        audit_template_retained.has_resource("AWS::KMS::Key", {"DeletionPolicy": "Retain"})

    def test_retained_shape_has_no_auto_delete(self, audit_template_retained: Template) -> None:
        # A retained bucket must not carry the auto-delete custom resource (it
        # would defeat retention by emptying the bucket on a stack delete).
        audit_template_retained.resource_count_is("Custom::S3AutoDeleteObjects", 0)


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

    # ── Data ───────────────────────────────────────────────────────────────────
    # The idempotency table and its CMK live in DataStack. Their
    # logical IDs lost the "App" construct prefix when the table moved out of
    # the BackendApp construct and into its own stack — a one-time change
    # captured here (the template ships with no live data, so no migration).

    def test_data_dynamodb_table_id(self, data_template: Template) -> None:
        assert "IdempotencyTableV203A5298E" in data_template.find_resources("AWS::DynamoDB::GlobalTable")

    def test_data_kms_key_id(self, data_template: Template) -> None:
        assert "DataEncryptionKey101796EE" in data_template.find_resources("AWS::KMS::Key")

    # ── Backend ────────────────────────────────────────────────────────────────

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
        assert "AppFunctionLogGroupB9961371" in log_groups
        assert "AppApiAccessLogsDD2CC3E5" in log_groups
        assert "AppApiExecutionLogs0AE84813" in log_groups

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

    def test_waf_webacl_id(self, waf_template: Template) -> None:
        # L1 CfnWebACL — its logical ID is the construct_id with no hash suffix.
        assert "WebACL" in waf_template.find_resources("AWS::WAFv2::WebACL")
