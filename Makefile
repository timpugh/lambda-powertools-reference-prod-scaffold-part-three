.DEFAULT_GOAL := help

# =============================================================================
# Two-environment model
# =============================================================================
# CDK and Lambda Powertools require incompatible `attrs` versions (CDK pulls
# attrs<26 via jsii; Powertools pulls attrs>=26). uv locks both resolutions
# in a single uv.lock via `[tool.uv.conflicts]`, but each resolution must
# install into its own venv.
#
# Both venvs live at the PROJECT ROOT (relative paths, gitignored):
#
#   .venv         — CDK workstation: cdk + test + lint + docs groups
#   .venv-lambda  — Lambda runtime:  lambda + test + lint groups (unit tests, OpenAPI gen)
#
# Both are created automatically by `make install` (which runs `uv sync` for
# each group set into the right env). You do not pick the location — uv does,
# based on the directory `make` is invoked from. Each clone of this repo gets
# its own pair; nothing is shared across projects on disk.
#
# Check status with `make doctor`. Nuke and rebuild with `make clean-venvs &&
# make install`.
#
# The venv selector uses the UV_PROJECT_ENVIRONMENT env var that uv honours
# natively — no activation dance, no symlink juggling.
LAMBDA_ENV := UV_PROJECT_ENVIRONMENT=.venv-lambda
LAMBDA_RUN := $(LAMBDA_ENV) uv run

.PHONY: help install install-cdk install-lambda doctor test test-cdk test-integration \
	lint format typecheck security cdk-synth cdk-notices cdk-deprecations \
	cdk-ls cdk-diff cdk-drift cdk-revert-drift cdk-diagnose cdk-gc cdk-rollback \
	deploy destroy destroy-clean _empty-frontend-buckets docs docs-open docs-serve lock upgrade deps-merge clean clean-venvs

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# =============================================================================
# Environment setup
# =============================================================================

install: install-cdk install-lambda ## Install both environments and pre-commit hooks
	.venv/bin/pre-commit install

# --locked mirrors CI (.github/workflows/ci.yml). Without it, a stale uv.lock
# would silently install whatever the resolver picks today, which can drift
# from CI's pinned set. With it, `make install` after a pyproject.toml edit
# will fail until `make lock` is run, which is the desired contract.
install-cdk: ## Install the CDK workstation env into .venv (cdk + test + lint + docs)
	uv sync --locked --group cdk --group test --group lint --group docs

# The lint group rides into .venv-lambda alongside the runtime so `make
# typecheck` can run mypy in this env over lambda/ and scripts/ (where
# Powertools is importable but aws-cdk-lib is not). Without lint here,
# mypy is missing entirely and the typecheck target falls back to the
# weaker .venv-side check that treats Powertools as Any.
install-lambda: ## Install the Lambda runtime env into .venv-lambda (lambda + test + lint)
	$(LAMBDA_ENV) uv sync --locked --only-group lambda --only-group test --only-group lint

# =============================================================================
# Diagnostics
# =============================================================================

doctor: ## Diagnostic snapshot — uv/cdk/drawio versions, venv state, pre-commit wiring
	@echo "=== Toolchain ==="
	@command -v uv >/dev/null 2>&1 && printf "uv:          %s\n" "$$(uv --version)" || echo "uv:          MISSING — install from https://docs.astral.sh/uv/"
	@command -v cdk >/dev/null 2>&1 && printf "cdk CLI:     %s\n" "$$(cdk --version)" || echo "cdk CLI:     MISSING — run 'npm install -g aws-cdk'"
	@command -v drawio >/dev/null 2>&1 && printf "drawio:      %s\n" "$$(drawio --version)" || echo "drawio:      MISSING (optional) — 'brew install --cask drawio' for diagram exports"
	@echo
	@echo "=== Virtual environments (project-local, gitignored) ==="
	@if [ -x .venv/bin/python ]; then \
		printf ".venv:        %s\n" "$$(.venv/bin/python --version)"; \
		.venv/bin/python -c "import aws_cdk" 2>/dev/null && echo "              [OK] CDK group installed" || echo "              [X]  CDK group missing — run 'make install-cdk'"; \
	else \
		echo ".venv:        NOT CREATED — run 'make install-cdk' or 'make install'"; \
	fi
	@if [ -x .venv-lambda/bin/python ]; then \
		printf ".venv-lambda: %s\n" "$$(.venv-lambda/bin/python --version)"; \
		.venv-lambda/bin/python -c "import aws_lambda_powertools" 2>/dev/null && echo "              [OK] Lambda runtime group installed" || echo "              [X]  Lambda runtime group missing — run 'make install-lambda'"; \
	else \
		echo ".venv-lambda: NOT CREATED — run 'make install-lambda' or 'make install'"; \
	fi
	@echo
	@echo "=== Pre-commit hooks ==="
	@if [ -f .git/hooks/pre-commit ]; then \
		echo "Installed:   [OK] .git/hooks/pre-commit present"; \
	else \
		echo "Installed:   [X]  not wired — run 'make install' (or '.venv/bin/pre-commit install')"; \
	fi

