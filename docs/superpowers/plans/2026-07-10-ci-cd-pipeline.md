# CI/CD Pipeline (CDK Pipelines via CodeConnections) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A self-mutating CDK Pipeline (CodePipeline + CodeBuild, GitHub `main` via CodeConnections) that deploys a persistent `dev` environment, gates on live integration tests, waits for manual approval, then deploys prod — with the whole app born inside a CDK-bootstrap permissions boundary.

**Architecture:** Mode-switched single app: `app.py` keeps its direct-`AppStage` shape by default (manual `make deploy` and ephemeral ENV deploys unchanged); `-c pipeline=true` synthesizes `PipelineStack` instead, which embeds `AppStage` twice (`env_name="dev"`, `env_name="prod"`). The permissions boundary is a standalone CFN-managed IAM policy applied to the bootstrap roles via `cdk bootstrap --custom-permissions-boundary` and to every app-created role via the `permissions_boundary` Stage/Stack prop.

**Tech Stack:** aws-cdk-lib 2.261.0 (`aws_cdk.pipelines`, `aws_codepipeline`, `aws_codebuild`), cdk-nag 3.0.1 v3 policy-validation plugins, pytest (`.venv` for `tests/cdk`), uv two-venv layout, GitHub CodeConnections.

**Spec:** `docs/superpowers/specs/2026-07-10-ci-cd-pipeline-design.md` (approved).

## Global Constraints

- **Two venvs, never mix**: `tests/cdk` and `infrastructure/` run in `.venv` (`make test-cdk` = `uv run pytest tests/cdk -v --override-ini="addopts=" --timeout=120`); never install Powertools there.
- **The nag gate is the report check, not the exit code**: local gate is `tests/cdk/test_stage.py::TestNagCompliance`; CLI gate is `cdk synth '**'` + `uv run python scripts/check_validation_report.py cdk.out`.
- **Granular nag rules (IAM4/IAM5) need exact `applies_to` finding ids** — the gate's failure output prints them; acknowledge via `acknowledge_rules` from `infrastructure/nag_utils.py`.
- **Every log group declares explicit retention; every stateful resource declares an explicit removal policy** (`TemplateConventionChecks`).
- **Snapshot updates**: regenerate with `UPDATE_SNAPSHOTS=1 make test-cdk` and pair every snapshot update with a matching fine-grained assertion change.
- **Conventional Commits** (`feat:`/`fix:`/`docs:`/`test:`/`build:`/`chore:`); **no `Co-Authored-By:` trailer**.
- **Names (verbatim)**: boundary policy `cdk-scaffold-boundary`; boundary CFN stack `CdkScaffoldBoundary`; pipeline stack `ServerlessAppPipeline`; repo `timpugh/lambda-powertools-reference-prod-scaffold-part-one`; branch `main`; region `us-east-1`; dev stack names `ServerlessAppBackend-dev-us-east-1` / `ServerlessAppFrontend-dev-us-east-1`; prod keeps legacy names.
- **`dev` env name is pipeline-reserved** (documented convention, Task 9).
- **Markdown changes** must pass `make lint-docs`.

---

### Task 1: Permissions-boundary CFN template + `make bootstrap-boundary`

**Files:**

- Create: `infrastructure/bootstrap/cdk-scaffold-boundary.json`
- Modify: `Makefile` (new target after `deploy-appconfig-monitor`)
- Test: `tests/cdk/test_bootstrap_boundary.py`

**Interfaces:**

