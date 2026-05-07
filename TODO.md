# TODO

Items that would improve this project for production use but are not yet implemented.

## Infrastructure

- [ ] **Multi-environment CDK stacks** — separate dev/staging/prod stacks with environment-specific config (SSM paths, AppConfig environments, DynamoDB table names)
- [ ] **API Gateway throttling** — add rate limiting and burst limits to prevent abuse
- [x] **WAF** — WAF WebACL deployed in `HelloWorldWafStack` and attached to CloudFront. AWS managed rule sets (IP reputation, CRS, known bad inputs) and a rate-limit rule per IP are active. WAF is not attached directly to API Gateway because the CloudFront layer already enforces it for all browser traffic.
- [ ] **SSM SecureString** — store the greeting parameter as a `SecureString` (KMS-encrypted) rather than plaintext. Note: CloudFormation does not support creating SecureString parameters, so this would require a custom resource or out-of-band provisioning.
- [ ] **Parameterise the SSM path** — pass the parameter path through CDK context rather than deriving it from the stack name
- [ ] **AppConfig initial value management** — manage the feature flag hosted configuration outside the CDK stack so it can be updated independently of a deployment

## Observability

- [ ] **CloudWatch alarms** — add alarms for Lambda error rate, p99 latency, and DynamoDB throttles, with SNS notifications
- [ ] **Dead letter queue (DLQ)** — configure a DLQ on the Lambda function to capture failed invocations
- [ ] **Structured error reporting** — integrate with an error tracking service (e.g. Sentry) for aggregated error visibility

## CI/CD

- [ ] **Deploy workflow** — GitHub Actions workflow to run `cdk deploy` on merge to `main` (deliberately deferred)
- [ ] **CDK diff on PRs** — run `cdk diff` in CI on pull requests to surface infrastructure changes before merge
- [x] **CDK synth in CI** — `cdk-check` CI job runs `cdk synth` (catching unsuppressed cdk-nag findings) and `aws_cdk.assertions.Template` tests that verify key security properties of each synthesized stack
- [ ] **Live integration tests in CI** — run API Gateway and CloudFront integration tests against a deployed dev stack as part of the CI pipeline (blocked on Deploy workflow above)

## Security