# =============================================================================
# Testing
# =============================================================================

test: ## Run unit tests with coverage (uses .venv-lambda — needs Powertools)
	$(LAMBDA_RUN) pytest tests/unit -v

test-cdk: ## Run CDK stack assertion tests (uses .venv — needs CDK)
	uv run pytest tests/cdk -v --override-ini="addopts=" --timeout=120

test-integration: ## Run integration tests against a deployed stack (uses .venv-lambda)
	# --override-ini drops the project-wide --cov-fail-under=100 gate (which
	# only makes sense for unit tests over lambda/) so integration tests don't
	# fail the run on coverage instead of behavior. Mirrors test-cdk's pattern.
	$(LAMBDA_RUN) pytest tests/integration -v --override-ini="addopts="

# =============================================================================
# Code quality
# =============================================================================

cdk-synth: ## Synthesize all CDK stacks and validate cdk-nag rules (requires CDK CLI: npm install -g aws-cdk)
	# The '**' glob descends into Stage-nested stacks. Without it, `cdk synth`
	# stops at the Stage manifest, the three nested stacks never synthesize,
	# and cdk-nag rules silently don't fire on them — so a "passing" synth
	# can mask findings that surface later in `cdk deploy`.
	cdk synth '**'

cdk-notices: ## Show AWS-published CDK notices (CVEs, deprecated CDK versions, upcoming breaking changes)
	cdk notices

cdk-deprecations: ## List every deprecated CDK API used by any stack (synth output filtered for "deprecated")
	cdk synth '**' 2>&1 | grep -i deprecat || echo "No deprecated CDK APIs in use"

cdk-ls: ## List all CDK stacks (uses '**' to descend into Stage-nested stacks)
	# Without '**', `cdk ls` stops at the top-level Stage manifest and
	# prints nothing useful. With it, the three nested stacks (Backend,
	# WAF, Frontend) are listed — handy as a sanity check after stack-graph
	# refactors or when verifying the Stage wiring is intact.
	cdk ls '**'

cdk-diff: ## Preview infra changes against deployed stacks (requires AWS credentials)
	# Same Stage-nesting trap as cdk-synth and deploy: bare `cdk diff`
	# walks only the App's direct children and reports no changes for the
	# three real stacks. Use this as the pre-PR companion to cdk-synth —
	# synth tells you cdk-nag is happy, diff tells you what would deploy.
	cdk diff '**'

cdk-drift: ## Detect drift between deployed resources and what CDK last shipped (requires AWS credentials)
	# Surfaces resources mutated outside CDK — console edits, manual SDK
	# calls, neighbor-stack collisions. Load-bearing for this template's
	# encryption posture: CMK key policies, IAM grants, and CloudTrail
	# trail config are easy to silently drift and easy to miss.
	cdk drift '**'

cdk-revert-drift: ## Deploy AND auto-revert out-of-band drift back to code (requires CDK CLI 2.1110.0+)
	# The remediation half of cdk-drift: where `cdk drift` only reports
	# resources mutated outside CDK, --revert-drift rolls them back to what
	# the code last shipped — in the same operation as any pending template
	# changes (CloudFormation's REVERT_DRIFT deployment mode). Self-healing
	# posture for this template's encryption invariants: a console-edited CMK
	# key policy or IAM grant snaps back to the committed state on deploy.
	#
	# Deliberately a separate, opt-in target rather than folded into `deploy`:
	# --revert-drift assumes code is always the source of truth, so it would
	# also undo a legitimate emergency console change made during an incident.
	# Keep the default `deploy` predictable; reach for this consciously after
	# `make cdk-drift` shows what would be reverted.
	cdk deploy '**' --revert-drift --require-approval never

cdk-diagnose: ## Root-cause CloudFormation failures with construct paths and source locations (CDK 2.1120.0+)
	# The --unstable=diagnose flag gates the command while it's behind the
	# unstable feature flag; drop the flag once it graduates to stable.
	# Output maps CFN errors back to the construct and the file:line where
	# it was defined — designed to be parseable by AI agents as well as
	# humans. Substitute a specific stack name for '**' to narrow scope.
	cdk --unstable=diagnose diagnose '**'

