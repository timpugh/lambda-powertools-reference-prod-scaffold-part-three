# Production-readiness batch — design

**Date:** 2026-07-08
**Branch:** `feat/production-readiness-batch`
**Status:** Approved by user (this session); implementation planning next.

## Goal

Work through the `TODO.md` production-readiness items that are implementable **purely as cloud
infrastructure-as-code** — no Route 53 / custom-domain dependency, no manual setup steps
(console clicks, out-of-band confirmations). Four themes are in scope: edge/API hardening,
observability alarms, data protection, and small wins.

## Scope decisions (user-confirmed)

- **CDK Pipelines CI/CD is deferred.** The GitHub source requires an AWS CodeConnections
  handshake — a one-time human authorization in the console that cannot be completed via IaC.
  The user chose to defer the pipeline (and with it, the "live integration tests in CI" item that
  is blocked on a CI-managed deployment) until they are ready for that handshake.
- **Excluded — needs Route 53 / a custom domain:** TLS 1.2+ floor fix (ACM + custom domain),
  HSTS `preload`, Mutual TLS, multi-region deployment.
- **Excluded — manual setup or not per-app cloud infra:** alarm subscriptions (email
  confirmation is out-of-band by design), GitHub branch protection, AMR SNS update-topic
  subscriptions, GuardDuty / Security Hub / multi-region management trail (account-baseline
  scope; CLAUDE.md forbids account-wide constructs here), SonarQube / Codecov / SLSA / fuzzing
  (already deliberately rejected in TODO.md), Sentry (third-party SaaS), CONTRIBUTING.md
  (not cloud infrastructure).
- **Edge architecture: Approach C** — same-origin API through CloudFront with a secret
  origin-verify header and a blocking regional WAF rule, plus converting the API from EDGE to
  REGIONAL. Chosen over (A) same-origin without the REGIONAL conversion and (B) a minimal
  runtime-SSM CORS restriction that would have left the CloudFront-bypass window open.

## Section 1 — Edge/API hardening (same-origin + origin lockdown + REGIONAL)

Today the browser reads `apiUrl` from `config.json` and calls the `execute-api` host directly,
cross-origin, with `allow_origin="*"`. Blocking non-CloudFront callers at the regional WAF is
therefore impossible without first moving the browser traffic behind CloudFront — which is what
makes CORS restriction and origin lockdown one combined design, not two items.

**API behind CloudFront.** `FrontendStack` adds an `/api/*` behavior to the existing
distribution:

- `origins.HttpOrigin` at the execute-api host, `origin_path="/Prod"` (the stage name).
- Cache policy `CACHING_DISABLED`; origin request policy `ALL_VIEWER_EXCEPT_HOST_HEADER`
  (API Gateway must receive its own Host header; RUM's X-Ray trace header on same-origin
  fetches still flows through to keep client↔server trace correlation).
- Viewer protocol HTTPS-only; all HTTP methods allowed.
- No new cross-stack references: the frontend already receives `api_url` / `api_id`.

**Origin-verify secret.** A Secrets Manager secret (generated random string, encrypted with the
backend CMK) is created in `BackendStack` and passed to `FrontendStack` (frontend already
depends on backend — no cycle). CloudFront injects it as an `x-origin-verify` custom origin
header on the API origin. `_attach_regional_waf` gains a **priority-0 blocking rule**: any
request whose `x-origin-verify` header does not exactly match the secret value (CFN dynamic
reference into the byte-match statement) is blocked. Direct `execute-api` callers receive 403.

- **No automatic rotation in v1.** A rotation Lambda would have to mutate CFN-managed
  CloudFront and WAF state out-of-band, creating drift. Manual rotation = update the secret
  value, then `make deploy`. The `AwsSolutions-SMG4` finding is suppressed with this rationale.
- **Known exposure, accepted:** the header value is readable by any principal with
  `waf:GetWebACL` (and appears in the deployed WAF rule). It is a defense-in-depth origin
  discriminator, not a credential that grants access to anything else.

