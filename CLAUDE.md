# CLAUDE.md

Project-level instructions for future Claude Code sessions. Loaded on every session against this repo.

## Project

Reference architecture for serverless AWS Lambda + Powertools applications: four-stack CDK composition (a stateful data stack plus WAF, backend, and frontend), five-rule-pack cdk-nag gating, end-to-end CMK encryption, WAF + CloudFront, CloudTrail data events, browser RUM with X-Ray correlation, Athena access-log analytics, and supply-chain hygiene. Designed to be forked via GitHub's "Use this template" — see the "Forking this template" section at the bottom.

## Environments — two venvs, never mix

CDK and Powertools require incompatible `attrs` versions (CDK pulls `attrs<26` via jsii; Powertools pulls `attrs>=26`). `[tool.uv.conflicts]` in `pyproject.toml` lets one `uv.lock` hold both resolutions, installed into separate venvs:

- `.venv` — CDK workstation. Used for `cdk synth`, `cdk deploy`, stack-assertion tests, lint/format/typecheck of `hello_world/`.
- `.venv-lambda` — Lambda runtime. Used for unit tests over `lambda/`, integration tests, the OpenAPI generator script.

`make install` provisions both venvs, runs `npm ci` (the CDK CLI and markdownlint are pinned in `package.json` and invoked via `npx` — never `npm install -g`), and wires pre-commit. Never install Powertools into `.venv` or CDK into `.venv-lambda`. One deliberate exception: plain `pydantic` is pinned in the `lint` group (so it lands in BOTH venvs) purely so mypy's `pydantic.mypy` plugin loads on each side — it has no `attrs` dependency, so it doesn't touch the conflict.

Run `make doctor` after `make install` to verify both venvs picked up the expected groups, `npx cdk`/`drawio` resolve, and pre-commit is wired. `make clean-venvs && make install` is the recovery path for a corrupted venv. `make pr` runs every CI gate locally in CI's order.

## `cdk synth` must use `'**'`

All four stacks live inside `HelloWorldStage` (a `cdk.Stage`). Bare `cdk synth` walks only the App's direct children, finds the Stage, doesn't recurse, and emits an empty synthesis that succeeds *without* running cdk-nag against the real stacks. `make cdk-synth` and the CI `cdk-check` job both invoke `cdk synth '**'`. If you run `cdk synth` directly during development, include the glob — otherwise the gate passes silently regardless of what cdk-nag would find.

## cdk-nag is a hard gate

Five rule packs run on every synth: AwsSolutions, Serverless, NIST 800-53 R5, HIPAA Security, PCI DSS 3.2.1. Findings fail CI. Resolve by:

1. **Fix the underlying issue** (preferred). README "Design decisions and known limitations" documents recurring patterns.
2. **Suppress with rationale**. Every suppression carries a `reason=` string. For `AwsSolutions-IAM5` wildcards, scope with `applies_to=["Resource::*"]` or a specific pattern and explain *why* the wildcard is unavoidable.

A bespoke validation Aspect rides alongside the packs (also wired by `apply_compliance_aspects`): `TemplateConventionChecks` in `hello_world/validation_aspects.py` enforces two project conventions no rule pack covers — every log group declares an explicit retention (never-expire is the CloudWatch default), and every stateful resource (`CfnBucket`, DynamoDB `CfnTable`/`CfnGlobalTable`, `CfnKey`) declares an explicit removal policy. Its violations are error-level annotations, so they fail the same gates as nag findings (and `TestNagCompliance`); it's unit-tested in `tests/cdk/test_validation_aspects.py`.

**Local nag gate**: `Template.from_stack()` does **NOT** raise on cdk-nag (or validation-Aspect) errors, but they surface as error-level annotations — and `tests/cdk/test_stage.py::TestNagCompliance` asserts that list is empty for every stack (prod and ephemeral shapes). So `make test-cdk` catches unsuppressed findings locally, without Docker. The CLI `cdk synth '**'` in the CI `cdk-check` job remains the authoritative gate (it also exercises asset bundling); run `make cdk-synth` with Docker started for the full CI-equivalent check before pushing IAM-touching code.

## Encryption posture