cdk-gc: ## Inspect (dry-run) unused Lambda/Docker assets in the CDK bootstrap S3/ECR repos
	# Every `cdk deploy` adds new Lambda zips and container images to the
	# CDKToolkit bootstrap bucket and ECR repo, but older revisions
	# accumulate forever. --action=print is dry-run only — it tags isolated
	# assets and reports what *would* be deleted on a subsequent run, but
	# deletes nothing. To actually GC, run `cdk --unstable=gc gc` directly:
	# the default (--action=full, --confirm=true) prompts interactively
	# before each deletion. --created-buffer-days=1 (default) skips assets
	# younger than a day; tune via --created-buffer-days=N for tighter
	# windows. The --unstable=gc flag gates the command while gc is behind
	# the unstable feature flag; drop it once gc graduates to stable.
	cdk --unstable=gc gc --action=print

cdk-rollback: ## Roll deployed stacks back to their last stable state (use after a partial deploy failure)
	# Pairs with cdk-diagnose: when a deploy half-fails and CloudFormation
	# parks a stack in UPDATE_ROLLBACK_FAILED, this returns it to the
	# last good state without manual console intervention. Same '**' trap
	# as cdk-synth and friends — bare `cdk rollback` only sees the empty
	# Stage manifest.
	cdk rollback '**'

# The '**' glob is required so CDK descends into the Stage-nested stacks —
# without it `cdk deploy` only sees the empty Stage manifest and exits with
# "No stack found in the main cloud assembly". --require-approval never
# skips the interactive IAM-change prompt; cdk-nag has already gated the
# change at synth time. Drop the flag for a manual review of every IAM diff.
deploy: ## Deploy all stacks to us-east-1 (use `cdk deploy '**' -c region=X` for other regions)
	cdk deploy '**' --require-approval never

# --force skips the interactive "are you sure?" prompt, mirroring how
# the deploy target uses --require-approval never. Without --force, the
# command fails outright in non-TTY contexts (CI, background shells)
# with "terminal is not attached so we are unable to get a confirmation".
# If you want the confirmation back for a one-off run, invoke cdk
# directly: `cdk destroy '**'`. Three stacks are destroyed independently
# — frontend first (consumes the WAF ARN), then backend and WAF.
destroy: ## Destroy all stacks in us-east-1 (use `cdk destroy '**' --force -c region=X` for other regions)
	cdk destroy '**' --force

# Region the frontend stack (and its log buckets) live in. Override to match a
# non-default deploy: `make destroy-clean REGION=ap-southeast-1`.
REGION ?= us-east-1

# Resolve every S3 bucket in the frontend stack by type (names are CDK-generated,
# so we can't hardcode them) and empty each. Idempotent: a missing stack or empty
# bucket is a no-op. Used by destroy-clean below.
_empty-frontend-buckets:
	@echo "Emptying frontend-stack S3 buckets in $(REGION)..."
	@for b in $$(aws cloudformation list-stack-resources \
		--stack-name HelloWorldFrontend-$(REGION) --region $(REGION) \
		--query "StackResourceSummaries[?ResourceType=='AWS::S3::Bucket'].PhysicalResourceId" \
		--output text 2>/dev/null); do \
		echo "  emptying s3://$$b"; \
		aws s3 rm "s3://$$b" --recursive --region $(REGION) >/dev/null 2>&1 || true; \
	done

# CloudFront / S3 / CloudTrail log delivery is ASYNCHRONOUS, so a log file can land
# in the access-log (or CloudTrail) bucket AFTER cdk's auto_delete_objects empties
# it during teardown — leaving DeleteBucket with a 409 "bucket not empty" and the
# stack in DELETE_FAILED. This target empties the frontend log buckets first to
# shrink that window, then destroys; if a straggler log still lands while the
# CloudFront distribution is deleting (which takes minutes), it empties once more
# and retries. Re-running the whole target is always safe — emptying is idempotent.
destroy-clean: ## Empty async-log buckets, then destroy all stacks (avoids the CloudFront-log teardown race). REGION=us-east-1 default.
	@$(MAKE) _empty-frontend-buckets REGION=$(REGION)
	cdk destroy '**' --force -c region=$(REGION) || { \
		echo "destroy hit a late-arriving log straggler — emptying again and retrying once..."; \
		$(MAKE) _empty-frontend-buckets REGION=$(REGION); \
		cdk destroy '**' --force -c region=$(REGION); \
	}

lint: ## Run all pre-commit hooks (ruff, mypy, pylint, bandit, xenon, pip-audit)
	uv run pre-commit run --all-files

format: ## Format code with ruff
	uv run ruff format .

typecheck: ## Run mypy type checking (CDK side in .venv, Lambda runtime + scripts in .venv-lambda)
	# .venv has aws-cdk-lib + boto3-stubs but not Powertools (attrs conflict),
	# so it checks the CDK construct code only. .venv-lambda has Powertools
	# and lint tooling, so it checks the Lambda handler and the scripts/
	# helpers that import from it (notably scripts/generate_openapi.py).
	# The pre-commit mypy hook holds the CDK side and excludes scripts/ for
	# the same reason — see .pre-commit-config.yaml.
	uv run mypy hello_world/
	$(LAMBDA_RUN) mypy lambda/ scripts/

