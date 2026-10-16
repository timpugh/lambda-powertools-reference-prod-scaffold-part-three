# AGENTS.md

Starting point for AI agents working in this repository — `lambda-powertools-reference-prod-scaffold-part-three`, the production-hardening scaffold of a five-stack AWS CDK (Python) reference architecture for Lambda + Powertools serverless applications, deployable directly or through a self-mutating CDK Pipeline, and designed to be forked via GitHub's "Use this template".

This file is the condensed navigation layer. Deeper, authoritative material: [CLAUDE.md](CLAUDE.md) (design rationale, live-verified gotchas, the part-three plan), [README.md](README.md) (full documentation and runbooks), the [ship-a-change skill](.claude/skills/ship-a-change/SKILL.md) (step-by-step process for shipping a change through every gate, including the boundary-first bootstrap, pipeline mode, and the pipeline-reserved `dev`), and the generated knowledge base at [.agents/summary/index.md](.agents/summary/index.md).

## Contents

- [Repo map](#repo-map) — where every kind of code lives
- [Entry points and modes](#entry-points-and-modes) — direct vs pipeline synthesis, the request path
- [Hard rules](#hard-rules) — things that break silently or expensively if ignored
- [Gates and how to satisfy them](#gates-and-how-to-satisfy-them) — what CI checks and the local mirror
- [Make target groups](#make-target-groups) — the canonical interface, grouped
- [Repo-specific patterns](#repo-specific-patterns) — deviations from Python/CDK defaults
- [Config an agent would miss](#config-an-agent-would-miss) — behavior hidden in config files
- [Deployment traps](#deployment-traps) — the context flags and lifecycle steps with sharp edges
- [Custom Instructions](#custom-instructions) — human/agent-maintained conventions

## Repo map

<!-- meta: navigation, directory-structure -->

| Path | What lives there |
|---|---|
| `app.py` | CDK entry point, mode-switched by `-c pipeline=true` (see [Entry points and modes](#entry-points-and-modes)); attaches the cdk-nag packs once at the App root |
| `infrastructure/app_stage.py` | Stage composing the five stacks; env naming (prod names are pinned — never rename); synth-time validators; the shared permissions-boundary descriptor |
| `infrastructure/data_stack.py`, `audit_stack.py` | The two **stateful** stacks (DynamoDB + CMK; CloudTrail trail + bucket + CMK) behind the `retain_data` switch |
| `infrastructure/waf_stack.py` | CloudFront-scoped WAF WebACL, always us-east-1 |
| `infrastructure/backend_stack.py` → `backend_app.py` | Thin stack shell → the domain construct (Lambda + extension layers, REGIONAL API Gateway, SSM, AppConfig, `OriginVerifySecret`, regional WAF lockdown, canary deploy, monitoring and alarms). Most backend changes land in `backend_app.py` |
| `infrastructure/frontend_stack.py` | S3 + CloudFront (including the same-origin `/api/*` proxy) + RUM + Athena/Glue log analytics |
| `infrastructure/pipeline_stack.py` | `PipelineStack`: CodeConnections source, ARM CodeBuild synth + nag gate, `Dev` stage + integration tests, `PromoteToProd` approval, `Prod` stage |
| `infrastructure/bootstrap/cdk-scaffold-boundary.json` | Standalone CloudFormation template (not CDK) for the `cdk-scaffold-boundary` permissions-boundary policy |
| `infrastructure/appconfig_extension.py` | Pinned per-region arm64 AppConfig Agent Lambda extension layer ARNs + the override-aware resolver `BackendApp` uses |
| `infrastructure/nag_utils.py` | cdk-nag plumbing (`attach_nag_packs`, `acknowledge_rules`) + shared helpers (log-sink buckets, KMS confused-deputy grants, WAF log redaction, alarm routing, DLQs, CDK-singleton suppressions) |
| `infrastructure/validation_aspects.py` | Project conventions enforced at synth: explicit log retention, explicit removal policy on stateful L1s |
| `lambda/` | Runtime code: `app.py` (handler/HTTP boundary) → `service.py` (business logic) → `models.py` (Pydantic contracts), plus `extension_store.py` (feature flags read from the AppConfig Lambda extension on `localhost:2772`). `requirements.txt` here is **generated** by `make lock` |
| `tests/` | `unit/` (handler + extension store, 100% branch-coverage gate), `cdk/` (assertions, in-process nag gates for the Stage and pipeline shapes, boundary template, snapshots), `integration/` (live stack through CloudFront `/api`; not in GitHub CI, run by the pipeline against `dev`) |
| `scripts/` | `check_validation_report.py` (the real nag gate), `generate_openapi.py`, `cdk_pr_diff.py`, `deps_merge.sh`, `audit_account.sh` (read-only account sweep), `dns_lifecycle.sh` (hosted-zone park/resume) |
| `docs/openapi.json` | **Committed, CI-gated** API spec — regenerate with `make openapi` after touching routes/models |
| `docs/superpowers/` | Dated design specs and implementation plans — historical records; code and README win where they differ |
| `Makefile` | The canonical interface; `make help` lists everything, `make pr` mirrors CI |

## Entry points and modes

<!-- meta: entry-points, modes -->

- **Direct mode (default)**: `app.py` builds one `AppStage` for the `region` + `env` context — `make deploy` (prod, legacy stack names) or `make deploy ENV=<name>` (namespaced ephemeral copy). Five stacks: `ServerlessApp{Waf,Data,Backend,Frontend,Audit}-{region}`.
- **Pipeline mode (`-c pipeline=true`)**: `app.py` builds `PipelineStack` (`ServerlessAppPipeline`) instead, embedding `AppStage` twice — `Dev` (env `dev`) and `Prod` (legacy names, adopted in place). It needs `CDK_DEFAULT_ACCOUNT` and a CodeConnections ARN, and rejects any `-c env`. Born once with `make deploy-pipeline`; afterwards it self-mutates from GitHub `main`.
- **Request path**: `lambda/app.py::lambda_handler` serves `GET /greeting`. Browsers reach it same-origin as `/api/greeting` through CloudFront; the direct `execute-api` URL gets a flat 403 from the regional WAF's `RejectNonCloudFront` rule, which requires the `x-origin-verify` header CloudFront injects.
- **Custom domain**: README "Custom domain" is a locked design, **not shipped** — no domain context key, certificate, or DNS record exists in code yet.

## Hard rules

<!-- meta: gotchas, critical, breakage -->

1. **Two venvs, never mixed.** CDK and Powertools need incompatible `attrs`. `.venv` = CDK side (synth, `tests/cdk`, infra lint); `.venv-lambda` = runtime side (`tests/unit`, integration, OpenAPI generator). Use the Make targets — they select the venv via `UV_PROJECT_ENVIRONMENT`. Recovery: `make clean-venvs && make install`.
2. **Every Stage-scoped CDK CLI command needs the `'**'` glob** (`cdk synth '**'`, `deploy '**'`, `diff '**'`). The five stacks are nested in a `cdk.Stage`; bare invocations see an empty manifest and silently do nothing. The pipeline targets instead name the top-level `ServerlessAppPipeline` stack.
3. **The nag gate is the report check, not the exit code.** `cdk synth` exits 0 even with findings (Python app → jsii's throwaway Node kernel). The gates are `scripts/check_validation_report.py` (CLI, CI, and the pipeline's Synth step) and the in-process `tests/cdk/test_stage.py::TestNagCompliance` / `tests/cdk/test_pipeline_stack.py::TestPipelineNagCompliance` (Docker-free). Fix findings or acknowledge via `acknowledge_rules` with a real reason; granular IAM rules need exact `applies_to` finding ids (the failure output prints them).
4. **Don't run `pytest tests/cdk` bare** — project-wide `addopts` hardcodes a 100% `lambda/` coverage gate only the unit suite satisfies. The Make targets pass `--override-ini="addopts="`.
5. **Committed generated artifacts must move with their source**: `docs/openapi.json` (`make openapi`), `lambda/requirements.txt` (`make lock`), CDK snapshots including the pipeline's (`UPDATE_SNAPSHOTS=1 make test-cdk`). CI fails on drift.
6. **Never rename prod stacks** (`ServerlessAppBackend-us-east-1` etc.) — CloudFormation matches by name, and the pipeline's prod stage adopts them in place. Never introduce account/region-wide constructs without flagging (they mutate neighbors' state).
7. **Stack-boundary moves are constrained**: the audit→frontend dependency is one-way by design; moving the access-log bucket into the audit stack or the trail into the frontend stack creates a cycle (see CLAUDE.md).
8. **The permissions boundary is mandatory for every deploy.** Every app-created role carries `cdk-scaffold-boundary`; a missing policy fails every role at deploy, and a permission the boundary denies surfaces at deploy or runtime — never at synth. A resource needing a new AWS service means extending `AllowServiceActions` in the boundary template and rerunning `make bootstrap-boundary`.
9. **Never commit account-identifying values.** The CodeConnections ARN stays out of code and `cdk.json`; it resolves from `CONN=` → the gitignored `./.env` → account auto-discovery.
10. **Commits**: Conventional Commit prefixes (enforced on PR titles); no `Co-Authored-By:` trailers.

## Gates and how to satisfy them

<!-- meta: ci, testing, quality-gates -->

`make pr` runs every CI gate locally in CI's order: lock-sync → pre-commit (ruff format/lint, mypy, bandit, pylint, xenon, pip-audit) → both-venv typecheck → markdownlint → unit tests (100% gate) → CDK tests → `cdk synth '**'` + nag report (needs Docker) → OpenAPI drift. A clean `make pr` should mean a green CI run.

Per-change quick routing:

| You changed… | Run before pushing |
|---|---|
| `lambda/` routes/models | `make test`, then `make openapi` + commit the spec |
| `infrastructure/` | `make test-cdk` (expect nag findings on new resources), update snapshots, `make cdk-synth` if IAM changed |
| `infrastructure/pipeline_stack.py` | `make test-cdk` — `make cdk-synth` does not synthesize the pipeline shape; the in-process pipeline gate and snapshot do |
| `infrastructure/bootstrap/*.json` | `make test-cdk` (boundary invariants), then `make bootstrap-boundary` in the target account |
| `infrastructure/feature_flags.json` | `make test` — the flag schema is validated in the unit suite, not by CDK |
| `pyproject.toml` deps | `make lock` + commit `uv.lock` **and** `lambda/requirements.txt` |
| Markdown | `make lint-docs` (root `*.md`, `docs/**`, and the `.agents/**` knowledge base are linted, locally and in CI; CHANGELOG.md excluded) |

PRs additionally get an oasdiff breaking-change gate and a sticky CloudFormation-diff comment (hermetic, credential-free). In pipeline mode the CD pipeline re-runs the synth + nag gate, deploys `dev`, runs the live integration suite, and waits at `PromoteToProd` before prod.

## Make target groups

<!-- meta: tooling, make-targets -->

| Group | Targets |
|---|---|
| Setup | `install`, `install-cdk`, `install-lambda`, `doctor`, `clean-venvs` |
| Quality | `pr`, `lint`, `lint-docs`, `typecheck`, `security`, `check-lock`, `format` |
| Tests | `test`, `test-cdk`, `test-integration`, `coverage`, `coverage-badge` |
| CDK wrappers (`'**'`-scoped) | `cdk-synth`, `cdk-ls`, `cdk-diff`, `cdk-drift`, `cdk-revert-drift`, `cdk-diagnose`, `cdk-rollback`, `cdk-deprecations` (plus `cdk-notices` and the dry-run `cdk-gc`) |
| Direct deploys | `deploy [ENV=]`, `deploy-appconfig-monitor`, `destroy`, `destroy-clean [ENV=] [REGION=]` |
| CD pipeline and boundary | `bootstrap-boundary`, `deploy-pipeline [CONN=]`, `destroy-pipeline` |
| Account operations | `audit-account` (read-only; exit 0 clean / 1 dirty / 2 no credentials), `hibernate-dns`, `wake-dns` (both need `DOMAIN_NAME` in `./.env`) |
| Artifacts and dependencies | `openapi`, `compare-openapi`, `docs`, `docs-open`, `docs-serve`, `lock`, `upgrade`, `deps-merge`, `clean` |

## Repo-specific patterns

<!-- meta: conventions, deviations, style -->

- **Fail loud at synth**: boolean context flags reject anything but `true`/`false`; env names, SSM paths, connection ARNs, and the AppConfig extension layer override are regex-validated; the AppConfig extension layer lookup raises on an unmapped region with no override; new log groups need explicit `retention=` and stateful L1s need explicit removal policies or synth errors.
- **Stage-scoped Aspects**: Aspects do not cross `cdk.Stage` boundaries, so `apply_compliance_aspects` is called per stack (including `PipelineStack`), while the nag packs attach once at the App root as validation plugins.
- **Suppressions carry rationale**: every `acknowledge_rules` entry has a `reason` worth reading; module docstrings and long comments are the design record (several marked "verified live") — match that density when editing.
- **Pinned toolchain**: the CDK CLI comes from `package.json` via `npx cdk` (never global installs); linters run as `language: system` pre-commit hooks so versions come from `pyproject.toml`.
- **Per-stack CMKs with confused-deputy-guarded service grants** (`aws:SourceAccount` + `aws:SourceArn`; the alarm and budget publish paths use `kms:ViaService` instead) — use the `nag_utils.py` grant helpers, and read CLAUDE.md's encryption section before touching key policies.
- **Same-origin API, no CORS**: there is no `CORSConfig` and no CORS header anywhere; new routes are reached as `/api/<route>` through the CloudFront viewer-request rewrite.
- **Out-of-CFN resources get cleanup custom resources** (`RumLogGroupCleanup`, `AppInsightsDashboardCleanup` pattern: `on_delete` SDK call, ARN-scoped IAM, ignore `ResourceNotFoundException`).
- **Telemetry contract**: `tenant_id` is EMF *metadata*, not a dimension — the `{service}` dimension set is pinned by a unit test; changing it blinds the dashboard and the AppConfig rollback alarm. WAF alarms address the WebACL by **name**, not by its metric name.
- **Everything is Python 3.14**: the Lambda runtime (`PYTHON_3_14`, arm64) and the workstation toolchain (`requires-python >= 3.14`, ruff `py314`, mypy `3.14`) are deliberately aligned — bump them together.

## Config an agent would miss

<!-- meta: configuration, hidden-behavior -->

- **`.pre-commit-config.yaml`**: `check-added-large-files` excludes `^tests/cdk/snapshots/` — the pipeline snapshot is legitimately large. Keep the exclusion scoped to that directory.
- **`pyproject.toml` pytest-env**: `AWS_BACKEND_STACK_NAME` / `AWS_FRONTEND_STACK_NAME` carry the `D:` prefix, so an exported value wins over the prod-name default — that is how the integration suite targets an ephemeral env or the pipeline's dev stacks.
- **`DEV_ENV_NAME = "dev"`** in `infrastructure/pipeline_stack.py` reserves `dev` for the pipeline; `make deploy ENV=dev` would fight it over the same stacks.
- **`cdk.json`** sets only `retain_data` among the production switches; `pipeline` is documented there in a comment only, and `code_connection_arn` must never be stored there.
- **`./.env`** (gitignored, optional, sourced per command and never exported): `CODE_CONNECTION_ARN` (read by the pipeline targets), `DOMAIN_NAME` (read by the DNS scripts), `HOSTED_ZONE_ID` (written by `make wake-dns`; no code consumes it yet).
- **`APPCONFIG_EXTENSION_ARM64_LAYER_ARNS`** in `infrastructure/appconfig_extension.py` pins the arm64 AppConfig extension (2.0.22365; `me-*` at 2.0.18836) per region; a region outside it fails synth with a `ValueError` unless `-c appconfig_extension_layer_arn=<arn>` / `APPCONFIG_EXTENSION_LAYER_ARN` supplies an ARN *in that region and its partition* (an active override emits a synth warning; in pipeline mode it is baked into the Synth env and sticks until you unset it and re-run `make deploy-pipeline`) (pinned by `tests/cdk/test_stage.py::TestAppConfigExtensionLayerRegion`). Refresh entries verbatim from the AppConfig docs, never compose ARNs.
- **uv is pinned to `0.12.24`** in three kinds of location that move together by hand (Dependabot covers none of them): `version:` on every `astral-sh/setup-uv` step in `.github/workflows/`, `UV_VERSION` in `infrastructure/pipeline_stack.py` (bumping it changes the pipeline snapshot), and the `required-version` floor in `pyproject.toml`'s `[tool.uv]`.
- **The AppConfig profile is `AWS.Freeform`** (the native feature-flags type serves a shape Powertools rejects), and pinned AppConfig/WAF/RUM names must change in the same commit as any replacement-forcing property change.
- **Pipeline nag acknowledgments** carry both partition renderings and construct-tree logical-id hashes; reshaping the pipeline's stages or steps can silently un-acknowledge findings until `make test-cdk` flags them.

## Deployment traps

<!-- meta: deployment, operations -->

- **Boundary first**: `make bootstrap-boundary`, then `npx cdk bootstrap aws://<account>/us-east-1 --custom-permissions-boundary cdk-scaffold-boundary` (plus the target region if different), before any deploy in a new account.
- **`appconfig_monitor` must never be set on a cold/first deploy** — the monitor's fresh alarm starts `INSUFFICIENT_DATA`, AppConfig treats that as a rollback signal, and stack creation aborts. Use `make deploy-appconfig-monitor` (guarded) only after a first `make deploy`; in pipeline mode, wait until both dev and prod have reported the metric.
- **`retain_data=true`** is the production switch (RETAIN + deletion/termination protection + Backup + the audit Object Lock tier); safe from the first deploy; sticky home is `cdk.json`. Object Lock is creation-time-only, so flipping it on a deployed stack replaces the CloudTrail bucket — set it before real audit data accumulates.
- **Pipeline**: push before (or with) any workstation `make deploy-pipeline`, never after; the AWS Connector for GitHub app must be *installed* with this repo granted, not merely authorized, or pushes start nothing.
- **`OriginVerifySecret` rotation** is the two-deploy recipe in `backend_app.py` — never rename the construct and never put a new value into the existing secret.
- **Teardown**: use `make destroy-clean [ENV=name]`, not bare destroy — async log delivery re-fills buckets and re-creates log groups; the target snapshots and sweeps them. Full lifecycle: `make destroy-pipeline` → `make destroy-clean ENV=dev` → `make destroy-clean` → `make audit-account` (then `make hibernate-dns` for long parks).
- **Ephemeral envs**: `make deploy ENV=<name>` gives a namespaced, collision-free copy of all five stacks; non-prod alarms page nobody.

## Custom Instructions

<!-- This section is maintained by developers and agents during day-to-day work.
     It is NOT auto-generated by codebase-summary and MUST be preserved during refreshes.
     Add project-specific conventions, gotchas, and workflow requirements here. -->
