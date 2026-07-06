# Contributing

Thanks for contributing to the Lambda Powertools Reference architecture. This guide covers environment setup, the quality gates your change must clear, and the contribution workflow. The [README](README.md) documents the architecture itself; [CLAUDE.md](CLAUDE.md) records design rationale; [AGENTS.md](AGENTS.md) is the condensed map for AI-assisted work.

## Contents

- [Development setup](#development-setup) — prerequisites and the two-venv install
- [Running tests and checks](#running-tests-and-checks) — which suite runs where, and the one-shot CI mirror
- [Coding standards](#coding-standards) — formatting, typing, complexity, and documentation expectations
- [Making common kinds of changes](#making-common-kinds-of-changes) — API, infrastructure, and dependency changes
- [Commit and PR conventions](#commit-and-pr-conventions) — Conventional Commits, PR titles, changelog
- [Releases](#releases) — the git-cliff-driven release recipe

## Development setup

Prerequisites: [uv](https://docs.astral.sh/uv/), Node.js (the pinned CDK CLI and markdownlint are npm packages invoked via `npx`), Python 3.14+ (matching the Lambda runtime; uv manages the interpreter), and Docker (only for `make cdk-synth`/deploys — Lambda bundling runs in a container).

```bash
make install   # both venvs + npm ci + pre-commit hooks
make doctor    # verify toolchain, venv contents, and hook wiring
```

This project deliberately maintains **two virtual environments** because CDK and Lambda Powertools require incompatible `attrs` versions: `.venv` (CDK workstation — synth, stack tests, infra lint) and `.venv-lambda` (Lambda runtime — unit/integration tests, OpenAPI generation). One `uv.lock` holds both resolutions via `[tool.uv.conflicts]`. Never install Powertools into `.venv` or CDK into `.venv-lambda`; always go through the Make targets, which select the right venv. If a venv gets corrupted: `make clean-venvs && make install`.

## Running tests and checks

| Command | What it runs |
|---|---|
| `make test` | Unit tests over `lambda/` in `.venv-lambda` — **100% branch coverage is enforced** |
| `make test-cdk` | CDK stack assertions, snapshot tests, and the in-process cdk-nag gate (no Docker needed) |
| `make test-integration` | Live tests against a deployed stack (requires AWS credentials; not run in CI) |
| `make cdk-synth` | `cdk synth '**'` plus the cdk-nag validation-report check (needs Docker) |
| `make lint` / `make typecheck` / `make lint-docs` | Pre-commit hooks / mypy in both venvs / markdownlint |
| `make pr` | **Everything CI gates on, locally, in CI's order.** Run this before pushing — a clean `make pr` should mean a green CI run |

Two traps worth knowing:

- Don't run `pytest tests/cdk` directly — the project-wide pytest `addopts` carries a 100% `lambda/` coverage gate that only the unit suite satisfies; the Make targets override it.
- All CDK CLI invocations need the `'**'` glob (the stacks are nested in a `cdk.Stage`); the Make targets already include it.

cdk-nag findings on new resources are expected: prefer fixing the resource; otherwise acknowledge with `acknowledge_rules(construct, [{"id": ..., "reason": ...}])` and a rationale a reviewer can evaluate. The gate's failure output prints the exact finding ids that granular IAM rules require in `applies_to`.

## Coding standards

Tooling is configured centrally in `pyproject.toml` and enforced by pre-commit + CI — you don't need to memorize rules, but the posture is:

- **Formatting/linting**: ruff (double quotes, 120-column lines, a broad rule set including security and pytest-style checks). `make format` to format.
- **Typing**: mypy strict-ish (`disallow_untyped_defs`, pydantic plugin). Tests are deliberately outside the mypy gate.
- **Design/complexity**: pylint design thresholds and xenon complexity caps fail the build; if a function outgrows them, refactor rather than raising limits (existing limit bumps carry written justifications).
- **Security**: bandit + ruff's security rules + pip-audit; suppressions live in config with dated reasons.
- **Documentation is part of the change.** This repo's module docstrings and long-form comments are its design record — several are marked "verified live". When your change invalidates one, update it; when you make a non-obvious decision, write it down where the code lives. New log groups must declare `retention=`; stateful resources must declare a removal policy (a synth-time Aspect enforces both).

## Making common kinds of changes

**API surface** (`lambda/app.py`, `lambda/models.py`): add tests to keep the 100% coverage gate green, then `make openapi` and **commit `docs/openapi.json`** — CI fails on spec drift, and PRs run an oasdiff breaking-change gate against the base branch.

**Infrastructure** (`infrastructure/`): follow the local pattern (per-stack CMKs, confused-deputy-guarded service grants via the `nag_utils.py` helpers, explicit log retention). Run `make test-cdk`, update snapshots if templates changed, and run `make cdk-synth` with Docker before pushing IAM-touching changes. Don't rename the prod stacks, and don't add account/region-wide constructs without flagging it in the PR. Read CLAUDE.md's topology sections before moving resources between stacks — several moves are known to create dependency cycles.

**Dependencies** (`pyproject.toml`): run `make lock` and commit `uv.lock` **and** `lambda/requirements.txt` (the exported file bundled into the deployed Lambda — CI gates their sync). `make upgrade` refreshes everything with a 7-day PyPI cooldown against fresh malicious releases. Dependabot PRs are processed with `make deps-merge`, which runs the same lock step on each PR branch.

## Commit and PR conventions

- **Conventional Commits**: `feat:` `fix:` `docs:` `chore:` `ci:` `test:` `refactor:` `build:` (breaking changes use `!`). PR titles are gated by `.github/workflows/pr-title.yml` because squash-merge subjects feed the changelog.
- `CHANGELOG.md` is generated by git-cliff (`git cliff -o CHANGELOG.md`) — never edit it by hand; style fixes belong in `cliff.toml`.
- No `Co-Authored-By:` trailers on commits (project preference).
- Keep PRs reviewable: the CI `cdk-diff` job posts a CloudFormation diff comment on every PR — check it for unintended resource replacements, especially on stateful resources.

## Releases

Releases are driven by git-cliff from the commit history (see README "Cutting a release" for the full recipe): `git cliff --bumped-version` picks the version → bump `pyproject.toml` + `make lock` → regenerate the changelog → one `chore:` commit → annotated `vX.Y.Z` tag → push; the tag triggers `.github/workflows/release.yml`, which publishes the GitHub Release from the tag annotation.
