# CI/CD pipeline design — CDK Pipelines via CodeConnections

**Date:** 2026-07-10
**Branch:** `feat/ci-cd-environment`
**Status:** Approved design, pre-implementation

## Problem

CI is complete (GitHub Actions: quality, test, cdk-check, cdk-diff-on-PR) but CD does not
exist — every deploy is a manual `make deploy` from a workstation. CLAUDE.md's phase-two plan
names CDK Pipelines via a CodeConnections handshake as the intended direction, and TODO.md
holds three blocked items that all hang off it: the deploy workflow, live integration tests in
CI as a post-deploy gate, and a multi-environment pipeline with approval gates. TODO.md also
carries an explicit sequencing constraint: narrow the CDK bootstrap permissions with a
permissions boundary **before** any CI system gets deploy-capable credentials.

## Decisions (settled during brainstorming)

| Decision | Choice |
|---|---|
| CD mechanism | CDK Pipelines (CodePipeline + CodeBuild, self-mutating), sourced from GitHub `main` via CodeConnections |
| Stage ladder | dev → live integration tests → manual approval → prod |
| Account topology | Single account, us-east-1 (multi-account retrofittable via stage env props) |
| Bootstrap hardening | In scope, sequenced **first** — the pipeline is born inside the permissions boundary |
| Dev environment | Persistent — updated in place each run, never torn down by the pipeline |
| App layout | Mode-switched `app.py` via a new `-c pipeline=true` context flag; default shape unchanged |

## Architecture

One CDK app, two synth shapes:

```
                          ┌─ default (unchanged) ───────────────────────────┐
  app.py ─ attach_nag_packs┤  AppStage(env_name from -c env) → make deploy   │
                          └─ -c pipeline=true ────────────────────────────┐ │
                             PipelineStack "ServerlessAppPipeline"         │ │
                                                                           ▼ ▼
GitHub main ──(CodeConnections)──▶ Synth (CodeBuild, Docker-privileged)
                                     • npm ci, uv sync (.venv / cdk group)
                                     • npx cdk synth -c pipeline=true '**'
                                     • scripts/check_validation_report.py   ← nag gate inside the pipeline
                                  ▶ SelfMutate
                                  ▶ Deploy AppStage(env_name="dev")         ← persistent, retain_data=false
                                  ▶ Post: integration tests against dev     ← .venv-lambda, exported stack names
                                  ▶ ManualApprovalStep("PromoteToProd")
                                  ▶ Deploy AppStage(env_name="prod")        ← legacy stack names
```

Key properties:

- **Pipeline-deployed prod updates the existing prod stacks.** `env_name="prod"` produces the
  legacy stack names (`ServerlessAppBackend-us-east-1` etc.), so the pipeline adopts them in
  place — no parallel copy, no migration. After adoption, prod deploys go through the pipeline;
  `make deploy` remains for ephemeral ENV environments.
- **The `dev` env name is pipeline-reserved** by documented convention. A manual
  `make deploy ENV=dev` would fight the pipeline over the same stacks; developers use any other
  validated env name for ephemeral copies.
- **The nag gate rides inside the pipeline.** The synth step reruns the exact CI pair —
  `cdk synth '**'` + `scripts/check_validation_report.py` — so a finding that slips past a
  bypassed branch protection still cannot deploy.
- **Flags stay cdk.json-driven.** The synth step runs from the repo checkout, so `retain_data`
  and `appconfig_monitor` behave exactly as documented today. `AppStage` reuse is unchanged —
  the pipeline consumes it via `pipeline.add_stage(...)`, which is what `cdk.Stage` exists for.
- **Everything is us-east-1**, so the WafStack's CloudFront-scope region requirement is
  satisfied without cross-region wiring.

### One-time manual pre-steps (in order)

1. **Permissions boundary**: deploy the boundary policy (`make bootstrap-boundary`), then
   re-bootstrap with `cdk bootstrap --custom-permissions-boundary cdk-scaffold-boundary`.
2. **CodeConnections handshake**: create the GitHub connection in the console, authorize the
   GitHub App on `timpugh/lambda-powertools-reference-prod-scaffold-part-one`, and put the
   connection ARN in `cdk.json` as `code_connection_arn` (an ARN, not a secret — safe to
   commit). Synth in pipeline mode fail-loud-validates its presence and shape.
3. **Birth the pipeline**: `make deploy-pipeline` (one `cdk deploy -c pipeline=true` of
   `ServerlessAppPipeline` from a workstation). The pipeline self-mutates from then on.

## Permissions boundary (first implementation step)

Follows AWS's documented CDK pattern, two halves:

1. **The boundary policy** — a customer-managed policy named `cdk-scaffold-boundary`, defined
   as a small standalone CloudFormation template in `infrastructure/bootstrap/` and deployed
   via `aws cloudformation deploy` (wrapped in `make bootstrap-boundary`) so the policy is
   IaC-auditable and updatable in place. It allows the service actions this app actually uses
   and carries the standard anti-escalation denies:
   - cannot create or attach policies granting admin beyond the boundary,
   - cannot delete, detach, or alter the boundary itself,
   - `iam:CreateRole` / `iam:PutRolePolicy` conditioned on the boundary being present on new
     roles.
   The allow-list is derived from the app's existing synthesized IAM policies rather than
   written from scratch.
