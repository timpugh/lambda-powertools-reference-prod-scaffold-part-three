# Review Notes

Consistency and completeness review of this documentation set, performed at refresh time (2026-10-16) against `HEAD` of `lambda-powertools-reference-prod-scaffold-part-three`. The previous review (2026-07-06) covered the first generation, made against the ancestor repo; this one reviews the refreshed set as a whole.

## Refresh scope

- **Delta folded in**: `7b6cb70..HEAD` (91 commits). Structural additions: pipeline mode (`PipelineStack`), the mandatory `cdk-scaffold-boundary`, the same-origin API with origin lockdown (CORS removed), `AppConfigExtensionStore` (the fourth Lambda module), the production-readiness batch (new alarms, spend budget, Backup plan, Object Lock tiering, WAF log redaction, request validation), and the account-operations scripts (`audit_account.sh`, `dns_lifecycle.sh`) with six new Make targets. Nothing was deleted or renamed; the five-stack `AppStage` claim still holds.
- **Stale claims corrected**: the `api_url` cross-stack edge (now `api_id` + `origin_verify_secret` + `alarm_topic`); every CORS passage; the execute-api-pinned CSP (now `connect-src 'self'`); `AppConfigStore` (now `AppConfigExtensionStore`); the retry budget (`total_max_attempts` is 2, not 3, and covers SSM + DynamoDB only); `destroy-clean`'s single retry (now up to three, with a merge-based snapshot); "no hand-authored CloudFormation" (the boundary template is one); "AppStage instantiated once per region + env" (true in direct mode only); the implicit multi-region support (originally only `us-east-1` synthesized; since resolved — see finding 6).

## Consistency check

Cross-document facts verified consistent after the refresh:

