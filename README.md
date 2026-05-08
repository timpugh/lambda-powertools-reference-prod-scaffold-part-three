# Lambda Powertools Reference

[![CI](https://github.com/timpugh/lambda-powertools-reference/actions/workflows/ci.yml/badge.svg)](https://github.com/timpugh/lambda-powertools-reference/actions/workflows/ci.yml)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/downloads/)
[![Docs](https://img.shields.io/badge/docs-GitHub%20Pages-blue)](https://timpugh.github.io/lambda-powertools-reference/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

**Docs:** https://timpugh.github.io/lambda-powertools-reference/

This project contains source code and supporting files for a serverless application that you can deploy with the AWS CDK. It includes the following files and folders.

- `app.py` - CDK entry point; instantiates the WAF, backend, and frontend stacks and calls `app.synth()`
- `lambda/` - Code for the application's Lambda function
- `hello_world/hello_world_stack.py` - The backend CDK stack — a thin wrapper that composes `HelloWorldApp`, applies cdk-nag Aspects, and wires CfnOutputs
- `hello_world/hello_world_app.py` - `HelloWorldApp` construct — owns the domain resources (Lambda, API Gateway, DynamoDB, SSM, AppConfig, monitoring)
- `hello_world/hello_world_waf_stack.py` - The WAF stack (CloudFront-scoped WebACL, always in `us-east-1`)
- `hello_world/hello_world_frontend_stack.py` - The frontend stack (S3 + CloudFront)
- `hello_world/nag_utils.py` - Shared cdk-nag rule-pack helper (`apply_compliance_aspects`) and suppression list for CDK-managed singleton Lambdas
- `frontend/` - Static assets (`index.html`) deployed to the frontend S3 bucket
- `tests/` - Unit and integration tests
- `tests/conftest.py` - Shared test fixtures (API Gateway event, Lambda context, mocks)
- `docs/` - Zensical documentation source files
- `pyproject.toml` - Consolidated tool configuration (ruff, mypy, pylint, pytest, coverage)
- `.pre-commit-config.yaml` - Pre-commit hook definitions (runs on every `git commit`)
- `.bandit` - Bandit security scanner configuration (excluded directories)
- `.vscode/` - VS Code workspace settings and recommended extensions (ruff, mypy, pylint, pytest)
- `.github/workflows/` - GitHub Actions workflows (`ci.yml`, `docs.yml`, `dependency-audit.yml`, `dependabot-auto-merge.yml`)
- `.github/dependabot.yml` - Dependabot configuration (weekly checks for GitHub Actions and all three Python requirements tiers)
- `Makefile` - Common development commands (`make help` to list all targets)
- `LICENSE` - Apache 2.0 license
- `TODO.md` - Outstanding work and deferred items

The application uses several AWS resources, including Lambda functions, an API Gateway API, a DynamoDB table, SSM parameters, AppConfig, an S3-backed CloudFront distribution, and a WAF WebACL. These resources are split across three stack files in `hello_world/` (`hello_world_stack.py` for the backend, `hello_world_waf_stack.py` for WAF, and `hello_world_frontend_stack.py` for S3/CloudFront). The Lambda function uses [AWS Lambda Powertools](https://docs.powertools.aws.dev/lambda/python/latest/) extensively — see the [Lambda Powertools features](#lambda-powertools-features) section below for details. Note that Powertools Tracer currently depends on the `aws-xray-sdk`, which is approaching deprecation. There is an [open RFC](https://github.com/aws-powertools/powertools-lambda/discussions/90) to replace it with OpenTelemetry as the tracing provider. You can update the stack to add AWS resources through the same deployment process that updates your application code.

## Getting started

### Prerequisites

To use the CDK, you need the following tools.

* [Node.js](https://nodejs.org/) - Required to install the CDK CLI (`npm install -g aws-cdk`)
* AWS CDK CLI - [Install the CDK CLI](https://docs.aws.amazon.com/cdk/v2/guide/getting-started.html)
* AWS CLI v2 - [Install the AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) - Used by `aws logs tail` for streaming Lambda logs (see [Fetch, tail, and filter Lambda function logs](#fetch-tail-and-filter-lambda-function-logs)). The CDK CLI also relies on the same AWS credentials the AWS CLI configures.
* [Python 3 installed](https://www.python.org/downloads/)
* [uv](https://docs.astral.sh/uv/) — Python package and environment manager (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
* A container runtime for bundling Lambda dependencies — either of:
  * [Finch](https://runfinch.com/) — AWS-supported, open-source, license-friendly (recommended)
  * [Docker](https://www.docker.com/) — drop-in alternative; CDK uses Docker by default when `CDK_DOCKER` is unset

### Quick start

Just want to explore the code and run tests without deploying anything to AWS?

```bash
git clone https://github.com/timpugh/lambda-powertools-reference.git
cd lambda-powertools-reference
python3 -m venv .venv && source .venv/bin/activate
make install
make test
```

No AWS credentials or deployed stack required — unit tests mock all external dependencies.

If you open the project in VS Code, the `.vscode/` directory pre-configures ruff (format on save), mypy, pylint, and pytest against `pyproject.toml`. The first time you open it, VS Code will prompt you to install the recommended extensions listed in `.vscode/extensions.json`.

### Makefile

Common commands are available via `make`. Run `make help` to see all targets:

```bash
make install            # set up both venvs (.venv + .venv-lambda) and pre-commit hooks
make test               # run unit tests with coverage (in .venv-lambda)
make test-cdk           # run CDK stack assertion tests (in .venv)
make test-integration   # run integration tests (requires deployed stack)
make lint               # run all pre-commit hooks (ruff, mypy, pylint, bandit, xenon, pip-audit)
make format             # format code with ruff
make typecheck          # run mypy in both venvs (CDK side in .venv, Lambda runtime + scripts in .venv-lambda)
make security           # run bandit (direct) + pip-audit (via pre-commit so the CVE ignore list stays single-sourced)
make cdk-synth          # synthesize all stacks (with '**' glob so cdk-nag descends into Stage-nested stacks)
make cdk-notices        # show AWS-published CDK notices (CVEs, deprecated CDK versions, breaking changes)
make cdk-deprecations   # list deprecated CDK APIs in use (synth output filtered for "deprecated")
make deploy             # cdk deploy '**' --require-approval never (us-east-1; pass `-c region=X` for others)
make destroy            # cdk destroy '**' (interactive confirmation; pass --force to skip)
make docs               # build Zensical HTML docs
make docs-open          # build and open docs in browser
make docs-serve         # regenerate OpenAPI + start Zensical dev server with hot reload
make lock               # regenerate uv.lock and lambda/requirements.txt from pyproject.toml
make upgrade            # upgrade all dependencies (respects COOLDOWN_DAYS, default 7)
make deps-merge         # process every open Dependabot PR (rebase + lock + push + arm auto-merge); use PR=N for one
make clean              # remove build artifacts, caches, and coverage files
```

### Editor setup (VS Code)

The repo keeps two Python environments for the `attrs` conflict (`.venv` for CDK, `.venv-lambda` for Lambda runtime code — see "Project dependencies").

**Recommended: open the workspace file.** Use `File > Open Workspace from File…` and pick `practice.code-workspace` at the repo root. This is the intended entry point and the only mode where VS Code gets both sides right.

The workspace declares four folder roots — `.` (CDK + `.venv`), `lambda/` (`.venv-lambda`), `tests/unit/` (`.venv-lambda`), and `scripts/` (`.venv-lambda`). Per-folder `python.defaultInterpreterPath` overrides live in each folder's `.vscode/settings.json`. The effect:

- **Pylance** spins up a separate instance per root, so CDK code resolves `aws_cdk` against `.venv` and Lambda code resolves `aws_lambda_powertools` against `.venv-lambda` at the same time. No red squiggles on one side or the other.
- **Terminals** opened from each root (right-click the folder in the explorer → *Open in Integrated Terminal*, or `Ctrl+\`` while focused on a file under that root) auto-activate that root's venv.
- **Test Explorer** discovers and runs unit tests under `.venv-lambda` and CDK tests under `.venv` independently.

**Fallback: open the folder directly.** `File > Open Folder` on the repo root still works — `.vscode/settings.json` defaults the interpreter to `.venv`, so CDK work is fine, but Pylance will flag Powertools imports under `lambda/` as unresolved (single-interpreter-per-workspace limitation). Use this mode only if you specifically don't want the workspace file loaded.

Note: [VS Code's `python-envs.pythonProjects` feature](https://code.visualstudio.com/docs/python/environments#_python-projects) would cover terminal activation and the test runner per folder inside a single workspace, but it assumes each project's venv lives under its folder. This repo keeps both venvs at the repo root (matching uv's layout and the `UV_PROJECT_ENVIRONMENT` switch the Makefile uses), so per-folder `python.defaultInterpreterPath` inside a multi-root workspace is the right fit. Pylance is single-interpreter per workspace anyway, so the multi-root workspace is the only way to get correct type resolution for both sides.

**Environment discovery (`python-envs.workspaceSearchPaths`).** The new Python Environments extension (`ms-python.vscode-python-envs`, recommended) defaults its search to `./**/.venv`, which only matches the literal name `.venv` and would miss `.venv-lambda`. `.vscode/settings.json` pins the search to both paths explicitly so the *Python: Select Interpreter* picker lists both venvs out of the box. If you add a third venv at a non-default path, append it to the array.

**Optional user setting: `python-envs.alwaysUseUv`.** VS Code scopes this setting to *user* only, so it can't be committed to the repo. If you set it to `true` in your User Settings, the extension creates new envs with `uv venv` instead of `python -m venv`. Worth turning on for this project — the Makefile already uses `uv lock`/`uv export`/`uv sync`, and uv is listed as a prerequisite.

**Debug configurations (`.vscode/launch.json`).** Three F5 configs are pre-wired:

- **Python: Current File** — generic debugpy launch on whatever `.py` file is focused.
- **Pytest: Current File** — runs `pytest ${file} -v --override-ini=addopts=` under the debugger. The `--override-ini=addopts=` flag clears the global pytest options from `pyproject.toml` (`-n auto`, `--cov-fail-under=100`, the HTML reporter), which would otherwise either fight the debugger or fail single-test runs on the coverage threshold. The same flag is also tagged with `"purpose": ["debug-test"]`, so VS Code's Test Explorer uses this configuration when you click the debug-icon next to a test instead of running its own uncustomized debugpy invocation.
- **CDK: Synth (app.py)** — runs the root `app.py` (the CDK entry point) under debugpy with the same dummy-account env vars the `cdk-check` CI job uses. Useful when synth blows up and you need to step through stack construction in [hello_world/](hello_world/) — set a breakpoint in any `*_stack.py`, hit F5, and walk the stack assembly.

**Test Explorer.** Pylance fixture inlay hints are on (`python.analysis.inlayHints.pytestParameters: true`) — the inferred type for each fixture parameter renders inline at test-function definitions, which makes the indirection in [tests/conftest.py](tests/conftest.py) easier to follow without a hover. The Test Explorer's *Run Tests with Coverage* toolbar button works out of the box because `pytest-cov` is already a dev dependency: it produces per-line gutter decorations for the file under test, scoped to that single run (independent of `make test`'s 100% threshold gate). Note that VS Code's docs flag a known issue where `pytest-cov` suppresses breakpoints when debugging — the Pytest debug config above already dodges this via `--override-ini=addopts=`, which strips `--cov` along with `-n auto`.

**Pylance defaults.** A few Pylance settings are on by default in `.vscode/settings.json` to make the editor signal closer to what the linters enforce:

- `python.analysis.typeCheckingMode: "strict"` — full Pylance type analysis inline. This overlaps with the mypy linter that runs on save and in CI, but the two are *separate engines with separate rule sets* — they share the goal of type checking but disagree on the specifics. Pylance is stricter on `Any`, more aggressive on inferred narrowing, and will surface problems that mypy lets pass (and vice versa). Expect a wave of new entries in the *Problems* panel after enabling this for the first time; triage with `python.analysis.diagnosticSeverityOverrides` if specific rules get too loud. If you'd rather only see mypy's view of the world, lower this to `"basic"` or `"off"` in your User Settings — the workspace value still wins, so override at folder/user scope rather than editing the committed file.
- `python.analysis.autoImportCompletions: true` — completing an unknown symbol offers to add the matching `import` line.
- `python.analysis.autoFormatStrings: true` — typing `{` inside a regular string auto-prefixes the literal with `f` so it becomes an f-string.
- `python.analysis.inlayHints.{variableTypes,functionReturnTypes,callArgumentNames}` — inline annotations for inferred local-variable types, function return types, and named-argument hints at call sites. `callArgumentNames` is set to `"all"` (the most verbose option); switch to `"partial"` in User Settings if it's too dense at call sites with many args.

### Debugging the Lambda function

This project does not use AWS SAM (`sam local invoke`) for local Lambda debugging. The reason: SAM's debug flow injects an unpinned `debugpy` into the runtime requirements, which conflicts with the project's `--require-hashes` install mode (`lambda/requirements.txt` is pinned by hash via `uv export`). Dropping hash-mode would weaken supply-chain integrity for every CI install, so the project debugs through pytest instead.

**The pytest-as-debugger pattern.** Open a test file under [tests/unit/](tests/unit/) that exercises the code path you want to inspect, set a breakpoint anywhere in [lambda/app.py](lambda/app.py), and press F5 → *Pytest: Current File*. The handler executes in-process with all of Powertools' real machinery (resolver routing, middleware order, idempotency wrapping, route-handler logic) — breakpoints, step-through, watch expressions, and exception inspection all work normally. The pytest fixtures in [tests/conftest.py](tests/conftest.py) build realistic API Gateway events, so the resolver sees the same shape it does in production.

**What this covers.** Code-logic bugs in handlers, parsers, validators, idempotency keys, response shapes, exception paths, feature-flag evaluation — i.e. roughly 90% of what you actually need to debug.

**What pytest debug cannot reproduce.** You're running in your local Python process, not a Lambda execution environment. The following are *only* observable on the deployed function:

- Lambda runtime behavior — cold-start timing, init-phase code, the 250 MB unzipped package limit, the `/tmp` 512 MB cap, the configured memory ceiling.
- IAM — your local AWS credentials are used, not the Lambda execution role. Permission denials in production won't surface locally.
- Real AWS service calls — most unit tests mock boto3. Service-side behavior (DynamoDB throttling, SSM parameter resolution latency, AppConfig deployment cadence) is invisible.
- Network and runtime context — VPC routing, Lambda layers, container reuse, environment variables injected by the deployed stack.

**For the remaining 10%, debug on the deployed function.** The observability stack is built for this:

- **Powertools `Logger`** writes structured JSON to CloudWatch Logs with a `correlation_id` injected from the API Gateway request ID. Filter Logs Insights by that ID to follow a single request end-to-end through every log line the handler emitted.
- **X-Ray traces** (auto-enabled via Powertools `Tracer`) show the full call graph including SDK calls to DynamoDB, SSM, and AppConfig with per-segment timing. Use traces to spot cold-start init time, downstream latency, and IAM denials (which surface as red segments).
- **CloudWatch RUM** correlates frontend requests to backend X-Ray traces via the `X-Amzn-Trace-Id` CORS header, so you can pivot from a slow user session to the matching backend trace.
- **CloudWatch Metrics** (custom Powertools metrics + the `MonitoringFacade` dashboard) surface throttles, errors, and concurrency at the aggregate level.

This combination is the standard professional workflow for serverless Python: local debug for logic, deployed observability for runtime behavior. The toolchain overhead of `sam local` is generally not worth it once Powertools' structured-logging + X-Ray story is in place.

## Architecture

### Lambda Powertools features

The Lambda function in `lambda/app.py` uses the following Powertools utilities:

#### Logger
Structured JSON logging with `@logger.inject_lambda_context`. Automatically includes Lambda context fields (function name, request ID, cold start) in every log entry. Configured via `POWERTOOLS_SERVICE_NAME` and `POWERTOOLS_LOG_LEVEL` environment variables. The legacy `LOG_LEVEL` fallback is intentionally not set — running both side-by-side hides which knob actually wins.

#### Tracer
X-Ray tracing with `@tracer.capture_lambda_handler` on the entry point and `@tracer.capture_method` on route handlers. Creates subsegments for each traced method.

#### Metrics
CloudWatch Embedded Metric Format (EMF) via `@metrics.log_metrics(capture_cold_start_metric=True)`. The `/hello` route emits a `HelloRequests` count metric. Metrics are published under the `HelloWorld` namespace (set via `POWERTOOLS_METRICS_NAMESPACE`).

#### Event Handler
`APIGatewayRestResolver` provides Flask-like routing with `@app.get("/hello")`. It parses the API Gateway event and routes to the correct handler based on HTTP method and path.

The resolver is constructed with `enable_validation=True`, which turns on Pydantic-based request and response validation driven entirely by function type annotations. The return-type annotation on the `hello()` handler is a `HelloResponse(BaseModel)` — Powertools validates the returned object against that model and serializes it to JSON. Adding request bodies later is the same pattern: declare a Pydantic model as a parameter type and it gets validated and documented automatically.

#### Idempotency
The `@idempotent` decorator uses a DynamoDB table to prevent duplicate processing of the same request. It keys on `requestContext.requestId` and records expire after 1 hour. The CDK stack provisions the DynamoDB table with PAY_PER_REQUEST billing and a TTL attribute.

#### Parameters
`get_parameter()` fetches the greeting message from SSM Parameter Store. The parameter path is set via the `GREETING_PARAM_NAME` environment variable. Values are cached automatically by Powertools to reduce API calls.

#### Feature Flags
`FeatureFlags` reads from AWS AppConfig to toggle behavior at runtime. The `enhanced_greeting` flag controls whether the response includes extra text. The CDK stack provisions the AppConfig application, environment, configuration profile, and an initial hosted configuration version.

#### OpenAPI spec (build-time, not runtime)
The Pydantic models and route type hints that power `enable_validation=True` also drive an OpenAPI 3 spec. The spec is generated at **documentation-build time** by `scripts/generate_openapi.py`, which imports the Lambda resolver, calls `app.get_openapi_json_schema(...)`, and writes `docs/openapi.json`. It is then rendered in the browser by [Scalar](https://github.com/scalar/scalar)'s standalone bundle via `docs/api.html`, which Zensical copies into the built site verbatim alongside the other `docs/` assets.

After Powertools emits the vanilla OpenAPI document, the script runs a small post-processor that walks every operation and attaches a uniform `x-amazon-apigateway-integration` extension — the AWS_PROXY Lambda integration that the CDK stack actually provisions. The integration `uri` carries literal `{region}` and `{lambdaArn}` placeholders that a reader substitutes before `aws apigateway import-rest-api`; the deployed API is always built by CDK, not imported from this file, so the extensions are **documentation-only** and exist to show the AWS wiring in context. The processor is deliberately undiscriminating — every path + verb gets the same block — because per-route customisation would drift from CDK, which is the real source of truth.

The injection is fully automated. Any new route added to the Lambda resolver (`@app.post("/greet")`, `@app.delete("/hello/{id}")`, etc.) picks up the extension automatically on the next `make docs` run — there is no per-route registration to keep in sync. The only routes that would be skipped are ones using an HTTP verb outside the standard set (`get`, `put`, `post`, `delete`, `options`, `head`, `patch`, `trace`), which is not a realistic scenario for a REST API.

The spec is intentionally **not** exposed as a runtime endpoint. Publishing the full API surface via a public `/openapi.json` would hand unauthenticated callers a map of every path and field name — a recon gift for attackers. Keeping it a build artifact gives us callable-facing docs for internal consumers without leaking the schema to the open internet. The `make docs` target regenerates the spec and rebuilds Zensical in one step, so the rendered API reference is always current.

#### Event Source Data Classes
`APIGatewayProxyEvent` provides typed access to the incoming API Gateway event. Instead of raw dict access like `event["requestContext"]["identity"]["sourceIp"]`, you get `event.request_context.identity.source_ip` with IDE autocomplete and type safety. Powertools includes data classes for many event sources:

- `APIGatewayProxyEvent` / `APIGatewayProxyEventV2` — REST and HTTP API events
- `S3Event` — S3 bucket notifications
- `SQSEvent` — SQS messages
- `DynamoDBStreamEvent` — DynamoDB stream records
- `EventBridgeEvent` — EventBridge events
- `SNSEvent`, `KinesisStreamEvent`, `CloudWatchLogsEvent`, and more

These are available from `aws_lambda_powertools.utilities.data_classes` and require no extra dependencies.

### AWS resources provisioned

Resources are split across three stacks. All resources in all stacks have `RemovalPolicy.DESTROY` so `cdk destroy` leaves nothing behind.

**`HelloWorldWaf-{region}`** (always in `us-east-1`):

| Resource | Purpose |
|---|---|
| WAF WebACL | CloudFront-scoped WebACL with 4 managed rule groups (IP reputation, common rule set, known bad inputs, anonymous IPs) + forwarded-IP rate limit |
| KMS Key | Encrypts the WAF log group |
| CloudWatch Log Group (`aws-waf-logs-*`) | Receives WAF access logs |

**`HelloWorld-{region}`** (backend, target region):

| Resource | Purpose |
|---|---|
| KMS Key | Encrypts all log groups and DynamoDB |
| Lambda Function | Runs the hello-world handler (256 MB, arm64/Graviton, X-Ray tracing, JSON logging, env vars CMK-encrypted) |
| CloudWatch Log Group | Lambda log group with 1-week retention, KMS-encrypted |
| API Gateway REST API | Exposes `GET /hello` with X-Ray tracing |
| CloudWatch Log Group (access) | API Gateway access logs (16-field JSON), KMS-encrypted |
| CloudWatch Log Group (execution) | API Gateway execution logs, KMS-encrypted |
| DynamoDB Table | Idempotency records (TTL, PAY_PER_REQUEST, PITR, KMS-encrypted, Contributor Insights enabled) |
| SSM Parameter | Greeting message (CDK-generated name, read via the `GREETING_PARAM_NAME` env var) |
| AppConfig Application | Feature flag configuration |
| AppConfig Environment | `{stack}-env` environment for feature flags |
| AppConfig Configuration Profile | `{stack}-features` profile with `AWS.AppConfig.FeatureFlags` type |
| Resource Group + Application Insights | CloudWatch Application Insights monitoring |
| CloudWatch Dashboard | Lambda, API GW, DynamoDB metrics via cdk-monitoring-constructs |
| Custom Resource (`AppInsightsDashboardCleanup`) | Deletes the Application Insights auto-created dashboard on destroy |

**`HelloWorldFrontend-{region}`** (frontend, target region):

| Resource | Purpose |
|---|---|
| KMS Key | Encrypts the frontend S3 bucket and auto-delete Lambda log group |
| S3 Bucket (frontend) | Private static assets, KMS-encrypted, server access logging enabled |
| S3 Bucket (access logs) | Receives S3 server access logs (`s3-access-logs/`), CloudFront standard access logs (`cloudfront/`), and Athena query results (`athena-results/`). SSE-S3 — log delivery requires it. 7-day expiration lifecycle rule applied to all prefixes |
| S3 Bucket (CloudTrail logs) | Stores CloudTrail data-event records, separate from the audited buckets to avoid feedback loops |
| CloudTrail Trail | Records every Get/Put/Delete object-level call against the frontend and access-log buckets, KMS-encrypted, file-validation enabled, with CloudWatch Logs integration |
| CloudFront Distribution | HTTPS-only, TLS 1.2+, WAF-protected, SECURITY_HEADERS policy, access logging to S3 |
| CloudWatch Log Group (auto-delete) | Auto-delete Lambda log group, KMS-encrypted |
| Glue Database | Catalog database for CloudFront and S3 access log analytics |
| Glue Table (`cloudfront_logs`) | 33-field tab-delimited schema for CloudFront standard access logs |
| Glue Table (`s3_access_logs`) | 26-field regex-parsed schema for S3 server access logs |
| Athena WorkGroup | Query execution config with SSE-KMS encrypted results (per-object override on the SSE-S3 bucket), CloudWatch metrics enabled |
| Athena Named Queries (5 CloudFront + 6 S3) | Pre-built SQL queries: top URIs, errors, top IPs, bandwidth by edge, cache hit ratio, top operations, error requests, top requesters, slow requests, access denied (403), object read audit |
| CloudWatch RUM AppMonitor | Real User Monitoring for the browser — page loads, JS errors, Core Web Vitals, fetch timings, user interactions, with X-Ray correlation |
| Cognito Identity Pool | Unauthenticated identity pool issuing guest credentials to the browser RUM client |
| IAM Role (RUM guest) | Assumed by the identity pool; scoped to `rum:PutRumEvents` on this app monitor only |

### Stack and construct composition

The project follows the CDK best practice ["model with constructs, deploy with stacks"](https://docs.aws.amazon.com/cdk/v2/guide/best-practices.html): domain resources live inside reusable `Construct` subclasses, and each `Stack` is a thin wrapper that only composes constructs and applies stack-wide concerns (Aspects, CfnOutputs, stack-level nag suppressions).

For the backend, that means:

- [`HelloWorldApp`](hello_world/hello_world_app.py) — a `Construct` that owns the KMS key, DynamoDB table, SSM parameter, AppConfig application, Lambda function, API Gateway, Application Insights monitoring, dashboard, and Logs Insights saved queries, along with per-resource cdk-nag suppressions.
- [`HelloWorldStack`](hello_world/hello_world_stack.py) — a `Stack` that instantiates `HelloWorldApp(self, "App")`, calls `apply_compliance_aspects(self)`, wires CfnOutputs, and attaches stack-level and singleton-scoped suppressions.

The WAF and frontend stacks are small enough (single logical unit each) that they keep their resources inline — the construct-extraction pattern is demonstrated on the backend as the reference example.

The three stacks are then composed into a [`cdk.Stage`](hello_world/hello_world_stage.py) — `HelloWorldStage`. A Stage is the CDK best-practice deployment unit ([AWS guide](https://docs.aws.amazon.com/cdk/v2/guide/best-practices.html), [AWS Heroes article](https://dev.to/aws-heroes/aws-cdk-deployment-best-practices-3doo)): it groups stacks that are always deployed together, scopes synthesis under its own subdirectory (`cdk.out/assembly-{stage}/`), and is the natural boundary for CDK Pipelines. Stack names are set explicitly via `stack_name=` inside the Stage so the CloudFormation names stay as `HelloWorld-{region}` etc. — without the override, the Stage ID would be prepended.

**Generated vs. physical resource names.** Following the ["use generated resource names"](https://docs.aws.amazon.com/cdk/v2/guide/best-practices.html) best practice, the backend does not set `table_name`, `parameter_name`, or `log_group_name` on the DynamoDB table, SSM parameter, Lambda log group, or API Gateway access log group — CDK auto-generates unique names derived from the construct path. This avoids two hazards: (1) replacement-style schema changes can't fail because the physical name is pinned, and (2) two regional deployments can't collide on the same physical name. Explicit names are retained only where AWS itself requires them: the API Gateway execution log group (`API-Gateway-Execution-Logs_{api-id}/{stage}` is a service-fixed format), the WAF log group (`aws-waf-logs-*` prefix is enforced), and the AppConfig L1 constructs (no auto-generation option via CDK).

### Frontend stack

The frontend is split across two CDK stacks — `HelloWorldWafStack` and `HelloWorldFrontendStack` — intentionally decoupled from the backend. This allows the frontend to be deployed and destroyed independently of the API, and demonstrates the standard CDK multi-stack and cross-region reference pattern.

#### Architecture

```
Browser → CloudFront → S3 (private bucket)
               ↓
        WAF WebACL (us-east-1, always)
```

The browser calls `GET /hello` directly from JavaScript against the API Gateway URL — CloudFront only serves static assets, it does not proxy API requests.

#### Three-stack design and cross-region support

This project uses three stacks, not two. WAF lives in its own stack because CloudFront-scoped WAF WebACLs are an AWS hard requirement to exist in `us-east-1` — even if every other resource is in a different region. By isolating WAF into `HelloWorldWafStack`, the backend and frontend can be deployed to any region without duplicating the WAF or violating the constraint.

Each regional deployment gets its own set of three independently named stacks:

| Stack | Region | Contents |
|-------|--------|----------|
| `HelloWorldWaf-{region}` | Always `us-east-1` | WAF WebACL with all rules |
| `HelloWorld-{region}` | Configurable | Lambda, API Gateway, DynamoDB, SSM, AppConfig |
| `HelloWorldFrontend-{region}` | Configurable | S3, CloudFront (references WAF ARN) |

**Deploying to us-east-1 (default):**

```bash
cdk deploy --all
```

**Deploying to a different region:**

```bash
cdk deploy --all -c region=ap-southeast-1
```

WAF stays in `us-east-1` (always). The backend and frontend deploy to the target region. CDK wires the WAF ARN across regions automatically — no manual steps.

**Destroying a specific regional deployment:**

```bash
cdk destroy --all -c region=ap-southeast-1
```

This tears down only the Singapore stack set (`HelloWorldWaf-ap-southeast-1`, `HelloWorld-ap-southeast-1`, `HelloWorldFrontend-ap-southeast-1`). Any other regional deployments are unaffected.

> **WAF cost note** — Each regional deployment provisions its own WAF WebACL at $5/month. This keeps deployments fully independent, which is the right default for a reference architecture. In a production setup with multiple long-lived environments, you could share a single `HelloWorldWaf` stack across all regions and pass its ARN to each frontend stack, eliminating the per-deployment cost. That optimization is intentionally deferred here in favour of deployment independence.

#### How cross-region references work

When the frontend stack is in a different region from the WAF stack, CDK cannot pass the WAF ARN as a direct CloudFormation output (outputs only work within a single region). Instead, CDK uses `cross_region_references=True` on the frontend stack to bridge the value automatically:

1. During `cdk deploy`, CDK writes the WAF ARN into an SSM Parameter in `us-east-1`
2. A CDK-managed custom resource in the frontend stack's region reads that SSM parameter at deploy time
3. The WAF ARN is resolved and attached to the CloudFront distribution

This is entirely transparent — you pass `waf.web_acl_arn` in `app.py` just like any other stack property. The SSM parameters are CloudFormation-managed and are cleaned up on `cdk destroy`.

The backend exposes `api_url` as a stack property. The frontend stack injects it into `config.json` at deploy time via `BucketDeployment`. The browser fetches `/config.json` at runtime so the API URL is never hardcoded in source.

The static assets themselves live in the `frontend/` directory at the project root. Currently this is just a single `index.html` that fetches `config.json` and calls the API — replace it with a built SPA bundle (e.g. the `dist/` output from a Vite or Next.js export build) and the existing `BucketDeployment` will pick it up automatically.

#### S3 bucket

The bucket is fully private — no public access of any kind. CloudFront reaches it exclusively via [Origin Access Control (OAC)](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-s3.html), the current AWS-recommended successor to OAI. The bucket is encrypted with SSE-KMS (customer-managed key with annual rotation), has SSL enforced, server access logging enabled to a dedicated log bucket, versioning disabled (git is the source of truth), and `auto_delete_objects=True` so `cdk destroy` empties and deletes it cleanly.

The access log bucket itself uses SSE-S3 rather than SSE-KMS — neither the S3 log delivery service nor CloudFront standard logging support writing to KMS-encrypted target buckets. The bucket is organized by prefix: `cloudfront/` for CloudFront standard logs, `s3-access-logs/` for S3 server access logs, and `athena-results/` for Athena query output. Glue catalog tables point at the log prefixes so Athena can query them directly with SQL. Athena's `PutObject` calls override the bucket-level SSE-S3 default on a per-object basis to write query results under `athena-results/` with SSE-KMS using the frontend CMK; the bucket-default constraint only applies to objects S3 chooses the encryption for, not to objects whose caller specifies it.

A **7-day expiration lifecycle rule** is applied uniformly to every prefix in the access log bucket — appropriate for a sample app where the value of an individual log entry decays quickly. The duration is intentionally short to keep storage cost bounded; tune it for your workload by editing the `lifecycle_rules` block in [hello_world_frontend_stack.py](hello_world/hello_world_frontend_stack.py). Common alternatives: extend the expiration to 30/90/365 days, or replace the flat expiration with a tiered transition — Standard → S3 Standard-IA at 30 days → Glacier Instant Retrieval at 90 days → Glacier Deep Archive at 180 days → expire at 7 years. Per-prefix rules are also supported if logs and Athena results need different retention.

#### CloudTrail object-level data events

A dedicated [CloudTrail Trail](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/logging-data-events-with-cloudtrail.html) records every object-level S3 API call (`GetObject`, `PutObject`, `DeleteObject`, etc.) against the frontend bucket and the access-log bucket. This is distinct from CloudTrail management events (which the AWS account already records by default) and from S3 server access logs (which only cover successful reads/writes through the S3 interface, not authorization failures or `DeleteObject` calls).

The trail writes to a separate dedicated bucket (`CloudTrailLogsBucket`) so the audit destination isn't itself among the audited resources — placing trail logs inside one of the buckets being audited would create a feedback loop where every audit write generates another audit event. The trail is also wired to a CloudWatch Log Group and is KMS-encrypted with the frontend stack's customer-managed key. File integrity validation is enabled, so CloudTrail publishes signed digest files that let you detect after-the-fact tampering with log entries.

*Cost/value note — this is the most expensive item in the recent hardening pass.* Honest read: the audited buckets in *this* sample contain a static SPA (`index.html`) and access-log files. Neither is sensitive customer data. The realistic security gain over what S3 server access logs already provide is modest. The reason the Trail is wired in anyway is **pedagogical**: getting object-level CloudTrail right is non-trivial — separate destination bucket to avoid feedback loops, KMS encryption, CloudWatch Logs delivery, file integrity validation, the cdk-nag suppressions on the auto-generated LogsRole — and the pattern transfers cleanly to forks where the audited buckets *do* hold sensitive data. The dollar cost at zero/sample traffic is also near zero (CloudTrail data events are billed per call: $0.10 per 100,000 events). Watch the *CloudTrail data event* line item in Cost Explorer if you fork into a high-traffic context — the price scales linearly with S3 API call volume and a busy production fork can easily generate $50–$200/month. If you fork this for a workload where the audited buckets aren't sensitive, removing the Trail is a clean one-call deletion (the helper [`_create_s3_audit_trail`](hello_world/hello_world_frontend_stack.py)).

#### CloudFront distribution

| Setting | Value | Why |
|---------|-------|-----|
| Viewer protocol | Redirect HTTP → HTTPS | Prevents plaintext traffic |
| Minimum TLS | TLS 1.2 (2021 policy) | Drops obsolete TLS 1.0/1.1 |
| Cache policy | `CACHING_OPTIMIZED` | S3 static assets — aggressive caching is correct |
| Response headers | `SECURITY_HEADERS` managed policy | Adds HSTS, X-Frame-Options, X-Content-Type-Options, etc. |
| Default root object | `index.html` | Serves the app at `/` |
| Error responses | 403/404 → `index.html` (200) | Supports SPA client-side routing |
| Cache invalidation | `/*` on every deploy | New assets served immediately |
| Access logging | S3 bucket with `cloudfront/` prefix | Every viewer request logged for audit and debugging |

#### WAF rules

The WebACL sits in front of CloudFront and inspects every request before it reaches S3. Five rules are active, evaluated in priority order:

| Priority | Rule | What it blocks |
|----------|------|---------------|
| 0 | `AWSManagedRulesAmazonIpReputationList` | Known malicious IPs — botnets, scanners, TOR exits |
| 1 | `AWSManagedRulesCommonRuleSet` | OWASP Top 10 web exploits |
| 2 | `AWSManagedRulesKnownBadInputsRuleSet` | Requests containing SQLi, XSS, and exploit payloads |
| 3 | `AWSManagedRulesAnonymousIpList` | Requests from VPNs, Tor exits, hosting providers, and other anonymizing services |
| 4 | `RateLimitPerIP` (custom) | Blocks any single client exceeding 200 requests per 5 minutes, keyed on `X-Forwarded-For` |

The rate-limit rule uses `aggregate_key_type="FORWARDED_IP"` with `header_name="X-Forwarded-For"` and `fallback_behavior="MATCH"`. All traffic enters through CloudFront, so the source IP at WAF is CloudFront's edge — without `FORWARDED_IP`, every viewer would be aggregated under whichever edge they happened to land on. `MATCH` makes a missing or malformed XFF trip the rule rather than skip it, so a determined caller can't bypass by stripping the header.

All rules are AWS WAF "free-tier" — no per-rule-group entity activation fee. Total fixed cost is $5/month (WebACL) + $1/month per rule = $10/month, plus $0.60 per million inspected requests. Adding any of the four paid-tier managed rule groups (Bot Control, ATP, ACFP, AntiDDoSRuleSet) layers a $10–20/month entity fee on top — see "Considered and rejected" in [Design decisions](#design-decisions-and-known-limitations) for why the AntiDDoS rule group was tried and removed.

All rules emit CloudWatch metrics and sampled requests, so WAF activity is visible in the console without additional configuration.

All rules emit CloudWatch metrics and sampled requests, so WAF activity is visible in the console without additional configuration.

WAF access logs are written to a CloudWatch Logs log group named `aws-waf-logs-{stack_name}` (the `aws-waf-logs-` prefix is an AWS requirement). The log group is KMS-encrypted with a customer-managed key and has 1-week retention.

The WAF WebACL lives in `HelloWorldWafStack` which is always pinned to `us-east-1`. This is an AWS hard constraint for CloudFront-scoped WebACLs. The cross-region reference pattern described above handles wiring the ARN to CloudFront automatically regardless of where the frontend stack is deployed.

#### Observability

Every layer of the stack emits structured logs and/or traces:

| Layer | Log destination | Format | X-Ray |
|-------|----------------|--------|-------|
| **Lambda** | CloudWatch Logs (JSON) | Powertools Logger — `xray_trace_id`, `function_name`, `request_id`, `level`, `message`, `timestamp`, `service`, plus custom keys | `tracing=Tracing.ACTIVE` |
| **API Gateway (access)** | CloudWatch Logs (JSON) | 16 fields via typed `AccessLogField` references — `requestId`, `accountId`, `apiId`, `stage`, `resourcePath`, `httpMethod`, `protocol`, `status`, `responseType`, `errorMessage`, `requestTime`, `ip`, `caller`, `user`, `responseLength`, `xrayTraceId` | `tracing_enabled=True` |
| **API Gateway (execution)** | CloudWatch Logs | AWS-managed format — request/response payloads, integration latency, errors | Same as above |
| **CloudFront** | S3 (`cloudfront/` prefix) | AWS fixed 33-field tab-delimited format — client IP, URI, status, edge location, etc. | N/A (traces propagate from API Gateway → Lambda) |
| **S3** | S3 (`s3-access-logs/` prefix) | AWS fixed ~25-field space-delimited format — requester, operation, key, status, bytes | N/A |
| **WAF** | CloudWatch Logs (`aws-waf-logs-*`) | AWS JSON — action, rule matched, request headers, country, URI | N/A |
| **Browser (RUM)** | CloudWatch RUM + CloudWatch Logs | AWS JSON — page loads, JS errors, Core Web Vitals, fetch timings, user interactions, session/user IDs | Client-side segment joins the backend trace via `X-Amzn-Trace-Id` |

X-Ray traces flow end-to-end: the browser RUM client emits a client-side segment and attaches an `X-Amzn-Trace-Id` header to outbound fetches, API Gateway continues that trace, Lambda adds subsegments (via Powertools Tracer `@tracer.capture_method`), and the `xrayTraceId` is included in API Gateway access logs for correlation. S3 and CloudFront access logs use AWS-fixed formats that are not customizable.

**CloudWatch RUM.** The frontend stack provisions an `AWS::RUM::AppMonitor` with `EnableXRay=true`. The app monitor ID, identity pool ID, and region are injected into `frontend/config.json` at deploy time; the HTML loads the RUM snippet from `<head>`, initializes `cwr` with `enableXRay: true`, and the single X-Ray trace shows browser → CloudFront → API Gateway → Lambda in one timeline. API Gateway CORS is configured to allow the `X-Amzn-Trace-Id` request header so the browser's trace ID propagates through the preflight.

The browser loads four plugins: `errors` (uncaught JS exceptions and unhandled promise rejections via `window.onerror`), `performance` (page load timings, Core Web Vitals), `http` (`fetch`/`XHR` latency and status), and `interaction` (user click events). The `errors` telemetry only catches *uncaught* exceptions — caught errors are invisible to RUM unless explicitly recorded, so the API-call handler in [frontend/index.html](frontend/index.html) calls `window.cwr("recordError", err)` from its `catch` block to surface API failures that the page swallows into an inline message.

The plugin set is *not* the same list in both places. The CloudFormation schema for `AWS::RUM::AppMonitor.AppMonitorConfiguration.Telemetries` accepts only `["errors", "performance", "http"]` and rejects `"interaction"` as an invalid enum value, even though `interaction` is a real, supported plugin. That server-side list is documentation for the AWS-generated snippet, not the live loader; the actual plugin set is controlled by the client-side `telemetries` array in `frontend/index.html`. So the AppMonitor lists three telemetries and the client lists four. Keep them divergent on purpose.

The `http` telemetry is written as a `[name, config]` tuple — `["http", { addXRayTraceIdHeader: true }]` — rather than the bare-string form. `enableXRay: true` defaults `addXRayTraceIdHeader` to true today, but stating it explicitly guards against future client-version regressions of that default and matches the AWS reference snippet pattern.

**Custom events.** The AppMonitor sets `CustomEvents.Status: ENABLED` so the frontend can call `cwr('recordEvent', type, details)` for domain telemetry beyond what the standard plugins capture. Without that flag, custom event uploads are silently dropped at the data plane. No event types are recorded today; the wiring is in place for when the application gains business-meaningful interactions.

**Session attributes.** Deploy-time metadata is attached to every RUM event in the session via `sessionAttributes`. The frontend stack injects `applicationName: <stack-name>` into `frontend/config.json`, and the client snippet reads it into the `cwr` config. Sourcing attributes from the deployed config (rather than hardcoding in the HTML) lets multiple deploys feed the same dashboard while remaining filterable. Attribute limits: max 10 per event, key ≤128 chars (alphanumeric, `:`, `_`, no `aws:` prefix, no reserved keys like `browserName`/`pageTitle`/`version`), value ≤256 chars (string/number/boolean).

**Extended metrics.** By default, RUM publishes scalar CloudWatch metrics (`JsErrorCount`, `Http4xxCount`, `PageViewCount`, `PerformanceNavigationDuration`, etc.) with only the `application_name` dimension — useful for top-line counts but blind to *which* browser, device, country, or page is producing them. Extended metrics add user-agent / geo / page dimensions to those scalars so dashboards can slice the same metric multiple ways. The frontend stack registers a CloudWatch destination on the AppMonitor and creates seven extended metric definitions covering JS errors (by browser / device / country), HTTP errors (by browser), and per-page navigation timing and view counts.

There is no native CloudFormation resource for RUM metric destinations or definitions — both are managed via the RUM API only — so the stack uses two `AwsCustomResource` constructs to call `PutRumMetricsDestination` and `BatchCreateRumMetricDefinitions` at deploy time. Several operational notes worth knowing if you change this code:

- **Each definition needs an `EventPattern`.** The AWS docs imply you can register a vended metric (`JsErrorCount`, `Http4xxCount`, etc.) with just `Name` and `DimensionKeys` and let RUM supply the `ValueKey` internally. This isn't true: the API requires an `EventPattern` that filters to the right RUM event type AND existence-checks every dimension key. Without one, the API returns `200 OK` with an `Errors[]` body — which `AwsCustomResource` treats as success — so the definitions silently never get created. The patterns in [hello_world/hello_world_frontend_stack.py](hello_world/hello_world_frontend_stack.py) match `event_type` (`com.amazon.rum.js_error_event`, `com.amazon.rum.http_event`, `com.amazon.rum.page_view_event`) plus `{"exists":true}` checks on each dimension key.
- **Http5xxCount has a vended-metric quirk.** Adding an explicit numeric range filter on `event_details.response.status` works for `Http4xxCount` but is rejected for `Http5xxCount` ("Value … for metric field event detail is not valid"). RUM applies the 5xx filter internally for that metric, so the EventPattern must omit the status range; for Http4xx the range is required. The two patterns are deliberately not symmetric.
- **`PerformanceNavigationDuration` is left out.** With our dimension shape RUM rejected it as "Value `event_details.duration` for metric field value key is not valid". A working configuration is possible but requires a different vended-metric or custom-metric form than the others; not worth the complexity for the reference architecture.
- **`on_create` and `on_update` reference the same call.** `AwsCustomResource` no-ops on CloudFormation UPDATE events when `on_update` is omitted. Without it, edits to the metric-definitions list never propagate to AWS — the deploy succeeds, the AppMonitor is unchanged, no error is reported.
- **Updates accumulate.** `BatchCreateRumMetricDefinitions` is not idempotent: re-running it with the same definitions creates duplicates rather than reconciling. If you change the metric list and want a clean replacement (rather than the old set plus the new set side-by-side), destroy and redeploy the frontend stack so the AppMonitor cascade-deletes its configuration before the new set is registered.
- **No alarms are wired.** Recording the metrics with dimensions is enough to enable ad-hoc CloudWatch metric queries and dashboard widgets sliced by the new dimensions; binding specific thresholds to alarms is a separate decision left to the operator.

There are also two CDK ordering concerns worth understanding:

- **IAM propagation.** The `RumMetricsDestination` policy bundles all three `rum:*` actions (`PutRumMetricsDestination`, `DeleteRumMetricsDestination`, `BatchCreateRumMetricDefinitions`) on the *first* AwsCustomResource so they ride a single policy attachment. By the time `RumExtendedMetrics` fires, the policy has been on the singleton role through one full `PutRumMetricsDestination` round-trip — enough lead time for IAM to propagate. Splitting the actions across each construct's own policy lost the race consistently with `AccessDenied`.
- **`BucketDeployment` depends on `RumExtendedMetrics`.** This is intentional: if `RumExtendedMetrics` fails, `BucketDeployment` never runs, which avoids the [known CDK bug](https://github.com/aws/aws-cdk/issues/15891) where the BucketDeployment provider's CloudFront invalidation can't complete during a rollback that's deleting the same distribution. Without this dependency, a metrics-side failure produces an unrecoverable `ROLLBACK_FAILED` stack with orphaned CloudFront, S3, and OAC resources that have to be cleaned up manually.

**Why Cognito.** Browsers are anonymous — they have no prior identity and nowhere to safely store long-lived AWS credentials — but the RUM data plane still needs authenticated SigV4 calls to `rum:PutRumEvents`. A Cognito Identity Pool with `AllowUnauthenticatedIdentities=true` is the AWS-standard bridge: it issues short-lived STS credentials to every anonymous browser session, which the RUM client then uses to sign telemetry uploads. This is what makes client-side telemetry possible without shipping an access key to the browser. The trust chain is:

1. **Browser fetches `/config.json`** — gets the identity pool ID, app monitor ID, and region (all non-sensitive public identifiers, safe to embed in static assets).
2. **Browser calls Cognito `GetId` + `GetCredentialsForIdentity`** — the identity pool returns a temporary, unauthenticated identity ID and short-lived STS credentials.
3. **STS assumes `RumUnauthenticatedRole` via `sts:AssumeRoleWithWebIdentity`** — the role's trust policy requires `cognito-identity.amazonaws.com:aud` to match this pool ID and `amr = "unauthenticated"`, so credentials from any other pool or flow are rejected.
4. **RUM client calls `rum:PutRumEvents`** — that is the role's *only* permission, and it's scoped to the one monitor ARN `arn:aws:rum:{region}:{account}:appmonitor/{stack-name}-rum`. A compromised browser session cannot escalate to any other RUM monitor, any other AWS service, or even the same monitor in a different account.

In short: the pool exists because the browser has no identity of its own, and the role exists to make sure anonymous browser credentials can do exactly one thing and nothing else. `AwsSolutions-COG7` (which flags unauthenticated identities) is suppressed on the pool with this rationale — it is the correct model for anonymous telemetry, not a security gap.

#### Access log analytics (Athena + Glue)

CloudFront and S3 access logs are stored in S3, not CloudWatch, so they cannot be queried with CloudWatch Logs Insights. Instead, the frontend stack provisions a Glue Data Catalog and Athena workgroup for SQL-based analytics.

**Glue catalog structure:**

| Table | Source prefix | Format | SerDe |
|-------|--------------|--------|-------|
| `cloudfront_logs` | `cloudfront/` | 33-field tab-delimited (2 header lines) | `LazySimpleSerDe` |
| `s3_access_logs` | `s3-access-logs/` | 26-field with quoted strings | `RegexSerDe` |

**Access log bucket layout:**

```
s3://<access-log-bucket>/
├── cloudfront/       ← CloudFront standard access logs
├── s3-access-logs/   ← S3 server access logs
└── athena-results/   ← Athena query results (SSE-KMS encrypted with the frontend CMK)
```

**Athena named queries (pre-built, ready to run):**

| Query | What it shows |
|-------|--------------|
| CloudFront - Top Requested URIs | Most frequently requested URIs with error counts |
| CloudFront - Error Responses | Recent 4xx/5xx responses with client and edge details |
| CloudFront - Top Client IPs | Highest-traffic client IPs with error counts |
| CloudFront - Bandwidth by Edge Location | Total bytes transferred per edge location |
| CloudFront - Cache Hit Ratio | Request counts and percentages by edge result type |
| S3 - Top Operations | Most common S3 operations with error counts |
| S3 - Error Requests | Recent failed S3 requests with error details |
| S3 - Top Requesters | Highest-traffic S3 requesters with error counts |
| S3 - Slow Requests | Highest-latency requests by `total_time` |
| S3 - Access Denied (403) | Recent 403 AccessDenied responses for IAM/policy debugging |
| S3 - Object Read Audit | Who read which object (GET.OBJECT) with status and bytes |

To run queries, open the Athena console, select the workgroup from the stack outputs, and choose a saved query. Results are stored in the access log bucket under `athena-results/`.

**Scaling note.** Logs land flat under their prefix and queries scan the full dataset. At this app's scale that's free in practice, but if traffic grows enough that Athena scans start costing real money, the standard next step is partitioning by `year=/month=/day=/hour=/` — ideally with Glue partition projection so no `MSCK REPAIR` is needed — and converting to Snappy Parquet for columnar pruning. See the AWS Big Data blog [*Analyze your Amazon CloudFront access logs at scale*](https://aws.amazon.com/blogs/big-data/analyze-your-amazon-cloudfront-access-logs-at-scale/) for a full Lambda + CTAS pipeline. Conversion is fully retroactive: existing gzip logs can be backfilled with a one-shot Athena CTAS whenever the cost justifies the added complexity.

**Query tuning reference.** The named queries above already follow the applicable guidance from [*Top 10 performance tuning tips for Amazon Athena*](https://aws.amazon.com/blogs/big-data/top-10-performance-tuning-tips-for-amazon-athena/) — every `ORDER BY` is paired with a `LIMIT`, no `SELECT *`, minimal `GROUP BY` columns, no joins, no `COUNT(DISTINCT)`. The remaining tips in that post (partitioning, bucketing, compression, file sizing, columnar formats) are all storage-side and are covered by the scaling note above.

#### Resource cleanup

Every resource in `HelloWorldWafStack` and `HelloWorldFrontendStack` has `RemovalPolicy.DESTROY`, including all CloudWatch log groups. `cdk destroy --all` leaves nothing behind in any region.

Note: CDK creates an internal singleton Lambda to empty the S3 bucket before deletion (`Custom::S3AutoDeleteObjects`). Its log group is explicitly declared in the stack so CloudFormation owns it and deletes it on destroy — following the same principle as the API Gateway execution log group in the backend stack.

### Monitoring

The stack includes a [cdk-monitoring-constructs](https://github.com/cdklabs/cdk-monitoring-constructs) MonitoringFacade that creates a CloudWatch dashboard with Lambda, API Gateway, and DynamoDB metrics out of the box.

## Working in the codebase

### Add a resource to your application

To add AWS resources, define new constructs in the appropriate file under `hello_world/`: backend domain resources (Lambda, API Gateway, DynamoDB, SSM, AppConfig, anything the Lambda talks to at runtime) belong in `hello_world_app.py` inside the `HelloWorldApp` construct, frontend resources (S3, CloudFront) belong in `hello_world_frontend_stack.py`, and WAF rules belong in `hello_world_waf_stack.py`. `hello_world_stack.py` itself stays lean — only add something there if it is genuinely stack-wide (a new CfnOutput, a new Aspect, a stack-level nag suppression). The CDK provides high-level constructs for most AWS services. Browse available constructs in the [AWS CDK API Reference](https://docs.aws.amazon.com/cdk/api/v2/python/). For resources without a dedicated CDK construct, you can use [CloudFormation resource types](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-template-resource-type-ref.html) directly via `CfnResource`.

### Useful CDK commands

* `cdk ls`                         list all stacks in the app
* `cdk synth`                      emit the synthesized CloudFormation template
* `cdk deploy --all`               deploy all stacks to us-east-1 (default)
* `cdk deploy --all -c region=X`   deploy all stacks to region X
* `cdk diff`                       compare deployed stack with current state
* `cdk destroy --all`              destroy all stacks in the default region

### Synthesize and validate locally

Synthesize your application to verify the CloudFormation template (requires a container runtime — Finch or Docker — to be running):

```bash
# If using Finch:
export CDK_DOCKER=finch
cdk synth

# If using Docker: just run `cdk synth` — CDK auto-detects Docker
cdk synth
```

For local *invocation* of the Lambda handler, use the pytest-driven debug flow described in [Debugging the Lambda function](#debugging-the-lambda-function) — `sam local invoke` is intentionally not used by this project (the unpinned `debugpy` it injects conflicts with the project's `--require-hashes` install mode for `lambda/requirements.txt`).

### Fetch, tail, and filter Lambda function logs

The AWS CLI `logs tail` command streams a CloudWatch log group directly — no SAM CLI dependency. Get the function's log group from the backend stack outputs (or look it up in the AWS Console under the Lambda's *Monitor* tab):

```bash
# Tail the live stream (Ctrl+C to stop)
aws logs tail /aws/lambda/HelloWorld-us-east-1-AppHelloWorldFunction... --follow

# Filter for errors only
aws logs tail /aws/lambda/HelloWorld-us-east-1-AppHelloWorldFunction... --follow \
    --filter-pattern '{ $.level = "ERROR" }'

# Tail with a JMESPath filter, picking out one structured field per line
aws logs tail /aws/lambda/HelloWorld-us-east-1-AppHelloWorldFunction... --follow \
    --format short \
    --filter-pattern '{ $.correlation_id = "abc-123" }'
```

The Lambda's `logging_format=JSON` config makes every line valid JSON, which `--filter-pattern` queries against directly using [CloudWatch metric-filter syntax](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/FilterAndPatternSyntax.html). Pair with [Powertools' `correlation_id` injection](https://docs.powertools.aws.dev/lambda/python/latest/core/logger/#setting-a-correlation-id) to follow a single request end-to-end across all the structured log entries it produced.

For richer ad-hoc queries (joins across log groups, percentile aggregations, time-series visualizations), use [CloudWatch Logs Insights](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/AnalyzingLogData.html) in the AWS Console — the WAF stack already pre-creates three saved queries (see [WAF rules](#waf-rules)) as a model for how to wire those into CDK.

### Documentation

Project documentation is generated by [Zensical](https://zensical.org/) (the MkDocs-Material successor from the same maintainer) with the [mkdocstrings](https://mkdocstrings.github.io/) Python handler, and covers two distinct audiences in one site:

- **Code reference (for developers)** — autodoc-rendered pages for `lambda/app.py` (Lambda handler), `hello_world/hello_world_stack.py` (backend stack wrapper), `hello_world/hello_world_app.py` (backend domain construct), `hello_world/hello_world_waf_stack.py` (WAF), `hello_world/hello_world_frontend_stack.py` (frontend), and `hello_world/nag_utils.py` (shared suppression utilities). Generated from Google-style docstrings via the `::: module.path` directive in each page.
- **HTTP API reference (for callers)** — a standalone [Scalar](https://github.com/scalar/scalar) API Reference page at `/api.html`, rendered in the browser from `/openapi.json`. Both files are generated pre-build by `scripts/generate_openapi.py` (which imports the Lambda resolver and serializes its schema) and copied into the built site verbatim by Zensical (non-markdown assets in `docs/` pass through untouched). The `make docs` target regenerates the spec before invoking Zensical, so the API page always matches the live code. Scalar's OSS bundle includes a built-in request sandbox — unlike Redoc, which gates "Try it out" behind Redocly's paid tier.

  The Scalar bundle is loaded from jsdelivr with a **pinned version + SRI hash** rather than a fresh-from-the-internet `@latest` tag. The browser verifies the integrity hash on every page load, so if the CDN is ever compromised to serve tampered bytes, execution fails closed. Upgrading is a two-line change: bump the version in `docs/api.html` and drop in a new hash (the file has a one-line `openssl` recipe in its comments).

  Scalar's code-sample panel defaults to **Python + `requests`** via the `defaultHttpClient` configuration on the `<script id="api-reference">` tag. Callers of this API are overwhelmingly writing Python against Lambda, so showing the Python snippet on page load beats Scalar's stock `curl` default. All other languages (Go, Node, Shell, etc.) remain one click away in the client picker.

Doc builds are best run in CI/CD pipelines or manually before publishing, rather than on every commit.

```bash
# Build HTML docs (regenerates the OpenAPI spec first)
make docs

# Under the hood, `make docs` runs:
#   python scripts/generate_openapi.py
#   zensical build

# Open in browser
open site/index.html
# Shortcut for build + open: make docs-open

# Or run the dev server with hot reload:
make docs-serve
```

### Cleanup

To delete the application and all associated AWS resources, run:

```bash
cdk destroy
```

Every resource in the stack — including all three CloudWatch log groups — is configured with `RemovalPolicy.DESTROY`, so a single `cdk destroy` leaves no dangling resources and no ongoing AWS costs.

## Deploy the application

This project needs a container runtime for bundling Lambda dependencies during synthesis. Either [Finch](https://runfinch.com/) or [Docker](https://www.docker.com/) works — CDK uses whichever runtime is pointed to by the `CDK_DOCKER` environment variable, and falls back to Docker when the variable is unset (see the [CDK GitHub issue](https://github.com/aws/aws-cdk/issues/23680#issuecomment-1741643237) where Finch support was added). Pick one:

```bash
# Option 1: Finch (recommended — license-friendly, AWS-supported open source)
finch vm start
export CDK_DOCKER=finch

# Option 2: Docker (drop-in alternative — no env var needed, CDK auto-detects it)
# Just make sure the Docker daemon is running; no CDK_DOCKER export required
```

To set up and deploy your application for the first time, run the following in your shell:

```bash
# Install both venvs and pre-commit hooks. Creates:
#   .venv         — CDK workstation (cdk + test + lint + docs groups)
#   .venv-lambda  — Lambda runtime   (lambda + test groups)
# See "Project dependencies" below for why two venvs are required.
make install

# Start your container runtime (pick one from the block above)
finch vm start           # if using Finch
# -- or --
# (nothing needed for Docker — just ensure Docker Desktop / daemon is running)

# Bootstrap CDK in us-east-1 (always required — WAF stack always deploys here)
cdk bootstrap aws://YOUR_ACCOUNT_ID/us-east-1

# Deploy all stacks to us-east-1 (default)
cdk deploy --all
```

The `cdk synth` and `cdk deploy` commands use your chosen container runtime to build an image that installs the Lambda dependencies from `lambda/requirements.txt` into the deployment package. The first run will be slower as it pulls the AWS Lambda Python build image (distributed via the SAM project, hence the "SAM build image" label you may see in logs).

### Recommended order for ongoing deploys

The first-time flow above is enough to get the stacks up. For *every deploy after that* — and for the first one too if you want a safety net — run the validation gates first. Each step catches a different class of failure cheaply, before AWS sees anything:

```bash
# 1. Synthesize all stacks. Runs cdk-nag against the four-pack policy
#    (AwsSolutions, NIST 800-53 R5, HIPAA Security, PCI DSS 3.2.1) and
#    fails on any unsuppressed finding. Catches IAM / encryption /
#    logging regressions before they reach CloudFormation.
make cdk-synth

# 2. Run the CDK assertion tests against the synthesized templates.
#    Catches structural drift in resource counts, properties, and
#    suppression coverage. ~120s timeout in the Makefile.
make test-cdk

# 3. Bootstrap CDK in the target region — only required the first
#    time per account+region pair. Idempotent if already bootstrapped.
#    WAF always lives in us-east-1, so us-east-1 is always required;
#    add the target region too if it differs.
cdk bootstrap aws://YOUR_ACCOUNT_ID/us-east-1

# 4. Deploy all three stacks in the Stage. --require-approval never
#    suppresses interactive IAM-change prompts since cdk-nag already
#    gated the change at step 1; drop the flag for a manual review of
#    every IAM diff.
cdk deploy --all --require-approval never

# 5. CfnOutputs print at the end of step 4 — capture CloudFrontDomainName
#    (the frontend URL), HelloWorldApiOutput (the API endpoint),
#    RumAppMonitorId (for browser RUM), and CloudWatchDashboardUrl
#    (the monitoring dashboard).
```

Skip a step only when you specifically don't need that gate: skip step 1 if you've just synthesized in another shell, skip step 2 if you're iterating on a single resource and the assertion tests don't cover it, skip step 3 once both regions are bootstrapped. Steps 4 and 5 are not optional.

After deployment, CloudFormation outputs useful values directly in the terminal. Each stack exposes the following:

**`HelloWorldWaf-{region}`:**
- `WebAclArn` — WAF WebACL ARN (also used internally by the frontend stack)
- `WebAclId` — WAF WebACL logical ID
- `WafLogGroupName` — CloudWatch log group name for WAF access logs

**`HelloWorld-{region}`:**
- `HelloWorldApiOutput` — API Gateway endpoint URL (`https://.../Prod/hello`)
- `HelloWorldFunctionOutput` — Lambda function ARN
- `HelloWorldFunctionIamRoleOutput` — Lambda IAM role ARN
- `IdempotencyTableName` — DynamoDB table name
- `GreetingParameterName` — SSM parameter path
- `AppConfigAppName` — AppConfig application name
- `CloudWatchDashboardUrl` — Direct link to the CloudWatch monitoring dashboard

**`HelloWorldFrontend-{region}`:**
- `CloudFrontDomainName` — `https://` URL to open in a browser
- `CloudFrontDistributionId` — Distribution ID for manual cache invalidations
- `FrontendBucketName` — S3 bucket name for direct asset inspection
- `GlueDatabaseName` — Glue catalog database for access log analytics
- `AthenaWorkGroupName` — Athena workgroup for querying access logs

### Deploying to a different region

Each target region must be bootstrapped before its first deploy. Bootstrap is a one-time step per region per account.

```bash
# Bootstrap the target region (in addition to us-east-1 which is always needed)
cdk bootstrap aws://YOUR_ACCOUNT_ID/ap-southeast-1

# Deploy all stacks — WAF stays in us-east-1, backend and frontend go to ap-southeast-1
cdk deploy --all -c region=ap-southeast-1
```

### Destroying a deployment

```bash
# Destroy the default us-east-1 deployment
cdk destroy --all

# Destroy a specific regional deployment (does not affect other regions)
cdk destroy --all -c region=ap-southeast-1
```

## Quality and security

### Tests

Tests are defined in the `tests` folder in this project. Make sure dependencies are installed first (see [Deploy the application](#deploy-the-application)).

#### Unit test architecture

Unit tests mock all external AWS dependencies so they run locally without credentials or a deployed stack. The key patterns used:

**Shared fixtures via conftest.py** — Reusable fixtures live in `tests/conftest.py`, including the API Gateway event, Lambda context mock, and the Lambda app module reference. The autouse mock that patches SSM Parameters and Feature Flags lives in `tests/unit/conftest.py` so it only applies to unit tests. Test files stay clean and focused on assertions.

**Environment variables** — All test env vars are centralized in `pyproject.toml` via pytest-env. This includes Powertools config, mock resource names, and the idempotency disable flag. No `os.environ` calls needed in test files.

**Idempotency disabled via env var** — `POWERTOOLS_IDEMPOTENCY_DISABLED=true` is set in `pyproject.toml` to tell Powertools to skip DynamoDB calls during tests. This is the recommended approach from Powertools docs. In production, this env var is not set, so idempotency is fully active.

**Mocking external calls with pytest-mock** — SSM Parameters and Feature Flags are mocked in `tests/unit/conftest.py` using `mocker.patch.object()`:
```python
mocker.patch.object(lambda_app, "get_parameter", return_value="hello world")
mocker.patch.object(lambda_app.feature_flags, "evaluate", return_value=False)
```

**Lambda context via pytest-mock** — A `MagicMock` provides the Lambda context object with realistic attributes (function name, ARN, request ID).

**Import path isolation** — The `lambda/` directory is added to `sys.path` in `tests/conftest.py` before the root directory to ensure `import app` resolves to the Lambda handler (`lambda/app.py`) and not the CDK entry point (`app.py`).

#### Running unit tests

```bash
python -m pytest tests/unit -v
# Shortcut: make test
```

#### CDK stack assertion tests

`tests/cdk/test_stacks.py` synthesizes each CDK stack in-process using `aws_cdk.assertions.Template` and verifies that key security properties are correctly configured (KMS encryption on DynamoDB, PITR, CloudFront TLS policy, WAF attached, etc.). If a construct property is accidentally changed, the test fails at synth time rather than silently deploying an insecure template. Any unsuppressed cdk-nag finding also causes synthesis to fail, so these tests double as a CI gate for infrastructure misconfigurations.

The suite also enforces the CDK best practice ["don't change logical IDs of stateful resources"](https://docs.aws.amazon.com/cdk/v2/guide/best-practices.html): `TestLogicalIdStability` freezes the logical IDs of all stateful resources (DynamoDB, KMS keys, S3 buckets, CloudFront distribution, SSM, AppConfig, WAF WebACL, and log groups) in a committed list. A refactor that moves or renames any of those constructs would change their logical ID — which for CloudFormation means resource replacement, i.e. data loss for stateful resources. The test catches that drift at PR time. If you genuinely need to change a logical ID, update the expected value in the same commit so the intent is reviewable.

```bash
python -m pytest tests/cdk -v --override-ini="addopts="
# Shortcut: make test-cdk (requires aws_cdk — use `make install`, not `make install-dev`)
```

Asset bundling (Docker) is skipped via the `aws:cdk:bundling-stacks` context key so these tests run without Docker.

#### Integration tests

Two suites of integration tests verify the live deployment:

**API Gateway** (`tests/integration/test_api_gateway.py`) — calls the live API Gateway endpoint and verifies the response body, content type headers, and response time (under 10 seconds, to account for Lambda cold starts with SSM and AppConfig initialization). The backend stack name is read from `AWS_BACKEND_STACK_NAME` in `pyproject.toml` (defaults to `HelloWorld-us-east-1`). Override for a different region:

```bash
AWS_BACKEND_STACK_NAME=HelloWorld-ap-southeast-1 pytest tests/integration/
```

**CloudFront / S3** (`tests/integration/test_frontend.py`) — fetches the CloudFront distribution URL from the frontend stack outputs and verifies that the index page is served, `config.json` contains the injected API URL, HTTPS is enforced, security headers are present, and unknown paths fall back to `index.html` (SPA routing). The frontend stack name is read from `AWS_FRONTEND_STACK_NAME` in `pyproject.toml` (defaults to `HelloWorldFrontend-us-east-1`). Override for a different region:

```bash
AWS_FRONTEND_STACK_NAME=HelloWorldFrontend-ap-southeast-1 pytest tests/integration/
```

If either stack is not deployed, its tests skip automatically rather than failing — so the default `pytest` run stays green without a live deployment. Other test environment variables are configured in `pyproject.toml` via pytest-env (see the `env` key under `[tool.pytest.ini_options]`).

All test environment variables are centralized in `pyproject.toml` rather than scattered across test files. Note that `POWERTOOLS_IDEMPOTENCY_DISABLED=true` is only active during test runs — in production, this env var is not set, so idempotency is fully active against the DynamoDB table.

```bash
python -m pytest tests/integration -v
# Shortcut: make test-integration
```

#### Timeout

Every test has a 30-second timeout enforced via `timeout = 30` in `pyproject.toml`. Tests that exceed this are terminated and marked as failed. To override for a specific test, use the `@pytest.mark.timeout(60)` decorator.

#### Test randomization

pytest-randomly shuffles test execution order on every run to catch order-dependent bugs. It activates automatically when installed — no additional configuration needed. The seed is printed at the top of the output. To reproduce a specific order:

```bash
python -m pytest tests/ -p randomly -p no:randomly  # disable
python -m pytest tests/ --randomly-seed=12345        # replay a specific seed
```

#### Coverage

Coverage runs automatically on every test run. Key flags set in `pyproject.toml`:

| Flag | Effect |
|---|---|
| `--cov=lambda` | Measures coverage for the `lambda/` source directory |
| `--cov-branch` | Tracks branch coverage (not just whether a line executed, but whether all conditional paths did) |
| `--cov-report=term-missing` | Prints uncovered line numbers in the terminal |
| `--cov-report=html` | Generates `htmlcov/index.html` for detailed browsing |
| `--cov-fail-under=100` | Fails the run if total coverage drops below 100% |
| `--no-cov-on-fail` | Skips the coverage report when tests fail (avoids misleading partial output) |

To open the HTML report after a test run:

```bash
open htmlcov/index.html
```

#### Parallel execution

Tests run in parallel automatically via `-n auto` in `addopts` (`pyproject.toml`). pytest-xdist distributes tests across CPU cores. To disable it for debugging:

```bash
python -m pytest tests/ -n0
```

#### HTML report

An HTML test report (`report.html`) is generated automatically on every test run via `--html=report.html --self-contained-html` in `addopts` (`pyproject.toml`). Open it in a browser to view detailed results.

### Linting and static analysis

This project uses several tools for code quality. Most are configured in `pyproject.toml`; bandit uses a separate `.bandit` file.

```bash
# Lint with ruff
ruff check .

# Format with ruff
ruff format .
# Shortcut for lint + format: make lint (runs all hooks) or make format (format only)

# Type check with mypy
mypy lambda/ hello_world/
# Shortcut: make typecheck

# Design and complexity checks with pylint
pylint lambda/ hello_world/

# Security scan with bandit
bandit -r lambda/ hello_world/

# Dependency vulnerability audit
pip-audit
# Shortcut for bandit + pip-audit: make security

# Code complexity with radon/xenon
radon cc lambda/ -a
xenon lambda/ -b B -m A -a A

# Run all of the above at once via pre-commit:
pre-commit run --all-files
# Shortcut: make lint
```

#### Bandit configuration (`.bandit`)

Bandit is a security-focused static analyzer that scans Python source code for common vulnerabilities. Its configuration lives in `.bandit` rather than `pyproject.toml` because the pre-commit bandit hook reads YAML config files by convention.

The `.bandit` file specifies which directories to exclude from scanning:

| Directory | Reason excluded |
|---|---|
| `tests/` | Test code uses `assert`, hardcoded strings, and other patterns that trigger false positives |
| `cdk.out/` | CDK-generated CloudFormation output — not code you write or can fix |
| `.venv/` | Third-party packages — vulnerabilities here are caught by `pip-audit` instead |

Everything outside these directories — `lambda/` and `hello_world/` — is scanned. That is the code you own and ship.

### Detecting deprecated APIs

Deprecated APIs are easy to ignore because they keep working — until the next major release removes them. There is no single command that catches every kind of deprecation, so this project uses a combination of approaches. Each one targets a different layer:

| # | Approach | Catches | How to run |
|---|----------|---------|------------|
| 1 | **CDK API deprecations** | Deprecated CDK properties or methods used by any stack (e.g. `FunctionOptions#logRetention` → `logGroup`) | `make cdk-deprecations` (greps `cdk synth` output for `deprecated`) |
| 2 | **`cdk notices`** | AWS-published advisories about the CDK toolchain itself — CVEs, deprecated CDK versions, upcoming breaking changes | `make cdk-notices` |
| 3 | **Python `DeprecationWarning` in tests** | Deprecated stdlib or third-party API calls hit by your tests (boto3, Powertools, etc.) | Temporarily add `filterwarnings = ["error::DeprecationWarning"]` to `[tool.pytest.ini_options]` in `pyproject.toml`, run `pytest`, then revert. Useful as a one-shot audit but too noisy to leave on permanently. |
| 4 | **Ruff `UP` (pyupgrade)** | Deprecated Python syntax — e.g. `typing.List` → `list`, `Optional[X]` → `X \| None` | Already enabled in `[tool.ruff.lint]` `select`. Runs on every `make lint` and on every commit via the pre-commit hook. |
| 5 | **`pip list --outdated`** | Version drift — packages that are multiple major versions behind are likely calling deprecated APIs | `pip list --outdated` |

The first two are CDK-specific, the next two are Python-specific, and the last one is a general health check across all dependencies. None of them are mutually exclusive.

`cdk synth` no longer passes `--no-notices` (it used to, to keep CI output clean), so notices and CDK API deprecation warnings now print on every synth in both local and CI runs.

### Pre-commit hooks

Pre-commit runs a chain of hooks automatically on every `git commit`. Hooks are defined in `.pre-commit-config.yaml`. Set it up once after cloning:

```bash
pre-commit install
```

To run all hooks manually without committing (useful before pushing or after changing config):

```bash
pre-commit run --all-files
```

#### Hook reference

| Hook | Source | What it does |
|---|---|---|
| `ruff` | astral-sh/ruff-pre-commit | Lints and auto-fixes code (runs before formatting) |
| `ruff-format` | astral-sh/ruff-pre-commit | Formats code (equivalent to black) |
| `mypy` | mirrors-mypy | Static type checking on `lambda/` and `hello_world/` (excludes `app.py` and `tests/`) |
| `bandit` | PyCQA/bandit | Security-focused static analysis on `lambda/` and `hello_world/` |
| `pylint` | local | Design and complexity checks on non-test, non-docs Python files |
| `trailing-whitespace` | pre-commit-hooks | Removes trailing whitespace |
| `end-of-file-fixer` | pre-commit-hooks | Ensures every file ends with a newline |
| `check-yaml` | pre-commit-hooks | Validates YAML syntax |
| `check-json` | pre-commit-hooks | Validates JSON syntax |
| `xenon` | local | Enforces cyclomatic complexity thresholds on `lambda/` (max absolute: B, module: A, average: A) |
| `pip-audit` | local | Scans all installed dependencies for known CVEs (runs on every commit) |

**Version pinning.** The `ruff` and `mypy` hooks are pinned to specific versions in `.pre-commit-config.yaml` (`rev:` for ruff, `additional_dependencies:` for mypy's `boto3-stubs` and `aws-cdk-lib`). These pins must stay in sync with the corresponding versions resolved in `uv.lock` — if Dependabot bumps ruff or boto3-stubs in the lock, update `.pre-commit-config.yaml` to match. A version mismatch means pre-commit and the local venv would run different tool versions, which can cause "passes locally, fails in CI" drift.

**Optional: aligning VS Code editor lint output with CI.** The same drift class can appear at a third surface: the Microsoft Pylint and Mypy extensions and the Astral Ruff extension all default to `importStrategy: "useBundled"`, which runs the tool binary that ships *inside the extension* rather than the one resolved in `uv.lock`. The bundled versions can be ahead, behind, or differ on rule defaults from the pins in `pyproject.toml` (currently `ruff==0.15.10`, `mypy==1.20.0`, `pylint==4.0.5`), so it's possible to see a clean editor while CI fails on a rule that exists in the pinned version. To make the editor use the exact same binaries as pre-commit and CI, add the following to your **personal** VS Code settings (User Settings, not the committed `.vscode/settings.json`):

```json
"ruff.importStrategy": "fromEnvironment",
"mypy-type-checker.importStrategy": "fromEnvironment",
"pylint.importStrategy": "fromEnvironment"
```

This points each extension at the active interpreter's `.venv/bin/`, where the `lint` dependency group installs the pinned binaries. The setting is intentionally *not* added to the committed workspace settings: a contributor who opens the repo before running `uv sync --group lint` would have empty `.venv/bin/` paths, and `fromEnvironment` would either silently fall back to bundled or surface a confusing error depending on the extension. Keeping the workspace default at `useBundled` means the editor "just works" out of the box; opt-in to `fromEnvironment` once you've synced the lint group locally.

### Security

Security follows the AWS [CDK security best practices guide](https://docs.aws.amazon.com/cdk/v2/guide/best-practices-security.html) — least-privilege IAM, encryption at rest and in transit, cdk-nag rule packs in the synth loop, and no hardcoded secrets. It is enforced at three layers, each covering a different surface area:

| Layer | Tool | What it scans | When it runs |
|---|---|---|---|
| **Source code** | bandit | `lambda/` and `hello_world/` for security anti-patterns (hardcoded secrets, shell injection, unsafe deserialization, etc.) | Pre-commit hook on every commit; CI quality job |
| **Dependencies** | pip-audit | Every dependency group in `uv.lock` for packages with known CVEs | Pre-commit hook on every commit; weekly Dependency Audit workflow |
| **Infrastructure** | cdk-nag | CDK stacks against AWS Solutions, Serverless, NIST 800-53 R5, HIPAA Security, and PCI DSS 3.2.1 rules | `cdk synth` — findings are printed and fail synthesis if unsuppressed |

These tools are complementary — no single one covers all three surfaces. Bandit catches code-level issues, pip-audit catches supply chain issues, and cdk-nag catches infrastructure misconfigurations.

### CDK security checks

All three stacks use [cdk-nag](https://github.com/cdklabs/cdk-nag) with five rule packs applied to every resource at synth time. Any finding that is not suppressed fails `cdk synth` — infrastructure misconfigurations are caught before deployment, not after.

Checks run automatically on every `cdk synth` and `cdk deploy`. There is no separate command needed. The pack set is attached by a single helper — [`apply_compliance_aspects`](hello_world/nag_utils.py) — that every stack calls in its constructor, so adding or removing a pack is a one-line change in one place.

#### Rule packs in use

| Pack | Import | Focus |
|------|--------|-------|
| `AwsSolutionsChecks` | `from cdk_nag import AwsSolutionsChecks` | AWS general best practices — IAM, encryption, logging, resilience |
| `ServerlessChecks` | `from cdk_nag import ServerlessChecks` | Serverless-specific rules — Lambda DLQ, tracing, memory, throttling |
| `NIST80053R5Checks` | `from cdk_nag import NIST80053R5Checks` | NIST 800-53 Rev 5 controls — the current standard used by many enterprises and federal workloads |
| `HIPAASecurityChecks` | `from cdk_nag import HIPAASecurityChecks` | HIPAA Security Rule — required when handling protected health information (PHI) |
| `PCIDSS321Checks` | `from cdk_nag import PCIDSS321Checks` | PCI DSS 3.2.1 — required when handling payment card data |

Running the HIPAA and PCI packs on top of NIST 800-53 R5 turned out to surface **zero net-new findings** on this stack — every rule that tripped was a same-named counterpart of a rule already raised (and suppressed) by NIST R5 (e.g. `HIPAA.Security-LambdaInsideVPC` duplicates `NIST.800.53.R5-LambdaInsideVPC`). The packs are kept enabled anyway so any future drift that introduces a HIPAA- or PCI-specific control gap is caught at synth time rather than in a later audit.

#### Other available rule packs

cdk-nag ships one additional pack that is not enabled in this project. It can be added by importing and applying it the same way as the packs above:

| Pack | Import | When to use |
|------|--------|-------------|
| `NIST80053R4Checks` | `from cdk_nag import NIST80053R4Checks` | NIST 800-53 Rev 4 — superseded by R5; only use if your compliance framework specifically requires R4. Running it alongside R5 duplicates findings on overlapping controls. |

Full rule documentation: [github.com/cdklabs/cdk-nag/blob/main/RULES.md](https://github.com/cdklabs/cdk-nag/blob/main/RULES.md)

#### Why cdk-nag and not cfn-guard or Checkov

cdk-nag is the most actively maintained CDK-native compliance gate and the only serious entrant in its category. Two adjacent tools come up in conversation, and both are deliberately *not* layered on top here:

| Tool | Architecture | Rule language | Suppression UX |
|------|--------------|---------------|----------------|
| **cdk-nag** (in use) | CDK Aspects, walks the construct tree at synth time | TypeScript / Python rules shipped in the cdk-nag package | Native CDK API — `NagSuppressions.add_resource_suppressions(...)` with reason strings, per-resource or stack-level, supports `apply_to_children=True` |
| **[cdk-validator-cfnguard](https://github.com/cdklabs/cdk-validator-cfnguard)** | `PolicyValidationPluginBeta1` plugin, runs the [cfn-guard](https://github.com/aws-cloudformation/cloudformation-guard) binary against the synthesized CFN JSON | [Guard DSL](https://github.com/aws-cloudformation/aws-guard-rules-registry) (declarative `.guard` files) | CFN template metadata (`Metadata.guard.SuppressedRules`) or CLI skip flags — no CDK-aware ergonomics |
| **[cdk-validator-checkov](https://github.com/cdklabs/cdk-validator-checkov)** | Same plugin pattern, wraps [Checkov](https://github.com/bridgecrewio/checkov) | Python rules (Checkov's 1,000+ rule database across many cloud providers) | Comments in synthesized JSON or CLI flags — same ergonomic gap as cfn-guard |

The two plugin wrappers are *architecturally newer* (the `PolicyValidationPluginBeta1` framework post-dates cdk-nag's Aspects pattern) but they're a different category: they bolt CFN-template scanners onto CDK after synthesis. They lose the CDK-aware view (construct tree, parent/child relationships, pre-synth attributes), and they introduce a second suppression model that doesn't compose with the cdk-nag suppressions already in this repo. The compliance content also overlaps heavily — NIST 800-53 R5, HIPAA Security, and PCI DSS 3.2.1 are all available in both ecosystems.

**When adding a plugin wrapper would make sense (not the case here):**

- You want one of the [aws-guard-rules-registry](https://github.com/aws-cloudformation/aws-guard-rules-registry) packs that cdk-nag doesn't ship (see below) — Control Tower Detective Guardrails, FedRAMP Moderate/High, CIS AWS Foundations Benchmark, ABS CCIG, Bank of Canada CCB, AWS Well-Architected, plus AWS Config-rule conversions.
- Your security team already authors policies in Guard DSL and you want the same `.guard` files enforced across CDK / SAM / Terraform-converted-to-CFN pipelines.
- You're validating CloudFormation generated *outside* CDK and want one tool for everything.

##### What's in `aws-guard-rules-registry`

The registry is AWS's open-source library of cfn-guard rule packs covering compliance frameworks, AWS service guardrails, and mass conversions of AWS Config managed rules into Guard DSL. The breadth is its main differentiator over cdk-nag — Config alone has hundreds of managed rules, all converted and grouped here. The downside is that mechanically-translated rules are noisier than hand-written equivalents, and the suppression model is template metadata rather than CDK-native API calls.

Notable rule packs (not exhaustive):

| Pack | Focus | cdk-nag overlap |
|------|-------|-----------------|
| **Control Tower Detective Guardrails** | The Detective Guardrails published by AWS Control Tower (root account, IAM, encryption, logging) | Partial — overlaps `AwsSolutionsChecks` on the IAM/encryption side; the Control Tower-specific account-level rules have no cdk-nag equivalent |
| **FedRAMP Moderate** | NIST 800-53 R4 controls applicable to FedRAMP Moderate baseline | High — most rules are NIST 800-53 R4/R5 controls already covered by cdk-nag's `NIST80053R5Checks` (FedRAMP Moderate is essentially a NIST R4 subset with FedRAMP-specific procedural overlay; the R5 pack covers most of the technical control gap) |
| **FedRAMP High** | NIST 800-53 R4 controls applicable to FedRAMP High baseline | High — same as FedRAMP Moderate plus stricter audit/logging/CloudTrail requirements (multi-region trail, log file validation, ≥1-year retention) |
| **CIS AWS Foundations Benchmark** | CIS hardening recommendations for AWS — IAM, CloudTrail, Config, monitoring | Moderate — IAM/encryption rules overlap; CIS-specific monitoring rules (CloudTrail metric filters, password policy, etc.) have no cdk-nag equivalent |
| **HIPAA Security** | HIPAA Security Rule controls | Full — cdk-nag's `HIPAASecurityChecks` covers the same controls |
| **PCI DSS 3.2.1** | PCI DSS controls | Full — cdk-nag's `PCIDSS321Checks` covers the same controls |
| **NIST 800-53 R4 / R5** | NIST controls (generic, not FedRAMP-specific) | Full — cdk-nag's `NIST80053R5Checks` (and `NIST80053R4Checks` if enabled) covers the same controls |
| **AWS Well-Architected** | Mappings against the Well-Architected Framework pillars | None — no cdk-nag equivalent |
| **AWS Config managed rule conversions** | Hundreds of Config managed rules translated to Guard DSL | Partial — many overlap NIST/HIPAA/PCI; the residual is broad-coverage AWS service rules cdk-nag doesn't ship |

##### What FedRAMP would actually buy on top of cdk-nag

FedRAMP Moderate and High are AWS's profiles for federal-government workloads. Both derive from NIST 800-53 — Moderate ≈ NIST 800-53 Moderate baseline; High ≈ NIST 800-53 High baseline plus extra audit/availability controls — so the bulk of the *technical* control content is already enforced by cdk-nag's `NIST80053R5Checks` pack. The FedRAMP-specific gaps in this reference architecture would be:

- **CloudTrail multi-region trail with log file validation** (FedRAMP requires this on the account; not enforced at the resource level by cdk-nag).
- **CloudTrail and CloudWatch Logs retention ≥ 1 year** (cdk-nag doesn't enforce a specific retention floor).
- **AWS Backup plan with cross-region copy** for stateful resources (cdk-nag flags `DynamoDBInBackupPlan` but doesn't require cross-region; this stack uses PITR instead and that suppression is documented above).
- **VPC flow logs on every VPC** (N/A here — this stack is fully serverless).
- **IAM password policy / MFA on root account** (account-level, not stack-level — neither cdk-nag nor cfn-guard can enforce this from a stack template).

Most of these are either N/A (no VPC, no root-account-level rules in a template), already implemented (encryption, logging), or covered procedurally rather than at the CFN level (CloudTrail and Backup are typically deployed account-wide, not per-stack). Adding the FedRAMP packs to *this* stack would surface ~zero net-new actionable findings; it would matter for an account intended for FedRAMP authorization, which a public reference architecture isn't.

For a serverless reference architecture already invested in the cdk-nag suppression model — `CDK_LAMBDA_SUPPRESSIONS`, `suppress_cdk_singletons`, per-construct `add_resource_suppressions` calls, the four-pack `apply_compliance_aspects` aspect — layering a second validator would be net-negative: duplicate findings, two allow-lists to maintain, no unique rule coverage gained. cdk-nag stays as the sole compliance gate.

#### Suppressions

Not every rule is appropriate for a sample application. Where a rule has been intentionally suppressed, the suppression lives in the stack file in either `NagSuppressions.add_stack_suppressions` (stack-wide) or `NagSuppressions.add_resource_suppressions`/`add_resource_suppressions_by_path` (targeted to a specific resource). Each entry includes a `reason` field explaining why it was suppressed rather than fixed.

Stack-level suppressions are reserved for findings that are genuinely stack-wide (e.g., no custom domain, no VPC by design). Everything else is suppressed at the resource level to keep the blast radius of each suppression as small as possible. CDK-managed singleton Lambdas (BucketDeployment provider, S3AutoDeleteObjects, AwsCustomResource) share a common suppression list defined in `hello_world/nag_utils.py` (`CDK_LAMBDA_SUPPRESSIONS`) and are targeted by their stable CDK construct IDs using `add_resource_suppressions_by_path`. `AwsSolutions-IAM5` suppressions on `HelloWorldFunction` use the `applies_to` parameter to scope them to specific wildcard actions and resources rather than suppressing all IAM5 findings on the role.

Every CloudWatch log group in the stack — including those for the BucketDeployment, AwsCustomResource, and S3 auto-delete provider Lambdas — is pre-created in CDK and passed to the consumer via `log_group=` (rather than the legacy `log_retention=` path that creates an unencrypted log group through the LogRetention singleton). This closes the `*-CloudWatchLogGroupEncrypted` finding for the singleton Lambdas without an Aspect-level suppression.

**What is encrypted with CMK:**
All CloudWatch log groups (Lambda, API Gateway access, API Gateway execution, WAF, auto-delete Lambda, BucketDeployment provider, AwsCustomResource provider) use AWS KMS customer-managed keys with annual key rotation enabled. The Lambda function's environment variables are also CMK-encrypted via `environment_encryption=` so the security boundary stays inside one key rather than splitting across the AWS-managed default and the project key. DynamoDB and the S3 frontend bucket use the same CMK pattern. Athena query results are written under `athena-results/` with SSE-KMS — Athena's PutObject calls override the bucket's SSE-S3 default on a per-object basis. The S3 access logging bucket itself uses SSE-S3 because the S3 log delivery service does not support KMS-encrypted target buckets. SSM parameters cannot use CMK (CloudFormation limitation — SecureString is not supported). AppConfig hosted configurations use AWS-managed keys (no CMK option in CDK).

Current suppressions across all stacks:

| Rule | Stack | Scope | Why suppressed |
|------|-------|-------|---------------|
| `AwsSolutions-APIG2` | Backend | Stack | Request validation not needed for sample app |
| `AwsSolutions-APIG3` | Backend | Stack | WAF applied at CloudFront, not directly on API Gateway |
| `AwsSolutions-APIG4` | Backend | Stack | No authorizer — auth is out of scope for this sample |
| `AwsSolutions-COG4` | Backend | Stack | No Cognito authorizer — same as APIG4 |
| `AwsSolutions-COG7` | Frontend | Resource (`RumIdentityPool`) | RUM requires unauthenticated guest credentials — anonymous visitors have no prior identity |
| `AwsSolutions-IAM4` | Backend, Frontend | Per-resource (CDK singletons + HelloWorldFunction) | CDK-managed Lambda roles use AWS managed policies; not configurable by the caller |
| `AwsSolutions-IAM5` | Backend, Frontend, WAF | Per-resource (with `applies_to`) | Wildcard permissions scoped to specific actions — X-Ray, KMS `GenerateDataKey*`/`ReEncrypt*`, CDK custom resource `Resource::*` |
| `AwsSolutions-L1` | Backend, Frontend | Per-resource (CDK singletons) | CDK-managed Lambda runtimes are not configurable; `HelloWorldFunction` uses Python 3.13 (latest) but cdk-nag rule not yet updated |
| `AwsSolutions-S1` | Frontend | Resource (log bucket) | The access log bucket itself — logging to itself would be circular |
| `AwsSolutions-CFR1` | Frontend | Stack | Geo restriction not required for sample app |
| `AwsSolutions-CFR4` | Frontend | Stack | Default CloudFront certificate — no custom domain for sample app |
| `Serverless-LambdaDLQ` | Backend, Frontend | Per-resource (CDK singletons) | CDK-managed Lambdas — DLQ is not configurable; `HelloWorldFunction` is synchronously invoked via API Gateway |
| `Serverless-LambdaDefaultMemorySize` | Backend, Frontend | Per-resource (CDK singletons) | CDK-managed singleton Lambdas — memory is not configurable; `HelloWorldFunction` uses explicit 256 MB |
| `Serverless-LambdaLatestVersion` | Backend, Frontend | Per-resource (CDK singletons) | CDK-managed Lambda runtimes are not configurable |
| `Serverless-LambdaTracing` | Backend, Frontend | Per-resource (CDK singletons only) | CDK-managed provider Lambdas do not expose tracing config; `HelloWorldFunction` passes natively |
| `Serverless-APIGWDefaultThrottling` | Backend | Stack | Custom throttling not configured for sample app |
| `CdkNagValidationFailure` | Backend | Stack | Intrinsic function reference prevents `Serverless-APIGWStructuredLogging` from validating |
| `NIST.800.53.R5-LambdaConcurrency` | Backend, Frontend | Per-resource (CDK singletons) | CDK-managed singleton Lambdas — concurrency is not configurable |
| `NIST.800.53.R5-LambdaDLQ` | Backend, Frontend | Per-resource (CDK singletons) | CDK-managed Lambdas — DLQ is not configurable; `HelloWorldFunction` is synchronously invoked |
| `NIST.800.53.R5-LambdaInsideVPC` | Backend, Frontend | Per-resource (CDK singletons) | CDK-managed singleton Lambdas — VPC is not configurable |
| `NIST.800.53.R5-IAMNoInlinePolicy` | Backend, Frontend, WAF | Per-resource | CDK-generated inline policies on singleton service roles — not directly configurable; also suppressed on `RumUnauthenticatedRole` where the single least-privilege `rum:PutRumEvents` policy is tightly bound to the role's one purpose |
| `NIST.800.53.R5-APIGWAssociatedWithWAF` | Backend | Stack | WAF applied at CloudFront, not directly on API Gateway |
| `NIST.800.53.R5-APIGWSSLEnabled` | Backend | Stack | Client-side SSL certificates not required for sample app |
| `NIST.800.53.R5-APIGWCacheEnabledAndEncrypted` | Backend | Stack | API Gateway cache cluster intentionally disabled — smallest size is ~$14/month for a sample app, and caching `GET /hello` would serve stale values across SSM parameter and AppConfig feature-flag changes |
| `NIST.800.53.R5-DynamoDBInBackupPlan` | Backend | Stack | AWS Backup plan not configured; PITR is enabled for point-in-time recovery |
| `NIST.800.53.R5-S3BucketLoggingEnabled` | Frontend | Resource (log bucket) | The access log bucket itself — logging to itself would be circular |
| `NIST.800.53.R5-S3BucketReplicationEnabled` | Frontend | Stack + Resource | Static assets are redeployable; replication not needed |
| `NIST.800.53.R5-S3BucketVersioningEnabled` | Frontend | Stack + Resource | Static assets are redeployable via `cdk deploy`; versioning not needed |
| `NIST.800.53.R5-S3DefaultEncryptionKMS` | Frontend | Resource (log bucket only) | S3 log delivery service does not support KMS target buckets; SSE-S3 required |
| `HIPAA.Security-*` | — | Mirrors the NIST R5 rows above | Every HIPAA Security finding duplicates a NIST R5 counterpart with the same reason (e.g. `HIPAA.Security-LambdaInsideVPC` ↔ `NIST.800.53.R5-LambdaInsideVPC`); suppressions are in the same locations as their NIST R5 twins |
| `PCI.DSS.321-*` | — | Mirrors the NIST R5 rows above | Same as HIPAA — each PCI finding duplicates a NIST R5 counterpart (e.g. `PCI.DSS.321-S3BucketVersioningEnabled` ↔ `NIST.800.53.R5-S3BucketVersioningEnabled`) and is suppressed alongside it |

Rules that were previously suppressed and have since been implemented are removed from this list. If you add a suppression, include a clear `reason` and consider whether the finding represents a genuine gap worth addressing in production.

#### CDK context flags (`cdk.json`)

CDK uses context flags to opt into newer behaviors that would otherwise be breaking changes. Each flag controls a specific aspect of how CDK generates CloudFormation templates. The `cdk.json` context block is divided into three groups:

**Original flags (from project creation):** `@aws-cdk/aws-lambda:recognizeLayerVersion`, `@aws-cdk/core:checkSecretUsage`, and `@aws-cdk/core:target-partitions`. These shipped with the CDK Python template and have been active since the first deploy.

**Safe flags (added later, zero template drift):** 12 additional flags that CDK 2.248.0 recommends but that produce no CloudFormation changes against the deployed stacks. These were validated by running `cdk diff --all` with each flag enabled — zero diffs across all three stacks. They cover improved validation, metadata collection, unique resource naming, and scoped KMS/DynamoDB/Lambda/CloudFront/API Gateway behaviors.

**Template-changing flags (deployed):** 7 flags that produce real CloudFormation mutations. These were validated with `cdk diff --all`, deployed, and confirmed with integration tests:

| Flag | Effect |
|---|---|
| `@aws-cdk/core:enablePartitionLiterals` | Hardcodes `arn:aws:` partition ARNs instead of `{"Ref": "AWS::Partition"}` |
| `@aws-cdk/aws-s3:serverAccessLogsUseBucketPolicy` | Migrates S3 access-log delivery from legacy ACL to bucket policy |
| `@aws-cdk/aws-s3:createDefaultLoggingPolicy` | Adds default logging policy to S3 buckets |
| `@aws-cdk/aws-s3:publicAccessBlockedByDefault` | Adds explicit public access block configuration |
| `@aws-cdk/custom-resources:logApiResponseDataPropertyTrueDefault` | Sets `logApiResponseData` to `false` by default for custom resources |
| `@aws-cdk/aws-lambda:createNewPoliciesWithAddToRolePolicy` | Creates separate IAM policy resources per `addToRolePolicy` call for finer-grained control |
| `@aws-cdk/aws-iam:minimizePolicies` | Consolidates IAM policy statements for tighter, least-privilege policies |

**Skipped flag:** `@aws-cdk/aws-apigateway:disableCloudWatchRole` is intentionally **not** enabled. It removes the account-level CloudWatch role for API Gateway, which is incompatible with NIST 800-53 R5 — execution logging (`AwsSolutions-APIG6` / `APIGWExecutionLoggingEnabled`) requires that role.

#### Commit `cdk.context.json`

The `cdk.context.json` file — distinct from the `cdk.json` context block above — caches environmental lookups (AZs, AMI IDs, hosted zones, SSM parameter values) that CDK resolves at synth time. **It is committed to the repo on purpose.** Per the CDK best practice ([AWS context guide](https://docs.aws.amazon.com/cdk/v2/guide/context.html), [AWS guide](https://docs.aws.amazon.com/cdk/v2/guide/best-practices.html), [AWS Heroes article](https://dev.to/aws-heroes/aws-cdk-deployment-best-practices-3doo)), the same cached values must be used across every synth of a given commit, or templates will drift depending on whose machine ran the build.

If `cdk.context.json` were gitignored, the first synth after a fresh clone would re-resolve every lookup against live AWS APIs and rewrite the file — which means two engineers synthesizing the same commit might produce different CloudFormation templates, and CI might produce yet a third. Committing the file pins the values so the synth is deterministic per commit. To refresh a cached value intentionally, run `cdk context --reset <key>` and commit the resulting diff.

### Commit message convention

This project follows [Conventional Commits](https://www.conventionalcommits.org/). Format:

```
type: short description
```

| Type | When to use |
|---|---|
| `feat` | A new feature |
| `fix` | A bug fix |
| `docs` | Documentation changes only |
| `chore` | Maintenance tasks that don't affect functionality (lock files, Makefile, LICENSE) |
| `ci` | Changes to CI/CD configuration (GitHub Actions, pre-commit) |
| `test` | Adding or updating tests |
| `refactor` | Code restructuring that neither fixes a bug nor adds a feature |
| `build` | Changes to the build system or dependencies |

### pyproject.toml configuration

All tool configuration is consolidated in `pyproject.toml`. Here is a summary of the key settings in each section:

#### `[tool.ruff]`

| Setting | Value | Purpose |
|---|---|---|
| `target-version` | `py313` | Enables Python 3.13-specific lint rules and syntax modernization |
| `line-length` | `120` | Maximum line length enforced by the formatter |
| `dummy-variable-rgx` | `^(_+\|...)$` | Allows `_`-prefixed variables to be unused without triggering a lint warning |

#### `[tool.ruff.lint]`

Ruff is configured with a broad set of rule groups. Each group targets a specific class of issue:

| Code | Plugin | What it catches |
|---|---|---|
| `E` / `W` | pycodestyle | Style errors and warnings |
| `F` | pyflakes | Undefined names, unused imports |
| `I` | isort | Import ordering |
| `C` | flake8-comprehensions | Inefficient list/dict/set comprehensions |
| `B` | flake8-bugbear | Likely bugs and design issues |
| `S` | flake8-bandit | Security anti-patterns |
| `UP` | pyupgrade | Modernize syntax to the target Python version |
| `SIM` | flake8-simplify | Suggest simpler code patterns |
| `RUF` | ruff-specific | Ruff's own opinionated rules |
| `T20` | flake8-print | Catches `print()` calls — use Powertools Logger instead |
| `PT` | flake8-pytest-style | Enforces pytest conventions (fixtures, raises, etc.) |
| `N` | pep8-naming | Naming conventions (snake_case, PascalCase, SCREAMING_SNAKE) |
| `RET` | flake8-return | Unnecessary `else` after `return`, redundant return values |

#### `[tool.mypy]`

| Setting | Purpose |
|---|---|
| `warn_return_any` | Warns when a typed function returns `Any`, which often masks missing type coverage |
| `warn_unused_ignores` | Warns when a `# type: ignore` comment is no longer needed, preventing stale suppression comments |
| `disallow_untyped_defs` | Every function must have complete type annotations |
| `check_untyped_defs` | Type-checks function bodies even if the function itself lacks annotations |
| `no_implicit_optional` | `f(x: str = None)` does not implicitly mean `Optional[str]` — must be explicit |
| `ignore_missing_imports` | Suppresses errors for third-party packages without type stubs (e.g. aws-lambda-powertools) |
| `show_error_codes` | Prints `[error-code]` next to each error — required to write precise `# type: ignore[code]` comments |

#### `[tool.pylint.design]`

Structural complexity thresholds. Pylint fails if any function or class exceeds these limits. Complexity is also enforced by the xenon pre-commit hook (which uses radon under the hood).

| Threshold | Value | What it limits |
|---|---|---|
| `max-args` | 8 | Parameters per function |
| `max-locals` | 25 | Local variables per function |
| `max-returns` | 6 | Return statements per function |
| `max-branches` | 12 | Branches (if/for/while/try) per function |
| `max-statements` | 50 | Statements per function body |
| `max-attributes` | 10 | Instance attributes per class |

#### `[tool.pytest.ini_options]`

Key flags in `addopts`:

| Flag | Purpose |
|---|---|
| `-ra` | Prints a short summary of all non-passed tests (failures, errors, skipped) at the end |
| `--cov=lambda` | Measures coverage for the `lambda/` directory |
| `--cov-branch` | Tracks branch coverage — not just whether a line ran, but whether all conditional paths did |
| `--cov-fail-under=100` | Fails the run if total coverage drops below 100% |
| `--no-cov-on-fail` | Skips coverage reporting when tests fail (avoids misleading partial results) |
| `-n auto` | Runs tests in parallel across all available CPU cores (pytest-xdist) |

`log_cli = true` and `log_cli_level = "WARNING"` stream log output in real time during the test run, showing only WARNING and above to reduce noise.

## CI/CD

### GitHub Actions

Four workflows are configured:

| Workflow | Trigger | What it does |
|---|---|---|
| **CI** | Push / PR to `main` | Three jobs: pre-commit hooks (`quality`), pytest unit tests (`test`), CDK synth + stack assertion tests (`cdk-check`) |
| **Docs** | Push to `main` | Builds Zensical docs and deploys to GitHub Pages |
| **Dependency Audit** | Every Monday 9am UTC | Runs `pip-audit` against each dependency group exported from `uv.lock` |
| **Dependabot Auto-merge** | Dependabot PRs | Approves and auto-merges patch/minor updates from `github_actions`, `uv`, and `pip` ecosystems when CI passes; majors stay manual. Gated on PR opener (not latest pusher) so maintainer pushes via `make deps-merge` don't disarm it |

All three CI jobs must pass before anything can merge to `main` (branch protection).

The CI uses [uv](https://docs.astral.sh/uv/) to install dependencies from the single `uv.lock` at the repo root. Each job syncs only the groups it needs, passing `--locked` so the job fails loudly if `pyproject.toml` and `uv.lock` are out of sync rather than silently regenerating the lock mid-build:
- `quality` job: `uv sync --locked --group cdk --group test --group lint --group docs` (runs pre-commit hooks, which cover every tool across every group)
- `test` job: `uv sync --locked --only-group lambda --only-group test` into `.venv-lambda` via `UV_PROJECT_ENVIRONMENT=.venv-lambda` — isolates Powertools' `attrs>=26` from CDK's `attrs<26`
- `cdk-check` job: `uv sync --locked --group cdk --group test` (CDK + pytest) + CDK CLI via `npm install -g aws-cdk`

Each job ends with `uv cache prune --ci` before `setup-uv`'s post-step saves the cache. `--ci` drops pre-built wheels (cheap to redownload) and keeps only the expensive-to-rebuild source distributions, shrinking the cached artifact `actions/cache` persists between runs.

The `cdk-check` job runs `cdk synth` to catch unsuppressed cdk-nag findings, then runs `tests/cdk/test_stacks.py` which uses `aws_cdk.assertions.Template` to verify key security properties of each synthesized stack (KMS encryption, DynamoDB PITR, API Gateway caching, CloudFront TLS version, etc.). These tests live under `tests/cdk/` rather than `tests/unit/` so the unit-test autouse fixture (which mocks Powertools internals) does not apply — the cdk-check job intentionally omits Powertools to avoid the `attrs` version conflict. Asset bundling (Docker) is skipped via the `aws:cdk:bundling-stacks` context key so the job runs without Docker build time.

#### Dependabot

Dependabot is configured in `.github/dependabot.yml` to check for updates every Monday across two ecosystems:

| Ecosystem | What it checks | Auto-merge? |
|-----------|----------------|-------------|
| `github-actions` | Workflow YAML for newer action versions (e.g. `actions/checkout@v4` → `v5`) | Patch and minor updates auto-merge via the `dependabot-auto-merge` workflow once CI passes; major updates require human review |
| `uv` | `pyproject.toml` + `uv.lock` — every dependency group in one lock file, regenerated atomically | Patch and minor updates auto-merge once CI passes; major updates require human review. Updates that touch the `lambda` runtime group (e.g. `boto*`, `aws-lambda-powertools`) need a one-time `make lock` push from a maintainer before CI greens — see "Python (`uv`) updates" below |

The `dependabot-auto-merge` workflow runs on every Dependabot PR and approves + arms auto-merge when **all** of the following hold:
1. The PR's *opener* is Dependabot — `github.event.pull_request.user.login == 'dependabot[bot]'`. Gating on the PR opener (not `github.actor`, the latest pusher) keeps the workflow eligible across maintainer follow-up pushes — for example, the `make deps-merge` script's `make lock` regenerations on Dependabot branches.
2. `dependabot/fetch-metadata` reports the ecosystem as `github_actions`, `uv`, or `pip`. Both `uv` and `pip` are accepted because Dependabot's `uv` ecosystem reports `pip` in `fetch-metadata` output (uv builds on pip's resolver, so the metadata action surfaces the underlying tool rather than the configured ecosystem).
3. `dependabot/fetch-metadata` reports the update type as `version-update:semver-patch` or `version-update:semver-minor`.

The metadata action is configured with `skip-commit-verification: true`. Without it, `fetch-metadata` refuses to proceed when the PR's latest commit isn't signed by Dependabot's GPG key — and after a `make deps-merge` push, the latest commit is the maintainer's. The job-level PR-opener gate above already protects the author identity, so dropping the commit-signature layer is what makes the maintainer-push flow work.

Once auto-merge is armed, GitHub itself waits for required status checks (the `quality`, `test`, and `cdk-check` jobs) to pass and then merges the PR. If any check fails, the PR stays open with auto-merge enabled but unsatisfied, surfacing the failure rather than silently merging.

Major updates (e.g. `actions/upload-artifact@v4 → @v7`, or an aws-cdk major bump) intentionally fall through to manual review because cooldowns catch *malicious* releases but not intentional API breaks, and majors warrant a human glance at the changelog.

If CI fails on a Dependabot PR (any ecosystem), it stays open for investigation rather than merging.

**Repo setting required for auto-merge to work.** GitHub repositories ship with GitHub Actions blocked from approving pull requests by default. Until this is changed, the `dependabot-auto-merge` workflow will fail with `GraphQL: GitHub Actions is not permitted to approve pull requests` and even GitHub Actions PRs must be merged manually. Enable it once under **Settings → Actions → General → "Allow GitHub Actions to create and approve pull requests"**. This only needs to be done a single time per repo. Leave the **Workflow permissions** radio set to **Read repository contents and packages permissions** (the safer default) — every workflow in this repo declares its own explicit `permissions:` block, so the elevated `Read and write permissions` default is not needed. See "Least-privilege workflow permissions" in the supply-chain hardening section below.

##### Python (`uv`) updates

All Python dependencies live in a single `pyproject.toml` with five groups (`lambda`, `cdk`, `test`, `lint`, `docs`) and resolve into one `uv.lock`. The `lambda` and `cdk` groups are declared mutually exclusive via `[tool.uv.conflicts]` so uv records two valid resolutions for the `attrs` conflict inside the same lock file and installs the right one into each venv at sync time — see the "attrs version conflict" note in Design decisions.

**One ecosystem entry covers everything.** Dependabot's `uv` ecosystem regenerates `pyproject.toml` + `uv.lock` together in a single PR, so the old pip-tools constraint chain (three `.in` files compiled in order, downstream-recompile dance when shared packages moved) no longer exists. Every group is updated atomically from a single lock regeneration.

**Grouped updates collapse the PR volume.** The uv ecosystem entry in `dependabot.yml` defines `groups:` that bundle related packages into a single PR — `aws-cdk*`/`constructs` update together, `aws-*`/`boto*`/`botocore`/`s3transfer` update together, `pytest` and its plugins update together, docs tooling together, linting tooling together, all remaining patch bumps roll into one weekly "patches" PR. Within a group, Dependabot regenerates `uv.lock` in one shot, so the cross-package version skew that would otherwise hit boto3+botocore+aws-lambda-powertools (when each is bumped in isolation) cannot happen. Major and minor bumps outside these groups still get individual PRs so each changelog can be reviewed on its own.

**Merge flow.** A Dependabot uv PR updates `pyproject.toml` + `uv.lock` in lockstep. CI re-syncs each job's venv from the new lock and runs the test suite against both resolutions (CDK venv in `cdk-check`, Lambda venv in `test`). Patch and minor PRs auto-merge once CI greens; majors stay open for human review. There is no per-tier recompile step, no Case 1/2/3 handling — the single lock file is always internally consistent.

**When a `make lock` push is required.** The packaged Lambda installs from a flat `lambda/requirements.txt` at deploy time (CDK's `PythonFunction` construct expects one). That file is generated from `uv.lock` via `uv export --only-group lambda --no-emit-project` and is treated as a build artifact. The `quality` job in CI re-runs that export and fails if the committed file drifts from what the current `uv.lock` would produce — this is what stops a stale `requirements.txt` from shipping to production Lambda when Dependabot bumps a runtime dep.

Dependabot updates `uv.lock` but **not** the exported `requirements.txt`, so any uv PR that touches the `lambda` group (`aws-lambda-powertools`, `boto*`, `botocore`, `aws-encryption-sdk`, etc.) will fail the drift check on first CI run. The PR sits open with auto-merge already armed, waiting on the failing check.

The simplest way to unblock these is `make deps-merge`, which automates the rebase + `make lock` + push + arm-auto-merge loop across every open Dependabot PR sequentially:

```bash
make deps-merge            # process every open Dependabot PR
make deps-merge PR=42      # process only PR #42 (skips the wait between PRs)
```

For each PR the script does: sync main → checkout the PR branch → rebase onto main → run `make lock` → run `uv run ruff format .` (catches reformatter drift from a `ruff` bump) → commit and force-push if there's content → arm `gh pr merge --auto --squash`. When processing all, it then waits for the PR to merge before moving to the next so each subsequent PR rebases onto a main that already includes its predecessors (sequential is required because every `make lock` regenerates `uv.lock` and concurrent processing would clobber predecessors at squash-merge time). PRs whose rebase conflicts, whose `make lock` fails, or whose CI surfaces a real failure are skipped with a clear log line — the script never `--admin`-bypasses failing checks.

Equivalent manual flow if you'd rather drive each PR by hand:

```bash
gh pr checkout <pr-number>     # check out the Dependabot branch
make lock                      # regenerates lambda/requirements.txt from uv.lock
git add lambda/requirements.txt
git commit -m "chore: regenerate lambda/requirements.txt"
git push
```

In either case the push triggers a fresh CI run; once it greens, the auto-merge that was already armed fires and GitHub merges the PR. No second approval click is needed.

PRs that only touch non-lambda groups (`aws-cdk`, `pytest`, `docs`, `linting`) do not drift `lambda/requirements.txt` and merge themselves with no maintainer involvement.

This compromise — auto-merge for everything, with a manual `make lock` push for lambda-runtime updates — was chosen because the alternative paths each carry their own cost: a Personal Access Token would let the workflow regenerate and push automatically (GitHub's `GITHUB_TOKEN` deliberately does not retrigger workflows on its own pushes, so a workflow-managed regeneration would stall auto-merge forever), but adds a long-lived credential to manage and rotate. Treating `lambda/requirements.txt` as a fully-generated artifact (regenerated at synth time, not committed) would remove the drift check entirely, but adds a build step before every `cdk synth` and forfeits the ability to inspect the exact pinned dependency set at any commit. The current setup keeps both the credential surface and the build pipeline unchanged at the cost of one occasional manual step.

Run `make lock` locally any time you change `pyproject.toml` so both files move together — never hand-edit `lambda/requirements.txt`.

**Useful Dependabot commands.** When a PR becomes stale relative to `main` (shown as `BEHIND` in `gh pr view`), comment `@dependabot rebase` on the PR to trigger a fresh rebase and CI run. Other useful commands include `@dependabot recreate` (regenerates the PR from scratch) and `@dependabot ignore this version` (skip a specific release). The full command list is at <https://docs.github.com/en/code-security/dependabot/working-with-dependabot/managing-pull-requests-for-dependency-updates>.

##### Supply-chain hardening

This repo layers six defenses against the supply-chain attack patterns described in GitGuardian's [*Renovate & Dependabot: the new malware delivery system*](https://blog.gitguardian.com/renovate-dependabot-the-new-malware-delivery-system/):

**1. Release cooldown.** Every ecosystem in `dependabot.yml` carries a `cooldown:` block that makes Dependabot wait a few days after a release before opening a PR. Fresh releases are the window in which malicious versions (tag hijacks, compromised maintainer accounts, typo-squats, the `xz-utils`/`nx`/`tj-actions/changed-files` class of incidents) typically get caught and yanked. The tiered schedule is 3 days for patches, 7 for minors, 14 for majors — larger jumps wait longer to let bugs surface.

**2. SHA-pinned GitHub Actions.** Every `uses:` reference in `.github/workflows/` is pinned to a 40-character commit SHA with the version in a trailing comment:

```yaml
- uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd  # v6.0.2
```

Tag references are mutable — a compromised maintainer account can rewrite `v6` to point at malicious code, and every workflow that said `@v6` instantly runs the malicious version on the next trigger. This is exactly what happened to `tj-actions/changed-files` in March 2025, where attackers rewrote the `v35` and `v38` tags to exfiltrate CI secrets from thousands of repos within an hour. SHA pins are immutable — a re-tagged release becomes a new commit, and our pin simply ignores it until a human updates it. Dependabot still opens update PRs for SHA-pinned actions; the diff replaces both the SHA and the version comment in one shot.

**3. Hash-locked installs.** `uv.lock` records a SHA256 for every wheel and sdist it resolves, and `uv sync` refuses to install anything whose on-disk hash does not match. The `lambda/requirements.txt` artifact exported from the lock for the deployed Lambda is generated with hashes too. Even if an attacker uploads a malicious version under an existing version number (e.g. after yanking the legitimate one), the install refuses it because the hash does not match.

**4. Restricted auto-merge.** Auto-merge is scoped to patch and minor updates only — never majors. Both ecosystems (GitHub Actions and uv) qualify, but uv updates that touch the `lambda` runtime group fail CI's `lambda/requirements.txt` drift check and require a one-time `make lock` push before they can merge (see "Python (`uv`) updates" above). The drift check is what makes uv auto-merge safe: it physically blocks any uv lock change from reaching `main` until the exported requirements file that the production Lambda actually installs from has been regenerated and reviewed alongside the lock. Combined with the cooldown (#1), the SHA pins (#2), and the hash-locked installs (#3), a malicious uv release cannot land on `main` without surviving 3–14 days of cooldown, hash verification at install time, and (for runtime deps) the human-driven `make lock` step.

**5. Local cooldown on `make upgrade`.** Dependabot's cooldown protects every dependency change that lands via a PR, but `make upgrade` runs locally on a developer laptop and bypasses Dependabot entirely. The Makefile mirrors the same defense by passing uv's `--exclude-newer` flag to `uv lock --upgrade`, filtering out any package version uploaded after a cutoff computed from `COOLDOWN_DAYS` (default 7). Override at the command line if you need to pull a fresher version: `make upgrade COOLDOWN_DAYS=1`. The cooldown is intentionally **only** applied to `upgrade`, not `lock`. `lock` reproduces decisions already encoded in `pyproject.toml` and the existing `uv.lock` — it cannot introduce a brand-new version, so cooldown is unnecessary and would actively conflict with freshly-bumped pins. `upgrade` is the only target where new versions enter the project and is the only place a fresh malicious release can land. Disable entirely with `COOLDOWN_DAYS=0`.

**6. Least-privilege workflow permissions.** Every workflow under `.github/workflows/` declares an explicit `permissions:` block scoped to exactly what it needs, and the repo-level default (`Settings → Actions → General → Workflow permissions`) is set to `read` rather than `write`. The combination means that if a malicious dependency runs inside a CI job, the `GITHUB_TOKEN` it sees has the minimum authority necessary — `ci.yml`, `dependency-audit.yml`, and `docs.yml`'s `build` job all run with `contents: read` and nothing else. Only two places escalate beyond read: `docs.yml`'s `deploy` job adds `pages: write` + `id-token: write` for GitHub Pages OIDC deployment, and `dependabot-auto-merge.yml` adds `contents: write` + `pull-requests: write` so it can approve and merge Dependabot PRs (and is gated on `github.actor == 'dependabot[bot]'` plus a patch/minor update-type check). Permissions are declared at the **job** level wherever a workflow has heterogeneous needs (docs.yml's build vs deploy) and at the **workflow** level otherwise. The repo-level `read` default acts as defense-in-depth: if any future workflow is added without an explicit `permissions:` block, it inherits `read` instead of write-everything. The repo separately keeps `can_approve_pull_request_reviews` enabled — that toggle is independent of the default and is required for the auto-merge workflow's `gh pr review --approve` call to succeed.

**What's intentionally not implemented: honeytokens.** The article also recommends honeytokens as a detection layer, which this repo deliberately skips. A honeytoken is a fake credential — typically an AWS access key or GitHub token that looks completely real but is registered with a canary service (Thinkst, GitGuardian, AWS canarytokens.org). Nothing legitimate ever authenticates with it, so any use is by definition either an attacker or a misconfigured script that shouldn't exist. When someone tries it, the canary service alerts the owner with the timestamp, source IP, and which specific token fired — that last detail reveals *where* the attacker read it from (CI logs, a specific repo clone, a leaked `.env`, etc.), which makes incident scoping much faster.

Honeytokens are specifically effective against the CI-exfiltration pattern this hardening section guards against: a malicious dependency running inside a CI runner typically sweeps environment variables and filesystem paths for anything that looks like a credential and pipes it to an attacker-controlled endpoint. If a honeytoken is reachable from the same runner (e.g. planted in `.github/workflows/` as a fake `DEPLOY_KEY` secret, or embedded in a committed `.env.example` file), the exfiltration tooling scoops it up alongside the real credentials and the alert fires within seconds of the breach — weeks before an attacker would otherwise tip their hand by actually using the stolen credentials.

Everything else in this section is *prevention*; honeytokens are *detection*. They do not stop attacks, they tell you an attack happened. The reason this repo skips them is not value but operational overhead: honeytokens are easy to plant (canarytokens.org generates one in about 30 seconds) but the alert routing is the real cost. You need somewhere for the alert to land (email you actually read, a Slack channel you watch, a pager), and you need a runbook for what to do when it fires. For a solo reference project with no production data, no customer PII, and CI secrets that can be rotated in 30 seconds, that plumbing is not worth standing up. For any repo running real workloads — production AWS accounts, customer data, deploy keys to anything that ships to users — honeytokens are a high-signal, low-noise addition that pays for itself the first time one fires, and GitGuardian's free `ggshield` CLI ships a generator that integrates with their alert console.

**What's intentionally not implemented: machine inventory.** The article recommends maintaining an inventory of every machine that runs unattended `pip install` / `npm install`, because each one is an exfiltration surface if a malicious package lands. At org scale this matters — dozens of build boxes, dev laptops with auto-updaters, and shared CI fleets each multiply the blast radius. For this repo the inventory is two items: one developer laptop and GitHub-hosted runners (which are ephemeral and Microsoft's problem). Writing that down as a doc would add a file to keep in sync with zero defensive value at this scale, so it is deliberately omitted. If this pattern is extended to a team or org repo, the inventory should be revisited and probably codified.

**What's intentionally not implemented: a post-compromise rotation runbook.** The article also recommends a written checklist for "assume a malicious dep ran in CI — what do I rotate, in what order, how fast?" The value of writing it down ahead of time is that during an actual incident, you are panicked and will forget steps. This repo deliberately skips the runbook because it currently has nothing to rotate: the only secret in use is the auto-scoped per-run `GITHUB_TOKEN`, which expires when each job ends. There are no AWS deploy keys, no API tokens, no `secrets.*` entries with real credentials. A runbook today would read "revoke nothing, there's nothing to revoke." The moment a real secret is added to this repo (an AWS deploy role, a Sentry DSN, anything in `secrets.*`), this section should become a runbook with the rotation order, who to notify, and the commands to run.

##### Why not Renovate?

[Renovate](https://docs.renovatebot.com/) is an alternative to Dependabot that historically handled multi-file Python setups more gracefully. With the migration to a single `uv.lock`, most of that advantage no longer applies — Dependabot's uv ecosystem regenerates the lock atomically in one PR. The remaining differences are at the edges:

- Renovate supports richer grouping rules (e.g., regex-based bundling, per-manager overrides) and auto-merge rules scoped by update type (patch/minor/major) for Python PRs.
- Renovate runs as a GitHub App, so it is zero-infrastructure.

This project uses Dependabot because it is the GitHub-native default and already integrated into the repo for GitHub Actions updates. The `uv` ecosystem support closed the gap that previously made Renovate compelling for the pip-tools constraint chain. The remaining Renovate edge is its [post-upgrade tasks](https://docs.renovatebot.com/configuration-options/#postupgradetasks) feature, which lets the bot regenerate `lambda/requirements.txt` in the same PR commit as the `uv.lock` bump (eliminating the manual `make lock` push that lambda-runtime updates currently require). For this repo the trade-off didn't pencil out — keeping a single bot integrated with GitHub-native auto-merge is simpler than running two — but a higher-volume production repo that wanted fully unattended uv merges would find Renovate's post-upgrade tasks worth installing for that one feature.

## Project dependencies

Dependencies are managed with [uv](https://docs.astral.sh/uv/) in [project mode](https://docs.astral.sh/uv/concepts/projects/). All direct dependencies live in `pyproject.toml` under five [dependency groups](https://peps.python.org/pep-0735/) (PEP 735); the fully resolved graph — with hashes for every wheel and sdist — lives in a single `uv.lock` at the repo root.

| Group | Purpose | Installed into |
|---|---|---|
| `lambda` | Lambda runtime dependencies (Powertools, boto3, X-Ray SDK) | `.venv-lambda` |
| `cdk` | CDK core, constructs, cdk-nag, cdk-monitoring-constructs | `.venv` |
| `test` | pytest, coverage, xdist, mock, html, timeout, randomly | both venvs |
| `lint` | ruff, mypy, pylint, bandit, radon, xenon, pre-commit, pip-audit, boto3-stubs | `.venv` |
| `docs` | zensical + mkdocstrings for the generated site | `.venv` |

**Two venvs, one lock file.** The `lambda` and `cdk` groups are declared mutually exclusive in `pyproject.toml`:

```toml
[tool.uv]
conflicts = [[{group = "lambda"}, {group = "cdk"}]]
```

This lets uv record **both** valid resolutions of the `attrs` conflict (25.4.0 for CDK, 26.1.0 for Powertools) inside a single `uv.lock` and install the correct one into each venv at sync time. The CDK-side venv lives at `.venv` and the Lambda-side venv lives at `.venv-lambda`; the Makefile switches between them by setting `UV_PROJECT_ENVIRONMENT=.venv-lambda` for Lambda-side commands. `make install` provisions both.

**`lambda/requirements.txt` is a generated artifact.** CDK's `PythonFunction` construct bundles Lambda dependencies from a flat `requirements.txt` at deploy time, so `make lock` runs `uv export --only-group lambda --no-emit-project --format requirements.txt` after every `uv lock` to keep the file in step. Never hand-edit it.

To regenerate the lock after editing `pyproject.toml`:

```bash
make lock    # runs `uv lock` + `uv export` for the Lambda runtime file
```

To upgrade all dependencies:

```bash
# Default: blocks any version uploaded to PyPI in the last 7 days (cooldown defense)
make upgrade

# Override the cooldown window
make upgrade COOLDOWN_DAYS=14   # stricter — only versions older than 14 days
make upgrade COOLDOWN_DAYS=1    # near-immediate — only the last 24 hours filtered
make upgrade COOLDOWN_DAYS=0    # disable cooldown entirely
```

`make upgrade` passes uv's `--exclude-newer` flag to `uv lock --upgrade` so brand-new PyPI releases (the window in which malicious versions typically get caught and yanked) are not pulled into the lockfile. See "Local cooldown on `make upgrade`" in the supply-chain hardening section for the full rationale.

To install or re-sync both venvs from the lock:

```bash
make install    # provisions .venv (CDK + test + lint + docs) and .venv-lambda (lambda + test)
```

`uv sync` only installs what is locked — no stale packages, no drift. Adding a dependency is `uv add <pkg> --group <group>` (updates `pyproject.toml` and `uv.lock` in one step).

### `lambda` group — Lambda runtime

| Library | Purpose |
|---|---|
| `aws-lambda-powertools[all]` | Full Powertools suite: Logger, Tracer, Metrics, Event Handler, Idempotency, Parameters, Feature Flags, Validation, and Event Source Data Classes |
| `aws-xray-sdk` | Required by Powertools Tracer for X-Ray instrumentation |
| `boto3` | AWS SDK, version-locked in the deployment package to avoid depending on the Lambda runtime's bundled version |

### `cdk` group — CDK and infrastructure

| Library | Purpose |
|---|---|
| `aws-cdk-lib` | Core CDK framework for defining AWS infrastructure |
| `constructs` | Base construct library used by CDK |
| `aws-cdk-aws-lambda-python-alpha` | `PythonFunction` construct that bundles Lambda dependencies in a container |
| `cdk-monitoring-constructs` | Auto-generates CloudWatch dashboards and alarms for Lambda and API Gateway |
| `cdk-nag` | Runs AWS Solutions security checks against the CDK stack during synthesis |

### `lint` group — Linting and static analysis

| Library | Purpose |
|---|---|
| `ruff` | Fast Python linter and formatter (configured in `pyproject.toml`) |
| `mypy` | Static type checker (configured in `pyproject.toml`) |
| `pylint` | Design and complexity checks complementing ruff (configured in `pyproject.toml`) |
| `bandit` | Security-focused static analysis (configured in `.bandit`) |
| `radon` | Computes code complexity metrics (cyclomatic complexity, maintainability index) |
| `xenon` | Enforces complexity thresholds, fails if code exceeds limits |
| `pip-audit` | Scans exported requirements files for known vulnerabilities |
| `pre-commit` | Git hook framework that runs linters and formatters on each commit |
| `boto3-stubs` | Type stubs for boto3, enables mypy to type-check AWS SDK calls |

### `docs` group — Documentation site

| Library | Purpose |
|---|---|
| `zensical` | Static-site documentation generator (MkDocs-Material successor), builds HTML from markdown in `docs/` (configured in `zensical.toml`) |
| `mkdocstrings` + `mkdocstrings-python` | Renders Python module/class/function reference from Google-style docstrings via the `::: module.path` directive |

### `test` group — Testing

| Library | Purpose |
|---|---|
| `pytest` | Test framework |
| `pytest-env` | Sets environment variables in `pyproject.toml` (e.g. `AWS_BACKEND_STACK_NAME`) |
| `pytest-cov` | Code coverage reporting |
| `pytest-xdist` | Parallel test execution with `-n auto` |
| `pytest-mock` | Provides `mocker` fixture for mocking (used for Lambda context in unit tests) |
| `pytest-html` | Generates HTML test reports |
| `pytest-timeout` | Enforces per-test time limits (configured in `pyproject.toml`) |
| `pytest-randomly` | Randomizes test execution order to catch order-dependent bugs |
| `boto3` | AWS SDK, used by integration tests to query CloudFormation stack outputs |
| `requests` | HTTP client, used by integration tests to call the live API Gateway endpoint |

## When forking for production

### Design decisions and known limitations

**`cdk.out/` is not committed** — this directory contains the synthesized CloudFormation template and bundled Lambda assets generated by `cdk synth`. It is gitignored because it is always reproducible from source and can be large. Run `cdk synth` locally to regenerate it before deploying or invoking locally with SAM.

**attrs version conflict** — CDK (via `jsii`) pins `attrs<26`, while `aws-lambda-powertools[all]>=3.27` requires `attrs>=26`. These two versions cannot coexist in a single Python environment. The project handles this by declaring the `lambda` and `cdk` dependency groups mutually exclusive in `pyproject.toml` (`[tool.uv.conflicts]`), which lets uv record both resolutions in a single `uv.lock` (25.4.0 for the CDK side, 26.1.0 for the Lambda side) and install each into its own venv: `.venv` for CDK work and `.venv-lambda` for Lambda runtime code. CI splits into separate `quality`/`cdk-check` (CDK venv) and `test` (Lambda venv) jobs for the same reason.

**SSM parameter name is CDK-generated** — the greeting parameter's name is auto-generated by CDK (derived from the construct path, e.g. `HelloWorld-us-east-1AppGreetingParameterD5E6E64F`) rather than set explicitly. The Lambda reads the name from the `GREETING_PARAM_NAME` env var, which CDK wires up from `greeting_param.parameter_name`, so the name never needs to be human-memorable. This follows the CDK "use generated resource names" best practice — see [Stack and construct composition](#stack-and-construct-composition).

**CORS is open (`allow_origin="*"`)** — the Lambda handler configures `APIGatewayRestResolver` with `CORSConfig(allow_origin="*")` for simplicity. In production, restrict this to the specific CloudFront domain (e.g., `allow_origin="https://d1234.cloudfront.net"`) and set `allow_credentials=True` if the API requires cookies or Authorization headers. Leaving CORS open in production allows any origin to call the API from a browser.

**Error handling** — the handler demonstrates the recommended pattern for production Lambda error handling. Critical downstream failures catch the *specific* expected exception type — `aws_lambda_powertools.utilities.parameters.exceptions.GetParameterError` for the SSM call, which is what Powertools raises after wrapping any underlying `botocore` failure — and re-raise as `InternalServerError` so the API responds with a meaningful HTTP status. Truly unexpected exceptions (anything *not* a `GetParameterError`) intentionally propagate to Powertools' default handler so the original type surfaces in CloudWatch metrics and X-Ray rather than being silently flattened to a generic 500. Non-critical failures (AppConfig feature flag evaluation) fall back to a safe default rather than failing the whole request. As you extend this project, apply the same pattern to any new downstream call: decide whether the failure is critical (catch the specific expected type and raise `InternalServerError`) or non-critical (log a warning, use a default), and add a corresponding unit test for each path.

**Required env vars fail loudly at import time** — every required environment variable (`IDEMPOTENCY_TABLE_NAME`, `APPCONFIG_*`, `GREETING_PARAM_NAME`) is read through a small `_require_env()` helper that raises `RuntimeError` immediately if the value is missing or empty. Without this, a misconfigured deploy only surfaces deep inside boto3 as an opaque parameter-validation error from a downstream call — the kind of failure that takes hours to track to its actual cause. Failing at module load means the misconfiguration shows up in CloudWatch on the very first cold start with the offending variable named in the message.

**RUM client URL floats to `1.x`** — [frontend/index.html](frontend/index.html) loads `https://client.rum.us-east-1.amazonaws.com/1.x/cwr.js` rather than pinning a specific patch version. AWS publishes a major-version-floating path so security and bug-fix patches reach the browser without requiring a frontend redeploy. Pinning to a literal `1.21.0` (or similar) means the only way to pick up a CVE fix is to bump the URL and run `cdk deploy`, which is fine if the project is actively maintained but a problem if it isn't. The major-version float trades the (small) risk of a minor regression in the AWS-shipped client for guaranteed access to security patches.

**Explicit resource creation prevents dangling resources** — AWS services sometimes create supporting resources outside of CloudFormation. The most common example is CloudWatch log groups: Lambda creates one automatically on first invocation, and API Gateway creates an execution log group (`API-Gateway-Execution-Logs_{api-id}/{stage}`) whenever execution logging is enabled. Neither is managed by CloudFormation, so neither is deleted when you run `cdk destroy` — they silently persist and accrue storage costs indefinitely.

Every resource in this stack is declared explicitly in CDK with `removal_policy=RemovalPolicy.DESTROY` so that `cdk destroy` leaves nothing behind. When you add new AWS services, check whether they create their own supporting resources (log groups, S3 buckets, parameter store entries, etc.) and declare those explicitly too. The pattern is: if AWS creates it, CDK should own it.

**Application Insights dashboard** — Application Insights automatically creates a CloudWatch dashboard named after its resource group when `auto_configuration_enabled=True`. This dashboard is created outside of CloudFormation and cannot be pre-declared in CDK. To ensure it is removed on `cdk destroy`, the backend stack includes a Lambda-backed custom resource (`AppInsightsDashboardCleanup`) that calls `DeleteDashboards` at destroy time, targeting the dashboard by the resource group name.

> **Note:** If you ever rename the Application Insights resource group (e.g., by changing the stack name), the dashboard associated with the old name will be left behind because the old custom resource no longer knows about it. Clean it up manually:
>
> ```bash
> # List Application Insights dashboards
> aws cloudwatch list-dashboards --query "DashboardEntries[?contains(DashboardName, 'ApplicationInsights')]"
>
> # Delete the old one
> aws cloudwatch delete-dashboard --dashboard-names "ApplicationInsights-<old-resource-group-name>"
> ```

**DynamoDB Contributor Insights is enabled** — `contributor_insights_enabled=True` is set on the `IdempotencyTable`. Contributor Insights is a free CloudWatch feature that surfaces the *most active partition keys* and the *most throttled requests* over rolling time windows. For an idempotency table where the partition key is the API Gateway request ID (effectively unique per call), this means you can see in the AWS Console which request IDs are hitting the table hardest, when retries are concentrating on the same key, and whether any partition is approaching the per-partition write throughput ceiling that triggers throttling on a PAY_PER_REQUEST table. Cost: zero — only enabled-and-active tables incur charges, which kicks in only if the table actually receives write traffic. *Cost/value note:* on this sample with effectively zero RPS, both cost and value are near zero — there are no hot keys to surface in an idle table. The feature pays off at production traffic when hot keys, retries, and per-partition throttling actually happen. Wiring it now means it's already on the day production traffic arrives.

**Lambda runs on arm64 (Graviton)** — the function is configured with `_lambda.Architecture.ARM_64`. Graviton-based Lambda is roughly 20% cheaper per GB-second than x86_64 and tends to deliver ~19% better performance for typical Python workloads. The PythonFunction bundler builds wheels matching the function architecture, so all dependencies (including any C-extension wheels like `pydantic-core` and `cryptography`) install correctly. Switch back to `Architecture.X86_64` only if you add a layer or dependency that's published for x86 only. *Cost/value note:* at sample-app traffic the absolute savings are pennies per month — the change earns its keep by establishing the right default before traffic arrives. At production traffic the same one-line choice compounds into real money.

**AppConfig IAM is fully resource-scoped** — both `appconfig:StartConfigurationSession` and `appconfig:GetLatestConfiguration` are bound to the same `application/{app_id}/environment/{env_id}/configuration/{profile_id}` ARN, built dynamically from the Stack partition/region/account and the AppConfig CFN refs. The session token in `GetLatestConfiguration` request bodies is opaque request data, not the IAM resource — IAM still authorizes the call against the configuration ARN, so a wildcard `Resource: "*"` is unnecessary. The X-Ray segment publish action genuinely has no resource-level ARN and remains the only `Resource: "*"` left in the Lambda's auto-generated policy; that one statement carries the corresponding nag suppression on its own.

**Lambda recursive-loop detection is set explicitly to `Terminate`** — the L2 `PythonFunction` construct doesn't expose this property, so it's set on the underlying `CfnFunction` via a property override. `Terminate` is the AWS default and is the correct posture: if the function ever invokes itself or sets up a self-triggering chain (Lambda → SNS → Lambda, etc.), Lambda will detect the loop and terminate after a small number of iterations. Setting it in code rather than relying on the runtime default makes the choice visible in review.

**Glue Data Catalog encryption was implemented and then deliberately reverted** — the stack briefly configured `AWS::Glue::DataCatalogEncryptionSettings` to encrypt catalog metadata and connection passwords with the frontend KMS key. Two reasons it was removed:

1. **Account/region-wide blast radius.** There is exactly one Glue Data Catalog per account per region, not one per stack. The encryption settings resource configures *that one shared catalog*. Deploying this reference architecture into an account that already has other teams using Glue would either silently override their settings or fail outright if their key policy rejects the change. For a repository whose explicit purpose is to be forked and dropped into existing AWS accounts, that's a footgun.
2. **The encrypted data has no realistic confidentiality requirement.** What's actually inside this stack's catalog: table definitions for `cloudfront_logs` and `s3_access_logs` with column names like `log_date`, `x_edge_location`, `c_ip`. These match the public CloudFront and S3 access-log schemas — anyone can look up the column list in AWS docs. There are no Glue connections, no JDBC sources, and therefore no connection passwords to protect. Encrypting non-sensitive metadata against a non-existent threat is checkbox security.

If you fork this and your catalog will hold genuinely sensitive table metadata (custom analytics tables for PII, partner data, etc.), wire `glue.CfnDataCatalogEncryptionSettings` into a **separate, intentionally account-scoped** stack rather than into a per-application stack — that way the account-wide nature of the resource is reflected in the deploy boundary, and forks of this app can't accidentally mutate it.

**WAF AntiDDoSRuleSet was implemented and then deliberately reverted** — `AWSManagedRulesAntiDDoSRuleSet` was added at WebACL priority 1 with `UsageOfAction=ENABLED` to provide application-layer DDoS protection. The first deploy attempt failed because I'd referenced a hallucinated rule group name (`AWSManagedRulesAmazonIpDDoSList`), which doesn't exist; the second deploy with the real rule group failed because AntiDDoSRuleSet requires a `ManagedRuleGroupConfig` with a `ClientSideActionConfig` declared; the third deploy with that config failed because `UsageOfAction=ENABLED` requires at least one regex in `ExemptUriRegularExpressions`, and this stack has no URIs to legitimately exempt. By the time we'd worked through the constraints, the cost picture was clearer:

| Charge | Amount | Source |
|---|---|---|
| Entity activation fee | **$20.00 / month** per WebACL using the rule group | Verified against the AWS pricing catalog (`/offers/v1.0/aws/awswaf/current/index.json`, group "AMR Anti-DDoS Entity") |
| Standard rule fee | $1.00 / month | Each managed rule group counts as one of the WebACL's rules |
| Per-request inspection fee | $0.15 / million requests | On top of the standard $0.60 / million inspection fee |

The other 5 rules in the WebACL (IP Reputation, CRS, Known Bad Inputs, Anonymous IP List, custom rate limit) are all AWS WAF "free-tier" — no entity fee, just $1/month per rule. Only four paid-tier rule groups exist: Bot Control, ATP, ACFP, and AntiDDoSRuleSet. Adding AntiDDoSRuleSet would have raised this stack's fixed WAF cost from $10/month to $31/month — a similar jump in fixed WAF cost for one rule whose effective enforcement (the `DDoSRequests` Block arm) is gated on AWS observing high-confidence DDoS classification, which is unlikely to fire on a Hello World demo's traffic profile. The existing per-IP rate-limit rule already covers crude L7 DDoS at 200 req/5min/forwarded IP. Same conclusion as the Glue catalog encryption entry above: high cost, low value at this scale, document and skip.

If you fork this for a workload with real traffic and a realistic DDoS threat model, the rule group is genuinely useful — but treat the $20/month entity fee plus the exempt-URI-list requirement as planning items rather than drop-in additions.

**CloudFront cache invalidation is decoupled from `BucketDeployment`** — the `s3deploy.BucketDeployment` construct accepts a `distribution=` parameter that auto-invalidates the CloudFront cache after each upload. The convenient-looking parameter has a known bug ([aws/aws-cdk#15891](https://github.com/aws/aws-cdk/issues/15891)): the construct's invalidation hook fires on *every* CFN lifecycle event, including Delete. On `cdk destroy`, that delete-time invalidation races with CloudFront's own deletion, surfacing as either "Unable to confirm cache invalidation was successful" (CloudFront returns ambiguous status while it's tearing itself down) or `AccessDenied on cloudfront:CreateInvalidation` (the IAM policy granting that permission was already deleted by CFN in the parallel teardown).

The fix is structural: drop `distribution=` from the `BucketDeployment` and replace it with a separate `cr.AwsCustomResource` that defines **`on_create` and `on_update` only — no `on_delete`**. CFN simply removes the resource from stack state during teardown without making any CloudFront API call. There's nothing to race with.

The non-obvious detail is the `CallerReference` value in the `CreateInvalidation` parameters. CloudFormation decides whether to fire `on_update` by comparing the resource properties JSON between deploys; if the JSON is identical, it skips the resource entirely. A static string would mean invalidations only fire on the very first deploy and never again — silently broken. The right value is `Fn.select(0, bucket_deployment.object_keys)`, which resolves at deploy time to the BucketDeployment's content-hashed S3 asset key. Same frontend assets → same key → same JSON → CFN sees no change → no invalidation fires (correct: nothing to invalidate). Different assets → different key → different JSON → CFN fires `on_update` → invalidation runs. As a bonus, backend-only deploys leave the frontend asset key unchanged, so they don't burn through the [1,000 free invalidation paths per AWS account per month](https://aws.amazon.com/cloudfront/pricing/) for no reason.

The pattern matches the three other `AwsCustomResource` blocks already in this stack (`AppInsightsDashboardCleanup`, `RumExtendedMetrics`, `RumMetricsDestination`), so it doesn't introduce a new abstraction; it just applies the existing one to the BucketDeployment's invalidation hook.

### Scaling beyond a reference architecture

This project is a single-developer sample. AWS publishes broader guidance in [Best practices for scaling AWS CDK adoption within your organization](https://aws.amazon.com/blogs/devops/best-practices-for-scaling-aws-cdk-adoption-within-your-organization/) — most of which (private Construct Hubs, golden pipelines, multi-account strategies, Internal Developer Platforms, communities of practice) is org-scale guidance that doesn't apply at this size. The notes below capture which of that post's recommendations are already in place, which were analyzed earlier and skipped, which are worth flagging if you fork this for a real workload, and which are simply N/A.

#### Already in place

| Recommendation | Status |
|----------------|--------|
| **cdk-nag with custom Aspects** for org-specific policy enforcement | Five-pack `apply_compliance_aspects` aspect, see [CDK security checks](#cdk-security-checks) |
| **cdk-monitoring-constructs** for dashboards and alarms | Wired through `MonitoringFacade` covering Lambda, API Gateway, DynamoDB |
| **CDK code held to application-code quality standards** | ruff, mypy, pylint, bandit, pip-audit all run via pre-commit and CI |
| **Automated CI/CD with policy gates** | GitHub Actions runs `cdk synth` (which runs cdk-nag) on every PR, blocks merge on findings |

#### Already analyzed and skipped

- **`cdk-validator-cfnguard`** / cfn-guard plugin layer — see [Why cdk-nag and not cfn-guard or Checkov](#why-cdk-nag-and-not-cfn-guard-or-checkov) for the full comparison. Adding it on top of cdk-nag would duplicate compliance content with no unique rule coverage gained.

#### Worth flagging if forked for a real workload

- **[`cdk-wakeful`](https://github.com/aws-samples/cdk-wakeful)** — an alerting layer on top of `cdk-monitoring-constructs`. The current `MonitoringFacade` creates dashboards and alarms but the alarms aren't wired to anything — nobody gets paged on a 5xx spike or Lambda error rate. cdk-wakeful auto-routes alarms to SNS, with built-in integrations for AWS Chatbot and SSM Incident Manager. Single-line CDK change to enable. Cost: an SNS topic + subscription (negligible). Real win if the stack is going to be operated; pointless if it's a portfolio piece nobody monitors.

- **AWS Config conformance packs deployed via CDK** — different *timing* layer from cdk-nag: cdk-nag is preventive at synth time, Config is detective at runtime. Catches drift introduced after deploy (someone disables KMS via console, a new resource type that an Aspect rule doesn't yet cover, IAM policy expansion via service-linked roles). Could live as a sibling stack `HelloWorldComplianceStack` deploying an `AWS::Config::ConformancePack` referencing one of AWS's published templates ([Operational Best Practices for HIPAA Security](https://docs.aws.amazon.com/config/latest/developerguide/operational-best-practices-for-hipaa_security.html), Operational Best Practices for FedRAMP, etc.). Fits the existing "synth-time + runtime + access-log analytics" theme. Cost: Config has per-rule-evaluation pricing that scales with resource count, plus the configuration recorder itself.

- **CloudFormation Hooks for post-synthesis validation** — defensive *during stack operations*, not at synth or runtime. Catches the case where someone bypasses CI and pushes raw CloudFormation to the API directly (e.g., a misconfigured deploy script, a manually-crafted change set). Implementation is substantial — Hooks are a separate registry resource type with their own Python handler and deploy infrastructure. For a single-developer reference architecture this is meaningful complexity for marginal benefit; the realistic threat model here is the developer's own pre-commit hooks, not adversarial CFN injection. More compelling in a multi-team org where multiple roles have CFN deploy permissions.

#### Skip — N/A at this scale

- **Private Construct Hub**, approve-and-publish model for constructs, golden pipelines (centrally-managed, immutable from workload teams), ITSM approval integration, multi-account strategy with cross-account pipelines, Internal Developer Platforms (Backstage / Humanitec / Port), Communities of Practice, value-stream team alignment, multi-region deployment — all assume an org with multiple workload teams sharing a platform-engineering function. None apply to a single repo with one author.

- **CDK Pipelines** specifically (replacing GitHub Actions with `pipelines.CodePipeline`): doesn't add capability — same build/test/deploy outcome — but costs ~$1/month per pipeline + CodeBuild minutes and migrates a working setup. Worth considering only if you specifically want a CDK-native, in-code pipeline definition that lives alongside the stacks; otherwise GitHub Actions is fine and free.

- **CodeGuru Security / CodeWhisperer** for CDK code analysis: overlaps Bandit (already wired) and the editor-side AI assistant. Both add per-line scanning costs and produce findings the existing pipeline already covers. Optional polish, not a gap.

### AWS services for post-deployment operations

The in-stack [Monitoring](#monitoring) section covers what's wired into the CDK stacks themselves (CloudWatch dashboards, alarms via `cdk-monitoring-constructs`, RUM, X-Ray, access-log analytics via Athena). The services below are the *account-level* operational layer that complements it — security findings, compliance evidence, cost analysis, ML-driven anomaly detection, runbook automation, and incident response. None of them are wired into this repo today; this section is a forward-looking inventory for anyone forking this for a real workload.

#### Alerting and incident response

- **AWS Chatbot** — bridges AWS service notifications (CloudWatch alarms, AWS Health events, Security Hub findings, GuardDuty alerts, CodePipeline status, Config rule changes) to **Slack channels**, Microsoft Teams, and Amazon Chime via SNS. Single SNS topic + one Chatbot configuration per channel; no custom Lambda required. Pairs naturally with [`cdk-wakeful`](#scaling-beyond-a-reference-architecture) for the in-stack alarm side.
- **AWS Systems Manager Incident Manager** — incident response orchestration: on-call rotations, escalation plans, response plans that auto-trigger runbooks, contact channels (SMS / voice / email / Slack via Chatbot). Wires into CloudWatch alarms or EventBridge rules. Charges per response plan execution + notification fees.
- **AWS Health Dashboard** — service-health events, planned maintenance, and account-specific issues (e.g., "your Lambda function in us-east-1 may be affected by Service Event X"). Free, surfaced via EventBridge for routing to Chatbot / SNS / SSM Incident Manager. Often skipped in greenfield setups and missed when an outage hits.
- **AWS Systems Manager Automation** — runbook engine for routine ops (patch, restart, snapshot, remediate non-compliant resources, copy logs). Hundreds of AWS-managed runbooks; you can author your own in YAML/JSON. Combines well with EventBridge rules to auto-remediate Config or Security Hub findings.

#### Cost and rightsizing

- **AWS Cost & Usage Report (CUR)** — granular hourly billing data (per-resource, per-tag, per-pricing-dimension) delivered to S3, queryable via Athena. The detailed alternative to Cost Explorer for chargeback, financial-team integration, and trend analysis. Free to enable; you pay for the S3 storage + Athena queries.
- **AWS Compute Optimizer** — ML-driven rightsizing recommendations for Lambda (memory tuning, provisioned-concurrency suggestions), EC2 (instance type), EBS, ECS on Fargate, RDS. Free for the basic tier. Often surfaces 20-40% cost savings on undersized Lambda memory configurations on the first scan. Worth enabling for any production stack with non-trivial Lambda traffic.
- **AWS Trusted Advisor** — cost / security / performance / fault tolerance / service-limit checks. The free tier ships seven core checks; the full ~110-check set requires Business or Enterprise Support. Findings are exposed via the Trusted Advisor console, EventBridge, and the Support API.
- **AWS Support API** — programmatic access to support cases, Trusted Advisor checks, and AWS Health events. Requires **Business Support tier or higher** ($100/month minimum). Useful when integrating Trusted Advisor findings or AWS Health events into custom dashboards or CI gates.

#### Security findings and threat detection

| Service | What it detects | Timing | Cost model |
|---------|-----------------|--------|------------|
| **AWS Inspector** | Software vulnerabilities (CVEs) in Lambda code, container images, and EC2 OS packages | Scans on resource creation/change + continuous re-scan | Per-resource scanned per month (~$1.50 / Lambda function / month, ~$0.09 / image scan) |
| **AWS GuardDuty** | *Threats* — anomalous API behavior, compromised credentials (`UnauthorizedAccess:IAMUser/`), malware on EC2/EBS, unusual traffic patterns, S3 anomalies | Continuous, ML-based on VPC Flow Logs, CloudTrail, DNS logs | Per GB of analyzed events; ~$3-15/month for a small account |
| **AWS Detective** | *Investigation aid* — graph-database analysis of GuardDuty/Security Hub findings, post-incident forensics | Triggered by an existing finding; not a detector itself | Per GB of ingested log data; only useful after Inspector/GuardDuty are already running |
| **AWS Shield** | DDoS attacks at L3-L7 | Always on for CloudFront/Route 53/ELB (Standard tier, free); managed mitigation team + cost protection requires Advanced ($3,000/month) | Standard: free. Advanced: $3,000/month flat plus traffic |

Short version of the "vs" question: **Inspector** = "what packages am I running with known CVEs"; **GuardDuty** = "is someone or something acting suspiciously in my account"; **Detective** = "this finding looks bad, show me everything related to it for the last 90 days"; **Shield** = "block volumetric DDoS traffic before it hits my origin." They compose — most production accounts run Inspector + GuardDuty as the detection layer, Detective as the investigation layer, Shield Standard automatically. Shield Advanced is reserved for high-stakes public-facing workloads.

- **AWS Security Hub** — the *aggregator*: pulls findings from Inspector, GuardDuty, IAM Access Analyzer, Macie, Firewall Manager, Health, Config, third-party tools, and applies the **Foundational Security Best Practices** + **CIS AWS Foundations Benchmark** + **PCI DSS** + **NIST 800-53 Rev 5** standards. Single pane of glass with a normalized finding format (ASFF). Routes via EventBridge to Chatbot / SSM Incident Manager / your ITSM. The recommended starting point for any production account.

#### Compliance and audit

- **AWS Config and Conformance Packs** — see also [Scaling beyond a reference architecture](#scaling-beyond-a-reference-architecture). Config records resource configurations and evaluates managed/custom rules continuously; Conformance Packs are pre-built rule collections (HIPAA, FedRAMP, NIST, PCI, CIS, etc.) deployed as a single CFN template. Different *timing* layer from cdk-nag — synth-time prevention vs. runtime detection of drift.
- **AWS Audit Manager** — collects evidence continuously for **specific compliance frameworks** (FedRAMP Moderate / High, HIPAA, PCI DSS, SOC 2, GDPR, NIST 800-53, etc.) and produces audit-ready reports. Pulls from Config, Security Hub, CloudTrail, IAM. Charges per assessment.

#### Resilience and chaos engineering

- **AWS Resilience Hub** — assess and improve workload resilience against defined RTO / RPO targets. Models failures across regions, AZs, and dependencies; recommends improvements (multi-AZ, backup plans, cross-region replication). Useful gate before declaring a workload "production-ready."
- **AWS Fault Injection Service (FIS)** — managed chaos engineering. Inject faults (kill instances, throttle APIs, simulate AZ failure, network impairment) on schedule or on-demand. Lower-effort entry point than DIY chaos with Toxiproxy/Chaos Mesh; integrates with Resilience Hub findings.

#### ML-driven anomaly detection

- **AWS DevOps Guru** — analyzes CloudWatch metrics, CloudTrail events, and (optionally) **CloudWatch Logs** for operational anomalies — surges, error-rate spikes, deployment-related regressions, IAM policy expansion, latency drift. Worth enabling with **log anomaly detection** on the Lambda log groups, **OpsItem creation** in Systems Manager OpsCenter (so anomalies become tracked tickets rather than dashboard noise), and the **cost estimator** (so you see the dollar impact of each anomaly when triaging). $0.0028/resource/hour after a free trial; meaningful spend at scale.
- **AWS Systems Manager Application Manager** — view-and-act dashboard scoped to "an application" (a CloudFormation stack and any tag-grouped resources). Surfaces CloudWatch metrics, CloudWatch Logs Insights queries, Config compliance, OpsItems, runbook executions, and cost — all filtered to one application. To make this work end-to-end:
  1. Tag every resource (and the CloudFormation stack itself) with `AppManagerCFNStackKey: <stack-name>` so cost data lights up in Cost Explorer alongside the operational data.
  2. Set up an AWS Config configuration recorder in the account.
  3. Attach the relevant **Conformance Packs** so compliance status appears under the Application Manager application view.
  4. Wire **CloudWatch Logs Insights queries** as saved queries on the application's log groups for one-click log triage.

#### Self-service and inventory

- **AWS Resource Groups** — group resources by tag, CloudFormation stack, or query for cross-service operations (bulk apply Tags, run an Automation document over the group, scope an Application Manager view). Free; the underlying primitive that Application Manager and many service-catalog patterns build on.
- **AWS Service Catalog** — internal portfolio of approved CDK / CloudFormation / Terraform templates that workload teams can self-deploy from with parameter inputs. Often paired with Control Tower to give downstream teams a constrained "menu" of deployable stacks. Org-scale only — single-developer reference architectures don't benefit.

#### Reviews and assessment

- **AWS Well-Architected Tool** — guided workload reviews against the six pillars (operational excellence, security, reliability, performance efficiency, cost optimization, sustainability) plus optional lenses (Serverless, SaaS, ML, etc.). Produces an improvement plan with prioritized remediations. Free; recommended before any "production-readiness" sign-off.

#### CloudTrail in new accounts (always)

For any AWS account that doesn't already have an org-level trail: **enable AWS CloudTrail with a multi-region trail that delivers to CloudWatch Logs**, in addition to its default S3 destination. The CloudWatch Logs destination is what makes anomaly detection (DevOps Guru, Security Hub, GuardDuty, custom EventBridge rules) work in real time — the S3-only flow is too high-latency for alerting. Reference: [Datadog's coverage of the underlying detection rule](https://docs.datadoghq.com/security/default_rules/def-000-bop/).

#### What *not* to wire up

- **Amazon QuickSight for CloudFront / S3 access-log visualization** — **don't.** QuickSight charges $24/user/month for Reader (capacity) plus per-session fees, and the visualizations it produces over access logs are achievable via Athena directly with no per-user license. The existing Glue catalog + Athena WorkGroup + named queries in the frontend stack already deliver the same insight for free. QuickSight is a fit only when you have a non-technical audience that needs interactive dashboards and BI-style drill-down — not for an engineering team querying access logs.

## Resources

- [AWS CDK Developer Guide](https://docs.aws.amazon.com/cdk/v2/guide/home.html) — introduction to CDK concepts and the CDK CLI.
- [AWS CDK best practices](https://docs.aws.amazon.com/cdk/v2/guide/best-practices.html) — the official best-practices guide this project follows.
- [AWS CDK security best practices](https://docs.aws.amazon.com/cdk/v2/guide/best-practices-security.html) — companion security-focused guide.
- [AWS CDK deployment best practices](https://dev.to/aws-heroes/aws-cdk-deployment-best-practices-3doo) — AWS Heroes article covering Stage usage, `cdk.context.json`, generated resource names, logical-ID stability, and the "model with constructs, deploy with stacks" pattern.
