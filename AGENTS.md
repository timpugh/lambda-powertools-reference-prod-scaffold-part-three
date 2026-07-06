# AGENTS.md

Starting point for AI agents working in this repository — a five-stack AWS CDK (Python) reference architecture for Lambda + Powertools serverless applications, designed to be forked via GitHub's "Use this template".

This file is the condensed navigation layer. Deeper, authoritative material: [CLAUDE.md](CLAUDE.md) (design rationale and live-verified gotchas), [README.md](README.md) (full documentation), the [ship-a-change skill](.claude/skills/ship-a-change/SKILL.md) (step-by-step process for shipping a change through every gate), and the generated knowledge base at [.agents/summary/index.md](.agents/summary/index.md).

## Contents

- [Repo map](#repo-map) — where every kind of code lives
- [Hard rules](#hard-rules) — things that break silently or expensively if ignored
- [Gates and how to satisfy them](#gates-and-how-to-satisfy-them) — what CI checks and the local mirror
- [Repo-specific patterns](#repo-specific-patterns) — deviations from Python/CDK defaults
- [Deployment traps](#deployment-traps) — the two context flags with sharp edges
- [Custom Instructions](#custom-instructions) — human/agent-maintained conventions

## Repo map

<!-- meta: navigation, directory-structure -->

| Path | What lives there |
|---|---|
| `app.py` | CDK entry point: context parsing (`region`, `env`, `retain_data`, `appconfig_monitor`), nag-pack attachment, one `AppStage` |
| `infrastructure/app_stage.py` | Stage composing the five stacks; env-name validation; strict context-flag parsing; stack naming (prod names are pinned — never rename) |
| `infrastructure/data_stack.py`, `audit_stack.py` | The two **stateful** stacks (DynamoDB+CMK; CloudTrail trail+bucket+CMK) behind the `retain_data` switch |
| `infrastructure/waf_stack.py` | CloudFront-scoped WAF WebACL, always us-east-1 |
| `infrastructure/backend_stack.py` → `backend_app.py` | Thin stack shell → the domain construct (Lambda, API Gateway, SSM, AppConfig, canary deploy, monitoring). Most backend changes land in `backend_app.py` |
| `infrastructure/frontend_stack.py` | S3 + CloudFront + RUM + Athena/Glue log analytics |
| `infrastructure/nag_utils.py` | cdk-nag plumbing (`attach_nag_packs`, `acknowledge_rules`) + shared helpers (log-sink buckets, KMS confused-deputy grants, DLQs, CDK-singleton suppressions) |
| `infrastructure/validation_aspects.py` | Project conventions enforced at synth: explicit log retention, explicit removal policy on stateful L1s |
| `lambda/` | Runtime code: `app.py` (handler/HTTP boundary) → `service.py` (business logic) → `models.py` (Pydantic contracts). `requirements.txt` here is **generated** by `make lock` |
| `tests/` | `unit/` (handler, 100% branch-coverage gate), `cdk/` (assertions + the in-process nag gate + snapshots), `integration/` (live stack, not in CI) |
| `scripts/` | `check_validation_report.py` (the real nag gate), `generate_openapi.py`, `cdk_pr_diff.py`, `deps_merge.sh` |
| `docs/openapi.json` | **Committed, CI-gated** API spec — regenerate with `make openapi` after touching routes/models |
| `Makefile` | The canonical interface; `make help` lists everything, `make pr` mirrors CI |

## Hard rules

<!-- meta: gotchas, critical, breakage -->

1. **Two venvs, never mixed.** CDK and Powertools need incompatible `attrs`. `.venv` = CDK side (synth, `tests/cdk`, infra lint); `.venv-lambda` = runtime side (`tests/unit`, integration, OpenAPI generator). Use the Make targets — they select the venv via `UV_PROJECT_ENVIRONMENT`. Recovery: `make clean-venvs && make install`.
2. **Every CDK CLI command needs the `'**'` glob** (`cdk synth '**'`, `deploy '**'`, `diff '**'`). The stacks are nested in a `cdk.Stage`; bare invocations see an empty manifest and silently do nothing.
3. **The nag gate is the report check, not the exit code.** `cdk synth` exits 0 even with findings (Python app → jsii's throwaway Node kernel). The gates are `scripts/check_validation_report.py` (CLI/CI) and `tests/cdk/test_stage.py::TestNagCompliance` (in-process, Docker-free). Fix findings or acknowledge via `acknowledge_rules` with a real reason; granular IAM rules need exact `applies_to` finding ids (the failure output prints them).
4. **Don't run `pytest tests/cdk` bare** — project-wide `addopts` hardcodes a 100% `lambda/` coverage gate only the unit suite satisfies. The Make targets pass `--override-ini="addopts="`.
5. **Committed generated artifacts must move with their source**: `docs/openapi.json` (`make openapi`), `lambda/requirements.txt` (`make lock`), CDK snapshots. CI fails on drift.
6. **Never rename prod stacks** (`ServerlessAppBackend-us-east-1` etc.) — CloudFormation matches by name. Never introduce account/region-wide constructs without flagging (they mutate neighbors' state).
7. **Stack-boundary moves are constrained**: the audit→frontend dependency is one-way by design; moving the access-log bucket into the audit stack or the trail into the frontend stack creates a cycle (see CLAUDE.md).
8. **Commits**: Conventional Commit prefixes (enforced on PR titles); no `Co-Authored-By:` trailers.

## Gates and how to satisfy them

<!-- meta: ci, testing, quality-gates -->

`make pr` runs every CI gate locally in CI's order: lock-sync → pre-commit (ruff format/lint, mypy, bandit, pylint, xenon, pip-audit) → both-venv typecheck → markdownlint → unit tests (100% gate) → CDK tests → `cdk synth '**'` + nag report (needs Docker) → OpenAPI drift. A clean `make pr` should mean a green CI run.

Per-change quick routing:

| You changed… | Run before pushing |
|---|---|
| `lambda/` routes/models | `make test`, then `make openapi` + commit the spec |
| `infrastructure/` | `make test-cdk` (expect nag findings on new resources), update snapshots, `make cdk-synth` if IAM changed |
| `pyproject.toml` deps | `make lock` + commit `uv.lock` **and** `lambda/requirements.txt` |
| Markdown | `make lint-docs` (root `*.md` and `docs/**` are linted; CHANGELOG.md excluded) |

PRs additionally get an oasdiff breaking-change gate and a sticky CloudFormation-diff comment (hermetic, credential-free).

## Repo-specific patterns

<!-- meta: conventions, deviations, style -->

- **Fail loud at synth**: boolean context flags reject anything but `true`/`false`; env names are regex-validated; new log groups need explicit `retention=` and stateful L1s need explicit removal policies or synth errors.
- **Suppressions carry rationale**: every `acknowledge_rules` entry has a `reason` worth reading; module docstrings and long comments are the design record (several marked "verified live") — match that density when editing.
- **Pinned toolchain**: the CDK CLI comes from `package.json` via `npx cdk` (never global installs); linters run as `language: system` pre-commit hooks so versions come from `pyproject.toml`.
- **Per-stack CMKs with confused-deputy-guarded service grants** (`aws:SourceAccount` + `aws:SourceArn`) — use the `nag_utils.py` grant helpers, and read CLAUDE.md's encryption section before touching key policies.
- **Out-of-CFN resources get cleanup custom resources** (`RumLogGroupCleanup`, `AppInsightsDashboardCleanup` pattern: `on_delete` SDK call, ARN-scoped IAM, ignore `ResourceNotFoundException`).
- **Telemetry contract**: `tenant_id` is EMF *metadata*, not a dimension — the `{service}` dimension set is pinned by a unit test; changing it blinds the dashboard and the AppConfig rollback alarm.
- **Lambda targets `PYTHON_3_14`/arm64** while the workstation toolchain targets 3.13 — handler code must satisfy both.

## Deployment traps

<!-- meta: deployment, operations -->

- **`appconfig_monitor` must never be set on a cold/first deploy** — the monitor's fresh alarm starts `INSUFFICIENT_DATA`, AppConfig treats that as a rollback signal, and stack creation aborts. Use `make deploy-appconfig-monitor` (guarded) only after a first `make deploy`.
- **`retain_data=true`** is the production switch (RETAIN + deletion/termination protection on the data and audit stacks); safe from the first deploy; sticky home is `cdk.json`.
- **Teardown**: use `make destroy-clean [ENV=name]`, not bare destroy — async log delivery re-fills buckets and re-creates log groups; the target snapshots and sweeps them.
- **Ephemeral envs**: `make deploy ENV=<name>` gives a namespaced, collision-free copy of all five stacks; non-prod alarms page nobody.

## Custom Instructions

<!-- This section is maintained by developers and agents during day-to-day work.
     It is NOT auto-generated by codebase-summary and MUST be preserved during refreshes.
     Add project-specific conventions, gotchas, and workflow requirements here. -->