security: ## Run bandit security scan and pip-audit vulnerability check
	# scripts/ is included to match the typecheck target and the pre-commit bandit
	# hook; bandit only scans .py files, so the shell scripts are harmlessly ignored.
	uv run bandit -r lambda/ hello_world/ scripts/
	# pip-audit goes through the pre-commit hook so the --ignore-vuln list
	# (currently CVE-2026-3219 — pip 26.0.1, no upstream fix) is sourced
	# from .pre-commit-config.yaml. Invoking pip-audit directly here would
	# duplicate the suppression list and silently drift when the upstream
	# fix lands.
	uv run pre-commit run pip-audit --all-files

# =============================================================================
# Documentation
# =============================================================================
#
# The OpenAPI generator imports lambda/app.py, which requires Powertools —
# so it runs in .venv-lambda. Zensical itself is only installed in .venv
# (the docs group), so the build step runs in .venv.

docs: ## Build Zensical HTML documentation (regenerates the OpenAPI spec first)
	$(LAMBDA_RUN) python scripts/generate_openapi.py
	uv run zensical build

docs-open: docs ## Build and open documentation in browser
	open site/index.html

docs-serve: ## Regenerate OpenAPI spec and start the Zensical dev server with hot reload
	$(LAMBDA_RUN) python scripts/generate_openapi.py
	uv run zensical serve

# =============================================================================
# Dependency management
# =============================================================================
#
# COOLDOWN_DAYS gates `make upgrade` against PyPI versions uploaded in the last
# N days. This is the local mirror of the Dependabot cooldown — it defends
# laptop-side dependency upgrades against fresh malicious releases (xz-utils /
# nx / tj-actions class incidents). The cooldown only applies to `upgrade`,
# not `lock`: `lock` reproduces decisions already encoded in pyproject.toml
# and the existing uv.lock, while `upgrade` is where brand-new versions
# enter the project and is the only place a fresh malicious release can land.
#
# Override at the command line: `make upgrade COOLDOWN_DAYS=14`.
COOLDOWN_DAYS ?= 7
# Lazy ('=' not ':=') so the python3 subshell only runs when the recipe that
# expands $(COOLDOWN_CUTOFF) actually fires. Otherwise every `make help` /
# `make test` invocation pays the python startup cost up-front.
# Bare `python3` (not `uv run python`) is intentional: this is a stdlib-only date
# calc, system python3 is already a documented prerequisite, and avoiding `uv run`
# preserves the lazy-eval startup savings above. python3's datetime is also more
# portable than shelling out to `date`, whose flags differ between macOS (-v) and
# GNU/Linux (-d) — this repo is developed on both.
COOLDOWN_CUTOFF = $(shell python3 -c 'from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc) - timedelta(days=$(COOLDOWN_DAYS))).strftime("%Y-%m-%dT00:00:00Z"))')

lock: ## Regenerate uv.lock and lambda/requirements.txt from pyproject.toml
	uv lock
	uv export --only-group lambda --no-emit-project --no-header --format requirements.txt -o lambda/requirements.txt

upgrade: ## Upgrade all dependencies to latest versions older than COOLDOWN_DAYS days
	uv lock --upgrade --exclude-newer $(COOLDOWN_CUTOFF)
	uv export --only-group lambda --no-emit-project --no-header --format requirements.txt -o lambda/requirements.txt

# Wrapper around scripts/deps_merge.sh — see the file header for the full
# step list. Pass PR=N to handle a single PR; omit to process every open
# Dependabot PR sequentially. Sequential is required because each `make lock`
# regenerates uv.lock, and concurrent processing would have later PRs clobber
# earlier ones during squash-merge.
deps-merge: ## Process Dependabot PRs (rebase + lock + push + arm auto-merge). Use PR=N for one, omit for all open.
	@bash scripts/deps_merge.sh $(PR)

# =============================================================================
# Cleanup
# =============================================================================

clean: ## Remove build artifacts, caches, and coverage files (preserves venvs)
	rm -rf site htmlcov .coverage report.html .pytest_cache .mypy_cache .ruff_cache cdk.out
	find . -type d -name __pycache__ -exec rm -rf {} +

# Separate from `clean` because re-installing both venvs takes minutes (CDK
# bundle, all groups) and is not something you want in a routine cache reset.
# When you DO need a fresh install (lockfile changes that uv refuses to
# reconcile, corrupted venv, switching Python versions), run this then
# `make install`.
clean-venvs: ## Wipe .venv and .venv-lambda (separate from `clean` which preserves them)
	rm -rf .venv .venv-lambda
	@echo "Venvs removed. Run 'make install' to recreate."
