# Production-Readiness Batch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved spec `docs/superpowers/specs/2026-07-08-production-readiness-batch-design.md` — same-origin API through CloudFront with secret-header origin lockdown and EDGE→REGIONAL, the remaining observability alarms + cost guardrails, `retain_data`-gated data protection (AWS Backup, Object Lock + archive tiering), and the small wins (Athena minimum encryption, SSM path context, AppConfig extension layer).

**Architecture:** All changes ride the existing five-stack `AppStage` composition. Cross-stack values flow only along existing dependency edges (Stage-computed strings, or backend→frontend parameters — the frontend already depends on the backend). Every alarm follows the repo posture: SNS action in `prod`, exists-but-silent with scoped nag suppressions elsewhere. `retain_data=False` shapes must be byte-identical in behavior to today except where a task explicitly says otherwise.

**Tech Stack:** AWS CDK v2 (Python, `.venv`), aws-lambda-powertools (`.venv-lambda`), cdk-nag v3 policy-validation plugins, cdk-monitoring-constructs, pytest (`aws_cdk.assertions` + unit), GNU make.

## Global Constraints

- Two venvs, never mix: CDK work runs in `.venv` (`make test-cdk`, `make cdk-synth`), Lambda runtime work in `.venv-lambda` (`make test`, `make openapi`). Never install Powertools into `.venv` or CDK into `.venv-lambda`.
- Every new cdk-nag suppression uses `acknowledge_rules` with a written reason; granular IAM4/IAM5 need exact `applies_to` finding ids (the failing gate output prints them — copy verbatim).
- Every template-shape change: run `UPDATE_SNAPSHOTS=1 make test-cdk`, review the snapshot diff, and pair it with a fine-grained assertion change in `tests/cdk/test_stacks.py` (or `test_stage.py` for non-default shapes).
- The nag gate is `make test-cdk` (all four shapes: prod, dev, `appconfig_monitor`, `retain_data`). Run it after every infra task.
- After touching `lambda/`, run `make test` (100% coverage gate) and `make openapi`; commit `docs/openapi.json` if it changed.
- Conventional commit prefixes (`feat:`/`fix:`/`docs:`/`test:`). **No `Co-Authored-By:` trailer** (CLAUDE.md).
- Alarm/budget thresholds in this plan are documented reference-workload values — keep the code comments that say so.
- Do not rename any deployed-prod stack, pinned physical name, or CfnOutput key.
- Branch: `feat/production-readiness-batch` (exists). Never delete it after completion (kept as an evolution checkpoint).

---

### Task 1: Frontend bucket versioning + noncurrent-version expiry

**Files:**
- Modify: `infrastructure/frontend_stack.py:278-290` (bucket), `:691-706` (stack suppressions)
- Test: `tests/cdk/test_stacks.py` (class `TestFrontendStack`)

**Interfaces:**
- Consumes: nothing new.
- Produces: no API change; `FrontendBucket` gains `VersioningConfiguration` + lifecycle.

