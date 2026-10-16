# Knowledge Base Index — lambda-powertools-reference-prod-scaffold-part-three

**Audience: AI assistants.** This directory is a generated knowledge base describing the `lambda-powertools-reference-prod-scaffold-part-three` repository (lineage: part-two ← part-one ← `lambda-powertools-reference`, full git history carried) — a five-stack AWS CDK (Python) reference architecture for Lambda + Powertools serverless applications, hardened for production and deployable either directly (`make deploy`) or through an opt-in self-mutating CDK Pipeline (`-c pipeline=true`). Load this index into context first; it carries enough metadata to decide which detail file (if any) you need to read next.

## How to use this knowledge base

1. **Start here.** This index summarizes every document and maps question types to files.
2. **Read detail files lazily.** Each file below is self-contained; read only the one(s) matching the question.
3. **Precedence.** The repository's own `CLAUDE.md` (project instructions) and `README.md` are authoritative over this summary. `AGENTS.md` (repo root) is the condensed agent-facing companion to this knowledge base. If this summary disagrees with the code, the code wins — these files describe the repo as analyzed on 2026-10-16 (a refresh of the 2026-07-06 generation, which was made against the ancestor repo; delta `7b6cb70..HEAD`).
4. **Verify before acting.** File paths and behaviors cited here were verified at generation time; re-verify anything load-bearing (especially version-sensitive facts in `dependencies.md`) before making changes.

## Documents

| File | Read it when you need… |
|---|---|
| [codebase_info.md](codebase_info.md) | The basics: identity and lineage, tech stack table, directory layout diagram, entry points (incl. `app.py`'s two modes), languages, observed patterns. Best first read for orientation. |
| [architecture.md](architecture.md) | The five-stack topology and its rules (naming, one-way dependencies, cross-region WAF, mandatory permissions boundary), the pipeline-mode wrapper (`PipelineStack`), runtime request flow through the same-origin `/api/*` path, the cdk-nag v3 gating architecture, encryption posture, log routing, deployment-safety machinery, and design principles. |
| [components.md](components.md) | Per-module responsibilities: every `infrastructure/` module (including `BackendApp`'s private methods and `PipelineStack`), the boundary template, the four Lambda modules, test suites, and automation (Makefile, scripts, workflows, pre-commit). |
| [interfaces.md](interfaces.md) | Boundaries: the `GET /greeting` HTTP contract (+ sequence diagram) as reached through CloudFront `/api/*`, cross-stack CDK interfaces, CDK context keys and the `./.env` resolution chain, Lambda env vars, Make targets, CI/CD gates, and AWS service integration gotchas. |
| [data_models.md](data_models.md) | Contracts: Pydantic models, the DynamoDB idempotency table schema, feature-flag JSON (freeform profile, extension-served), telemetry contracts (EMF dimension set, WAF alarm dimensions), the origin-verify/`config.json` contract, OpenAPI artifact, nag suppression shape, validation-report format, boundary policy shape, `./.env` keys, WAF Glue schema, drift-gated committed artifacts. |
| [workflows.md](workflows.md) | Processes: local dev loop, CI, the CD pipeline (one-time setup, push-to-deploy, promotion), cold-deploy → monitor-enable sequence, ephemeral envs, teardown + `audit-account`, DNS hibernate/wake, releases, dependency updates, and the add-a-resource / change-the-API checklists. |
| [dependencies.md](dependencies.md) | The two-venv `attrs` conflict model, dependency groups, npm-pinned CDK CLI, AWS-published Lambda layers (AppConfig extension, Lambda Insights), pre-commit strategy, supply-chain hygiene (pins, cooldowns, audit gates), version-sensitive facts. |
| [review_notes.md](review_notes.md) | Known gaps and caveats of this documentation set itself (consistency/completeness review). |

## Question → file routing

| Question type | Consult |
|---|---|
| "Where is X defined / which module owns Y?" | components.md, then codebase_info.md's layout diagram |
| "Why is synth/deploy failing on a nag finding?" | architecture.md (gating), data_models.md (suppression shape), workflows.md (add-a-resource) |
| "How do I run tests / why is coverage failing?" | workflows.md (dev loop), components.md (test suites), dependencies.md (which venv) |
| "How do stacks reference each other / can I move resource X between stacks?" | architecture.md (topology rules — several moves are known to create cycles) |
| "What's the API contract / how do I add a route?" | interfaces.md, data_models.md, workflows.md (API change) |
| "How do I deploy / tear down / enable the AppConfig monitor?" | workflows.md (deployment), interfaces.md (context keys) |
| "How does the CD pipeline work / why didn't my push start a run?" | architecture.md (pipeline mode), workflows.md (CD pipeline), then README "CI/CD pipeline" (authoritative runbook) |
| "Is the account clean after teardown / what still bills?" | workflows.md (teardown + account audit), interfaces.md (Make targets) |
| "Is the custom domain implemented?" | No — architecture.md (design principles) and README "Custom domain" (a locked design contract; no domain context key exists in code yet) |
| "Why two venvs / can I install package X?" | dependencies.md |
| "What's pinned where / how do updates land?" | dependencies.md, workflows.md (dependency updates) |
| "What's deliberately out of scope / not production-ready?" | architecture.md (principles), repo `TODO.md` (production readiness checklist — authoritative) |

## Relationships between documents

```mermaid
graph LR
    INDEX["index.md (you are here)"] --> INFO["codebase_info.md<br/>orientation"]
    INFO --> ARCH["architecture.md<br/>system design"]
    ARCH --> COMP["components.md<br/>module detail"]
    ARCH --> IFACE["interfaces.md<br/>boundaries"]
    IFACE --> DM["data_models.md<br/>contracts"]
    COMP --> WF["workflows.md<br/>processes"]
    WF --> DEP["dependencies.md<br/>toolchain"]
    INDEX --> REV["review_notes.md<br/>known gaps"]
```

## Example queries this knowledge base can answer

- *"Why does `cdk synth` exit 0 even though there are cdk-nag findings?"* → architecture.md, Compliance gating (the exit code lands in jsii's throwaway Node kernel; the report check is the gate).
- *"I added a log group and `make test-cdk` now fails with an annotation error."* → components.md, `validation_aspects.py` (explicit retention is enforced).
- *"Can I set `appconfig_monitor=true` in cdk.json before the first deploy?"* → workflows.md, First (cold) deploy (no — it bricks stack creation).
- *"Why is `tenant_id` metrics metadata instead of a dimension?"* → data_models.md, Telemetry contracts.
- *"Which venv runs `scripts/generate_openapi.py`?"* → dependencies.md (`.venv-lambda` — it imports Powertools).
- *"A fresh-account `make deploy` fails with IAM errors on every role — why?"* → workflows.md, First (cold) deploy (`make bootstrap-boundary` must run before bootstrap and before any deploy).
- *"Pushes to `main` never start the pipeline, but manual runs work — why?"* → workflows.md, CD pipeline (explicit V2 trigger + `DetectChanges: false` + the GitHub app *installed*, not merely authorized).
- *"Can I deploy to `ap-southeast-1`?"* → interfaces.md, CDK context keys (yes — it is in the pinned AppConfig extension layer map; an unmapped region needs `-c appconfig_extension_layer_arn=<arn>`, else synth fails loudly).