**CORS eliminated.** `config.json`'s `apiUrl` becomes the relative path `/api`, making the
browser call same-origin. The Powertools `CORSConfig`, API Gateway preflight, and the CORS
headers on gateway 4XX/5XX responses are all removed — strictly stronger than restricting
`allow_origin`. CSP `connect-src` drops the execute-api host (covered by `'self'`).

**EDGE → REGIONAL.** `endpoint_configuration` on the RestApi switches to REGIONAL.
Verified against the CloudFormation reference: `AWS::ApiGateway::RestApi
EndpointConfiguration.Types` is *Update requires: No interruption* — an in-place update; the
execute-api URL does not change. This removes the redundant edge hop now that CloudFront
fronts the API, and unlocks the regional security-policy set for a future custom domain.

**Request validation.** A `RequestValidator` (validate parameters + body) is attached to the
API, retiring the `AwsSolutions-APIG2` suppression. `/greeting` accepts no input today, so this
is the gate machinery installed live and asserted, ready for the first parameterized route.

**Integration tests.** `tests/integration/test_api_gateway.py` switches to
`{cloudfront_url}/api/greeting`; a new test asserts the direct execute-api URL returns 403
(live proof of the lockdown). `test_frontend.py`'s `config.json` assertion updates to the
relative `apiUrl`.

## Section 2 — Observability alarms

All new alarms follow the existing pattern: SNS alarm action in the `prod` environment only,
with the existing scoped non-prod suppressions for the alarm-action nag rules.

- **Lambda error-rate** and **DynamoDB throttle** alarms via flags on the existing
  `MonitoringFacade` calls (`add_fault_rate_alarm=` on `monitor_lambda_function`; the exact
  DynamoDB kwarg verified against cdk-monitoring-constructs at implementation).
- **WAF BlockedRequests spike alarms** on both WebACLs — the CloudFront-scoped alarm lives in
  `WafStack` (us-east-1, where its metrics are), the regional one in the backend stack. Static
  documented threshold (spike detection, not anomaly detection).
- **WCU alarm: not implementable.** WAFv2 publishes no WCU-consumption CloudWatch metric —
  WCU is static rule capacity, known at configuration time. TODO.md is updated with this
  finding instead of shipping a fake alarm.
- **Athena query-failure alarm** on the access-logs workgroup metrics (metric/dimension names
  verified at implementation; workgroup metrics publishing enabled if not already).
- **RUM cost guardrails:** a CloudWatch alarm on RUM ingestion volume plus an AWS Budgets
  cost budget (`CfnBudget`) scoped to RUM usage, both notifying the existing prod SNS topic.
  The topic policy gains a confused-deputy-guarded `budgets.amazonaws.com` grant following the
  documented `grant_cloudwatch_alarms_to_key` pattern (service-principal caveats per CLAUDE.md).
- **Lambda Insights** on `ApiFunction` (~$0.50/month): `insights_version=` one-liner, plus the
  managed-policy IAM4 suppression with rationale, plus cleanup of the out-of-CFN
  `/aws/lambda-insights` log group via the repo's `AwsCustomResource` cleanup pattern
  (mirroring `RumLogGroupCleanup`) and inclusion in the `destroy-clean` sweeps.

## Section 3 — Data protection