2. **`@aws-cdk/core:permissionsBoundary` context in cdk.json** so every role the app creates
   automatically carries the boundary — required, because the boundary's own `CreateRole`
   condition would otherwise reject the app's roles. This touches every role in every stack,
   so **all committed snapshots regenerate** (each role gains a `PermissionsBoundary`
   property), paired with fine-grained assertions per the snapshot-update rule.

Then re-bootstrap. Only after this lands does the pipeline stack ever deploy.

## Components

- **`infrastructure/pipeline_stack.py`** (new) — `PipelineStack`, the only new stack:
  - `pipelines.CodePipeline` with `CodePipelineSource.connection(...)` on `main`,
    self-mutation on, Docker-privileged CodeBuild defaults (PythonFunction asset bundling
    needs Docker).
  - A **dedicated CMK** (per-stack key pattern) encrypting the artifact bucket and the
    CodeBuild log groups, consistent with the repo's encryption posture.
  - **Explicit CFN-owned log groups** handed to every CodeBuild step (90-day retention).
    Satisfies `TemplateConventionChecks` and avoids the dangling-resource problem — CodeBuild
    otherwise auto-creates never-expire log groups outside CloudFormation.
  - Dev stage → integration-test post-step (`uv sync` the `.venv-lambda` groups, pytest with
    `AWS_BACKEND_STACK_NAME` / `AWS_FRONTEND_STACK_NAME` env vars pointed at the dev stacks —
    the pytest-env `D:`-prefix fix exists exactly for this; IAM scoped to
    `cloudformation:DescribeStacks` on those stacks) → `ManualApprovalStep("PromoteToProd")` →
    prod stage.
- **`app.py` + `infrastructure/app_stage.py`** — the `pipeline` context flag, plumbed and
  fail-loud-validated like `retain_data`; `code_connection_arn` validated as
  required-when-pipeline.
- **`infrastructure/bootstrap/`** (new) — the boundary policy artifact + deploy recipe.
- **Makefile** — `make bootstrap-boundary`, `make deploy-pipeline`; existing targets untouched.
- **Docs** — README section (pipeline + boundary + runbook), CLAUDE.md phase-two update,
  TODO.md check-offs (deploy workflow, multi-environment pipeline, live integration tests in
  CI, bootstrap permissions boundary).

## Failure modes and known traps

1. **Nag findings on generated pipeline roles.** CDK Pipelines emits wildcard-heavy roles
   (IAM5) and inline policies (IAMNoInlinePolicy). Expect an acknowledgment batch with exact
   `applies_to` finding ids (the gate's failure output prints them); iterate via
   `make test-cdk` with the pipeline shape added to `TestNagCompliance`'s matrix.
2. **Boundary too tight.** A boundary that blocks a legitimate action fails at deploy or
   runtime, not synth. Mitigation: derive the allow-list from the app's synthesized policies
   and verify with a full ephemeral-env deploy + integration run before the pipeline relies
   on it.
3. **AppConfig cold-deploy trap, now ×2.** Both cold deploys (dev on the pipeline's first
   run, prod on the first approval) must happen with `appconfig_monitor=false`. Flip it in
   `cdk.json` only after **both** environments' `FeatureFlagEvaluationFailure` metrics have
   reported — the same sequencing rule as today, extended to two environments, documented in
   the pipeline runbook.
4. **Export-retention interaction.** The two temporary export retentions (TODO.md /
   `backend_stack.py`, `backend_app.py`) must stay until the pipeline has deployed prod once;
   the pipeline's first prod deploy counts as the "deploy once with the export retained" step
   of CDK's two-deploy removal recipe.
5. **Self-mutation loops are normal.** A pipeline-definition change makes run N update the
   pipeline and restart as run N+1 — documented so it doesn't read as a stuck pipeline.

## Testing

- **`tests/cdk`**: pipeline shape joins the `TestNagCompliance` synthesis matrix; a normalized
  snapshot for `PipelineStack`; fine-grained assertions for the load-bearing properties (stage
  order dev→approval→prod, connection source, privileged mode, explicit log-group retention,
  artifact-bucket CMK, permissions boundary present on roles); context-validation tests for
  the two new context keys (`pipeline`, `code_connection_arn`).
- **Boundary policy**: assertion tests that the anti-escalation denies exist and the document
  is valid IAM policy JSON; real verification is the live ephemeral deploy in trap 2.
- **Live verification** (phase-two's whole point): one full pipeline run observed end to end —
  cold dev deploy, integration tests green against dev, manual approval, prod deploy.

## Out of scope

- Multi-account dev/prod split (retrofittable via stage env props).
- A staging environment (dev + canary-in-prod covers the ladder for a solo-dev scaffold).
- Ephemeral per-run pipeline environments (the persistent dev env was chosen deliberately).
- Alarm subscriptions and the remaining phase-two verification items (SNS delivery, spend
  budget cost-filter confirmation, canary rollout observation) — separate work that the
  pipeline enables but does not include.
- PR preview environments via GitHub Actions.

## Cost notes

CodePipeline (~$1/month active pipeline), CodeBuild minutes per run, one additional KMS key
(~$1/month), standing dev environment (serverless, mostly per-request). Modest relative to the
existing stack; called out in the README section.