- Consumes: nothing (standalone artifact).
- Produces: managed policy name `cdk-scaffold-boundary` (referenced by Task 2's `BOUNDARY_POLICY_NAME` and the re-bootstrap command); Make target `bootstrap-boundary`.

- [ ] **Step 1: Write the failing test**

```python
"""Assertions over the standalone permissions-boundary CFN template.

The boundary is plain JSON (not CDK) so it can be deployed before the CDK
bootstrap roles exist. These tests pin the anti-escalation invariants; the
allow-list breadth is verified live (ephemeral deploy) per the spec.
"""

import json
from pathlib import Path

TEMPLATE = Path(__file__).parents[2] / "infrastructure" / "bootstrap" / "cdk-scaffold-boundary.json"
BOUNDARY_ARN_SUB = "arn:${AWS::Partition}:iam::${AWS::AccountId}:policy/cdk-scaffold-boundary"


def _statements() -> list[dict]:
    doc = json.loads(TEMPLATE.read_text())
    policy = doc["Resources"]["BoundaryPolicy"]["Properties"]
    assert policy["ManagedPolicyName"] == "cdk-scaffold-boundary"
    return policy["PolicyDocument"]["Statement"]


def _by_sid(sid: str) -> dict:
    matches = [s for s in _statements() if s.get("Sid") == sid]
    assert matches, f"statement {sid!r} missing"
    return matches[0]


def test_allow_list_covers_core_services() -> None:
    allow = _by_sid("AllowServiceActions")
    assert allow["Effect"] == "Allow"
    for prefix in ("cloudformation:*", "lambda:*", "iam:*", "sts:*", "codepipeline:*", "codebuild:*", "codeconnections:*"):
        assert prefix in allow["Action"], f"{prefix} missing from boundary allow-list"


def test_boundary_policy_cannot_be_tampered_with() -> None:
    deny = _by_sid("DenyBoundaryPolicyTampering")
    assert deny["Effect"] == "Deny"
    assert set(deny["Action"]) == {
        "iam:CreatePolicyVersion",
        "iam:DeletePolicy",
        "iam:DeletePolicyVersion",
        "iam:SetDefaultPolicyVersion",
    }
    assert deny["Resource"] == {"Fn::Sub": BOUNDARY_ARN_SUB}


def test_boundary_cannot_be_removed_from_principals() -> None:
    deny = _by_sid("DenyBoundaryRemoval")
    assert set(deny["Action"]) == {
        "iam:DeleteRolePermissionsBoundary",
        "iam:DeleteUserPermissionsBoundary",
    }


def test_new_principals_require_the_boundary() -> None:
    for sid in ("DenyRoleCreationWithoutBoundary", "DenyBoundaryReplacement"):
        deny = _by_sid(sid)
        cond = deny["Condition"]["StringNotEquals"]["iam:PermissionsBoundary"]
        assert cond == {"Fn::Sub": BOUNDARY_ARN_SUB}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cdk/test_bootstrap_boundary.py -v --override-ini="addopts=" --timeout=120`
Expected: FAIL — `FileNotFoundError` (template doesn't exist).

- [ ] **Step 3: Write the template**

`infrastructure/bootstrap/cdk-scaffold-boundary.json` — JSON (not YAML) so tests parse it with stdlib, no CFN-tag parsing dependency:

```json
{
  "AWSTemplateFormatVersion": "2010-09-09",
  "Description": "cdk-scaffold-boundary: permissions boundary for the CDK bootstrap CloudFormationExecutionRole and every role this app creates. Deploy via `make bootstrap-boundary` BEFORE `cdk bootstrap --custom-permissions-boundary cdk-scaffold-boundary` and before any deploy of this app.",
  "Resources": {
    "BoundaryPolicy": {
      "Type": "AWS::IAM::ManagedPolicy",
      "Properties": {
        "ManagedPolicyName": "cdk-scaffold-boundary",
        "Description": "Maximum permissions for CDK-deployed roles in this scaffold. Allows the services the app uses; denies escalation past or tampering with the boundary itself.",
        "PolicyDocument": {
          "Version": "2012-10-17",
          "Statement": [
            {
              "Sid": "AllowServiceActions",
              "Effect": "Allow",
              "Action": [
                "apigateway:*",
                "appconfig:*",
                "applicationinsights:*",
                "athena:*",
                "backup:*",
                "backup-storage:*",
                "budgets:*",
                "cloudformation:*",
                "cloudfront:*",
                "cloudtrail:*",
                "cloudwatch:*",
                "codebuild:*",
                "codeconnections:*",
                "codedeploy:*",
                "codepipeline:*",
                "codestar-connections:*",
                "cognito-identity:*",
                "dynamodb:*",
                "events:*",
                "glue:*",
                "iam:*",
                "kms:*",
                "lambda:*",
                "logs:*",
                "resource-groups:*",
                "rum:*",
                "s3:*",
                "secretsmanager:*",
                "sns:*",
                "sqs:*",
                "ssm:*",
                "sts:*",
                "wafv2:*",
                "xray:*"
              ],
              "Resource": "*"
            },
            {
              "Sid": "DenyBoundaryPolicyTampering",
              "Effect": "Deny",
              "Action": [
                "iam:CreatePolicyVersion",
                "iam:DeletePolicy",
                "iam:DeletePolicyVersion",
                "iam:SetDefaultPolicyVersion"
              ],
              "Resource": {
                "Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:policy/cdk-scaffold-boundary"
              }
            },
            {
              "Sid": "DenyBoundaryRemoval",
              "Effect": "Deny",
              "Action": [
                "iam:DeleteRolePermissionsBoundary",
                "iam:DeleteUserPermissionsBoundary"
              ],
              "Resource": "*"
            },
            {
              "Sid": "DenyRoleCreationWithoutBoundary",
              "Effect": "Deny",
              "Action": ["iam:CreateRole", "iam:CreateUser"],
              "Resource": "*",
              "Condition": {
                "StringNotEquals": {
                  "iam:PermissionsBoundary": {
                    "Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:policy/cdk-scaffold-boundary"
                  }
                }
              }
            },
            {
              "Sid": "DenyBoundaryReplacement",
              "Effect": "Deny",
              "Action": [
                "iam:PutRolePermissionsBoundary",
                "iam:PutUserPermissionsBoundary"
              ],
              "Resource": "*",
              "Condition": {
                "StringNotEquals": {
                  "iam:PermissionsBoundary": {
                    "Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:policy/cdk-scaffold-boundary"
                  }
                }
              }
            }
          ]
        }
      }
    }
  }
}
```

Rationale notes (put in the module docstring of the test, already written above): the broad `Allow` is correct for a *boundary* — it caps maximum permissions; roles still only get their scoped identity policies (effective = intersection). `iam:*` must be allowed so the CFN exec role can create the app's roles; escalation is blocked by the four Deny statements instead. `sts:*` is needed for pipeline role assumption into the bootstrap roles.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/cdk/test_bootstrap_boundary.py -v --override-ini="addopts=" --timeout=120`
Expected: 4 passed.

- [ ] **Step 5: Add the Make target**

In `Makefile`, directly after the `deploy-appconfig-monitor` recipe (uses the existing `REGION ?= us-east-1` variable defined near `_empty-frontend-buckets`; move nothing):

<!-- markdownlint-disable MD010 -- Makefile recipe lines require literal leading tabs; this block is copied verbatim into the Makefile. -->
```makefile
bootstrap-boundary: ## Deploy/update the cdk-scaffold-boundary IAM policy (run BEFORE re-bootstrapping or deploying)
	# The permissions boundary must exist in the account before (a) `cdk
	# bootstrap --custom-permissions-boundary cdk-scaffold-boundary` and
	# (b) any deploy of this app — every app role now references it (see
	# app_stage.BOUNDARY_POLICY_NAME). IAM managed policies are global;
	# the stack region is irrelevant but pinned for determinism.
	aws cloudformation deploy \
		--template-file infrastructure/bootstrap/cdk-scaffold-boundary.json \
		--stack-name CdkScaffoldBoundary \
		--capabilities CAPABILITY_NAMED_IAM \
		--region $(REGION)
```
<!-- markdownlint-enable MD010 -->

Note: `REGION ?= us-east-1` is defined lower in the Makefile than this target — that's fine, Make expands variables at recipe-run time, not parse time.

- [ ] **Step 6: Verify the Makefile parses**

Run: `make -n bootstrap-boundary`
Expected: prints the `aws cloudformation deploy ... --region us-east-1` command (dry run, no execution).

- [ ] **Step 7: Commit**

```bash
git add infrastructure/bootstrap/cdk-scaffold-boundary.json tests/cdk/test_bootstrap_boundary.py Makefile
git commit -m "feat: add the cdk-scaffold-boundary permissions-boundary policy and bootstrap-boundary target"
```

---

### Task 2: Apply the boundary to every app-created role

**Files:**

- Modify: `infrastructure/app_stage.py` (constant + `AppStage.__init__` super call)
- Test: `tests/cdk/test_stage.py` (new `TestPermissionsBoundary` class)
- Regenerate: `tests/cdk/snapshots/*` (every role gains a `PermissionsBoundary` property)

**Interfaces:**

- Consumes: policy name `cdk-scaffold-boundary` from Task 1.
- Produces: `BOUNDARY_POLICY_NAME: str = "cdk-scaffold-boundary"` module constant in `infrastructure/app_stage.py` (Task 4's `PipelineStack` imports it).

Design note: the spec named the `@aws-cdk/core:permissionsBoundary` cdk.json context key; this task uses the equivalent programmatic `permissions_boundary` Stage prop instead, because in-process test `App`s don't read cdk.json — the prop makes the boundary a code-level guarantee visible to every synth (tests, snapshots, CLI, pipeline) identically.

- [ ] **Step 1: Write the failing test**

Append to `tests/cdk/test_stage.py`:

```python
class TestPermissionsBoundary:
    """Every role the app creates must carry the cdk-scaffold-boundary.

    The boundary policy's DenyRoleCreationWithoutBoundary statement makes
    an unbounded role a deploy-time failure once the CFN exec role is
    bounded — this test moves that failure to synth time.
    """

    @pytest.mark.parametrize("stack_attr", ["waf", "data", "backend", "frontend", "audit"])
    def test_every_role_carries_the_boundary(self, prod_stage: AppStage, stack_attr: str) -> None:
        template = Template.from_stack(getattr(prod_stage, stack_attr))
        roles = template.find_resources("AWS::IAM::Role")
        unbounded = [
            logical_id
            for logical_id, role in roles.items()
            if "PermissionsBoundary" not in role.get("Properties", {})
        ]
        assert not unbounded, f"roles without the permissions boundary in {stack_attr}: {unbounded}"

    def test_backend_actually_has_roles(self, prod_stage: AppStage) -> None:
        # Guard against the parametrized test passing vacuously.
        assert Template.from_stack(prod_stage.backend).find_resources("AWS::IAM::Role")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cdk/test_stage.py::TestPermissionsBoundary -v --override-ini="addopts=" --timeout=120`
Expected: FAIL — backend (at least) lists unbounded roles.

- [ ] **Step 3: Apply the boundary in AppStage**

In `infrastructure/app_stage.py`, add below `DEFAULT_ENV_NAME`:

```python
# The IAM permissions boundary every app-created role carries (and the CDK
# bootstrap roles carry via `cdk bootstrap --custom-permissions-boundary`).
# The policy itself is the standalone CFN template in
# infrastructure/bootstrap/cdk-scaffold-boundary.json — deploy it with
# `make bootstrap-boundary` BEFORE any deploy of this app; a role that
# references a missing policy fails at deploy with an IAM error.
BOUNDARY_POLICY_NAME = "cdk-scaffold-boundary"
```

In `AppStage.__init__`, change the super call:

```python
        super().__init__(
            scope,
            construct_id,
            permissions_boundary=cdk.PermissionsBoundary.from_name(BOUNDARY_POLICY_NAME),
            **kwargs,
        )
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `uv run pytest tests/cdk/test_stage.py::TestPermissionsBoundary -v --override-ini="addopts=" --timeout=120`
Expected: 6 passed.

- [ ] **Step 5: Regenerate snapshots (paired with the Step 1 assertion)**

Run: `UPDATE_SNAPSHOTS=1 make test-cdk` then `make test-cdk`
Expected: first run rewrites `tests/cdk/snapshots/*`; second run fully green. Inspect the snapshot diff: the ONLY change per role is an added `PermissionsBoundary` key (an `Fn::Join`/`Fn::Sub` ARN ending in `policy/cdk-scaffold-boundary`). Any other diff is a bug — stop and investigate.

- [ ] **Step 6: Commit**

```bash
git add infrastructure/app_stage.py tests/cdk/test_stage.py tests/cdk/snapshots/
git commit -m "feat: carry the cdk-scaffold-boundary permissions boundary on every app-created role"
```

---

### Task 3: `code_connection_arn` validator

**Files:**

- Modify: `infrastructure/app_stage.py` (new validator next to `validate_ssm_param_path`)
- Test: `tests/cdk/test_stage.py` (new `TestCodeConnectionArnValidation` class)

**Interfaces:**

- Consumes: nothing new.
- Produces: `validate_code_connection_arn(raw: str | None) -> str` in `infrastructure/app_stage.py` — raises `ValueError` on `None` or malformed input, returns the ARN otherwise (Task 7's `app.py` calls it; note it is *required*, unlike `validate_ssm_param_path`).

- [ ] **Step 1: Write the failing test**

Append to `tests/cdk/test_stage.py` (add `validate_code_connection_arn` to the existing `from infrastructure.app_stage import (...)` block):

```python
class TestCodeConnectionArnValidation:
    """Pipeline mode requires a well-formed CodeConnections ARN, fail-loud at synth."""

    VALID = "arn:aws:codeconnections:us-east-1:111111111111:connection/12345678-abcd-4ef0-9876-0123456789ab"
    VALID_LEGACY = "arn:aws:codestar-connections:us-east-1:111111111111:connection/12345678-abcd-4ef0-9876-0123456789ab"

    def test_valid_arn_passes_through(self) -> None:
        assert validate_code_connection_arn(self.VALID) == self.VALID

    def test_legacy_codestar_service_name_accepted(self) -> None:
        # Connections created before the 2024 rename still carry the old service name.
        assert validate_code_connection_arn(self.VALID_LEGACY) == self.VALID_LEGACY

    def test_missing_arn_fails_with_handshake_hint(self) -> None:
        with pytest.raises(ValueError, match="CodeConnections"):
            validate_code_connection_arn(None)

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "not-an-arn",
            "arn:aws:s3:::bucket",
            "arn:aws:codeconnections:us-east-1:111111111111:host/12345678-abcd-4ef0-9876-0123456789ab",
        ],
    )
    def test_malformed_arns_fail_at_synth(self, bad: str) -> None:
        with pytest.raises(ValueError, match="code_connection_arn"):
            validate_code_connection_arn(bad)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cdk/test_stage.py::TestCodeConnectionArnValidation -v --override-ini="addopts=" --timeout=120`
Expected: FAIL — `ImportError: cannot import name 'validate_code_connection_arn'`.

- [ ] **Step 3: Write the validator**

In `infrastructure/app_stage.py`, below `validate_ssm_param_path`:

```python
# CodeConnections connection ARNs (the service was renamed from
# codestar-connections in 2024; pre-rename connections keep the old service
# segment, so both are accepted). Region/account left loose — IAM validates
# them at deploy; this only catches paste errors at synth.
_CODE_CONNECTION_ARN_RE = re.compile(
    r"^arn:aws:(codeconnections|codestar-connections):[a-z0-9-]+:\d{12}:connection/[A-Za-z0-9-]+$"
)


def validate_code_connection_arn(raw: str | None) -> str:
    """Validate the `code_connection_arn` context key at synth time.

    Unlike :func:`validate_ssm_param_path` this key is REQUIRED (in pipeline
    mode there is no default source to fall back to), so ``None`` is an
    error pointing at the one-time console handshake, not a pass-through.
    """
    if raw is None:
        raise ValueError(
            "Missing CDK context key 'code_connection_arn' (required with -c pipeline=true). "
            "Complete the one-time CodeConnections handshake in the console (Developer Tools "
            "> Connections > Create connection > GitHub), then set the connection ARN in "
            "cdk.json or pass -c code_connection_arn=arn:aws:codeconnections:..."
        )
    if not _CODE_CONNECTION_ARN_RE.match(raw):
        raise ValueError(
            f"Invalid value for CDK context key 'code_connection_arn': {raw!r}. "
            "Expected a connection ARN like "
            "arn:aws:codeconnections:us-east-1:123456789012:connection/<uuid>."
        )
    return raw
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/cdk/test_stage.py::TestCodeConnectionArnValidation -v --override-ini="addopts=" --timeout=120`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add infrastructure/app_stage.py tests/cdk/test_stage.py
git commit -m "feat: validate the code_connection_arn context key fail-loud at synth"
```

---

### Task 4: PipelineStack core (source, synth, CMK, log group, artifact bucket)

**Files:**

- Create: `infrastructure/pipeline_stack.py`
- Test: `tests/cdk/test_pipeline_stack.py`

**Interfaces:**

- Consumes: `BOUNDARY_POLICY_NAME`, `AppStage`, `validate_code_connection_arn` (Tasks 2–3); `apply_compliance_aspects`, `acknowledge_rules`, `grant_logs_service_to_key`, `create_auto_delete_objects_log_group` from `infrastructure/nag_utils.py`.
- Produces: `PipelineStack(scope, construct_id, *, code_connection_arn: str, retain_data: bool = False, appconfig_monitor: bool = False, ssm_param_path: str | None = None, **kwargs)` in `infrastructure/pipeline_stack.py`, exposing `self.pipeline` (`pipelines.CodePipeline`). Tasks 5–6 extend this same file; Task 7's `app.py` instantiates it.

Note: this task builds the pipeline WITHOUT stages (added in Task 5). `pipelines.CodePipeline` requires at least one stage or wave to `build_pipeline()`; to keep this task independently green, the Step 1 test synthesizes via `Template.from_stack` only after Task 5 — so in THIS task, tests assert construction-level properties by adding a throwaway `wave = pipeline.add_wave("Placeholder")`? No — do not ship placeholder waves. Instead this task's test synthesizes the full stack with a private helper `_pipeline_stack()` that Task 5 reuses; the ONE test written here (`test_synth_smoke`) is allowed to fail until Task 5 completes ONLY if CDK rejects a stage-less pipeline — in that case implement Task 4 and Task 5's Step 3 together and run both tasks' tests at Task 5's Step 4. Try stage-less first; `build_pipeline()` is only forced at synth, and this task's test does synthesize, so if it errors with "pipeline must have at least one stage", proceed to Task 5 Step 3 before committing, and squash both into Task 5's commit.

- [ ] **Step 1: Write the failing test**

Create `tests/cdk/test_pipeline_stack.py`:

```python
"""Assertion tests for the CI/CD pipeline stack (spec: 2026-07-10-ci-cd-pipeline-design).

Synthesizes the pipeline shape the same way app.py does with -c pipeline=true
(nag packs attached at the App root, Docker bundling skipped). The pipeline
needs an explicit account+region (CDK Pipelines deploys concrete
environments), so fixtures pin a dummy account.
"""

import pytest

aws_cdk = pytest.importorskip("aws_cdk", reason="aws_cdk not installed — skipping CDK pipeline tests")

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from infrastructure.pipeline_stack import PipelineStack

# Same key tests/cdk/test_stage.py uses to skip Docker bundling.
_NO_BUNDLING = {"aws:cdk:bundling-stacks": []}

ACCOUNT = "111111111111"
REGION = "us-east-1"
CONNECTION_ARN = f"arn:aws:codeconnections:{REGION}:{ACCOUNT}:connection/12345678-abcd-4ef0-9876-0123456789ab"


def _pipeline_stack() -> PipelineStack:
    app = cdk.App(context=_NO_BUNDLING)
    return PipelineStack(
        app,
        "ServerlessAppPipeline",
        code_connection_arn=CONNECTION_ARN,
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )


@pytest.fixture(scope="module")
def pipeline_template() -> Template:
    return Template.from_stack(_pipeline_stack())


class TestPipelineCore:
    def test_source_is_the_codeconnections_repo(self, pipeline_template: Template) -> None:
        pipeline_template.has_resource_properties(
            "AWS::CodePipeline::Pipeline",
            Match.object_like(
                {
                    "Stages": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Name": "Source",
                                    "Actions": [
                                        Match.object_like(
                                            {
                                                "Configuration": Match.object_like(
                                                    {
                                                        "ConnectionArn": CONNECTION_ARN,
                                                        "FullRepositoryId": "timpugh/lambda-powertools-reference-prod-scaffold-part-one",
                                                        "BranchName": "main",
                                                    }
                                                )
                                            }
                                        )
                                    ],
                                }
                            )
                        ]
                    )
                }
            ),
        )

    def test_synth_codebuild_is_docker_privileged(self, pipeline_template: Template) -> None:
        pipeline_template.has_resource_properties(
            "AWS::CodeBuild::Project",
            Match.object_like({"Environment": Match.object_like({"PrivilegedMode": True})}),
        )

    def test_codebuild_log_group_is_explicit_with_retention(self, pipeline_template: Template) -> None:
        pipeline_template.has_resource_properties(
            "AWS::Logs::LogGroup", Match.object_like({"RetentionInDays": 90})
        )

    def test_artifact_bucket_is_cmk_encrypted(self, pipeline_template: Template) -> None:
        pipeline_template.has_resource_properties(
            "AWS::S3::Bucket",
            Match.object_like(
                {
                    "BucketEncryption": {
                        "ServerSideEncryptionConfiguration": [
                            Match.object_like(
                                {
                                    "ServerSideEncryptionByDefault": Match.object_like(
                                        {"SSEAlgorithm": "aws:kms"}
                                    )
                                }
                            )
                        ]
                    }
                }
            ),
        )

    def test_every_pipeline_role_carries_the_boundary(self, pipeline_template: Template) -> None:
        roles = pipeline_template.find_resources("AWS::IAM::Role")
        assert roles, "pipeline stack should create roles"
        unbounded = [
            logical_id
            for logical_id, role in roles.items()
            if "PermissionsBoundary" not in role.get("Properties", {})
        ]
        assert not unbounded, f"roles without the permissions boundary: {unbounded}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cdk/test_pipeline_stack.py -v --override-ini="addopts=" --timeout=120`
Expected: FAIL — `ModuleNotFoundError: No module named 'infrastructure.pipeline_stack'`.

- [ ] **Step 3: Write PipelineStack**

Create `infrastructure/pipeline_stack.py`:

```python
"""PipelineStack — the self-mutating CD pipeline (CDK Pipelines).

Synthesized ONLY in pipeline mode (``-c pipeline=true`` — see app.py); the
default shape keeps the direct-AppStage layout for manual `make deploy` and
ephemeral ENV deploys. Sourced from GitHub ``main`` via a CodeConnections
connection (one-time console handshake; the ARN arrives via the
``code_connection_arn`` context key, validated fail-loud in app_stage).

Stage ladder (spec 2026-07-10-ci-cd-pipeline-design): a persistent ``dev``
environment (pipeline-reserved env name), live integration tests against it,
a manual approval, then prod — which reuses the legacy stack names, so the
pipeline updates the existing prod stacks in place.

Encryption posture: per-stack CMK (matches every other stack), encrypting
the artifact bucket and the CodeBuild log group. The log group is CFN-owned
and handed to every generated CodeBuild project — CodeBuild otherwise
auto-creates never-expire log groups outside CloudFormation (the
dangling-resource problem this repo's cleanup patterns exist for).
"""

from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_codepipeline as codepipeline
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import pipelines
from constructs import Construct

from infrastructure.app_stage import BOUNDARY_POLICY_NAME, AppStage
from infrastructure.nag_utils import (
    acknowledge_rules,
    apply_compliance_aspects,
    create_auto_delete_objects_log_group,
    grant_logs_service_to_key,
)

GITHUB_REPO = "timpugh/lambda-powertools-reference-prod-scaffold-part-one"
GITHUB_BRANCH = "main"

# The pipeline owns this env name end to end (deploys it, tests against it,
# never tears it down). Manual `make deploy ENV=dev` would fight the pipeline
# over the same stacks — documented as reserved in the README.
DEV_ENV_NAME = "dev"


class PipelineStack(cdk.Stack):
    """CodePipeline (dev → integration tests → approval → prod), self-mutating."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        code_connection_arn: str,
        retain_data: bool = False,
        appconfig_monitor: bool = False,
        ssm_param_path: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            scope,
            construct_id,
            permissions_boundary=cdk.PermissionsBoundary.from_name(BOUNDARY_POLICY_NAME),
            **kwargs,
        )
        apply_compliance_aspects(self)

        # Per-stack CMK, same pattern as every other stack in the app.
        self.encryption_key = kms.Key(
            self,
            "PipelineKey",
            description="CMK for the CD pipeline's artifact bucket and CodeBuild logs",
            enable_key_rotation=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        grant_logs_service_to_key(
            self.encryption_key,
            region=self.region,
            account=self.account,
            partition=self.partition,
        )

        # One CFN-owned log group for every generated CodeBuild project
        # (synth, self-mutate, asset publishing, integration tests) —
        # explicit retention per TemplateConventionChecks, CMK-encrypted,
        # and destroyed with the stack instead of dangling.
        self.build_log_group = logs.LogGroup(
            self,
            "PipelineBuildLogs",
            encryption_key=self.encryption_key,
            retention=logs.RetentionDays.THREE_MONTHS,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # Artifact bucket: transient build artifacts only — destroy-friendly,
        # CMK-encrypted, 90-day expiry so failed-run leftovers don't accrete.
        self.artifact_bucket = s3.Bucket(
            self,
            "PipelineArtifacts",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.encryption_key,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            auto_delete_objects=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            lifecycle_rules=[s3.LifecycleRule(expiration=cdk.Duration.days(90))],
        )
        create_auto_delete_objects_log_group(self, self.encryption_key)

        underlying = codepipeline.Pipeline(
            self,
            "Cd",
            pipeline_name="ServerlessAppPipeline",
            artifact_bucket=self.artifact_bucket,
            restart_execution_on_update=True,
        )

        synth = pipelines.CodeBuildStep(
            "Synth",
            input=pipelines.CodePipelineSource.connection(
                GITHUB_REPO,
                GITHUB_BRANCH,
                connection_arn=code_connection_arn,
            ),
            install_commands=[
                "npm ci",
                "pip install uv",
            ],
            commands=[
                # Same pair as `make cdk-synth` / the CI cdk-check job, plus
                # pipeline mode so the assembly contains this stack (required
                # for self-mutation). '**' descends into the Stage-nested
                # stacks so asset bundling runs against the real stacks.
                "npx cdk synth -c pipeline=true '**'",
                "uv run python scripts/check_validation_report.py cdk.out",
            ],
            primary_output_directory="cdk.out",
        )

        self.pipeline = pipelines.CodePipeline(
            self,
            "Pipeline",
            code_pipeline=underlying,
            synth=synth,
            # PythonFunction asset bundling runs Docker during `cdk synth`.
            docker_enabled_for_synth=True,
            code_build_defaults=pipelines.CodeBuildOptions(
                build_environment=codebuild.BuildEnvironment(
                    build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                ),
                logging=codebuild.LoggingOptions(
                    cloud_watch=codebuild.CloudWatchLoggingOptions(
                        log_group=self.build_log_group,
                    )
                ),
            ),
        )

        self._add_stages(
            retain_data=retain_data,
            appconfig_monitor=appconfig_monitor,
            ssm_param_path=ssm_param_path,
        )

        # Force role/project generation now so the acknowledgments below see
        # the final construct tree (build_pipeline is otherwise deferred to
        # synth, after which acknowledgments can no longer be attached).
        self.pipeline.build_pipeline()
        self._acknowledge_pipeline_findings()

    def _add_stages(
        self,
        *,
        retain_data: bool,
        appconfig_monitor: bool,
        ssm_param_path: str | None,
    ) -> None:
        # Filled in by Task 5.
        pass

    def _acknowledge_pipeline_findings(self) -> None:
        # Filled in by Task 6 from the actual gate output.
        pass
```

(The `_add_stages` / `_acknowledge_pipeline_findings` bodies are completed by Tasks 5–6 in this same file; they are private seams, not public interface. If synth fails with "pipeline must have at least one stage" before Task 5, see the task-header note.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/cdk/test_pipeline_stack.py -v --override-ini="addopts=" --timeout=120`
Expected: 5 passed (or the stage-less synth error — then jump to Task 5 Step 3 and run both tasks' tests together).

- [ ] **Step 5: Commit**

```bash
git add infrastructure/pipeline_stack.py tests/cdk/test_pipeline_stack.py
git commit -m "feat: add the PipelineStack core (CodeConnections source, gated synth, CMK, owned build logs)"
```

---

### Task 5: Stage ladder — dev, integration gate, approval, prod

**Files:**

- Modify: `infrastructure/pipeline_stack.py` (`_add_stages` body)
- Test: `tests/cdk/test_pipeline_stack.py` (new `TestStageLadder` class)

**Interfaces:**

- Consumes: `AppStage` (env-name namespacing, `retain_data`/`appconfig_monitor`/`ssm_param_path` params — exact signature in `infrastructure/app_stage.py`); `PipelineStack` internals from Task 4.
- Produces: the deployed ladder Dev → (IntegrationTest post-step) → Prod (ManualApproval pre-step). No new public symbols.

- [ ] **Step 1: Write the failing test**

Append to `tests/cdk/test_pipeline_stack.py`:

```python
def _pipeline_stages(template: Template) -> list[dict]:
    pipelines_found = template.find_resources("AWS::CodePipeline::Pipeline")
    assert len(pipelines_found) == 1
    return next(iter(pipelines_found.values()))["Properties"]["Stages"]


class TestStageLadder:
    def test_dev_deploys_before_prod(self, pipeline_template: Template) -> None:
        names = [s["Name"] for s in _pipeline_stages(pipeline_template)]
        assert "Dev" in names and "Prod" in names
        assert names.index("Dev") < names.index("Prod")

    def test_prod_gates_on_manual_approval(self, pipeline_template: Template) -> None:
        prod = next(s for s in _pipeline_stages(pipeline_template) if s["Name"] == "Prod")
        approvals = [
            a for a in prod["Actions"] if a["ActionTypeId"]["Category"] == "Approval"
        ]
        assert len(approvals) == 1
        assert approvals[0]["Name"] == "PromoteToProd"
        # RunOrder 1 = the approval blocks every deploy action in the stage.
        assert approvals[0]["RunOrder"] == 1

    def test_dev_stage_runs_the_integration_gate(self, pipeline_template: Template) -> None:
        dev = next(s for s in _pipeline_stages(pipeline_template) if s["Name"] == "Dev")
        action_names = [a["Name"] for a in dev["Actions"]]
        assert "IntegrationTest" in action_names

    def test_integration_gate_can_read_only_the_dev_stacks(self, pipeline_template: Template) -> None:
        # The test step's role may DescribeStacks on the two dev stacks and
        # nothing broader — the prod stacks are deliberately out of reach.
        pipeline_template.has_resource_properties(
            "AWS::IAM::Policy",
            Match.object_like(
                {
                    "PolicyDocument": Match.object_like(
                        {
                            "Statement": Match.array_with(
                                [
                                    Match.object_like(
                                        {
                                            "Action": "cloudformation:DescribeStacks",
                                            "Resource": [
                                                Match.object_like({}),
                                                Match.object_like({}),
                                            ],
                                        }
                                    )
                                ]
                            )
                        }
                    )
                }
            ),
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cdk/test_pipeline_stack.py::TestStageLadder -v --override-ini="addopts=" --timeout=120`
Expected: FAIL — no `Dev`/`Prod` stages exist yet.

- [ ] **Step 3: Implement `_add_stages`**

Replace the `_add_stages` body in `infrastructure/pipeline_stack.py`:

```python
    def _add_stages(
        self,
        *,
        retain_data: bool,
        appconfig_monitor: bool,
        ssm_param_path: str | None,
    ) -> None:
        env = cdk.Environment(account=self.account, region=self.region)

        # Persistent dev environment, updated in place each run. retain_data
        # is pinned False (dev data is regenerable by definition) and
        # ssm_param_path is NOT forwarded — a fixed parameter name would
        # collide with prod's in the shared account. appconfig_monitor IS
        # forwarded: once flipped in cdk.json (after both cold deploys —
        # README runbook), dev exercises the same rollout machinery prod uses.
        dev = AppStage(
            self,
            "Dev",
            region=self.region,
            env_name=DEV_ENV_NAME,
            retain_data=False,
            appconfig_monitor=appconfig_monitor,
            ssm_param_path=None,
            env=env,
        )

        integration_test = pipelines.CodeBuildStep(
            "IntegrationTest",
            install_commands=["pip install uv"],
            commands=[
                "make install-lambda",
                "make test-integration",
            ],
            env={
                # pytest-env's D:-prefixed defaults yield to these (the
                # exported-stack-name override fix — see pyproject.toml).
                "AWS_BACKEND_STACK_NAME": f"ServerlessAppBackend-{DEV_ENV_NAME}-{self.region}",
                "AWS_FRONTEND_STACK_NAME": f"ServerlessAppFrontend-{DEV_ENV_NAME}-{self.region}",
            },
            role_policy_statements=[
                iam.PolicyStatement(
                    actions=["cloudformation:DescribeStacks"],
                    resources=[
                        self.format_arn(
                            service="cloudformation",
                            resource="stack",
                            resource_name=f"ServerlessAppBackend-{DEV_ENV_NAME}-{self.region}/*",
                        ),
                        self.format_arn(
                            service="cloudformation",
                            resource="stack",
                            resource_name=f"ServerlessAppFrontend-{DEV_ENV_NAME}-{self.region}/*",
                        ),
                    ],
                )
            ],
        )
        self.pipeline.add_stage(dev, post=[integration_test])

        # Prod reuses the legacy stack names (env_name="prod" is AppStage's
        # default naming), so the pipeline updates the existing prod stacks
        # in place. The manual approval is the only gate between a green
        # integration run and prod.
        prod = AppStage(
            self,
            "Prod",
            region=self.region,
            retain_data=retain_data,
            appconfig_monitor=appconfig_monitor,
            ssm_param_path=ssm_param_path,
            env=env,
        )
        self.pipeline.add_stage(prod, pre=[pipelines.ManualApprovalStep("PromoteToProd")])
```

- [ ] **Step 4: Run all pipeline tests to verify they pass**

Run: `uv run pytest tests/cdk/test_pipeline_stack.py -v --override-ini="addopts=" --timeout=120`
Expected: 9 passed (Task 4's 5 + this task's 4). Nested-stage synthesis makes this slower — expect ~1–2 minutes.

- [ ] **Step 5: Commit**

```bash
git add infrastructure/pipeline_stack.py tests/cdk/test_pipeline_stack.py
git commit -m "feat: add the dev-integration-approval-prod stage ladder to the pipeline"
```

---

### Task 6: Nag-gate the pipeline shape and acknowledge its findings

**Files:**

- Modify: `infrastructure/pipeline_stack.py` (`_acknowledge_pipeline_findings` body)
- Test: `tests/cdk/test_pipeline_stack.py` (new `TestPipelineNagCompliance` class)

**Interfaces:**

- Consumes: `attach_nag_packs`, `acknowledge_rules` (exact shapes in `infrastructure/nag_utils.py`); the `_unacknowledged_findings` gate pattern from `tests/cdk/test_stage.py`.
- Produces: a nag-clean pipeline shape — the compliance gate every later change to the pipeline stack runs against.

- [ ] **Step 1: Write the failing test**

Append to `tests/cdk/test_pipeline_stack.py` (extend the existing imports with `from infrastructure.nag_utils import attach_nag_packs` and `from tests.cdk.test_stage import _unacknowledged_findings` — if `tests` is not importable as a package, copy the 20-line `_unacknowledged_findings` helper into this module with a comment naming its origin):

```python
def _nag_pipeline_stack() -> PipelineStack:
    app = cdk.App(context=_NO_BUNDLING)
    attach_nag_packs(app)
    return PipelineStack(
        app,
        "ServerlessAppPipeline",
        code_connection_arn=CONNECTION_ARN,
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )


class TestPipelineNagCompliance:
    def test_pipeline_shape_has_no_unacknowledged_findings(self) -> None:
        stack = _nag_pipeline_stack()
        findings = _unacknowledged_findings(stack.node.root)
        details = "\n".join(f"  {f}" for f in findings)
        assert not findings, (
            f"unacknowledged cdk-nag findings in the pipeline shape — fix the resource or "
            f"add a scoped acknowledgment with a reason (see CLAUDE.md):\n{details}"
        )

    def test_convention_checks_have_no_error_annotations(self) -> None:
        from aws_cdk.assertions import Annotations

        stack = _nag_pipeline_stack()
        Annotations.from_stack(stack).has_no_error("*", Match.any_value())
```

- [ ] **Step 2: Run the gate and READ the finding list**

Run: `uv run pytest tests/cdk/test_pipeline_stack.py::TestPipelineNagCompliance -v --override-ini="addopts=" --timeout=120`
Expected: FAIL, printing every unacknowledged finding **with its exact rule and finding id**. This output is the input to Step 3 — do not guess ids.

- [ ] **Step 3: Acknowledge, iteratively, with reasons**

Fill `_acknowledge_pipeline_findings` in `infrastructure/pipeline_stack.py`. The exact `applies_to` values come from Step 2's output (IAM5 findings match individual `Rule[Finding]` ids only — a bare `AwsSolutions-IAM5` matches nothing). The rules you should expect, with the reasons to record:

```python
    def _acknowledge_pipeline_findings(self) -> None:
        # CDK Pipelines generates its own least-possible roles; the wildcards
        # below are the construct's documented shape (artifact-bucket object
        # access, CodeBuild report groups, cdk-assets publishing), not
        # hand-written policy. Exact applies_to ids come from the gate
        # output (tests/cdk/test_pipeline_stack.py::TestPipelineNagCompliance).
        acknowledge_rules(
            self,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "CDK Pipelines-generated roles: artifact-bucket object access, "
                        "CodeBuild report groups, and cdk-assets publishing require "
                        "prefix wildcards; the roles are construct-managed, not hand-written."
                    ),
                    "applies_to": [
                        # PASTE the exact Rule[Finding] ids printed by the gate here,
                        # one per wildcard, e.g.:
                        # "Action::s3:GetObject*",
                        # "Resource::<PipelineArtifacts...>.Arn/*",
                    ],
                },
                {
                    "id": "AwsSolutions-CB3",
                    "reason": (
                        "The synth CodeBuild project runs Docker for PythonFunction "
                        "asset bundling (docker_enabled_for_synth) — privileged mode "
                        "is the documented requirement."
                    ),
                },
            ],
        )
```

Loop: add/adjust acknowledgments → rerun the Step 2 command → repeat until green. Expect additional entries for `IAMNoInlinePolicy` variants (NIST/HIPAA/PCI — CDK generates default policies inline) and S3 server-access-log/replication/versioning rules on the artifact bucket (transient artifacts; mirror the reason wording used for such rules elsewhere in the repo — grep `nag_utils.py` `_LOG_SINK_SUPPRESSION_RULES` for tone). Record each rule's reason honestly; never acknowledge a finding you can instead fix (e.g., a missing log-group retention is a fix, not an acknowledgment).

- [ ] **Step 4: Run the full CDK suite to verify no regressions**

Run: `make test-cdk`
Expected: all green, including both new pipeline test classes and the pre-existing `TestNagCompliance` shapes.

- [ ] **Step 5: Commit**

```bash
git add infrastructure/pipeline_stack.py tests/cdk/test_pipeline_stack.py
git commit -m "test: nag-gate the pipeline shape and acknowledge its construct-generated findings"
```

---

### Task 7: Mode-switch app.py

**Files:**

- Modify: `app.py`
- Test: manual synth verification (app.py is the entry script; its collaborators are unit-tested in Tasks 3–6)

**Interfaces:**

- Consumes: `PipelineStack` (Task 4), `validate_code_connection_arn` (Task 3), existing `parse_context_flag`.
- Produces: `-c pipeline=true` synthesizes `ServerlessAppPipeline`; default shape byte-identical to today.

- [ ] **Step 1: Add the mode switch**

In `app.py`: add `from infrastructure.app_stage import validate_code_connection_arn` to the existing import block and `from infrastructure.pipeline_stack import PipelineStack` below it. Then replace the single `AppStage(...)` call at the bottom with:

```python
# Pipeline mode (`-c pipeline=true`): synthesize the self-mutating CD
# pipeline instead of a directly-deployable stage. The pipeline embeds
# AppStage twice (dev + prod) — see infrastructure/pipeline_stack.py and
# README "CI/CD pipeline". Default False keeps this file's legacy shape:
# `make deploy` and ephemeral ENV deploys are untouched.
pipeline_mode: bool = parse_context_flag(app.node.try_get_context("pipeline"), "pipeline")

if pipeline_mode:
    # The pipeline owns its environments (dev + prod); an -c env override
    # here would silently do nothing, so fail loud instead.
    if app.node.try_get_context("env") is not None:
        raise ValueError(
            "The 'env' context key has no effect with -c pipeline=true — the pipeline "
            "deploys its own 'dev' and 'prod' environments. Drop -c env, or drop "
            "-c pipeline=true for a direct ephemeral deploy."
        )
    # CDK Pipelines deploys concrete environments, so the pipeline stack
    # needs an explicit account. The CDK CLI resolves CDK_DEFAULT_ACCOUNT
    # from the active AWS credentials at synth.
    account = os.environ.get("CDK_DEFAULT_ACCOUNT")
    if not account:
        raise ValueError(
            "CDK_DEFAULT_ACCOUNT is not set — pipeline mode needs AWS credentials at "
            "synth so the pipeline stack gets a concrete account (run via the cdk CLI "
            "with credentials, e.g. `make deploy-pipeline`)."
        )
    PipelineStack(
        app,
        "ServerlessAppPipeline",
        code_connection_arn=validate_code_connection_arn(app.node.try_get_context("code_connection_arn")),
        retain_data=retain_data,
        appconfig_monitor=appconfig_monitor,
        ssm_param_path=ssm_param_path,
        env=cdk.Environment(account=account, region=target_region),
    )
else:
    # Stage id composition lives next to the Stage (app_stage.stage_id):
    # prod keeps the legacy id so existing cdk.out assembly paths and tooling
    # keyed on the stage name stay stable; other envs get their own id.
    AppStage(
        app,
        stage_id(env_name, target_region),
        region=target_region,
        env_name=env_name,
        retain_data=retain_data,
        appconfig_monitor=appconfig_monitor,
        ssm_param_path=ssm_param_path,
    )

app.synth()
```

Also update `app.py`'s module docstring: add a paragraph after the `env` paragraph describing the `pipeline` + `code_connection_arn` keys and the `-c env` exclusion (mirror the docstring style of the existing context-key paragraphs).

- [ ] **Step 2: Verify the default shape is untouched**

Run: `npx cdk ls '**'`
Expected: the five legacy stack ids under `ServerlessApp-us-east-1-stage`, exactly as before this task.

- [ ] **Step 3: Verify pipeline mode (needs AWS credentials for account resolution)**

Run: `npx cdk ls '**' -c pipeline=true -c code_connection_arn=arn:aws:codeconnections:us-east-1:111111111111:connection/12345678-abcd-4ef0-9876-0123456789ab`
Expected: `ServerlessAppPipeline` plus the Dev/Prod stage-nested stacks. Also verify both fail-loud paths: the same command *without* `-c code_connection_arn` must fail mentioning the handshake; with `-c env=foo` added it must fail mentioning the pipeline owns its environments.

- [ ] **Step 4: Run the repo-wide checks**

Run: `make test-cdk && make cdk-synth`
Expected: green (cdk-synth needs Docker running; the default shape carries no pipeline stack so the committed CI job is unaffected).

- [ ] **Step 5: Commit**

```bash
git add app.py
git commit -m "feat: mode-switch app.py — -c pipeline=true synthesizes the CD pipeline"
```

---

### Task 8: Pipeline snapshot, Makefile target, cdk.json documentation

**Files:**

- Modify: `tests/cdk/test_snapshots.py`, `Makefile`, `cdk.json`
- Create (generated): `tests/cdk/snapshots/pipeline.json` (or the module's naming scheme — match it)

**Interfaces:**

- Consumes: `_normalize` and the snapshot-file naming convention in `tests/cdk/test_snapshots.py` (read the module first; reuse, don't reimplement); `PipelineStack` fixture pattern from `tests/cdk/test_pipeline_stack.py`.
- Produces: `make deploy-pipeline` (the one-time pipeline birth); a committed pipeline snapshot.

- [ ] **Step 1: Add the snapshot test**

Append to `tests/cdk/test_snapshots.py`, reusing its existing `_normalize` helper and snapshot read/write mechanics exactly as the parametrized stack test uses them (same `UPDATE_SNAPSHOTS` env var, same snapshots directory, same assertion message style). The fixture mirrors `tests/cdk/test_pipeline_stack.py`:

```python
@pytest.fixture(scope="module")
def pipeline_stack() -> "PipelineStack":
    from infrastructure.pipeline_stack import PipelineStack

    app = cdk.App(context=_NO_BUNDLING)
    return PipelineStack(
        app,
        "ServerlessAppPipeline",
        code_connection_arn="arn:aws:codeconnections:us-east-1:111111111111:connection/12345678-abcd-4ef0-9876-0123456789ab",
        env=cdk.Environment(account="111111111111", region="us-east-1"),
    )


def test_pipeline_template_matches_snapshot(pipeline_stack: "PipelineStack") -> None:
    # Same normalize-then-compare flow as test_template_matches_snapshot —
    # the snapshot tracks the pipeline's infrastructure shape, not asset hashes.
    ...
```

Replace the `...` with the exact read/compare/update logic the existing test uses (copy its body, substituting the pipeline template and a `pipeline` snapshot name). If `_normalize` leaves per-build churn in the pipeline template (CodeBuild spec asset hashes are the likely culprit), extend `_normalize` minimally and note why in a comment.

- [ ] **Step 2: Generate, then verify stability**

Run: `UPDATE_SNAPSHOTS=1 make test-cdk && make test-cdk`
Expected: snapshot file created on the first run; second run green — twice in a row (pytest-randomly reorders; a hash leak shows up as flapping).

- [ ] **Step 3: Add `make deploy-pipeline`**

In `Makefile`, after `bootstrap-boundary`:

<!-- markdownlint-disable MD010 -- Makefile recipe lines require literal leading tabs; this block is copied verbatim into the Makefile. -->
```makefile
deploy-pipeline: ## One-time deploy of the CD pipeline (self-mutates afterwards). CONN=<connection-arn> unless set in cdk.json
	# Prerequisites (in order): `make bootstrap-boundary`, then
	# `npx cdk bootstrap --custom-permissions-boundary cdk-scaffold-boundary`,
	# then the CodeConnections console handshake (README "CI/CD pipeline").
	# After this one deploy the pipeline updates ITSELF from GitHub main —
	# rerunning this target is only needed if the pipeline stack was deleted.
	$(CDK) deploy ServerlessAppPipeline -c pipeline=true \
		$(if $(CONN),-c code_connection_arn=$(CONN)) --require-approval never
```
<!-- markdownlint-enable MD010 -->

- [ ] **Step 4: Document the keys in cdk.json**

In `cdk.json`'s `context` block, extend the `_comment_production_switches` entry: append to the existing string: `" pipeline (default false) synthesizes the CD pipeline instead of a direct stage — used by make deploy-pipeline and the pipeline's own synth, never set it here. code_connection_arn holds the CodeConnections handshake ARN and IS meant to live here once created."` Do not add a `"pipeline": true` value — the default shape must stay the direct stage.

- [ ] **Step 5: Verify and commit**

Run: `make -n deploy-pipeline CONN=arn:aws:codeconnections:us-east-1:1:connection/x && make test-cdk`
Expected: dry-run prints the cdk deploy command with the `-c code_connection_arn` flag; tests green.

```bash
git add tests/cdk/test_snapshots.py tests/cdk/snapshots/ Makefile cdk.json
git commit -m "test: snapshot the pipeline shape; build: add deploy-pipeline and document the pipeline context keys"
```

(Split into two commits — `test:` for the snapshot files, `build:` for Makefile+cdk.json — if the PR-title/commit tooling objects to the compound message.)

---

### Task 9: Documentation, TODO check-offs, full gate

**Files:**

- Modify: `README.md`, `CLAUDE.md`, `TODO.md`

**Interfaces:**

- Consumes: everything shipped in Tasks 1–8.
- Produces: the operator runbook; updated project memory for future sessions.

- [ ] **Step 1: README — new "CI/CD pipeline" section**

Add a section (place it near the deployment sections; match the README's heading style) covering, in this order:

1. **What ships**: the mode-switched app, the stage ladder diagram from the spec, the fact that prod is adopted in place (legacy stack names) and `dev` is pipeline-reserved.
2. **One-time setup runbook (ordered, copy-paste-able)**:
   - `make bootstrap-boundary`
   - `npx cdk bootstrap aws://<account>/us-east-1 --custom-permissions-boundary cdk-scaffold-boundary`
   - CodeConnections handshake: console → Developer Tools → Connections → Create connection → GitHub → authorize the repo; paste the ARN into `cdk.json` as `code_connection_arn`.
   - `make deploy-pipeline` (or `make deploy-pipeline CONN=<arn>` before committing the ARN).
3. **Cold-deploy sequencing ×2**: both first deploys (dev on the first pipeline run, prod on the first approval) must happen with `appconfig_monitor=false`; flip it in `cdk.json` only after BOTH environments' `FeatureFlagEvaluationFailure` metrics report. Link the existing "Deployment safety" section.
4. **Export-retention interaction**: the pipeline's first prod deploy counts as the "deploy once with the export retained" step of the two-deploy removal recipe (link the TODO item).
5. **Self-mutation note**: a pipeline-definition change makes run N update the pipeline and restart as run N+1 — expected, not stuck.
6. **Boundary caveat**: every deploy now requires `cdk-scaffold-boundary` to exist in the account (fork setup step); a boundary-blocked action fails at deploy/runtime, not synth.
7. **Cost note** (verbatim from the spec's "Cost notes" section).

- [ ] **Step 2: CLAUDE.md updates**

- "Project" section: move "CDK Pipelines via a CodeConnections handshake" from the phase-two futures into the shipped list; phase two's remainder (alarm subscriptions, prod-shaped verification of SNS/spend-budget/canary) stays.
- Add a short section "CI/CD pipeline and the permissions boundary" summarizing: the mode switch (`-c pipeline=true`), `dev` being pipeline-reserved, the boundary being mandatory for every deploy (`make bootstrap-boundary` first), and where the runbook lives (README). Keep it to the file's telegraphic style.

- [ ] **Step 3: TODO.md check-offs**

- `[x]` **Deploy workflow** (CI/CD section) — implemented as the CDK pipeline, not a GitHub Actions deploy; note the pointer.
- `[x]` **Live integration tests in CI** (both occurrences: Operations gates + CI/CD section) — running as the pipeline's dev post-step.
- `[x]` **Multi-environment deployment pipeline** (Deployment-safety gates) — dev → approval → prod; note staging was deliberately skipped (spec).
- `[x]` **CDK bootstrap permissions narrowed with a permissions boundary** (Deployment-safety gates) and the Security section's "Narrow the CDK bootstrap permissions" item — note the policy name and that runtime roles carry it too (also resolves the IAM section's "Permissions boundary on the Lambda execution role" cross-reference).
- Leave "Branch protection enforced" open.

- [ ] **Step 4: Full local gate**

Run: `make pr && make lint-docs`
Expected: every CI gate green locally (check-lock, lint, typecheck, lint-docs, test, test-cdk, cdk-synth with Docker running, compare-openapi — the OpenAPI spec is untouched by this work, so compare-openapi must pass without regeneration).

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md TODO.md
git commit -m "docs: document the CI/CD pipeline runbook and check off the delivered TODO gates"
```

---

### Task 10: Live verification (requires AWS account + user at the console)

**Files:** none (operational task; record outcomes in the PR description).

**Interfaces:**

- Consumes: the full runbook from Task 9.
- Produces: a live-verified pipeline — the spec's acceptance criterion.

- [ ] **Step 1: Boundary rollout on an ephemeral env first** (spec trap 2)

```bash
make bootstrap-boundary
npx cdk bootstrap aws://<account>/us-east-1 --custom-permissions-boundary cdk-scaffold-boundary
make deploy ENV=boundary-check
make test-integration AWS_BACKEND_STACK_NAME=ServerlessAppBackend-boundary-check-us-east-1 AWS_FRONTEND_STACK_NAME=ServerlessAppFrontend-boundary-check-us-east-1
make destroy-clean ENV=boundary-check
```

Expected: deploy succeeds (boundary allow-list is sufficient), integration tests green, clean teardown. An `AccessDenied` during deploy = a missing service prefix in the boundary allow-list — add it to `cdk-scaffold-boundary.json`, rerun `make bootstrap-boundary`, retry. Record every prefix added.

- [ ] **Step 2: CodeConnections handshake**

Console: Developer Tools → Connections → Create connection → GitHub → authorize `timpugh/lambda-powertools-reference-prod-scaffold-part-one`. Put the ARN in `cdk.json` (`code_connection_arn`), commit to the branch.

- [ ] **Step 3: Birth the pipeline and watch one full run**

```bash
make deploy-pipeline
```

Then observe in the CodePipeline console: Source → Synth (nag gate runs inside) → SelfMutate → Dev deploy (cold — all five dev stacks) → IntegrationTest green → pause at PromoteToProd. **Approve** → prod stacks update in place. Verify: `appconfig_monitor` is false in cdk.json throughout; the dev stacks carry `-dev-` names; prod kept legacy names; the CodeBuild log group in CloudWatch is the stack-owned one (no `/aws/codebuild/*` stragglers).

- [ ] **Step 4: Verify self-mutation**

Push a trivial pipeline change (e.g., a comment in `pipeline_stack.py`) to `main` via PR; watch run N SelfMutate and restart as run N+1. Confirm no manual redeploy was needed.

- [ ] **Step 5: Record outcomes**

Update the PR description (or `TODO.md`'s phase-two notes) with: boundary prefixes added during Step 1, the one-run timings, and any 403 window observed on the prod frontend during the first pipeline prod deploy (expected per the same-origin migration note).

---

## Self-review (completed)

- **Spec coverage**: mechanism/ladder/account/boundary-first/persistent-dev/mode-switch → Tasks 4–7; boundary policy + prop + snapshot regen → Tasks 1–2; CodeConnections ARN validation → Task 3; nag gating + conventions + CMK + owned log groups → Tasks 4, 6; integration gate + scoped IAM → Task 5; Makefile/cdk.json/runbook/TODO check-offs → Tasks 8–9; live verification incl. trap 2 → Task 10. Spec's cdk.json-context boundary wiring refined to the equivalent `permissions_boundary` prop (noted in Task 2).
- **Placeholders**: Task 6's `applies_to` comment is deliberate — the ids are unknowable pre-synth and the task's Step 2 makes producing them the explicit input; everything else is concrete code.
- **Type consistency**: `BOUNDARY_POLICY_NAME`/`validate_code_connection_arn` defined in `infrastructure/app_stage.py` (Tasks 2–3) and consumed with those exact names (Tasks 4, 7); `PipelineStack` kwargs identical at definition (Task 4) and call site (Task 7); `DEV_ENV_NAME`-derived stack names match the integration-test env vars and IAM ARNs (Task 5).