- **AWS Backup for DynamoDB — only when `retain_data=true`:** in `DataStack`, a BackupVault
  (encrypted with the data stack's CMK), a plan with a daily rule (35-day retention) and a
  monthly-to-cold-storage rule (1-year retention), and a selection targeting the idempotency
  table. The `DynamoDBInBackupPlan` suppressions become conditional: retired in the retain
  shape, kept in the destroy-friendly default. `TestNagCompliance` already synthesizes the
  `retain_data` shape, so the retired suppression is asserted.
- **Audit-bucket compliance tier — only when `retain_data=true`:** the CloudTrail log bucket
  gains versioning + Object Lock (GOVERNANCE mode, 1-year default retention) and a lifecycle of
  Glacier at 90 days → Deep Archive at 365 days → expiry at 7 years (documented compliance
  horizon), plus noncurrent-version expiry. Default shape unchanged (90-day expiry, no
  versioning). **Caveat documented in README:** Object Lock is creation-time-only, so flipping
  `retain_data` on an already-deployed stack replaces the bucket — acceptable for a template;
  flip it before real audit data accumulates.
- **Frontend bucket versioning — always on**, with a 30-day noncurrent-version expiry to bound
  cost. `auto_delete_objects` deletes all versions, so teardown stays clean.

## Section 4 — Small wins

- **Athena `MinimumEncryptionConfiguration`** via a CFN property override on the workgroup —
  contingent on the current CloudFormation schema accepting the field (checked at
  implementation; if CFN still rejects it, TODO.md records the finding and the current
  workgroup-enforced SSE-KMS posture stands).
- **SSM parameter path via CDK context:** an optional context key overrides the stack-name-derived
  greeting-parameter path; validated at synth (fail-loud like `retain_data`), default preserves
  today's behavior. Plumbed `app.py` → `AppStage` → `BackendStack` like the existing flags.
- **AppConfig Lambda extension layer:** the regional extension layer ARN added to `ApiFunction`
  (fail-loud region→ARN mapping), and the handler's feature-flag store fetches from
  `http://localhost:2772/...` with the existing fallback path (the `FeatureFlagEvaluationFailure`
  EMF metric semantics unchanged). 100% `lambda/` coverage maintained; unit tests mock the
  localhost endpoint.

## Cross-cutting gates and conventions

- Two-venv discipline (`.venv` CDK / `.venv-lambda` runtime) per CLAUDE.md.
- `make openapi` after any handler change; commit the artifact (CI drift gate).
- Snapshot updates (`UPDATE_SNAPSHOTS=1 make test-cdk`) paired with matching fine-grained
  assertion changes in `tests/cdk/test_stacks.py`.
- `make test-cdk` nag gate green across all four shipped shapes (prod, dev, `appconfig_monitor`,
  `retain_data`); every new suppression carries a written rationale; granular IAM4/IAM5
  acknowledgments use exact finding ids.
- README, TODO.md, and CLAUDE.md updated to reflect the new posture (same-origin API, origin
  lockdown, backup/archive tiers, new alarms); conventional-commit messages; `make pr` before
  push.
- New fixed cost: ~$1–2/month (Secrets Manager secret, Lambda Insights, ~8 alarms, budget);
  backup and archive storage costs apply only to `retain_data` forks.

## Error handling and failure modes

- Origin-lockdown misconfiguration fails **closed** (WAF blocks) — the integration test that
  calls through CloudFront catches a broken header injection immediately.
- The AppConfig extension fetch failure falls into the existing feature-flag fallback path
  (200 + default greeting + `FeatureFlagEvaluationFailure` metric) — no new failure mode.
- EDGE→REGIONAL is a no-interruption CFN update; if a live deploy contradicts the documented
  behavior, the change is one line to revert.
- Alarms that reference by-name metrics must not transitively reference the Lambda (the
  AppConfig-monitor dependency-cycle lesson in CLAUDE.md) — new alarms use facade metrics or
  by-name metrics accordingly, asserted by `make test-cdk`.

## Verification

- Unit: `make test-cdk` (stack assertions, nag gate, snapshots), `make test` (lambda, 100%
  coverage), OpenAPI drift gate.
- Local CI-equivalent: `make pr` with Docker running (asset bundling + validation-report gate).
- Live (user-run): `make deploy` on an ephemeral env; integration suite against the deployed
  CloudFront URL, including the new direct-execute-api-403 assertion; teardown via
  `make destroy-clean ENV=<name>`.