- [ ] **API Gateway authentication** — add an API key, IAM auth, or Cognito authorizer to restrict access
- [x] **Lambda least-privilege IAM** — DDB, SSM, AppConfig grants are scoped to the specific resource ARNs. `appconfig:GetLatestConfiguration` and X-Ray segment publishes remain at `Resource: "*"` because their target ARNs (configurationsession token, segment) are dynamically generated at call time and not addressable by IAM at policy-creation time — documented in the IAM5 nag suppression.
- [ ] **VPC placement** — place the Lambda function inside a VPC if it needs to access private resources
- [ ] **CORS origin restriction** — the Lambda handler uses `allow_origin="*"`. In production, restrict to the specific CloudFront domain and set `allow_credentials=True` if cookies or Authorization headers are needed.
- [ ] **Narrow the CDK bootstrap permissions** — the default `cdk bootstrap` creates a `CloudFormationExecutionRole` with `AdministratorAccess`. Any identity that can `sts:AssumeRole` into the deployment roles (by default, any principal in the account) can do anything in the account during deploy. Fine for a solo-dev laptop, a headache for organizations. Fix path: re-bootstrap with `cdk bootstrap --custom-permissions-boundary <POLICY_NAME>` so CFN can do anything inside the boundary but can't escape it (e.g., can't attach `AdministratorAccess` or create roles that bypass the boundary). At the org level, use SCPs via AWS Organizations to prevent tampering with the boundary. Restrict who can assume `DeploymentActionRole` to the CI role + named humans. **Sequence this before the Deploy workflow above** — once CI gets credentials that can assume the bootstrap roles, the admin default becomes a real blast radius.
- [ ] **Enforce TLS 1.2+ minimum on both edges** — the CloudFront distribution and API Gateway both currently sit on AWS-managed default certificates (`*.cloudfront.net` and `*.execute-api.{region}.amazonaws.com`), which pin the TLS floor at **TLS 1.0**. Verified empirically: `curl --tls-max 1.0 https://<dist>.cloudfront.net` and the equivalent against the execute-api endpoint both complete a full handshake. The CDK code at [hello_world_frontend_stack.py:208](hello_world/hello_world_frontend_stack.py#L208) sets `TLS_V1_2_2021` but AWS silently overrides it whenever `CloudFrontDefaultCertificate: true`. The cdk-nag rule `AwsSolutions-CFR4` correctly flags this and is intentionally suppressed at [hello_world_frontend_stack.py:463](hello_world/hello_world_frontend_stack.py#L463). **Fix path:** acquire a domain, provision an ACM certificate (CloudFront cert must live in us-east-1, API Gateway custom-domain cert lives in the API's region), attach as `viewer_certificate` / `apigateway.DomainName`, then set the strongest matching `securityPolicy` (e.g. `SecurityPolicy_TLS13_2025_EDGE` for an edge-optimized API Gateway domain, `SecurityPolicy_TLS13_1_3_2025_09` for a regional one, and `TLSv1.2_2021` minimum on CloudFront). Once the custom domain is wired and verified, remove the CFR4 suppression. Also reconsider whether the API needs to remain `EDGE` — CloudFront already fronts it, so making the backend `REGIONAL` removes the redundant edge layer and unlocks the regional `securityPolicy` set (which includes post-quantum and PFS variants).

## Code

- [ ] **Input validation on caller-facing inputs** — `enable_validation=True` is set on the `APIGatewayRestResolver` ([lambda/app.py:42](lambda/app.py#L42)) and Pydantic models drive response validation, so the framework is wired. The `/hello` route currently accepts no query string, path, or body parameters, so there is nothing to validate yet. When new routes are added that accept caller input, type-annotate the handler parameters with Pydantic-compatible types (or `Annotated[..., Query/Body]`) so Powertools enforces the schema and rejects malformed input with a 422 before any business logic runs.
- [ ] **Contributing guide** — `CONTRIBUTING.md` with fork/branch/PR workflow and pre-commit setup instructions
- [ ] **Changelog** — auto-generated `CHANGELOG.md` from conventional commit history using `conventional-changelog`
- [x] **`cdk destroy` BucketDeployment cache-invalidation race** — Fixed by decoupling cache invalidation from `s3deploy.BucketDeployment` into a standalone `cr.AwsCustomResource` that defines `on_create` + `on_update` only (no `on_delete`). CFN removes the resource from stack state during teardown without making any CloudFront API call to race with. CallerReference is gated on the BucketDeployment's content-hashed S3 object key so invalidations only fire when frontend assets actually change, conserving the 1000-paths/month free quota. Permanent fix for [aws/aws-cdk#15891](https://github.com/aws/aws-cdk/issues/15891) — see [README "Decoupled CloudFront cache invalidation"](README.md#design-decisions-and-known-limitations) for the longer write-up.

## Service-level hardening

Per-service hardening items grouped by AWS service so each block can be tackled in isolation. Items deferred for "no direct CDK construct" or "would require restructuring upstream data" reasons are flagged.

### API Gateway

- [ ] **APIGW-layer request validation** — currently validation runs inside Lambda via `enable_validation=True` on the resolver. APIGW-level `request_validator_options` rejects malformed requests at the gateway before they hit Lambda billing. Cheaper for adversarial traffic.
- [ ] **Resource policy on the REST API** — restrict invocation to specific source IPs, VPCs, or AWS accounts without needing a full authorizer. Useful for partner-facing or internal-only APIs.
- [ ] **Custom domain + endpoint export + canary deployments + Mutual TLS** — out of scope for the sample app: custom domain depends on owning a domain ([TLS item](#security)); canary deploys/MTLS only justify their complexity once you have real traffic and partners.

### Lambda

- [ ] **Dead Letter Queue (DLQ)** — already covered by [Observability](#observability). Lower priority for the synchronous API path; required if you ever add async invokes (EventBridge, SNS, S3 events).
- [ ] **Reserved concurrency** — uncapped function can absorb the entire account concurrency limit (default 1000) on a runaway loop. Setting a per-function ceiling (e.g. 100) bounds blast radius and keeps the rest of the account responsive.
- [ ] **Async failure destination** — N/A for the current sync API. If async triggers are added, configure `on_failure` to an SQS DLQ or EventBridge bus for visibility into failed invokes.
- [ ] **SnapStart for Python** — Python SnapStart launched in November 2024. Roughly 70% cold-start reduction at the cost of a one-time init snapshot. Worth enabling if cold-start latency becomes a UX issue.
- [ ] **Lambda Insights** — extension-based enhanced metrics (CPU time, memory utilization, init duration, network bytes). One-line CDK setting (`insights_version=lambda.LambdaInsightsVersion.VERSION_X`); ~$0.50/month per function for the metric stream.

### DynamoDB

- [ ] **Deletion protection** — `deletion_protection=True` prevents accidental `DeleteTable`. For the idempotency table the data is regenerable, but the construct still belongs on a reference architecture.
- [ ] **AWS Backup plan** — AWS Backup integration for compliance/long-term retention. PITR alone covers <35 days; AWS Backup supports years.

### S3

- [ ] **Versioning on the frontend bucket** — currently disabled because git is the source of truth for deployed assets. If git is ever lost or assets get manually overwritten, recovery requires a redeploy from a known-good commit. Enabling versioning gives in-bucket recovery as well, and is also a prerequisite for cross-region replication.
- [ ] **S3 Inventory / Storage Lens / Object Lock / Macie** — `(Required)` per the broader S3 best-practice set: Inventory exports object-level metadata daily, Storage Lens gives org-wide visibility, Object Lock enforces write-once retention for compliance, Macie scans for sensitive data. None justify themselves at sample-app scale; revisit at production scale or under compliance scope.

### IAM

- [ ] **Permissions boundary on the Lambda execution role** — already covered by [Narrow the CDK bootstrap permissions](#security) and the broader bootstrap-hardening item; the same `cdk bootstrap --custom-permissions-boundary` work applies to runtime roles, not just deployment roles.
- [ ] **Account-level identity governance is out of scope for this stack** — root-account MFA, IAM Identity Center, GuardDuty, AWS Config, Security Hub, CloudTrail organization trail, SCPs/RCPs, Access Analyzer, credential reports, password policy, root activity alarms. These are real `(Required)` items in any IAM critical-workload review, but they belong in an account-baseline / landing-zone configuration (e.g. AWS Control Tower) rather than in a per-application CDK stack. If forking this for a real workload, ensure a separate account-baseline mechanism owns these.
- [ ] **Inline policies on Lambda/CloudTrail service roles** — CDK generates default policies inline for the Lambda execution role, the CloudTrail LogsRole, and similar service roles. The `IAMNoInlinePolicy` rule is suppressed in each location with the same reasoning ("CDK generates the default policy inline — not directly configurable"). This is a CDK behavior, not a stack defect; would only change if CDK starts emitting managed policies by default.

### Athena

- [ ] **Bytes-scanned-per-query data usage control** — set `BytesScannedCutoffPerQuery` on the workgroup to cap runaway scans at a known dollar amount. Cheap insurance against forgotten `WHERE` clauses.
- [ ] **Workgroup-level query result reuse** — *Deferred: no CDK / CFN support.* The CFN `AWS::Athena::WorkGroup.WorkGroupConfiguration` schema does not expose a result-reuse default. Result reuse can only be set per-query in `StartQueryExecution` calls, which doesn't fit a CDK-declared workgroup. Revisit when CFN adds `ResultReuseConfiguration` to the workgroup schema.
- [ ] **Cost allocation tags on the workgroup** — apply tags (Environment, Project, Owner) so Athena query costs roll up cleanly in Cost Explorer.
- [ ] **Partition projection on access-log tables** — *Deferred: requires upstream log restructuring.* The Glue tables for `cloudfront_logs` and `s3_access_logs` currently scan every file in the prefix on every query because CloudFront standard v1 logs and S3 server access logs both write to flat key spaces (no `year=YYYY/month=MM/...` directories). Implementing partition projection requires either: (a) migrating CloudFront from standard v1 to standard v2 logs (separate CDK construct via the Logs delivery API) AND switching S3 server access logs to `target_object_key_format=PartitionedPrefix` for date-based S3 prefixes; or (b) a re-organize Lambda to copy logs into Hive-style partition prefixes. Both are significant changes and (a) requires CDK to catch up on standard v2 wiring. Revisit when the v2 path is well-supported.
- [ ] **Athena CloudWatch alarms** — `QueryFailed` rate, `ProcessedBytes` per query/per workgroup. Same gap as the broader [CloudWatch alarms](#observability) item.

### Glue

- [ ] **Partition projection on tables** — same item as Athena above; same deferral reasoning. Glue table parameters (`projection.enabled`, `projection.<col>.type`, `storage.location.template`) would carry the projection definitions, but the projection only helps if the underlying S3 layout is partitioned, which it isn't yet.
- [ ] **Glue Security Configuration** — encryption-at-rest for Glue job bookmarks, S3-side encryption pushdown for crawlers, and CloudWatch encryption settings. N/A until Glue jobs or crawlers are added; the current stack only uses the catalog (database + tables).
- [~] **Glue Data Catalog encryption** — *implemented and deliberately reverted.* Two reasons: (1) `AWS::Glue::DataCatalogEncryptionSettings` is account/region-scoped — there is one Glue catalog per account per region, so deploying this reference architecture into an account with other Glue users would silently override their encryption settings or conflict outright; (2) the catalog metadata in *this* stack (column names from public CloudFront/S3 access-log schemas) carries no confidentiality requirement, and the stack has no Glue connections, so encrypting connection passwords protects nothing. If you fork this and your catalog will hold genuinely sensitive table metadata, put `glue.CfnDataCatalogEncryptionSettings` into a separate, intentionally account-scoped stack so the deploy boundary reflects the resource's account-wide nature. See "Considered and rejected" in the README for the longer write-up.

### Cognito

- [ ] **All Cognito User Pool hardening checks become live if user-facing auth is added** — the current stack has no User Pool. The Identity Pool exists only as the WebIdentity broker for RUM guest credentials and is already scoped to a single `rum:PutRumEvents` action on a specific monitor ARN. If a User Pool is added (login, signup, federated identities), the following items become required: User Pool Plus tier for advanced security, MFA configuration, password policy (12+ chars), `PreventUserExistenceErrors`, threat protection, token revocation, deletion protection, custom domain with ACM, recovery mechanisms, hosted UI customization, sign-in/sign-up alarms, MAU quota monitoring. Cognito threat protection requires the Plus tier (paid).

### Systems Manager

- [ ] **Greeting parameter as `SecureString`** — already in [Infrastructure](#infrastructure) as "SSM SecureString". Carries forward — CFN does not natively create SecureString parameters, would require a custom resource.
- [ ] **Parameter Store expiration policy** — set `policies` JSON with `Expiration`/`ExpirationNotification`/`NoChangeNotification` rules so credential-style parameters surface staleness. N/A for the current greeting parameter (not a credential), but worth wiring once any rotating secret/credential lives in Parameter Store.

### WAF

- [ ] **CloudWatch alarms on BlockedRequests spikes and WebACLCapacityUnits (WCU)** — same gap as the broader [CloudWatch alarms](#observability) item. Sustained block spikes are a leading indicator of an attack ramp; WCU alarms surface when added rules push the WebACL toward the 1500 WCU pricing threshold.
- [ ] **Pin AMR managed-rule-group versions** — the WebACL currently uses the floating "default" version of each AMR. Pinning to a specific version (e.g. `Version_2.0` of `AWSManagedRulesCommonRuleSet`) means rule updates from AWS go through your release process rather than landing automatically. Trade-off: less drift, but you have to track AMR change announcements and bump the version manually.
- [ ] **Subscribe to AMR SNS update topics** — AWS publishes notifications when managed rule groups change behavior (action shifts, new sub-rules, deprecations). Subscribing means you find out *before* the change lands rather than from a Slack page about a sudden block-rate spike.
- [ ] **Add `AWSManagedRulesAnonymousIpList` AMR** — blocks Tor exit nodes, hosting providers, and known anonymizing services. Cheap on WCU (~25), low false-positive rate. Skip only if you have legitimate traffic from those sources (rare).
- [ ] **CAPTCHA / Challenge actions on high-risk routes** — N/A until login/signup/credential-bearing routes exist. When they do, replacing a `Block` action on a rate-based rule with `Challenge` (silent JS challenge) or `CAPTCHA` (visible) catches bots without false-positiving real users.
- [ ] **Geo-blocking rule** — if you have countries that should never reach the app, a single `geoMatchStatement` rule blocks them at the edge. Free, low WCU. Skip if global traffic is expected.
- [ ] **Bot Control / ATP / ACFP** — paid advanced AMRs (Bot Control common ~$10/M requests, targeted higher). Bot Control covers automated browser-based traffic; ATP (Account Takeover Prevention) protects login flows; ACFP (Account Creation Fraud Prevention) protects signup flows. Skip until login/signup routes exist and are observably under attack.
- [~] **AntiDDoSRuleSet** — *implemented and deliberately reverted.* The rule group provides L7 anti-DDoS via Challenge + Block, but it carries a $20/month per-WebACL entity activation fee plus $0.15/million requests on top of the standard WAF base ($5/month WebACL + $1/month per rule + $0.60/million inspections). For this reference architecture that would have raised fixed WAF cost from $9/month to $30/month — a 70% increase for one rule whose Block arm only fires on AWS-observed high-confidence DDoS classification, which the existing per-IP rate limit (1,000 req/5min/IP) already covers at the threat profile a Hello World demo realistically faces. Also requires `ManagedRuleGroupConfig.ClientSideActionConfig` declared and (when `UsageOfAction=ENABLED`) at least one URI in `ExemptUriRegularExpressions` — neither fits a CloudFront-fronted SPA with no health-check or m2m endpoints to exempt. See "Considered and rejected" in the README for the longer write-up. Revisit if the workload ever sees enough traffic that AWS's classifier has signal to act on, or when the $20/month is rounding error.