- **Stack inventory and names** — five Stage stacks (`ServerlessApp{Waf,Data,Backend,Frontend,Audit}-{region}`, env segment for non-prod) plus the top-level `ServerlessAppPipeline` in pipeline mode — consistent across codebase_info.md, architecture.md, components.md, and interfaces.md; verified against `app.py`, `app_stage.py`, and `pipeline_stack.py`.
- **Cross-stack edges** — the architecture.md diagram, the interfaces.md table, components.md (AppStage), and the codebase_info.md patterns describe the same edges; verified against `app_stage.py`.
- **Same-origin API** — no CORS claim survives anywhere in the set; the origin lockdown is described identically in architecture.md, interfaces.md, data_models.md, and components.md; verified against `frontend_stack.py`, `backend_app.py`, and `lambda/app.py`.
- **cdk-nag gate** — the report check is the gate (CLI, CI, and the pipeline's Synth step; in-process tests for the Stage and pipeline shapes); consistent in architecture.md, interfaces.md, workflows.md, and AGENTS.md.
- **Two-venv model** — consistent in dependencies.md, workflows.md, and components.md; matches `[tool.uv.conflicts]`, the Makefile, and the pipeline's two CodeBuild steps.
- **Deployment traps** — `appconfig_monitor` cold-deploy abort (twice in pipeline mode), the `'**'` glob, boundary-before-bootstrap, the pipeline-reserved `dev`, and push-before-`deploy-pipeline` are stated identically wherever they appear.
- **Custom domain** — described everywhere as a design contract with no code behind it; verified that no domain context key exists in `app.py` or `infrastructure/`.
- **Make targets** — `bootstrap-boundary`, `deploy-pipeline`, `destroy-pipeline`, `audit-account`, `hibernate-dns`, and `wake-dns` carry identical semantics in interfaces.md, workflows.md, components.md, and AGENTS.md; verified against the Makefile and both scripts.

### Inconsistencies found in the repository itself (all resolved)

These were flagged during the refresh and have since been fixed in their owning files (working tree, 2026-10-16, not yet committed). Each entry keeps the original finding and adds how it was resolved.

Requested findings:

1. **`zensical.toml` site identity** — `site_name` / `site_url` pointed at the ancestor. **Resolved**: `site_name`, `site_url`, `repo_url`, and `repo_name` now name the part-three repo and Pages site.
2. **`SECURITY.md:9-10`** — vulnerability-report links targeted the ancestor repo. **Resolved**: both links repointed at `lambda-powertools-reference-prod-scaffold-part-three/security`.
3. **README table of contents** — missing several sections. **Resolved**: the TOC now lists "Fresh AWS account setup", "The CodeConnections handshake", "Where the connection ARN lives", "The PromoteToProd gate", the top-level "Custom domain (part-three arc)", and "Code scoring and attestation".
4. **`TODO.md:120`** — called the custom domain out of scope. **Resolved**: L120 (plus L21 and L101) now treat it as the planned part-three arc, linking README "Custom domain"; Mutual TLS stays out of scope.
5. **No git tags** despite `CHANGELOG.md` recording `3.0.1` / `3.0.2`. **Resolved locally**: annotated tags `v3.0.1` (`b8bb5b2`) and `v3.0.2` (`8dd38d8`) exist in the local clone, giving `git cliff --bumped-version` a baseline. They are **not pushed** — `origin` still has no tags and `release.yml` has not run; pushing them is a user decision, because it publishes the two GitHub Releases.

Additional findings verified during this refresh:

6. **Multi-region documented but unsynthesizable.** **Resolved (code + tests)**: `APPCONFIG_EXTENSION_ARM64_LAYER_ARNS` now pins the arm64 extension (2.0.22365; `me-*` at 2.0.18836) for every region in AWS's table, keyed by deploy region, with a `-c appconfig_extension_layer_arn` / `APPCONFIG_EXTENSION_LAYER_ARN` override for anything else. `tests/cdk/test_stage.py::TestAppConfigExtensionLayerRegion` pins a mapped non-us-east-1 synth, the unmapped-region `ValueError`, and override precedence/region matching.
7. **ship-a-change bootstrap step** omitted the boundary. **Resolved**: the step now runs `make bootstrap-boundary` first and bootstraps with `--custom-permissions-boundary cdk-scaffold-boundary`; the skill also covers pipeline mode, the pipeline-reserved `dev`, and the region/override precondition.
8. **`llms.txt` stale.** **Resolved**: refreshed to cover pipeline mode, the boundary, the same-origin API, `data_stack.py` as the table's owner, cdk-nag v3 validation plugins, and the newer modules and scripts.
9. **README L13 lineage note** described the part-one spawn. **Resolved**: it now describes the part-three spawn from part-two (`main`, 2026-07-14) and the full lineage chain.
10. **Stale in-code prose.** **Resolved**: the `build_managed_threat_rules` docstring now describes `RejectNonCloudFront`; the `backend_stack.py` `api_id` comment now says it builds the `/api/*` origin domain; the `backend_app.py` module docstring says the table lives in `DataStack`; the pytest-env comment no longer claims an AppConfig boto3 client. The same table-ownership fix was applied to `docs/backend_app.md` and `docs/backend_stack.md`.
11. **`scripts/audit_account.sh`** missed Cognito *identity* pools. **Resolved**: it now also scans identity pools across enabled regions and counts app-named ones as dirty. The user-pool scan stays for forks that add authentication.
12. **`docs/index.md` / nav coverage** — six modules had no autodoc page. **Resolved**: `app_stage`, `data_stack`, `audit_stack`, `pipeline_stack`, `validation_aspects`, and `extension_store` pages were added to the `zensical.toml` nav and `docs/index.md`. Every `infrastructure/` module now has a page. `models` and `service` pages were then added too, so every Python module under `infrastructure/` and `lambda/` has a page and the "every Python module" claim in `docs/index.md` holds. No gap remains.
13. **uv was unpinned** (originally described as "the one unpinned toolchain input", which contradicted dependencies.md's list of three residual unpinned inputs; uv was one of those three). **Resolved**: pinned (originally `0.12.23`, now `0.12.24`) via `version:` on every `setup-uv` step (`ci.yml` ×4, `dependency-audit.yml`, `docs.yml`) and `UV_VERSION` in `pipeline_stack.py` (both CodeBuild `pip install uv==…` steps; snapshot regenerated), with `required-version = ">=0.12.24"` as a floor in `[tool.uv]`. These pins have to move together by hand: `.github/dependabot.yml` bumps the setup-uv action SHA and the uv-managed packages, but not the action's `version:` input, `UV_VERSION`, or `required-version`. The other two residuals (AWS-published layers, the CodeBuild managed image) remain.

Related: `make lint-docs` and the CI `quality` job now run markdownlint over `.agents/**/*.md`, so this knowledge base is lint-gated like the rest of the Markdown.

**Gate status at this update** (recorded, not fixed here): `make lint` fails only on pip-audit, which reports 17 newly published advisories (pip, pymdown-extensions, urllib3, virtualenv) against the *unchanged* `uv.lock`. That failure predates these fixes and needs a dependency bump. `make cdk-synth` could not run on the x86 host used for this update, because there is no arm64 emulation for the bundling image.

## Completeness check

Covered in depth for this refresh (read): `app.py`, `app_stage.py`, `pipeline_stack.py`, `lambda/app.py`, `lambda/models.py`, `lambda/extension_store.py`, the boundary template, `scripts/audit_account.sh`, `scripts/dns_lifecycle.sh`, `cdk.json`, the Makefile's new and changed targets, the `pyproject.toml` dependency groups and pytest config, the scorecard and pre-commit deltas, and README "CI/CD pipeline" and "Custom domain".

Read via targeted sections and greps: `backend_app.py` (every method the delta introduced read; the rest via docstrings), `frontend_stack.py` (`__init__` head, the response-headers policy, `_build_api_origin_behavior`, `_attach_analytics_alarms`, the `BucketDeployment`), `data_stack.py` and `audit_stack.py` (the `retain_data` paths), `nag_utils.py` (the new helpers), `waf_stack.py` (rate rule and logging), and the new test files (structure and test names).

Documented at lower resolution (acceptable gaps, listed for transparency):

1. **`backend_app.py` / `frontend_stack.py` interiors** — individual suppression rationales, RUM extended-metrics wiring, and Athena named queries are not enumerated; read the source when they matter.
2. **`docs/superpowers/` specs and plans** — listed, not summarized. They are dated pre-implementation design records; the code and README supersede them wherever they differ.
3. **`tests/cdk/test_stacks.py` / `test_snapshots.py` assertions** — suite purposes documented; individual assertions not enumerated.
4. **`frontend/index.html`** — the RUM bootstrap and the `/api/greeting` call were verified; the inline JavaScript is not exhaustively documented.
5. **README beyond the delta's sections** — treated as authoritative and pointed to, not re-summarized.
6. **Live-verification history** — the "verified live" claims (pipeline runs, canary rollback, alarm paging, teardown passes) are carried from README and CLAUDE.md, not re-verified; nothing is deployed from this repo.

Language-support limitations: none material. The repository is Python-first; the Bash scripts were read, and the only hand-authored CloudFormation is the boundary JSON template.

## Overlapping agent-facing documentation in the repo

This knowledge base coexists with several hand-maintained agent and contributor surfaces, treated as authoritative inputs and pointed to rather than duplicated:

- `CLAUDE.md` — project instructions (deepest rationale; imports index.md by path, so the index's file name and location are load-bearing).
- `AGENTS.md` — refreshed alongside this set; its Custom Instructions section was preserved byte-for-byte (currently empty).
- `llms.txt` — compact agent pointer file; refreshed (finding 8 resolved).
- `.claude/skills/ship-a-change/SKILL.md` — the process SOP for shipping a change; bootstrap step now boundary-first and pipeline-aware (finding 7 resolved).
- `.claude/skills/wa-review/SKILL.md` — Well-Architected review skill; generic enough to remain accurate.
- `.claude/skills/{agent-sop-author,code-assist,code-task-generator,codebase-summary,eval,pdd}` — generic SOP skills, not repo-specific.

**Risk to manage**: these surfaces drift apart (findings 7 and 8 were live examples, now fixed). When behavior changes, update `CLAUDE.md` / `README.md` first, then this summary, `AGENTS.md`, `llms.txt`, and the ship-a-change skill.

## Recommendations

1. **Refresh cadence**: regenerate after structural changes (new stacks or modes, moved modules, changed gates), not routine edits. The custom-domain arc is the next trigger — when it lands, move it from "planned" to shipped in architecture.md and add the domain context keys and `./.env` consumers to interfaces.md, data_models.md, and workflows.md.
2. **Commit the repository fixes, then decide on the tags**: findings 1-13 are fixed in the working tree. Pushing `v3.0.1` / `v3.0.2` to `origin` is the remaining step, and it is deliberate because it triggers `release.yml` publishing. Separately, bump the dependencies flagged by pip-audit so `make lint` passes again.
3. **When AWS ships a new AppConfig extension version** (or a new region), refresh `APPCONFIG_EXTENSION_ARM64_LAYER_ARNS` verbatim from the docs and regenerate snapshots; if AWS ever publishes `us-iso-east-1`, re-point `TestAppConfigExtensionLayerRegion`'s unmapped example.
4. When deep detail on `backend_app.py` / `frontend_stack.py` / `pipeline_stack.py` matters (IAM acknowledgments, RUM wiring, pipeline internals), read the module docstrings first — they carry the rationale this summary compresses.