Every data-bearing resource that supports a per-resource customer-managed key is CMK-encrypted: DynamoDB, Lambda env vars, all log groups, the frontend S3 bucket, AppConfig hosted configuration content, SQS DLQs, and CloudTrail trail log files (per-object SSE-KMS into an SSE-S3 bucket). Keys are scoped per stack rather than shared — the WAF, backend, and frontend stacks each own one CMK, and the **DynamoDB table is encrypted by its own dedicated CMK in `HelloWorldDataStack`** (keeping the key with the stateful data it protects is what makes the `retain_data` switch meaningful — a retained table whose key lived in a destroyable compute stack would be unreadable after teardown; see that module's docstring). Keys are deliberately *not* shared across the stack boundary, so each carries a tighter, least-privilege key policy. Account/region-wide encryption settings (X-Ray, Glue Data Catalog) are deliberately out of scope — they'd mutate state shared with other apps in the account.

Service-principal grants on CMKs must be confused-deputy-guarded with `aws:SourceAccount` + `aws:SourceArn`. See `grant_logs_service_to_key` / `grant_guardduty_service_to_key` in `hello_world/nag_utils.py` for the canonical pattern. One documented exception: `grant_cloudwatch_alarms_to_key` (alarm→SNS publish path) uses `aws:SourceAccount` + `kms:ViaService` and deliberately omits `aws:SourceArn` — CloudWatch is not documented to set it on via-SNS KMS calls, and an unmatched required condition would silently drop alarm notifications. Verify alarm delivery on a live deploy when touching that statement.

## Stateful resources live in their own stack (`retain_data`)

The stateful data layer (the DynamoDB idempotency table + its dedicated CMK) lives in `hello_world/hello_world_data_stack.py`, separate from the stateless compute/backend stack — the CDK best practice "keep stateful resources in their own stack." This is baked in deliberately as production-template preparation: **stack topology is the expensive-to-retrofit decision; `RemovalPolicy.RETAIN` is a one-line flag.** So the *structure* ships now, and a production fork flips exactly one switch — `retain_data` (CDK context `-c retain_data=true`, plumbed `app.py` → `HelloWorldStage` → `HelloWorldDataStack`). `retain_data=True` flips the table and its CMK to `RETAIN`, turns on DynamoDB deletion protection, and enables stack termination protection. The default is `False` so the template and ephemeral environments tear down cleanly. The table is handed to the backend cross-stack (`idempotency_table=`), where the Lambda gets its `IDEMPOTENCY_TABLE_NAME` env var, a scoped read/write grant, and monitoring — the single cross-stack relationship. The `DynamoDBInBackupPlan` nag suppressions live on the data stack (it owns the table); a `retain_data=True` fork should add an AWS Backup plan (see `TODO.md`). The `IdempotencyTableName` CfnOutput moved to the data stack too.

## Dangling-resource cleanup pattern

Services that create supporting resources outside CloudFormation (CloudWatch log groups, dashboards) don't get cleaned up by `cdk destroy`. Two cleanup `AwsCustomResource` patterns ship in this repo:

- `AppInsightsDashboardCleanup` — deletes the auto-created Application Insights dashboard
- `RumLogGroupCleanup` — deletes the auto-created `/aws/vendedlogs/RUMService_*` log group

When adding services that create supporting AWS resources outside CFN, mirror the pattern: Lambda-backed `cr.AwsCustomResource` with an `on_delete` SDK call, IAM scoped to the specific resource ARN, `ignore_error_codes_matching="ResourceNotFoundException"` for the case where the resource never materialized.

## Conventional Commits + git-cliff drive `CHANGELOG.md`

Commit prefix grammar (see README "Commit message convention"):

`feat:` / `fix:` / `docs:` / `chore:` / `ci:` / `test:` / `refactor:` / `build:`

`cliff.toml` maps these to Keep-a-Changelog groups. Regenerate with `git cliff -o CHANGELOG.md`. Dependabot bumps and `Merge pull request` commits are filtered out by design. The prefix grammar is enforced on PR titles by `.github/workflows/pr-title.yml` (squash-merge subjects feed git-cliff). Release recipe in README "Cutting a release" — driven by `git cliff --bumped-version`, `make lock`, annotated tag; pushing the `vX.Y.Z` tag triggers `.github/workflows/release.yml`, which publishes the GitHub Release from the annotated tag automatically.

## OpenAPI spec is committed and gated

`docs/openapi.json` is generated from `lambda/app.py` (`make openapi`) and **committed**. CI fails on drift (regenerate-and-compare in the `test` job; `make compare-openapi` locally) and on breaking API changes (oasdiff against the base branch's spec, PRs only). After touching routes, Pydantic models, or `responses=` metadata, run `make openapi` and commit the result — otherwise CI rejects the push.

## Deployment environments

Stack names carry an environment dimension: the default `prod` keeps the legacy names (`HelloWorld-us-east-1` etc. — never rename these; CloudFormation matches by name), while `make deploy ENV=<name>` / `-c env=<name>` deploys a fully namespaced, collision-free copy (ephemeral per-developer/per-branch stacks). Non-prod skips the SNS alarm topic (alarms exist but page nobody) with scoped nag suppressions for the alarm-action rules. Env names are validated at synth (`validate_env_name` in `hello_world/hello_world_stage.py`). `make destroy-clean ENV=<name>` scopes the bucket-emptying and log-group sweeps to that environment's stack names — the sweeps are deliberately env-prefixed so tearing down one environment can't delete another's log groups. Prefix sweeps alone are insufficient: CloudFormation truncates the *stack-name portion* of Lambda physical names at the 64-char limit (a live teardown left `/aws/lambda/HelloWorldFrontend-us-eas-…` behind), so `destroy-clean` also snapshots every CFN-owned log group's exact name pre-destroy and deletes re-appearances post-destroy (`_snapshot-log-groups` / `_delete-snapshotted-log-groups`).

## Behaviors to avoid

- **No `Co-Authored-By:` trailer on commits.** Personal preference.
- **Don't introduce account/region-wide CDK constructs** without flagging them explicitly. `glue.CfnDataCatalogEncryptionSettings`, `xray.UpdateEncryptionConfig`, and similar mutate state shared with other apps in the deploying account. Forks dropping this stack into an existing AWS account would silently override neighbor teams' settings.
- **Don't commit `cdk.out/`, `report.html`, `htmlcov/`, `.coverage`, or `site/`.** Reproducible from source; gitignored already.

## Forking this template

When a fork is spawned from this template via GitHub's "Use this template":

1. **Edit this CLAUDE.md's "Project" section** to describe the fork's workload, and add a workload-specific guidance section near the bottom (see [nba-data-api/CLAUDE.md](https://github.com/timpugh/nba-data-api/blob/main/CLAUDE.md) for an example).
2. **Run the post-template setup steps**: enable GitHub Pages (`gh api repos/<owner>/<repo>/pages -X POST -f build_type=workflow` — requires the repo to be public on the free plan), bootstrap CDK in the target account+region (`cdk bootstrap aws://<account>/us-east-1`), and walk through the Production readiness checklist in `TODO.md` before customer traffic.
3. **Don't drift from this template silently.** If you fix something here that other forks would benefit from, push it back upstream. If you change something that diverges intentionally (different encryption posture, different observability stack), document the *why* in your fork's CLAUDE.md so future contributors don't try to reconcile.
