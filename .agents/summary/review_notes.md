# Review Notes

Consistency and completeness review of this documentation set, performed at generation time (2026-07-06).

## Consistency check

Cross-document facts verified consistent:

- **Stack inventory and names** (five stacks; prod names `ServerlessApp{Waf,Data,Backend,Frontend,Audit}-{region}`) — consistent across codebase_info.md, architecture.md, components.md, and verified against `infrastructure/app_stage.py`.
- **cdk-nag gate description** (v3 policy-validation plugins; report check is the gate, not the exit code) — consistent across architecture.md, interfaces.md, workflows.md; matches `scripts/check_validation_report.py` and `tests/cdk/test_stage.py` docstrings.
- **Two-venv model** — consistent across dependencies.md, workflows.md, components.md; matches `pyproject.toml` `[tool.uv.conflicts]` and the Makefile.
- **Coverage policy** (100% branch gate on `lambda/` in the unit suite only; combined cross-venv coverage is informational) — consistent; matches `pyproject.toml` `addopts` and the Makefile's `--override-ini` handling.
- **Deployment traps** (`appconfig_monitor` cold-deploy abort; `'**'` glob) — described identically in architecture.md, interfaces.md, workflows.md; matches `cdk.json`, `Makefile`, and `app.py` comments.

Inconsistencies noticed **in the repository itself** (not introduced by this documentation) — both **resolved on 2026-07-06**:

1. **PITR window wording** *(resolved)*: `TODO.md` said "PITR alone is a 35-day rolling window", while `data_stack.py` configures `recovery_period_in_days=1`. TODO.md and README now distinguish the configured 1-day window from the feature's 35-day cap.
2. **Two Python versions in play** *(resolved)*: the workstation toolchain targeted Python 3.13 (`requires-python >= 3.13`, ruff/mypy `py313`) while the Lambda runtime is `PYTHON_3_14`. The toolchain floors are now aligned to 3.14 (`requires-python >= 3.14`, ruff `py314`, mypy `3.14`) — the local venvs already ran 3.14, so only the config floors and prose moved.

## Completeness check

Areas covered in depth: infrastructure modules (all docstrings + full reads of the smaller stacks), the Lambda handler/service/model layers (full reads), Makefile (full read), `pyproject.toml` (full read), CI workflow (full read), pre-commit config (full read), test fixtures and the nag-gate test design, `cdk.json`, scripts headers.

Areas documented at lower resolution (acceptable gaps, listed for transparency):

1. **`infrastructure/backend_app.py` and `infrastructure/frontend_stack.py` interior wiring** — these two modules were read via docstrings, method signatures, and targeted greps (runtime, architecture, concurrency verified) rather than line-by-line. Fine-grained facts (exact throttling numbers, individual suppression rationales, RUM session-sampling config) should be read from source when needed.
2. **GitHub workflows other than `ci.yml`** (`codeql.yml`, `scorecard.yml`, `dependency-audit.yml`, `dependabot-auto-merge.yml`, `docs.yml`, `release.yml`, `pr-title.yml`) — summarized from names and cross-references in README/Makefile comments, not read in full.
3. **`frontend/index.html`** — verified to contain the RUM client bootstrap (config from `/config.json`) and the `GET /greeting` fetch, but the inline JavaScript was not exhaustively documented. No build step exists.
4. **Zensical site content** (`docs/*.md`, `zensical.toml` nav) — the docs pipeline is documented; individual published pages are not summarized (they are mkdocstrings renderings of module docstrings already covered).
5. **`tests/cdk/test_stacks.py` and `test_snapshots.py` internals** — suite purposes documented; individual assertions not enumerated.
6. **Integration test specifics** (`tests/integration/`) — noted as live-stack tests excluded from CI; individual cases not documented.

Language-support limitations: none material. The repository is Python-first; the only non-Python application code is the small inline JavaScript in `frontend/index.html` (item 3 above).

## Overlapping agent-facing documentation in the repo

This knowledge base coexists with four hand-maintained agent/contributor surfaces. They were treated as authoritative inputs, and the consolidated files point to them rather than duplicating them:

- `CLAUDE.md` — project instructions (deepest rationale; load-bearing gotchas with "verified live" provenance).
- `llms.txt` — a compact agent-pointer file whose key facts match this summary.
- `.claude/skills/ship-a-change/SKILL.md` — a process SOP (registered as a project skill) for shipping a change through every gate; the procedural companion to workflows.md.
- `.claude/skills/wa-review/SKILL.md` — Well-Architected review skill.

**Risk to manage**: five overlapping documents can drift apart. When repository behavior changes, update `CLAUDE.md`/`README.md` first (authoritative), then regenerate or hand-patch this summary, `AGENTS.md`, and `llms.txt`.

## Recommendations

1. **Refresh cadence**: regenerate this summary after structural changes (new stacks, moved modules, changed gates), not for routine code changes — most content is structural and stable.
2. **Consider linking `AGENTS.md` from `llms.txt`** (or vice versa) so non-Claude agents discover one canonical starting file.
3. When deep detail on `backend_app.py`/`frontend_stack.py` matters (IAM suppressions, RUM wiring), read the module docstrings first — they carry the rationale this summary compresses.