- [ ] **Step 1: Write the failing test** — add to `TestFrontendStack` in `tests/cdk/test_stacks.py` (match the file's existing `Match` import and fixture style):

```python
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
```

- [ ] **Step 2: Run it to fail** — `make test-cdk` → the new test FAILS (no VersioningConfiguration on any bucket).
- [ ] **Step 3: Implement** — in `infrastructure/frontend_stack.py`, change the `FrontendBucket` construction:

```python
bucket = s3.Bucket(
    self,
    "FrontendBucket",
    block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
    encryption=s3.BucketEncryption.KMS,
    encryption_key=frontend_encryption_key,
    enforce_ssl=True,
    server_access_logs_bucket=access_log_bucket,
    server_access_logs_prefix="s3-access-logs/",
    # Versioning gives in-bucket recovery if assets are overwritten out-of-band
    # (git stays the source of truth; this is the belt to that suspender) and is
    # a prerequisite for any future replication. The 30-day noncurrent-version
    # expiry bounds the storage cost of redeploy churn. auto_delete_objects
    # removes ALL versions on destroy, so teardown stays clean.
    versioned=True,
    lifecycle_rules=[
        s3.LifecycleRule(
            id="ExpireNoncurrentVersions",
            enabled=True,
            noncurrent_version_expiration=Duration.days(30),
            abort_incomplete_multipart_upload_after=Duration.days(1),
        ),
    ],
    removal_policy=RemovalPolicy.DESTROY,
    auto_delete_objects=True,
)
```

Then in the stack-level suppressions block (`stack_suppressions` near line 691): delete `versioning_reason` and the three `*S3BucketVersioningEnabled` tuples (NIST/HIPAA/PCI). Keep all replication entries. (The access-log bucket's versioning suppressions live in `_LOG_SINK_SUPPRESSION_RULES` and are untouched.)

- [ ] **Step 4: Run to pass + snapshots** — `make test-cdk` → new test passes, snapshot test fails; `UPDATE_SNAPSHOTS=1 make test-cdk`, review `tests/cdk/snapshots/ServerlessAppFrontend-us-east-1.json` diff (VersioningConfiguration + lifecycle + removed suppression metadata only), then `make test-cdk` green.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: enable versioning with noncurrent-version expiry on the frontend bucket"`

---

### Task 2: SSM greeting-parameter path via CDK context

**Files:**
- Modify: `app.py`, `infrastructure/app_stage.py`, `infrastructure/backend_stack.py`, `infrastructure/backend_app.py:199-203`
- Test: `tests/cdk/test_stage.py`

**Interfaces:**
- Produces: `validate_ssm_param_path(raw: str | None) -> str | None` in `infrastructure/app_stage.py`; `AppStage(..., ssm_param_path: str | None = None)`; `BackendStack(..., ssm_param_path: str | None = None)`; `BackendApp(..., ssm_param_path: str | None = None)`. Default `None` keeps CDK auto-naming (no template change in default shapes).

- [ ] **Step 1: Write the failing tests** — in `tests/cdk/test_stage.py`:

```python
from infrastructure.app_stage import validate_ssm_param_path  # add to existing import


class TestSsmParamPathPlumbing:
    """`-c ssm_param_path=/my/path` overrides the greeting parameter name."""

    def test_default_keeps_autogenerated_name(self, prod_stage: AppStage) -> None:
        # No Name property when the context key is absent — CDK auto-generates,
        # exactly as before this feature existed (no prod template churn).
        params = Template.from_stack(prod_stage.backend).find_resources("AWS::SSM::Parameter")
        assert all("Name" not in p["Properties"] for p in params.values())

    def test_context_path_sets_parameter_name(self) -> None:
        app = cdk.App(context=_NO_BUNDLING)
        stage = AppStage(
            app, "ServerlessApp-us-east-1-stage", region="us-east-1", ssm_param_path="/serverless-app/greeting"
        )
        Template.from_stack(stage.backend).has_resource_properties(
            "AWS::SSM::Parameter", {"Name": "/serverless-app/greeting"}
        )

    @pytest.mark.parametrize("bad", ["no-leading-slash", "/trailing/", "/has space", "/", ""])
    def test_invalid_paths_fail_at_synth(self, bad: str) -> None:
        with pytest.raises(ValueError, match="ssm_param_path"):
            validate_ssm_param_path(bad)

    def test_none_passes_through(self) -> None:
        assert validate_ssm_param_path(None) is None
```

- [ ] **Step 2: Run to fail** — `make test-cdk` → import error (`validate_ssm_param_path` missing).
- [ ] **Step 3: Implement** — in `infrastructure/app_stage.py`, next to `validate_env_name`:

```python
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
```

`AppStage.__init__` gains `ssm_param_path: str | None = None,` (keyword-only, after `appconfig_monitor`), calls `validate_ssm_param_path(ssm_param_path)` next to `validate_env_name`, and forwards `ssm_param_path=ssm_param_path` into `BackendStack`. `BackendStack.__init__` gains the same parameter, documented in its docstring, forwarded to `BackendApp`. `BackendApp.__init__` gains it and uses it:

```python
self.greeting_param = ssm.StringParameter(
    self,
    "GreetingParameter",
    # None keeps CDK auto-naming (the default; Lambda reads the name via
    # GREETING_PARAM_NAME either way). A fork sets -c ssm_param_path=/org/app/greeting
    # to slot the parameter into its own SSM hierarchy.
    parameter_name=ssm_param_path,
    string_value="hello world",
)
```

In `app.py`, after the `appconfig_monitor` block:

```python
# Optional SSM path override for the greeting parameter (`-c ssm_param_path=/my/app/greeting`).
# Default None keeps CDK's auto-generated name; validated at synth (fail-loud like retain_data).
ssm_param_path: str | None = validate_ssm_param_path(app.node.try_get_context("ssm_param_path"))
```

(import it in the existing `from infrastructure.app_stage import ...` line) and pass `ssm_param_path=ssm_param_path` to `AppStage`.

**CAUTION:** changing `parameter_name` on a deployed stack replaces the parameter — the docstring for the context key must say "set it before first deploy, or accept parameter replacement (value is re-created as 'hello world')".

- [ ] **Step 4: Run to pass** — `make test-cdk` green; default-shape snapshots unchanged (verify: `git diff --stat tests/cdk/snapshots/` is empty).
- [ ] **Step 5: Commit** — `git commit -am "feat: add ssm_param_path context override for the greeting parameter"`

---

### Task 3: Athena `MinimumEncryptionConfiguration` (contingent on CFN support)

**Files:**
- Modify: `infrastructure/frontend_stack.py:1204-1238` (workgroup); possibly only `TODO.md`
- Test: `tests/cdk/test_stacks.py` (`TestFrontendStack`)

**Interfaces:** none — template-only.

- [ ] **Step 1: Verify support.** Run in `.venv`:

```bash
.venv/bin/python -c "import inspect; from aws_cdk import aws_athena; print('minimum_encryption_configuration' in inspect.signature(aws_athena.CfnWorkGroup.WorkGroupConfigurationProperty.__init__).parameters)"
```

Also search AWS docs (`aws___search_documentation`, topic `cloudformation`, phrase "AWS::Athena::WorkGroup MinimumEncryptionConfiguration") to confirm the CFN schema carries the field. Decision gate:
  - L1 exposes it → use the typed property (Step 3, variant A).
  - CFN supports it but the pinned L1 doesn't → raw override (variant B).
  - CFN does not support it → skip implementation; update the TODO.md entry with the dated finding ("checked 2026-07-08: CFN schema still lacks the field; workgroup-enforced SSE_KMS remains the posture") and commit that as `docs:` — task done.

- [ ] **Step 2: Write the failing test** (only if supported):

```python
def test_workgroup_enforces_minimum_encryption(self, frontend_template: Template) -> None:
    # Floor even if a future change relaxes enforce_work_group_configuration.
    frontend_template.has_resource_properties(
        "AWS::Athena::WorkGroup",
        Match.object_like(
            {
                "WorkGroupConfiguration": Match.object_like(
                    {"MinimumEncryptionConfiguration": Match.object_like({"EncryptionOption": "SSE_KMS"})}
                )
            }
        ),
    )
```

- [ ] **Step 3: Implement.** Variant A — add to `WorkGroupConfigurationProperty`:

```python
minimum_encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
    encryption_option="SSE_KMS",
    kms_key=encryption_key.key_arn,
),
```

Variant B — after the `workgroup = athena.CfnWorkGroup(...)` block:

```python
# Belt-and-suspenders floor (TODO "MinimumEncryptionConfiguration"): the L1
# doesn't expose the field in the pinned CDK, so it's a raw override. Remove
# the override in favor of the typed property once the L1 catches up.
workgroup.add_property_override(
    "WorkGroupConfiguration.MinimumEncryptionConfiguration",
    {"EncryptionOption": "SSE_KMS", "KmsKey": encryption_key.key_arn},
)
```

- [ ] **Step 4: Run to pass + snapshots** — `make test-cdk`; `UPDATE_SNAPSHOTS=1 make test-cdk`; review diff; green.
- [ ] **Step 5: Commit** — `git commit -am "feat: enforce Athena workgroup minimum encryption (SSE-KMS floor)"` (or the `docs:` TODO commit if unsupported).

---

### Task 4: Lambda fault-rate + DynamoDB throttle alarms (MonitoringFacade flags)

**Files:**
- Modify: `infrastructure/backend_app.py:731-754` (`_build_monitoring`)
- Test: `tests/cdk/test_stacks.py` (`TestBackendStack`)

**Interfaces:** none — adds alarms under the existing facade (SNS-in-prod and non-prod suppressions are inherited from the facade subtree).

- [ ] **Step 1: Verify kwarg names** (cdk-monitoring-constructs API):

```bash
.venv/bin/python -c "
import inspect
from cdk_monitoring_constructs import MonitoringFacade as F
print(inspect.signature(F.monitor_lambda_function))
print(inspect.signature(F.monitor_dynamo_table))
"
```

Expect `add_fault_rate_alarm` on `monitor_lambda_function` and `add_read_throttled_events_count_alarm` / `add_write_throttled_events_count_alarm` on `monitor_dynamo_table`. If the DynamoDB kwargs differ, use the closest throttled-events kwargs the signature actually shows and mirror them in the test names/comments.

- [ ] **Step 2: Write the failing test:**

```python
def test_lambda_fault_rate_and_ddb_throttle_alarms_exist(self, backend_template: Template) -> None:
    # TODO "CloudWatch alarms — still open" items: Lambda error-rate and
    # DynamoDB throttle alarms via the existing facade calls.
    alarms = backend_template.find_resources("AWS::CloudWatch::Alarm")
    descriptions = " ".join(json.dumps(a["Properties"]) for a in alarms.values())
    assert "Fault-Rate" in descriptions or "fault rate" in descriptions.lower(), "expected a Lambda fault-rate alarm"
    assert "throttle" in descriptions.lower(), "expected DynamoDB throttled-events alarms"
```

(`json` is already imported in `test_stacks.py`; check and add if not.)

- [ ] **Step 3: Implement** — in `_build_monitoring`, extend the existing calls (import `ErrorCountThreshold` from `cdk_monitoring_constructs` alongside the existing imports):

```python
monitoring.monitor_lambda_function(
    lambda_function=self.function,
    add_latency_p90_alarm={"p90": LatencyThreshold(max_latency=Duration.seconds(3))},
    # 5% of invocations erroring is systematic failure, not a cold-start blip —
    # reference value, size to real traffic in a fork.
    add_fault_rate_alarm={"error": ErrorRateThreshold(max_error_rate=5)},
)
...
monitoring.monitor_dynamo_table(
    table=self.idempotency_table,
    # Any sustained throttling on the idempotency table delays every request
    # (two serial writes per request ride the handler's bounded retry budget).
    add_read_throttled_events_count_alarm={"critical": ErrorCountThreshold(max_error_count=1)},
    add_write_throttled_events_count_alarm={"critical": ErrorCountThreshold(max_error_count=1)},
)
```

- [ ] **Step 4: Run to pass + snapshots** — `make test-cdk`; `UPDATE_SNAPSHOTS=1 make test-cdk`; review (new alarms + dashboard widgets only); green. The dev-shape nag test proves the non-prod suppression subtree still covers the new alarms.
- [ ] **Step 5: Commit** — `git commit -am "feat: add Lambda fault-rate and DynamoDB throttle alarms"`

---

### Task 5: WAF BlockedRequests alarms (+ shared operational-alarm routing helper)

**Files:**
- Modify: `infrastructure/nag_utils.py` (new helper), `infrastructure/backend_app.py` (new `_attach_waf_alarms`), `infrastructure/app_stage.py` (pass `cf_web_acl_name`), `infrastructure/backend_stack.py` (forward it)
- Test: `tests/cdk/test_stacks.py`

**Interfaces:**
- Produces: `route_operational_alarm(alarm: cloudwatch.Alarm, topic: sns.ITopic | None) -> None` in `nag_utils.py` (topic → `SnsAction`; None → the two `CloudWatchAlarmAction` suppressions). Consumed again in Task 6.
- Produces: `BackendApp(..., cf_web_acl_name: str | None = None)` / `BackendStack(..., cf_web_acl_name: str | None = None)`; `AppStage` passes `cf_web_acl_name=f"{waf_stack_name}-cf"` (a plain string — same no-cross-stack-ref technique as the WAF log locations).

- [ ] **Step 1: Verify metric dimensions** — search AWS docs (`aws___search_documentation`, phrase "AWS WAF CloudWatch metrics BlockedRequests dimensions WebACL Region Rule") and pin: REGIONAL ACL metrics dims `{Region, WebACL, Rule}` in the ACL's region; CLOUDFRONT ACL metrics dims (in us-east-1) — confirm whether `Region: "Global"` is required. Adjust Step 3's `dimensions_map` to exactly what the docs say.
- [ ] **Step 2: Write the failing test:**

```python
def test_waf_blocked_requests_alarms_exist(self, backend_template: Template) -> None:
    # Spike alarms on both WebACLs (TODO "WAF — BlockedRequests"); the
    # CloudFront-scoped one lives here too because its metrics are only in
    # us-east-1 (which is this fixture's region).
    alarms = backend_template.find_resources("AWS::CloudWatch::Alarm")
    blocked = [a for a in alarms.values() if a["Properties"].get("MetricName") == "BlockedRequests"]
    assert len(blocked) == 2, f"expected regional + CloudFront BlockedRequests alarms, got {len(blocked)}"
```

- [ ] **Step 3: Implement.**

`nag_utils.py` — add near `grant_cloudwatch_alarms_to_key` (new imports: `from aws_cdk import aws_cloudwatch as cloudwatch`, `from aws_cdk import aws_cloudwatch_actions as cloudwatch_actions`, `from aws_cdk import aws_sns as sns`):

```python
def route_operational_alarm(alarm: cloudwatch.Alarm, topic: sns.ITopic | None) -> None:
    """Wire an operational alarm to the environment's paging posture.

    Mirrors the MonitoringFacade defaults for alarms created OUTSIDE the facade:
    production passes the CMK-encrypted alarm topic (the alarm pages); non-prod
    passes None (the alarm is a dashboard signal only) and gets the same scoped
    CloudWatchAlarmAction acknowledgments the facade subtree carries.
    """
    if topic is not None:
        alarm.add_alarm_action(cloudwatch_actions.SnsAction(topic))
        return
    reason = "Ephemeral/dev environment — alarms are dashboard signals only; no paging channel by design"
    acknowledge_rules(
        alarm,
        [
            {"id": "NIST.800.53.R5-CloudWatchAlarmAction", "reason": reason},
            {"id": "HIPAA.Security-CloudWatchAlarmAction", "reason": reason},
        ],
    )
```

`backend_app.py` — thread `cf_web_acl_name: str | None = None` through `BackendStack.__init__` → `BackendApp.__init__` (document: "CloudFront WebACL name, passed by the Stage for the by-name BlockedRequests alarm; None skips it"). In `BackendApp.__init__`, after `self.alarm_topic = self._build_monitoring(...)`:

```python
# BlockedRequests spike alarms on both WebACLs. Created after _build_monitoring
# so the prod alarm topic exists to route to.
self._attach_waf_alarms(cf_web_acl_name)
```

New method (import `route_operational_alarm` from `nag_utils`):

```python
def _attach_waf_alarms(self, cf_web_acl_name: str | None) -> None:
    """Alarm on BlockedRequests spikes for the regional and CloudFront WebACLs.

    A sustained block spike is a leading indicator of an attack ramp (TODO
    "WAF — CloudWatch alarms"). Metrics are addressed by WebACL *name* (both
    names are deterministic strings) — no construct reference, no cross-stack
    or cross-region reference. The CloudFront ACL publishes its metrics only
    in us-east-1, so its alarm is created only when this stack IS in
    us-east-1 (the default deployment); other regions keep the regional alarm
    and document the gap. There is deliberately NO WCU alarm: WAFv2 publishes
    no WCU-consumption metric — WCU is static rule capacity (see TODO.md).
    Threshold is a reference value — size to real traffic in a fork.
    """
    stack = Stack.of(self)

    def _blocked_alarm(alarm_id: str, description: str, dimensions: dict[str, str]) -> None:
        alarm = cloudwatch.Alarm(
            self,
            alarm_id,
            metric=cloudwatch.Metric(
                namespace="AWS/WAFV2",
                metric_name="BlockedRequests",
                dimensions_map=dimensions,
                statistic="Sum",
                period=Duration.minutes(5),
            ),
            threshold=100,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description=description,
        )
        route_operational_alarm(alarm, self.alarm_topic)

    _blocked_alarm(
        "RegionalWafBlockedRequestsAlarm",
        "Sustained BlockedRequests spike on the regional (API Gateway) WebACL — possible attack ramp",
        {"Region": stack.region, "WebACL": f"{stack.stack_name}-api", "Rule": "ALL"},
    )
    if cf_web_acl_name is not None and stack.region == "us-east-1":
        _blocked_alarm(
            "CloudFrontWafBlockedRequestsAlarm",
            "Sustained BlockedRequests spike on the CloudFront WebACL — possible attack ramp",
            # Dimensions per Step 1's docs check (CLOUDFRONT-scope metrics live in us-east-1).
            {"WebACL": cf_web_acl_name, "Rule": "ALL"},
        )
```

`app_stage.py` — pass `cf_web_acl_name=f"{waf_stack_name}-cf"` into the `BackendStack(...)` call (with a comment referencing the WAF-log-location technique above it).

- [ ] **Step 4: Run to pass + snapshots** — `make test-cdk`; fix the exact dims per Step 1 if the nag/dev shapes complain; `UPDATE_SNAPSHOTS=1 make test-cdk`; review; green.
- [ ] **Step 5: Commit** — `git commit -am "feat: add WAF BlockedRequests spike alarms on both WebACLs"`

---

### Task 6: Athena query-failure + RUM session-spike alarms (frontend)

**Files:**
- Modify: `infrastructure/frontend_stack.py` (signature + new `_attach_analytics_alarms`), `infrastructure/app_stage.py` (pass `alarm_topic`)
- Test: `tests/cdk/test_stacks.py` (`TestFrontendStack`), `tests/cdk/test_stage.py`

**Interfaces:**
- Consumes: `route_operational_alarm` (Task 5); `BackendStack.app.alarm_topic` (`sns.Topic | None`, existing).
- Produces: `FrontendStack(..., alarm_topic: sns.ITopic | None = None)`. `AppStage` passes `alarm_topic=self.backend.app.alarm_topic` (frontend already depends on backend — no new edge; in non-prod it's None).

- [ ] **Step 1: Verify metric names** — docs search: "Athena CloudWatch metrics QueryState FAILED workgroup dimensions" (expect namespace `AWS/Athena`, dims `{QueryState, QueryType, WorkGroup}`, alarm on `SampleCount` of `TotalExecutionTime`) and "CloudWatch RUM metrics application_name SessionCount" (expect namespace `AWS/RUM`, dim `application_name`). Pin exact names into Step 3.
- [ ] **Step 2: Write the failing tests:**

```python
# TestFrontendStack
def test_athena_and_rum_alarms_exist(self, frontend_template: Template) -> None:
    alarms = frontend_template.find_resources("AWS::CloudWatch::Alarm")
    names = [a["Properties"].get("MetricName") for a in alarms.values()]
    assert "TotalExecutionTime" in names, "expected the Athena failed-queries alarm"
    assert "SessionCount" in names, "expected the RUM session-spike alarm"

def test_frontend_alarms_route_to_topic_in_prod(self, frontend_template: Template) -> None:
    alarms = frontend_template.find_resources("AWS::CloudWatch::Alarm")
    assert all(a["Properties"].get("AlarmActions") for a in alarms.values()), (
        "every frontend alarm must carry an SNS action in the prod shape"
    )
```

(The prod-shape fixture receives the topic once `AppStage` passes it; the dev-shape nag test in `test_stage.py` covers the suppression path.)

- [ ] **Step 3: Implement.** `frontend_stack.py`: add `alarm_topic: sns.ITopic | None = None,` to `__init__` (import `from aws_cdk import aws_sns as sns`, `from aws_cdk import aws_cloudwatch as cloudwatch`; import `route_operational_alarm` in the `nag_utils` import block). Document in the docstring: "the backend's CMK-encrypted alarm topic (None in non-prod) — the backend CMK already carries the CloudWatch-via-SNS grant". In `__init__`, store `self._rum_monitor_name = rum_monitor_name` next to where `rum_monitor_name` is computed (it is a local there today). At the end of `_create_athena_glue_resources`, call `self._attach_analytics_alarms(workgroup_name, self._rum_monitor_name)`. New method:

```python
def _attach_analytics_alarms(self, workgroup_name: str, rum_monitor_name: str) -> None:
    """Alarm on Athena query failures and RUM session spikes.

    Athena: publish_cloud_watch_metrics_enabled is already on for the
    workgroup; >=3 FAILED DML queries in an hour means the saved queries (or
    the Glue schemas under them) are broken — worth a look, not a page storm.
    RUM: the identity pool is necessarily public (browser RUM), so ingestion
    volume is the abuse signal — a session spike far above sample-app baseline
    is either real traffic or someone minting guest credentials (see TODO
    "Bound RUM ingestion cost"). The spend backstop is the AWS Budgets guard
    in the backend stack. Thresholds are reference values.
    """
    athena_failed = cloudwatch.Alarm(
        self,
        "AthenaFailedQueriesAlarm",
        metric=cloudwatch.Metric(
            namespace="AWS/Athena",
            metric_name="TotalExecutionTime",
            dimensions_map={"WorkGroup": workgroup_name, "QueryState": "FAILED", "QueryType": "DML"},
            statistic="SampleCount",
            period=Duration.hours(1),
        ),
        threshold=3,
        evaluation_periods=1,
        comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        alarm_description="Repeated Athena query failures in the access-logs workgroup",
    )
    route_operational_alarm(athena_failed, self._alarm_topic)

    rum_sessions = cloudwatch.Alarm(
        self,
        "RumSessionSpikeAlarm",
        metric=cloudwatch.Metric(
            namespace="AWS/RUM",
            metric_name="SessionCount",
            dimensions_map={"application_name": rum_monitor_name},
            statistic="Sum",
            period=Duration.hours(1),
        ),
        threshold=1000,
        evaluation_periods=1,
        comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        alarm_description="RUM session volume far above sample-app baseline — possible guest-credential abuse",
    )
    route_operational_alarm(rum_sessions, self._alarm_topic)
```

Store `self._alarm_topic = alarm_topic` in `__init__`. `app_stage.py`: pass `alarm_topic=self.backend.app.alarm_topic` in the `FrontendStack(...)` call (comment: legit cross-stack ref along the existing frontend→backend edge).

- [ ] **Step 4: Run to pass + snapshots** — `make test-cdk` (all shapes; dev shape proves the None-topic suppressions); `UPDATE_SNAPSHOTS=1 make test-cdk`; review; green.
- [ ] **Step 5: Commit** — `git commit -am "feat: add Athena query-failure and RUM session-spike alarms"`

---

### Task 7: CloudWatch spend budget (RUM cost backstop, prod only)

**Files:**
- Modify: `infrastructure/nag_utils.py` (`grant_budgets_to_key`), `infrastructure/backend_app.py` (`_build_alarm_topic` + new budget)
- Test: `tests/cdk/test_stacks.py`

**Interfaces:**
- Produces: `grant_budgets_to_key(key: kms.Key, *, account: str, region: str) -> None` in `nag_utils.py`.

- [ ] **Step 1: Verify the cost-filter service name** — docs search "AWS Budgets cost filter Service dimension Amazon CloudWatch value name" (Cost Explorer service names; expect `"Amazon CloudWatch"` — pin whatever the docs show into Step 3).
- [ ] **Step 2: Write the failing test:**

```python
def test_cloudwatch_spend_budget_notifies_alarm_topic(self, backend_template: Template) -> None:
    # RUM has no server-side ingestion cap (public guest pool by design), so a
    # spend budget is the backstop (TODO "Bound RUM ingestion cost").
    budgets_found = backend_template.find_resources("AWS::Budgets::Budget")
    assert len(budgets_found) == 1
    budget = next(iter(budgets_found.values()))["Properties"]
    subs = budget["NotificationsWithSubscribers"][0]["Subscribers"]
    assert subs[0]["SubscriptionType"] == "SNS"

def test_non_production_env_has_no_budget(self) -> None:
    app = cdk.App(context=_NO_BUNDLING)
    stage = AppStage(app, "S", region="us-east-1", env_name="dev-x")
    Template.from_stack(stage.backend).resource_count_is("AWS::Budgets::Budget", 0)
```

(Match the existing non-prod test style at `test_stacks.py:686` for the second one — reuse its stage-construction pattern.)

- [ ] **Step 3: Implement.** `nag_utils.py`:

```python
def grant_budgets_to_key(key: kms.Key, *, account: str, region: str) -> None:
    """Grant AWS Budgets the KMS operations to publish to a CMK-encrypted SNS topic.

    Same shape and caveats as :func:`grant_cloudwatch_alarms_to_key` — Budgets
    publishes via SNS as its service principal, so it needs Decrypt +
    GenerateDataKey* through sns.{region}; aws:SourceArn is deliberately
    omitted (not documented for the via-SNS KMS call; an unmatched required
    condition would silently drop the notification). Verify delivery on a live
    deploy when touching this statement.
    """
    key.add_to_resource_policy(
        iam.PolicyStatement(
            sid="AllowBudgetsViaSns",
            actions=["kms:Decrypt", "kms:GenerateDataKey*"],
            principals=[iam.ServicePrincipal("budgets.amazonaws.com")],
            resources=["*"],
            conditions={
                "StringEquals": {
                    "aws:SourceAccount": account,
                    "kms:ViaService": f"sns.{region}.amazonaws.com",
                },
            },
        )
    )
```

`backend_app.py` (import `from aws_cdk import aws_budgets as budgets` and `grant_budgets_to_key`): in `_build_alarm_topic`, after the CloudWatch statements:

```python
# AWS Budgets publishes the CloudWatch-spend notification to this same topic
# (see _attach_cloudwatch_spend_budget). Confused-deputy guard per the Budgets
# SNS docs: SourceAccount pins the caller's account.
grant_budgets_to_key(self.encryption_key, account=stack.account, region=stack.region)
topic.add_to_resource_policy(
    iam.PolicyStatement(
        sid="AllowBudgetsPublish",
        actions=["sns:Publish"],
        principals=[iam.ServicePrincipal("budgets.amazonaws.com")],
        resources=[topic.topic_arn],
        conditions={"StringEquals": {"aws:SourceAccount": stack.account}},
    )
)
```

In `__init__`, right after `self.alarm_topic = self._build_monitoring(...)`:

```python
if self.alarm_topic is not None:
    self._attach_cloudwatch_spend_budget(self.alarm_topic)
```

```python
def _attach_cloudwatch_spend_budget(self, topic: sns.Topic) -> None:
    """Monthly cost budget over the CloudWatch service (RUM bills under it).

    RUM's guest identity pool is necessarily public, ingestion is billed
    per-event with no server-side cap, and session_sample_rate is a
    client-side knob an abuser ignores — so spend is the authoritative abuse
    signal (TODO "Bound RUM ingestion cost"). Budgets are account-global;
    prod-only (the caller gates on the topic) and stack-named so multiple
    deployments never collide. $10/month is a reference limit for a
    sample-app baseline near $0 — an 80% actual-spend breach means something
    changed; size it to real traffic in a fork.
    """
    stack = Stack.of(self)
    budgets.CfnBudget(
        self,
        "CloudWatchSpendBudget",
        budget=budgets.CfnBudget.BudgetDataProperty(
            budget_name=f"{stack.stack_name}-cloudwatch-spend",
            budget_type="COST",
            time_unit="MONTHLY",
            budget_limit=budgets.CfnBudget.SpendProperty(amount=10, unit="USD"),
            cost_filters={"Service": ["Amazon CloudWatch"]},  # value per Step 1 docs check
        ),
        notifications_with_subscribers=[
            budgets.CfnBudget.NotificationWithSubscribersProperty(
                notification=budgets.CfnBudget.NotificationProperty(
                    notification_type="ACTUAL",
                    comparison_operator="GREATER_THAN",
                    threshold=80,
                    threshold_type="PERCENTAGE",
                ),
                subscribers=[
                    budgets.CfnBudget.SubscriberProperty(subscription_type="SNS", address=topic.topic_arn)
                ],
            )
        ],
    )
```

- [ ] **Step 4: Run to pass + snapshots** — `make test-cdk`; `UPDATE_SNAPSHOTS=1 make test-cdk`; review; green.
- [ ] **Step 5: Commit** — `git commit -am "feat: add prod CloudWatch spend budget notifying the alarm topic"`

---

### Task 8: Lambda Insights on ApiFunction

**Files:**
- Modify: `infrastructure/backend_app.py:370-432` (function), `:1170-1242` (suppressions)
- Test: `tests/cdk/test_stacks.py`

**Interfaces:** none.

- [ ] **Step 1: Pick the newest Insights version the pinned CDK knows:**

```bash
.venv/bin/python -c "from aws_cdk import aws_lambda as l; print([v for v in dir(l.LambdaInsightsVersion) if v.startswith('VERSION')])"
```

Use the highest listed (ARM64 needs ≥ 1.0.119; any current constant is fine).

- [ ] **Step 2: Write the failing test:**

```python
def test_lambda_insights_enabled(self, backend_template: Template) -> None:
    # Insights = extension layer + the managed policy on the execution role.
    fn = backend_template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": "app.lambda_handler"}}
    )
    layers = json.dumps(next(iter(fn.values()))["Properties"].get("Layers", []))
    assert "LambdaInsightsExtension" in layers, "expected the Lambda Insights extension layer"
```

- [ ] **Step 3: Implement** — on the `PythonFunction`, after `reserved_concurrent_executions=100,`:

```python
# Lambda Insights: CPU/memory-utilization/network enhanced metrics via the
# extension layer (~$0.50/month for this one function). The extension writes
# to the REGIONAL, account-shared /aws/lambda-insights log group — deliberately
# NOT owned or cleaned up by this stack (claiming a shared-name regional group
# would collide with any other Insights user in the account; same principle as
# the account-wide constructs this template avoids — see CLAUDE.md).
insights_version=_lambda.LambdaInsightsVersion.VERSION_1_0_XXX,  # highest from Step 1
```

Add to the `self.function` acknowledgments block (`_add_resource_suppressions`), next to the existing IAM4 entry:

```python
{
    "id": "AwsSolutions-IAM4",
    "reason": "CloudWatchLambdaInsightsExecutionRolePolicy is the AWS-documented policy for the Insights extension (log + metric writes)",
    "applies_to": ["Policy::arn:<AWS::Partition>:iam::aws:policy/CloudWatchLambdaInsightsExecutionRolePolicy"],
},
```

If `make test-cdk` reports a differently rendered finding id, copy the exact id from the gate output.

- [ ] **Step 4: Run to pass + snapshots** — `make test-cdk`; `UPDATE_SNAPSHOTS=1 make test-cdk`; review; green.
- [ ] **Step 5: Commit** — `git commit -am "feat: enable Lambda Insights on the API function"`

---

### Task 9: AWS Backup plan for DynamoDB (`retain_data=true` only)

**Files:**
- Modify: `infrastructure/data_stack.py`
- Test: `tests/cdk/test_stacks.py` (`TestDataStack`, fixtures `data_template` / `data_template_retained` exist)

**Interfaces:** none exported.

- [ ] **Step 1: Write the failing tests:**

```python
def test_default_shape_has_no_backup_plan(self, data_template: Template) -> None:
    data_template.resource_count_is("AWS::Backup::BackupVault", 0)

def test_retained_shape_has_backup_vault_plan_and_selection(self, data_template_retained: Template) -> None:
    # retain_data=True is the production posture; PITR (1-day window) alone
    # can't satisfy long-horizon compliance retention (TODO "AWS Backup plan").
    data_template_retained.resource_count_is("AWS::Backup::BackupVault", 1)
    data_template_retained.resource_count_is("AWS::Backup::BackupPlan", 1)
    data_template_retained.resource_count_is("AWS::Backup::BackupSelection", 1)
```

- [ ] **Step 2: Run to fail** — `make test-cdk`.
- [ ] **Step 3: Implement** — `data_stack.py` (add `from aws_cdk import aws_backup as backup`). Replace the unconditional `DynamoDBInBackupPlan` acknowledgment block with:

```python
if retain_data:
    # Production posture: AWS Backup on top of PITR. PITR's rolling window
    # (1 day here — records TTL out after an hour) covers oops-recovery;
    # AWS Backup covers the compliance horizon: daily backups kept 35 days
    # plus a monthly backup moved to cold storage and kept a year. The vault
    # uses this stack's CMK (key lives with the data — module docstring) and
    # is RETAINed like the table it protects. This retires the
    # DynamoDBInBackupPlan suppressions in the retain shape; the
    # destroy-friendly default keeps them below.
    vault = backup.BackupVault(
        self,
        "IdempotencyBackupVault",
        encryption_key=self.encryption_key,
        removal_policy=RemovalPolicy.RETAIN,
    )
    plan = backup.BackupPlan(
        self,
        "IdempotencyBackupPlan",
        backup_vault=vault,
        backup_plan_rules=[
            backup.BackupPlanRule.daily(),
            backup.BackupPlanRule.monthly1_year(),
        ],
    )
    plan.add_selection(
        "IdempotencyTableSelection",
        resources=[backup.BackupResource.from_dynamo_db_table(self.idempotency_table)],
    )
    # The selection's auto-created role uses the AWS-documented service policy.
    acknowledge_rules(
        plan,
        [
            {
                "id": "AwsSolutions-IAM4",
                "reason": "AWSBackupServiceRolePolicyForBackup is the documented service role policy for AWS Backup selections",
                "applies_to": [
                    "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
                ],
            },
        ],
    )
else:
    # PITR-only in the destroy-friendly default: the idempotency cache is
    # regenerable data; a backup plan would slow teardown for no recovery value.
    acknowledge_rules(
        self,
        [
            {
                "id": "NIST.800.53.R5-DynamoDBInBackupPlan",
                "reason": "Destroy-friendly default — PITR covers the regenerable idempotency cache; retain_data=true adds the AWS Backup plan",
            },
            {
                "id": "HIPAA.Security-DynamoDBInBackupPlan",
                "reason": "Destroy-friendly default — PITR covers the regenerable idempotency cache; retain_data=true adds the AWS Backup plan",
            },
        ],
    )
```

- [ ] **Step 4: Run to pass** — `make test-cdk`. The `test_retain_data_shape_has_no_unacknowledged_findings` gate will surface any missing retain-shape suppressions — copy exact finding ids from its output if needed. Default-shape snapshots must be unchanged (`git diff --stat tests/cdk/snapshots/` empty).
- [ ] **Step 5: Commit** — `git commit -am "feat: add retain_data-gated AWS Backup plan for the idempotency table"`

---

### Task 10: Audit-bucket compliance tier (`retain_data=true` only)

**Files:**
- Modify: `infrastructure/nag_utils.py:527-586` (`create_sse_s3_log_bucket`), `infrastructure/audit_stack.py:131-143`
- Test: `tests/cdk/test_stacks.py` (add a retained-audit fixture + tests; mirror `data_template_retained`)

**Interfaces:**
- Produces: `create_sse_s3_log_bucket(..., versioned: bool = False, object_lock_default_retention: s3.ObjectLockRetention | None = None, transitions: list[s3.Transition] | None = None)` — defaults preserve all existing call sites byte-for-byte.

- [ ] **Step 1: Write the failing tests** (add an `audit_template_retained` module fixture mirroring how `data_template_retained` is built, then):

```python
def test_default_audit_bucket_shape_unchanged(self, audit_template: Template) -> None:
    buckets = audit_template.find_resources("AWS::S3::Bucket")
    assert all("ObjectLockConfiguration" not in b["Properties"] for b in buckets.values())

def test_retained_audit_bucket_has_object_lock_and_archive_tiering(self, audit_template_retained: Template) -> None:
    # Compliance tier (TODO "Audit-grade log retention"): versioning + Object
    # Lock (GOVERNANCE 1y) + Glacier@90d → Deep Archive@365d → expire @ 7y.
    audit_template_retained.has_resource_properties(
        "AWS::S3::Bucket",
        Match.object_like(
            {
                "VersioningConfiguration": {"Status": "Enabled"},
                "ObjectLockEnabled": True,
                "ObjectLockConfiguration": Match.object_like(
                    {"Rule": {"DefaultRetention": {"Mode": "GOVERNANCE", "Days": 365}}}
                ),
                "LifecycleConfiguration": Match.object_like(
                    {
                        "Rules": Match.array_with(
                            [
                                Match.object_like(
                                    {
                                        "ExpirationInDays": 2555,
                                        "Transitions": Match.array_with(
                                            [
                                                Match.object_like({"StorageClass": "GLACIER", "TransitionInDays": 90}),
                                                Match.object_like(
                                                    {"StorageClass": "DEEP_ARCHIVE", "TransitionInDays": 365}
                                                ),
                                            ]
                                        ),
                                    }
                                )
                            ]
                        )
                    }
                ),
            }
        ),
    )
```

- [ ] **Step 2: Run to fail** — `make test-cdk`.
- [ ] **Step 3: Implement.** `nag_utils.py` — extend the helper (keyword-only additions; docstring gains: "versioned/object_lock/transitions serve the retain_data compliance tier on the CloudTrail bucket — Object Lock is creation-time-only, so flipping it on an existing deployment REPLACES the bucket"):

```python
def create_sse_s3_log_bucket(
    scope: Construct,
    construct_id: str,
    *,
    suppression_reason: str,
    expiration_days: int,
    removal_policy: RemovalPolicy,
    auto_delete: bool,
    bucket_name: str | None = None,
    object_ownership: s3.ObjectOwnership | None = None,
    versioned: bool = False,
    object_lock_default_retention: s3.ObjectLockRetention | None = None,
    transitions: list[s3.Transition] | None = None,
) -> s3.Bucket:
    ...
    bucket = s3.Bucket(
        scope,
        construct_id,
        bucket_name=bucket_name,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        encryption=s3.BucketEncryption.S3_MANAGED,
        enforce_ssl=True,
        object_ownership=object_ownership,
        versioned=versioned,
        object_lock_default_retention=object_lock_default_retention,
        lifecycle_rules=[
            s3.LifecycleRule(
                id=f"ExpireAfter{expiration_days}Days",
                enabled=True,
                expiration=Duration.days(expiration_days),
                transitions=transitions,
                # Versioned buckets: expiry only adds a delete marker; the
                # noncurrent rule is what actually reclaims storage.
                noncurrent_version_expiration=Duration.days(expiration_days) if versioned else None,
                abort_incomplete_multipart_upload_after=Duration.days(1),
            ),
        ],
        removal_policy=removal_policy,
        auto_delete_objects=auto_delete,
    )
    # Versioning suppressions only apply while the bucket is actually unversioned.
    rules = [r for r in _LOG_SINK_SUPPRESSION_RULES if not (versioned and "Versioning" in r)]
    acknowledge_rules(bucket, [{"id": rule, "reason": suppression_reason} for rule in rules])
    return bucket
```

`audit_stack.py` — change the `CloudTrailLogsBucket` call:

```python
# Compliance tier rides retain_data: 7-year expiry with Glacier@90d /
# Deep Archive@365d tiering, versioning, and Object Lock (GOVERNANCE, 1-year
# default retention — write-once for the audit horizon a fork's compliance
# scope needs; raise to COMPLIANCE mode + longer retention deliberately).
# CAUTION: Object Lock is creation-time-only — flipping retain_data on an
# already-deployed stack REPLACES this bucket (documented in README); flip it
# before real audit data accumulates. Default shape is unchanged: 90-day
# expiry, no versioning, destroy-friendly.
cloudtrail_log_bucket = create_sse_s3_log_bucket(
    self,
    "CloudTrailLogsBucket",
    suppression_reason=(
        "CloudTrail log bucket — SSE-S3 (CloudTrail delivery doesn't support KMS-CMK "
        "destination buckets; trail log files are per-object SSE-KMS), self-logging would "
        "create circular audit trails, no replication for an append-only, "
        "integrity-validated log sink"
    ),
    expiration_days=2555 if retain_data else 90,
    removal_policy=removal_policy,
    auto_delete=auto_delete,
    versioned=retain_data,
    object_lock_default_retention=s3.ObjectLockRetention.governance(Duration.days(365)) if retain_data else None,
    transitions=(
        [
            s3.Transition(storage_class=s3.StorageClass.GLACIER, transition_after=Duration.days(90)),
            s3.Transition(storage_class=s3.StorageClass.DEEP_ARCHIVE, transition_after=Duration.days(365)),
        ]
        if retain_data
        else None
    ),
)
```

- [ ] **Step 4: Run to pass** — `make test-cdk` (the retain-shape nag test surfaces any newly-live versioning-rule findings — none expected since versioned=True satisfies them). Default snapshots unchanged.
- [ ] **Step 5: Commit** — `git commit -am "feat: add retain_data-gated Object Lock and archive tiering to the CloudTrail bucket"`

---

### Task 11: WAF log header redaction + drop-ALLOW logging filter

**Files:**
- Modify: `infrastructure/nag_utils.py` (shared constants), `infrastructure/waf_stack.py:135-142`, `infrastructure/backend_app.py:954-962`
- Test: `tests/cdk/test_stacks.py` (both `TestWafStack` and `TestBackendStack`)

**Interfaces:**
- Produces in `nag_utils.py`: `waf_log_redacted_fields() -> list[wafv2.CfnLoggingConfiguration.FieldToMatchProperty]` and `WAF_LOG_DROP_ALLOW_FILTER: dict` (shared so the two ACLs never drift — same R0801 rationale as `build_managed_threat_rules`).

- [ ] **Step 1: Write the failing tests** (one per stack class; same body, different fixture):

```python
def test_waf_logging_redacts_credentials_and_drops_allow(self, waf_template: Template) -> None:
    waf_template.has_resource_properties(
        "AWS::WAFv2::LoggingConfiguration",
        Match.object_like(
            {
                "RedactedFields": Match.array_with(
                    [
                        Match.object_like({"SingleHeader": {"Name": "authorization"}}),
                        Match.object_like({"SingleHeader": {"Name": "cookie"}}),
                    ]
                ),
                "LoggingFilter": Match.object_like({"DefaultBehavior": "KEEP"}),
            }
        ),
    )
```

- [ ] **Step 2: Run to fail** — `make test-cdk`.
- [ ] **Step 3: Implement.** `nag_utils.py`:

```python
def waf_log_redacted_fields() -> list[wafv2.CfnLoggingConfiguration.FieldToMatchProperty]:
    """Headers scrubbed from WAF logs at write time (both WebACLs share this).

    WAF logs carry full request headers by default; if the API ever accepts an
    Authorization header or a session cookie, it must not land in the
    aws-waf-logs-* buckets unredacted (TODO "WAF logging — redacted_fields").
    A function (not a module constant) so each CfnLoggingConfiguration gets its
    own property instances.
    """
    return [
        wafv2.CfnLoggingConfiguration.FieldToMatchProperty(single_header={"Name": "authorization"}),
        wafv2.CfnLoggingConfiguration.FieldToMatchProperty(single_header={"Name": "cookie"}),
    ]


# Drop ALLOW logs, keep BLOCK/COUNT/CAPTCHA/CHALLENGE: log volume then scales
# with threat traffic, not with legitimate traffic (TODO "logging_filter").
# Traffic analytics stay available via the CloudFront/S3 access-log tables;
# the WAF Athena queries analyze blocked traffic and are unaffected.
WAF_LOG_DROP_ALLOW_FILTER: dict[str, object] = {
    "DefaultBehavior": "KEEP",
    "Filters": [
        {
            "Behavior": "DROP",
            "Requirement": "MEETS_ALL",
            "Conditions": [{"ActionCondition": {"Action": "ALLOW"}}],
        }
    ],
}
```

Both `CfnLoggingConfiguration` sites gain (import the two names at each site):

```python
redacted_fields=waf_log_redacted_fields(),
logging_filter=WAF_LOG_DROP_ALLOW_FILTER,
```

If synth rejects the raw `logging_filter` dict shape, check the L1's expected property casing via `.venv/bin/python -c "import inspect; from aws_cdk import aws_wafv2 as w; print(inspect.signature(w.CfnLoggingConfiguration.__init__))"` and adjust (the property is typed `Any` in most releases; the AWS API shape above is the documented one).

- [ ] **Step 4: Run to pass + snapshots** — `make test-cdk`; `UPDATE_SNAPSHOTS=1 make test-cdk`; review both WAF-touching snapshots; green.
- [ ] **Step 5: Commit** — `git commit -am "feat: redact credential headers and drop ALLOW records in WAF logs"`

---

### Task 12: API Gateway EDGE→REGIONAL + request validator

**Files:**
- Modify: `infrastructure/backend_app.py:507-582`, `infrastructure/backend_stack.py:161` (drop APIG2 suppression)
- Test: `tests/cdk/test_stacks.py`

**Interfaces:** none exported (the execute-api URL format is unchanged by the endpoint-type migration — CFN "No interruption", verified in the spec).

- [ ] **Step 1: Write the failing tests:**

```python
def test_api_is_regional(self, backend_template: Template) -> None:
    # CloudFront fronts the API (Task 14), so the EDGE layer is redundant;
    # REGIONAL also unlocks the regional security-policy set for a future
    # custom domain. CFN updates EndpointConfiguration in place.
    backend_template.has_resource_properties(
        "AWS::ApiGateway::RestApi", {"EndpointConfiguration": {"Types": ["REGIONAL"]}}
    )

def test_request_validator_attached(self, backend_template: Template) -> None:
    backend_template.has_resource_properties(
        "AWS::ApiGateway::RequestValidator",
        {"ValidateRequestBody": True, "ValidateRequestParameters": True},
    )
    backend_template.has_resource_properties(
        "AWS::ApiGateway::Method",
        Match.object_like({"HttpMethod": "GET", "RequestValidatorId": Match.any_value()}),
    )
```

- [ ] **Step 2: Run to fail** — `make test-cdk`.
- [ ] **Step 3: Implement** — in the `RestApi` constructor add (before `deploy_options`):

```python
# REGIONAL: CloudFront fronts this API (the /api/* behavior in the frontend
# stack), making API Gateway's own edge layer a redundant second CDN hop.
# EndpointConfiguration updates in place per the CFN reference ("No
# interruption") and the execute-api URL does not change.
endpoint_configuration=apigw.EndpointConfiguration(types=[apigw.EndpointType.REGIONAL]),
```

After the `RestApi` block, before `greeting_resource`:

```python
# Gateway-layer request validation (retires AwsSolutions-APIG2). /greeting
# takes no parameters or body today, so this is the gate machinery installed
# and asserted, live for the first parameterized route — API Gateway then
# rejects malformed requests before they reach Lambda billing.
request_validator = self.api.add_request_validator(
    "RequestValidator",
    validate_request_body=True,
    validate_request_parameters=True,
)
```

Change the method line:

```python
greeting_resource.add_method("GET", apigw.LambdaIntegration(self.alias), request_validator=request_validator)
```

In `backend_stack.py`, delete the `AwsSolutions-APIG2` suppression line and leave a retirement comment mirroring the APIG3 style ("no longer suppressed — a RequestValidator is attached in BackendApp").

- [ ] **Step 4: Run to pass + snapshots** — `make test-cdk` (nag gate proves APIG2 passes without the suppression); `UPDATE_SNAPSHOTS=1 make test-cdk`; review; green.
- [ ] **Step 5: Commit** — `git commit -am "feat: convert the API to REGIONAL and attach gateway request validation"`

---

### Task 13: Origin-verify secret + WAF RejectNonCloudFront rule

**Files:**
- Modify: `infrastructure/backend_app.py` (secret + `_attach_regional_waf`), `infrastructure/backend_stack.py` (expose secret)
- Test: `tests/cdk/test_stacks.py`

**Interfaces:**
- Produces: `BackendApp.origin_verify_secret` / `BackendStack.origin_verify_secret` (`secretsmanager.ISecret`) — consumed by Task 14 via `AppStage`.
- Removes: the `RateLimitDirectCallers` WAF rule (fully superseded — every non-CloudFront caller is now blocked outright, not merely rate-limited).

- [ ] **Step 1: Write the failing tests** — replace `test_regional_waf_rate_limits_direct_callers_only` (`test_stacks.py:497`) with:

```python
def test_regional_waf_rejects_requests_without_origin_secret(self, backend_template: Template) -> None:
    # Origin lockdown (TODO "Close the CloudFront-bypass window", option b):
    # CloudFront injects x-origin-verify (frontend stack); this rule blocks
    # anything that doesn't carry it, so the direct execute-api URL rejects
    # non-CloudFront callers outright. Supersedes the RateLimitDirectCallers
    # rate rule (blocking beats rate-limiting the same traffic).
    acls = backend_template.find_resources("AWS::WAFv2::WebACL")
    regional = next(a for a in acls.values() if a["Properties"]["Scope"] == "REGIONAL")
    rules = {r["Name"]: r for r in regional["Properties"]["Rules"]}
    assert "RateLimitDirectCallers" not in rules
    reject = rules["RejectNonCloudFront"]
    assert reject["Action"] == {"Block": {}}
    byte_match = reject["Statement"]["NotStatement"]["Statement"]["ByteMatchStatement"]
    assert byte_match["FieldToMatch"] == {"SingleHeader": {"Name": "x-origin-verify"}}
    assert byte_match["PositionalConstraint"] == "EXACTLY"
    assert "{{resolve:secretsmanager:" in json.dumps(byte_match["SearchString"])

def test_origin_verify_secret_is_cmk_encrypted(self, backend_template: Template) -> None:
    backend_template.has_resource_properties(
        "AWS::SecretsManager::Secret", Match.object_like({"KmsKeyId": Match.any_value()})
    )
```

- [ ] **Step 2: Run to fail** — `make test-cdk`.
- [ ] **Step 3: Implement.** `backend_app.py` (add `from aws_cdk import aws_secretsmanager as secretsmanager` to the import block). In `__init__`, before `self._attach_regional_waf()`:

```python
# Shared origin-verify secret: CloudFront (frontend stack) injects it as the
# x-origin-verify header toward the API origin; the regional WAF blocks any
# request without it (_attach_regional_waf). No automatic rotation: a rotation
# Lambda would have to mutate the CFN-managed CloudFront origin header and WAF
# rule out-of-band (drift). Manual rotation = put a new secret value, then
# `make deploy` (both consumers read it via CFN dynamic references at deploy).
# Known, accepted exposure: the resolved value is readable via waf:GetWebACL —
# it is an origin discriminator for defense in depth, not a credential.
self.origin_verify_secret = secretsmanager.Secret(
    self,
    "OriginVerifySecret",
    description="Header value CloudFront must present to reach the API origin",
    encryption_key=self.encryption_key,
    generate_secret_string=secretsmanager.SecretStringGenerator(
        exclude_punctuation=True,
        include_space=False,
        password_length=32,
    ),
    removal_policy=RemovalPolicy.DESTROY,
)
```

In `_attach_regional_waf`, replace the whole `direct_caller_rate_rule` block with:

```python
# Block anything not funneled through CloudFront (TODO option (b) — the
# stronger origin lockdown). Priority 4 (after the shared managed groups at
# 0-3, whose priorities are shared with the CloudFront ACL and must not be
# renumbered): a funneled request carries the header and passes; a direct
# execute-api caller doesn't and is blocked — which supersedes the previous
# XFF-scoped RateLimitDirectCallers rate rule entirely. The byte-match reads
# the secret via a CFN dynamic reference, resolved at deploy time.
# DEPLOY-ORDER CAVEAT: the backend deploys before the frontend, so during the
# one deploy that introduces this rule, browsers still on the old direct
# execute-api config.json get 403s until the frontend behavior + cache
# invalidation land (minutes). Documented in README.
reject_non_cloudfront_rule = wafv2.CfnWebACL.RuleProperty(
    name="RejectNonCloudFront",
    priority=4,
    action=wafv2.CfnWebACL.RuleActionProperty(block={}),
    statement=wafv2.CfnWebACL.StatementProperty(
        not_statement=wafv2.CfnWebACL.NotStatementProperty(
            statement=wafv2.CfnWebACL.StatementProperty(
                byte_match_statement=wafv2.CfnWebACL.ByteMatchStatementProperty(
                    field_to_match=wafv2.CfnWebACL.FieldToMatchProperty(
                        single_header={"Name": "x-origin-verify"}
                    ),
                    positional_constraint="EXACTLY",
                    search_string=self.origin_verify_secret.secret_value.unsafe_unwrap(),
                    text_transformations=[
                        wafv2.CfnWebACL.TextTransformationProperty(priority=0, type="NONE")
                    ],
                )
            )
        )
    ),
    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
        cloud_watch_metrics_enabled=True,
        metric_name=f"{stack.stack_name}-api-RejectNonCloudFront",
        sampled_requests_enabled=True,
    ),
)
```

…and use `rules=[*build_managed_threat_rules(f"{stack.stack_name}-api"), reject_non_cloudfront_rule]`. Update the method docstring (the rate-rule paragraphs → the lockdown story). Update `_attach_regional_waf`'s dead references and the `__init__` comment at line 604.

Run `make test-cdk`; the nag gate will report the secret-rotation findings (`AwsSolutions-SMG4` plus the NIST/HIPAA/PCI rotation rules) with exact ids — acknowledge them on `self.origin_verify_secret`:

```python
rotation_reason = (
    "No automatic rotation by design: rotating requires updating the CFN-managed CloudFront "
    "origin header and WAF rule together — a rotation Lambda would mutate both out-of-band "
    "(drift). Manual rotation: put a new secret value, then redeploy (both consumers resolve "
    "it via CFN dynamic references)."
)
acknowledge_rules(
    self.origin_verify_secret,
    [
        {"id": "AwsSolutions-SMG4", "reason": rotation_reason},
        # plus the NIST/HIPAA/PCI rotation rule ids exactly as the gate output names them
    ],
)
```

`backend_stack.py`: add `self.origin_verify_secret = self.app.origin_verify_secret` next to `self.api_url`.

- [ ] **Step 4: Run to pass + snapshots** — `make test-cdk` all green; `UPDATE_SNAPSHOTS=1 make test-cdk`; review; green.
- [ ] **Step 5: Commit** — `git commit -am "feat: block non-CloudFront callers at the regional WAF via an origin-verify secret"`

---

### Task 14: CloudFront `/api/*` behavior, URL rewrite, CSP, and relative apiUrl

**Files:**
- Modify: `infrastructure/frontend_stack.py` (signature, distribution, CSP, config.json), `infrastructure/app_stage.py` (pass secret, drop `api_url`)
- Test: `tests/cdk/test_stacks.py` (`TestFrontendStack`)

**Interfaces:**
- Consumes: `BackendStack.origin_verify_secret` (Task 13), `api_id` (existing).
- Changes: `FrontendStack.__init__` **drops `api_url`** (config.json now ships the relative `/api`) and **adds `origin_verify_secret: secretsmanager.ISecret`**. `AppStage` updates the call site accordingly. `BackendStack.api_url` stays (used by `ApiUrlOutput`).

- [ ] **Step 1: Write the failing tests:**

```python
def test_api_behavior_proxies_to_api_gateway(self, frontend_template: Template) -> None:
    # Same-origin API: /api/* rides the distribution to the execute-api origin
    # with caching disabled and all-viewer-except-host forwarding (API Gateway
    # must receive its own Host header; RUM's X-Amzn-Trace-Id passes through).
    frontend_template.has_resource_properties(
        "AWS::CloudFront::Distribution",
        Match.object_like(
            {
                "DistributionConfig": Match.object_like(
                    {
                        "CacheBehaviors": Match.array_with(
                            [
                                Match.object_like(
                                    {
                                        "PathPattern": "/api/*",
                                        # Managed CachingDisabled / AllViewerExceptHostHeader policy ids
                                        "CachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
                                        "OriginRequestPolicyId": "b689b0a8-53d0-40ab-baf2-68738e2966ac",
                                        "ViewerProtocolPolicy": "https-only",
                                    }
                                )
                            ]
                        )
                    }
                )
            }
        ),
    )

def test_api_origin_injects_origin_verify_header(self, frontend_template: Template) -> None:
    frontend_template.has_resource_properties(
        "AWS::CloudFront::Distribution",
        Match.object_like(
            {
                "DistributionConfig": Match.object_like(
                    {
                        "Origins": Match.array_with(
                            [
                                Match.object_like(
                                    {
                                        "OriginPath": "/Prod",
                                        "OriginCustomHeaders": Match.array_with(
                                            [Match.object_like({"HeaderName": "x-origin-verify"})]
                                        ),
                                    }
                                )
                            ]
                        )
                    }
                )
            }
        ),
    )

def test_api_path_rewrite_function_exists(self, frontend_template: Template) -> None:
    # CloudFront does NOT strip the matched path pattern: /api/greeting would
    # reach the origin as /Prod/api/greeting without this viewer-request rewrite.
    frontend_template.resource_count_is("AWS::CloudFront::Function", 1)

def test_csp_connect_src_has_no_execute_api_host(self, frontend_template: Template) -> None:
    policies = frontend_template.find_resources("AWS::CloudFront::ResponseHeadersPolicy")
    csp = json.dumps(policies)
    assert "execute-api" not in csp, "same-origin /api means the CSP no longer needs the execute-api host"
```

- [ ] **Step 2: Run to fail** — `make test-cdk`.
- [ ] **Step 3: Implement** in `frontend_stack.py` (add `from aws_cdk import aws_secretsmanager as secretsmanager`):

Signature: remove `api_url: str,`, add `origin_verify_secret: secretsmanager.ISecret,` (docstring: "injected as the x-origin-verify custom origin header; the regional WAF blocks requests without it — see BackendApp._attach_regional_waf"). Before the `Distribution`:

```python
# CloudFront functions run per viewer request on the /api/* behavior only.
# CloudFront does not strip the matched path pattern, and the origin_path
# below prepends /Prod — without this rewrite, /api/greeting would reach
# API Gateway as /Prod/api/greeting (404). JS 2.0 runtime; ~free at this scale.
api_rewrite_fn = cloudfront.Function(
    self,
    "ApiPathRewriteFunction",
    comment="Strip the /api prefix before forwarding to the API Gateway origin",
    runtime=cloudfront.FunctionRuntime.JS_2_0,
    code=cloudfront.FunctionCode.from_inline(
        "function handler(event) {\n"
        "  var request = event.request;\n"
        "  request.uri = request.uri.replace(/^\\/api/, '');\n"
        "  if (request.uri === '') { request.uri = '/'; }\n"
        "  return request;\n"
        "}"
    ),
)

api_origin = origins.HttpOrigin(
    f"{self._api_id}.execute-api.{self.region}.amazonaws.com",
    origin_path="/Prod",
    protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
    custom_headers={
        # Resolved at deploy via a CFN dynamic reference — the same value the
        # regional WAF's RejectNonCloudFront rule matches on.
        "x-origin-verify": origin_verify_secret.secret_value.unsafe_unwrap(),
    },
)
```

Distribution gains:

```python
additional_behaviors={
    "/api/*": cloudfront.BehaviorOptions(
        origin=api_origin,
        viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
        allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
        cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
        origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
        function_associations=[
            cloudfront.FunctionAssociation(
                event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                function=api_rewrite_fn,
            )
        ],
    ),
},
```

On the `error_responses` block add the caveat comment:

```python
# NOTE: custom error responses are distribution-wide — a 403/404 emitted by
# the /api/* origin (e.g. a WAF managed-rule block, an unknown API route) is
# also rewritten to index.html+200. The API's own contract codes (400/409/500)
# are unaffected. Accepted for this reference app; a fork that needs raw API
# 403/404s should move the SPA fallback into a CloudFront Function instead.
```

config.json: `"apiUrl": "/api",` (comment: relative path — same-origin through the /api/* behavior; CORS machinery deleted with it). CSP: delete the `f"https://{self._api_id}.execute-api..."` line from `connect-src` and update `_build_response_headers_policy`'s docstring accordingly (api_id is still used for the origin domain).

`app_stage.py`: in the `FrontendStack(...)` call, delete `api_url=self.backend.api_url,` and add `origin_verify_secret=self.backend.origin_verify_secret,`.

- [ ] **Step 4: Run to pass + snapshots** — `make test-cdk`; new nag findings, if any, get exact-id acknowledgments; `UPDATE_SNAPSHOTS=1 make test-cdk`; review; green.
- [ ] **Step 5: Commit** — `git commit -am "feat: route the API same-origin through CloudFront with origin-verify header injection"`

---

### Task 15: Remove CORS (Lambda + API Gateway preflight) and regenerate OpenAPI

**Files:**
- Modify: `lambda/app.py:109-127,384-417`, `infrastructure/backend_app.py:572-582`
- Test: `tests/unit/test_handler.py:173-174,459`; `docs/openapi.json` (regenerated)

**Interfaces:** none exported. The browser call is same-origin after Task 14, so no CORS headers are needed anywhere.

- [ ] **Step 1: Update the unit tests first** — in `tests/unit/test_handler.py`, replace the two `Access-Control-Allow-Origin` assertions (lines ~174 and ~459) with negative assertions:

```python
# Same-origin through CloudFront (config.json apiUrl=/api): no CORS headers
# anywhere — stronger than restricting allow_origin (TODO "CORS origin restriction").
assert "Access-Control-Allow-Origin" not in ret["headers"]
assert ret["headers"]["Content-Type"] == "application/json"
```

- [ ] **Step 2: Run to fail** — `make test` → the two updated tests FAIL (handler still emits the header).
- [ ] **Step 3: Implement.** `lambda/app.py`:
  - Delete the `CORSConfig` import and the whole `cors=CORSConfig(...)` argument: `app = APIGatewayRestResolver(enable_validation=True)` (keep the enable_validation comment block; replace the CORS comment with: "No CORSConfig: the browser reaches this API same-origin via CloudFront's /api/* behavior, so CORS does not apply — see infrastructure/frontend_stack.py").
  - In the 400 and 409 hand-built responses, drop the `"Access-Control-Allow-Origin": "*",` line and rewrite each headers comment to note the responses are same-origin so no CORS header is required.

  `infrastructure/backend_app.py`: delete the `greeting_resource.add_cors_preflight(...)` call and its comment block (the X-Amzn-Trace-Id note moves to the frontend's ALL_VIEWER_EXCEPT_HOST_HEADER comment, already written in Task 14).
- [ ] **Step 4: Run to pass** — `make test` (100% coverage holds — only deletions), `make openapi` then `git diff docs/openapi.json` (expect no change — CORS never appeared in the spec; commit if it did), `make test-cdk` + `UPDATE_SNAPSHOTS=1 make test-cdk` (OPTIONS method disappears from the backend snapshot).
- [ ] **Step 5: Commit** — `git commit -am "feat: remove CORS machinery — the API is same-origin behind CloudFront"`

---

### Task 16: Integration tests — CloudFront path + direct-URL 403

**Files:**
- Modify: `tests/integration/test_api_gateway.py`, `tests/integration/test_frontend.py:62-69`, `pyproject.toml:273` (env block)
- Test: these ARE the tests (they skip without a live stack; CI stays green).

**Interfaces:**
- Consumes: `CloudFrontDomainName` output (frontend stack, existing), `ApiUrlOutput` (backend, existing).

- [ ] **Step 1: pyproject env** — next to `AWS_BACKEND_STACK_NAME=ServerlessAppBackend-us-east-1` add `"AWS_FRONTEND_STACK_NAME=ServerlessAppFrontend-us-east-1",`.
- [ ] **Step 2: Rework `test_api_gateway.py`.** Add a `cloudfront_api_url` fixture (same skip pattern as the existing one, reading `AWS_FRONTEND_STACK_NAME` + `CloudFrontDomainName`, returning `f"{domain}/api/greeting"`); repoint `test_api_gateway`, `test_api_gateway_response_headers`, `test_api_gateway_response_time_warm`, and `test_missing_idempotency_key_returns_400` at it; keep the old `api_gateway_url` fixture solely for:

```python
def test_direct_execute_api_is_blocked(self, api_gateway_url):
    """The origin-lockdown proof: the public execute-api URL rejects callers
    that don't arrive through CloudFront (regional WAF RejectNonCloudFront)."""
    response = requests.get(api_gateway_url, timeout=10, headers=_idempotency_headers())
    assert response.status_code == 403
```

Update the module docstring (CloudFront is now the front door; the direct URL must 403). Also relax `test_api_gateway_response_time_warm`'s budget comment if needed (an extra CloudFront hop; keep the 2.0s budget).

- [ ] **Step 3: `test_frontend.py`** — update `test_config_json_contains_api_url` to assert `data["apiUrl"] == "/api"`.
- [ ] **Step 4: Verify collection** — `make test-integration` → all skip cleanly without a deployed stack (no import errors); `make test` and `make test-cdk` still green.
- [ ] **Step 5: Commit** — `git commit -am "test: point integration tests at the CloudFront /api path and assert the direct URL is blocked"`

---

### Task 17: AppConfig Lambda extension layer + localhost feature-flag store

**Files:**
- Create: `lambda/extension_store.py`, `tests/unit/test_extension_store.py`
- Modify: `infrastructure/backend_app.py` (layer + env var), `lambda/app.py:150-166`
- Test: `tests/unit/test_extension_store.py`, existing `tests/unit/test_handler.py`

**Interfaces:**
- Produces: `AppConfigExtensionStore(application: str, environment: str, name: str, max_age: int = 300, endpoint: str = "http://localhost:2772")` — a Powertools `StoreProvider` with `get_configuration() -> dict` and `get_raw_configuration` property; raises `ConfigurationStoreError` on any fetch/parse failure (so `service.build_greeting`'s existing fallback + `FeatureFlagEvaluationFailure` metric path is unchanged).

- [ ] **Step 1: Verify the layer ARN** — docs search (`aws___search_documentation`, phrase "AWS AppConfig Lambda extension ARM64 layer ARN us-east-1 versions"). Copy the current us-east-1 ARM64 ARN verbatim (account `027255383542`, layer `AWS-AppConfig-Extension-Arm64`).
- [ ] **Step 2: Write the failing tests** — `tests/unit/test_extension_store.py`:

```python
"""Unit tests for the AppConfig-extension-backed feature-flag store."""

import io
import json

import pytest
from aws_lambda_powertools.utilities.feature_flags.exceptions import ConfigurationStoreError

from extension_store import AppConfigExtensionStore


@pytest.fixture
def store() -> AppConfigExtensionStore:
    return AppConfigExtensionStore(application="app", environment="env", name="flags", max_age=300)


def _stub_urlopen(monkeypatch, payload: bytes):
    calls: list[str] = []

    def fake_urlopen(url, timeout):
        calls.append(url)
        return io.BytesIO(payload)

    monkeypatch.setattr("extension_store.urllib.request.urlopen", fake_urlopen)
    return calls


def test_fetches_flags_from_extension_endpoint(monkeypatch, store):
    flags = {"enhanced_greeting": {"default": False}}
    calls = _stub_urlopen(monkeypatch, json.dumps(flags).encode())
    assert store.get_configuration() == flags
    assert calls == ["http://localhost:2772/applications/app/environments/env/configurations/flags"]


def test_result_is_cached_within_ttl(monkeypatch, store):
    calls = _stub_urlopen(monkeypatch, b"{}")
    store.get_configuration()
    store.get_configuration()
    assert len(calls) == 1, "second call within max_age must hit the in-memory cache"


def test_http_error_raises_store_error(monkeypatch, store):
    def boom(url, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr("extension_store.urllib.request.urlopen", boom)
    with pytest.raises(ConfigurationStoreError):
        store.get_configuration()


def test_bad_json_raises_store_error(monkeypatch, store):
    _stub_urlopen(monkeypatch, b"<html>not json</html>")
    with pytest.raises(ConfigurationStoreError):
        store.get_configuration()


def test_raw_configuration_property(monkeypatch, store):
    _stub_urlopen(monkeypatch, b'{"a": 1}')
    assert store.get_raw_configuration == {"a": 1}
```

- [ ] **Step 3: Run to fail** — `make test` → import error.
- [ ] **Step 4: Implement `lambda/extension_store.py`:**

```python
"""Feature-flag store backed by the AWS AppConfig Lambda extension.

The extension (a Lambda layer wired in infrastructure/backend_app.py) polls
AppConfig in the background and serves cached configuration over
``http://localhost:2772`` — cutting per-invocation AppConfig API spend and
cold-path latency versus SDK polling (the AWS-recommended pattern for Lambda:
see "Using AWS AppConfig Agent with AWS Lambda" in the AppConfig user guide).

This module adapts that local endpoint to Powertools' ``StoreProvider``
interface so ``FeatureFlags`` consumes it unchanged. Any failure — endpoint
down (e.g. running outside Lambda), HTTP error, malformed body — raises
``ConfigurationStoreError``, which the service layer's fallback already
handles (default flag values + the ``FeatureFlagEvaluationFailure`` metric).

A small monotonic-clock TTL cache mirrors the ``max_age`` posture of the SDK
store this replaces; the extension caches too, so this mostly saves the
localhost round-trip on hot paths.
"""

import json
import time
import urllib.request
from typing import Any

from aws_lambda_powertools.utilities.feature_flags import StoreProvider
from aws_lambda_powertools.utilities.feature_flags.exceptions import ConfigurationStoreError


class AppConfigExtensionStore(StoreProvider):
    """Powertools feature-flag store reading from the AppConfig extension endpoint."""

    def __init__(
        self,
        *,
        application: str,
        environment: str,
        name: str,
        max_age: int = 300,
        endpoint: str = "http://localhost:2772",
    ) -> None:
        super().__init__()
        self._url = f"{endpoint}/applications/{application}/environments/{environment}/configurations/{name}"
        self._max_age = max_age
        self._cached: dict[str, Any] | None = None
        self._fetched_at = 0.0

    def get_configuration(self) -> dict[str, Any]:
        """Return the flag configuration, served from the TTL cache when fresh."""
        now = time.monotonic()
        if self._cached is not None and (now - self._fetched_at) < self._max_age:
            return self._cached
        try:
            with urllib.request.urlopen(self._url, timeout=2) as response:  # noqa: S310 — fixed localhost URL
                body = response.read()
            config = json.loads(body)
        except (OSError, ValueError) as exc:
            raise ConfigurationStoreError(f"Unable to fetch configuration from the AppConfig extension: {exc}") from exc
        if not isinstance(config, dict):
            raise ConfigurationStoreError("AppConfig extension returned a non-object configuration document")
        self._cached = config
        self._fetched_at = now
        return config

    @property
    def get_raw_configuration(self) -> dict[str, Any]:
        """Raw configuration — same document; required by the StoreProvider ABC."""
        return self.get_configuration()
```

(If `make test` shows `urlopen` needs a context-manager stub, wrap the test's `io.BytesIO` accordingly — `io.BytesIO` supports the context protocol, so `with` works.)

`lambda/app.py` — replace the `AppConfigStore` construction (and drop the now-unused `AppConfigStore` import and `boto3.client("appconfigdata", ...)` line; keep `boto3` if still used elsewhere — it isn't, so remove the import too if nothing else needs it and `make lint` agrees):

```python
from extension_store import AppConfigExtensionStore

# Feature flags via the AppConfig Lambda extension (localhost:2772): the layer
# polls AppConfig in the background, so flag reads never call the AppConfig
# data plane from handler code. Failures raise ConfigurationStoreError, which
# service.build_greeting already degrades gracefully (default value +
# FeatureFlagEvaluationFailure metric) — no new failure mode.
app_config_store = AppConfigExtensionStore(
    application=_ENV.APPCONFIG_APP_NAME,
    environment=_ENV.APPCONFIG_ENV_NAME,
    name=_ENV.APPCONFIG_PROFILE_NAME,
    max_age=_ENV.APPCONFIG_MAX_AGE_SECONDS,
)
feature_flags = FeatureFlags(store=app_config_store)
```

`infrastructure/backend_app.py` — module-level constant + wiring after the function definition:

```python
# AWS-published AppConfig Lambda extension layer (ARM64), per region. The ARN is
# region- and version-pinned by AWS (account 027255383542) — take new entries
# verbatim from "Using AWS AppConfig Agent with AWS Lambda" in the AppConfig
# user guide. Fail-loud mapping: an unmapped region must break synth, not
# silently deploy a Lambda whose flag store points at a dead localhost port.
_APPCONFIG_EXTENSION_ARM64_LAYER_ARNS: dict[str, str] = {
    "us-east-1": "<ARN from Step 1, verbatim>",
}
```

```python
extension_layer_arn = _APPCONFIG_EXTENSION_ARM64_LAYER_ARNS.get(stack.region)
if extension_layer_arn is None:
    raise ValueError(
        f"No AppConfig extension layer ARN mapped for region {stack.region!r} — "
        "add it to _APPCONFIG_EXTENSION_ARM64_LAYER_ARNS from the AppConfig Lambda extension docs."
    )
self.function.add_layers(
    _lambda.LayerVersion.from_layer_version_arn(self, "AppConfigExtensionLayer", extension_layer_arn)
)
```

Add to the function's `environment` block: `"AWS_APPCONFIG_EXTENSION_PREFETCH_LIST": f"/applications/{self.app_config_app.name}/environments/{app_config_env.name}/configurations/{app_config_profile.name}",` with a comment (prefetch at extension start = flags ready before the first invocation). The existing IAM grant (`StartConfigurationSession`/`GetLatestConfiguration` on the profile ARN) covers the extension — it calls AppConfig with the function role.

- [ ] **Step 5: Run to pass** — `make test` (100% incl. new module; fix `test_handler.py` stubs if they patched `app.app_config_store` internals), `make openapi` (no change expected), `make test-cdk` + `UPDATE_SNAPSHOTS=1 make test-cdk` (layer + env var in backend snapshot), `make lint`.
- [ ] **Step 6: Commit** — `git commit -am "feat: serve feature flags through the AppConfig Lambda extension layer"`

---

### Task 18: Documentation — README, TODO.md, CLAUDE.md

**Files:**
- Modify: `README.md`, `TODO.md`, `CLAUDE.md`

- [ ] **Step 1: TODO.md** — update every touched item, mirroring the existing `[x]`/`[~]` conventions:
  - `[x]`: CORS restriction (superseded by same-origin — say so), origin lockdown option (b) (both bypass-window items), APIGW request validation (`AwsSolutions-APIG2` retired), S3 versioning on frontend bucket, AWS Backup plan (retain-gated), audit-log Glacier/Deep-Archive + Object Lock (retain-gated), Lambda error-rate + DDB throttle alarms, WAF BlockedRequests alarms, Athena CloudWatch alarm, RUM ingestion alarm + Budgets, Lambda Insights, WAF redacted_fields + logging_filter, SSM path parameterization, AppConfig extension layer, Athena MinimumEncryptionConfiguration (or the dated not-supported finding from Task 3).
  - Amend with findings: WCU alarm marked not-implementable (no WCU CloudWatch metric — static capacity); the TLS item notes the API is now REGIONAL (edge redundancy removed; custom-domain path now uses the regional securityPolicy set).
- [ ] **Step 2: README.md** — add/extend sections following its existing style: "Same-origin API and origin lockdown" (the /api/* behavior, rewrite function, secret header, WAF rule, SPA-error-response caveat, the one-deploy 403 window, manual secret-rotation recipe), extend "Audit stack and log retention" (retain-shape tiering + Object Lock + bucket-replacement caveat), extend the alarms/monitoring section (new alarms + budget + reference thresholds), note Lambda Insights and the shared `/aws/lambda-insights` log group (deliberately not stack-owned), note the AppConfig extension layer.
- [ ] **Step 3: CLAUDE.md** — surgical updates only: the origin-lockdown pattern + its no-rotation rationale (new subsection or a line in the WAF section), `retain_data` section gains AWS Backup + Object Lock (+ replacement caveat), note that the API is REGIONAL and same-origin behind CloudFront (so future route work must keep the /api rewrite in mind), add `ssm_param_path` to the context-key list in whatever section mentions `retain_data`/`appconfig_monitor` plumbing.
- [ ] **Step 4: Lint** — `make lint-docs`; fix any markdownlint findings.
- [ ] **Step 5: Commit** — `git commit -am "docs: record the production-readiness batch (same-origin API, alarms, data protection)"`

---

### Task 19: Final gate — full local CI + spec cross-check

- [ ] **Step 1:** Start Docker Desktop (asset bundling needs it), then run `make pr` — every CI gate in CI's order (check-lock, lint, typecheck, lint-docs, test, test-cdk, cdk-synth incl. the validation-report nag gate, compare-openapi). Fix anything it surfaces; re-run until green.
- [ ] **Step 2:** Cross-check the spec (`docs/superpowers/specs/2026-07-08-production-readiness-batch-design.md`) section by section against `git log --oneline main..HEAD` — every spec bullet maps to a commit, or the deviation is documented in TODO.md/README (known deviations to confirm are recorded: Lambda Insights log-group cleanup deliberately NOT implemented — shared regional group; WCU alarm not implementable; Athena min-encryption possibly doc-only).
- [ ] **Step 3:** Commit any stragglers; report completion with the commit list and the deviations. Do NOT delete the branch; do NOT deploy — live verification (`make deploy` on an ephemeral env + integration suite + `make destroy-clean`) is the user's call.

## Deviations from the spec (pre-agreed in this plan)

1. **Lambda Insights log-group cleanup CR is deliberately dropped** — `/aws/lambda-insights` is a fixed-name, region-shared log group; a per-stack cleanup CR could delete another workload's Insights logs, violating the repo's no-account-wide-mutations rule. Documented instead (Task 8/18).
2. **The RUM "ingestion volume" alarm is a SessionCount spike alarm + a CloudWatch-service spend budget** — RUM publishes no direct ingestion-volume metric; sessions are the closest vended proxy and the budget is the authoritative spend backstop (Tasks 6–7).
3. **`RateLimitDirectCallers` WAF rule is removed** — fully superseded by the blocking `RejectNonCloudFront` rule (Task 13).
