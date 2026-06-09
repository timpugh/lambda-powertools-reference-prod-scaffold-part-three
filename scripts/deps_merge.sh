#!/usr/bin/env bash
# scripts/deps_merge.sh — automate the Dependabot maintainer flow.
#
# Background: this project commits both `uv.lock` and `lambda/requirements.txt`
# (the second is exported from the lambda group of the first; cdk-monitoring-
# constructs' PythonFunction bundles from it). Dependabot's `uv` ecosystem
# bumps `pyproject.toml` and usually regenerates `uv.lock`, but does NOT
# regenerate the exported requirements file — and CI's drift gate fails when
# they diverge. The fix is `make lock` on the PR branch, then a force-push.
# This script automates that loop.
#
# For each open Dependabot PR (or a specific one if a number is passed):
#   1. Sync local main from origin
#   2. Check out the PR branch
#   3. Rebase onto current main
#   4. Run `make lock` to regenerate uv.lock + lambda/requirements.txt
#   5. Run `uv run ruff format .` to absorb reformatter drift from tool bumps
#   6. Commit any resulting changes
#   7. Force-push the PR branch
#   8. Arm GitHub auto-merge (squash) on the PR
#   9. (When processing all) wait for the PR to merge or fail before moving on
#
# Conflicts during rebase, `make lock` failures, and CI failures cause the PR
# to be skipped with a clear log message rather than auto-resolved. The script
# never uses --admin to force-merge through failing checks; that's a manual
# call you make per-PR if appropriate.
#
# Usage:
#   scripts/deps_merge.sh           # process every open Dependabot PR
#   scripts/deps_merge.sh 42        # process only PR #42 (skips the wait loop)

set -euo pipefail

# ---- ANSI helpers ---------------------------------------------------------
red()    { printf '\033[31m%s\033[0m\n' "$*" >&2; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
blue()   { printf '\033[1;34m%s\033[0m\n' "$*"; }

# ---- Sanity checks --------------------------------------------------------
command -v gh >/dev/null 2>&1 || { red "gh CLI not installed (https://cli.github.com/)"; exit 1; }
command -v uv >/dev/null 2>&1 || { red "uv not installed (https://docs.astral.sh/uv/)"; exit 1; }
gh auth status >/dev/null 2>&1 || { red "gh not authenticated. Run 'gh auth login'."; exit 1; }

# ---- Restore starting branch on exit --------------------------------------
ORIGINAL_BRANCH=$(git rev-parse --abbrev-ref HEAD)
cleanup() {
    git rebase --abort 2>/dev/null || true
    if [ "$(git rev-parse --abbrev-ref HEAD)" != "$ORIGINAL_BRANCH" ]; then
        git checkout "$ORIGINAL_BRANCH" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# ---- Build the list of PRs to process -------------------------------------
SINGLE_PR="${1:-}"
PR_NUMBERS=()
if [ -n "$SINGLE_PR" ]; then
    PR_NUMBERS+=("$SINGLE_PR")
else
    # Oldest-first: ensures earlier PRs land before later ones rebase, so each
    # `make lock` runs against a main that includes its predecessors. Plain
    # `while read` loop instead of `mapfile` so this stays bash-3.2 compatible
    # (macOS ships bash 3.2, which lacks mapfile).
    while IFS= read -r pr_number; do
        PR_NUMBERS+=("$pr_number")
    done < <(
        gh pr list --state open --author "app/dependabot" \
            --json number,createdAt --jq 'sort_by(.createdAt)[].number'
    )
fi

if [ ${#PR_NUMBERS[@]} -eq 0 ]; then
    green "No open Dependabot PRs to process."
    exit 0
fi

blue "Will process ${#PR_NUMBERS[@]} PR(s): ${PR_NUMBERS[*]}"
echo

# ---- Process one PR -------------------------------------------------------
# Returns 0 on a successful local push (caller may then poll for merge).
# Returns 1 on any skip condition (conflict, lock failure, push failure).
process_pr() {
    local pr=$1
    blue "=== PR #$pr ==="

    # Refuse to operate on a dirty tree — `git checkout main` would silently
    # leave us on the wrong branch, after which `make lock` would regenerate
    # files against the wrong base and the force-push would clobber the PR.
    if [ -n "$(git status --porcelain)" ]; then
        red "Working tree is dirty. Commit or stash before running. Skipping PR #$pr."
        return 1
    fi

    if ! git checkout main >/dev/null 2>&1; then
        red "Could not check out main. Skipping PR #$pr."
        return 1
    fi
    git pull origin main --quiet

    if ! gh pr checkout "$pr" >/dev/null 2>&1; then
        red "Could not check out PR #$pr (may already be closed). Skipping."
        return 1
    fi

    local branch
    branch=$(git rev-parse --abbrev-ref HEAD)
    yellow "  branch: $branch"

    if ! git rebase main >/dev/null 2>&1; then
        red "  Rebase conflict — aborting and skipping. Resolve manually if you want this PR landed."
        git rebase --abort 2>/dev/null || true
        return 1
    fi

    yellow "  Running make lock..."
    if ! make lock >/dev/null 2>&1; then
        red "  make lock failed — skipping. Inspect locally with 'gh pr checkout $pr && make lock'."
        return 1
    fi

    yellow "  Running ruff format..."
    if ! uv run ruff format . >/dev/null 2>&1; then
        red "  ruff format failed — skipping. Inspect locally."
        return 1
    fi

    # Commit only if there's actually drift to capture. If a commit attempt
    # fails (typically a pre-commit hook flagging the regenerated artefacts),
    # surface the failure rather than force-pushing whatever is staged.
    if ! git diff --quiet --exit-code || ! git diff --staged --quiet --exit-code; then
        git add uv.lock lambda/requirements.txt 2>/dev/null || true
        git add -u 2>/dev/null || true
        if ! git commit -m "chore: regenerate lockfile and run ruff format" >/dev/null 2>&1; then
            red "  git commit failed (pre-commit hook?). Skipping PR."
            return 1
        fi
    fi

    yellow "  Force-pushing..."
    # --force-if-includes (Git 2.30+) closes a window in plain --force-with-lease
    # where a fetch between checkout and push could let a concurrent push slip
    # under the lease check.
    if ! git push --force-with-lease --force-if-includes origin "$branch" >/dev/null 2>&1; then
        red "  Push failed — skipping (remote may have moved)."
        return 1
    fi

    yellow "  Arming auto-merge..."
    # Don't swallow the result: if arming fails (auto-merge disabled on the repo,
    # branch protection, insufficient perms) the operator must know, otherwise
    # wait_for_pr later burns its full timeout on a PR that will never auto-merge.
    if ! gh pr merge "$pr" --auto --squash >/dev/null 2>&1; then
        red "  Could not arm auto-merge (auto-merge disabled, branch protection, or perms?) — skipping."
        red "  Merge this PR manually, or enable auto-merge, then re-run."
        return 1
    fi

    green "  Local work complete. Auto-merge armed; CI will merge on green."
    return 0
}

# ---- Wait for a PR to land or fail ---------------------------------------
wait_for_pr() {
    local pr=$1
    local timeout=1800   # 30 min hard cap per PR
    local elapsed=0
    yellow "  Waiting for PR #$pr to merge (30 min cap)..."
    while [ $elapsed -lt $timeout ]; do
        local state
        state=$(gh pr view "$pr" --json state --jq '.state' 2>/dev/null || echo "UNKNOWN")
        case "$state" in
            MERGED) green "  PR #$pr merged ✓"; return 0 ;;
            CLOSED) yellow "  PR #$pr closed (likely superseded by an earlier merge)"; return 0 ;;
        esac
        # Bail early on any failed check so we don't burn the full timeout.
        local has_failure
        has_failure=$(gh pr view "$pr" --json statusCheckRollup --jq \
            '[.statusCheckRollup[].conclusion] | any(. == "FAILURE")' 2>/dev/null || echo "false")
        if [ "$has_failure" = "true" ]; then
            red "  PR #$pr has failing checks — leaving open for manual investigation."
            return 1
        fi
        sleep 30
        elapsed=$((elapsed + 30))
    done
    yellow "  Timed out waiting for PR #$pr — moving on."
    return 1
}

# ---- Main loop ------------------------------------------------------------
for pr in "${PR_NUMBERS[@]}"; do
    if process_pr "$pr"; then
        # Single-PR mode: kick off the work and exit; user picks up from there.
        # All-PRs mode: wait so the next PR rebases onto a main that includes this one.
        if [ -z "$SINGLE_PR" ] && [ ${#PR_NUMBERS[@]} -gt 1 ]; then
            # If the predecessor doesn't actually merge (failing checks or timeout),
            # the sequential-rebase invariant is broken: every remaining PR would
            # rebase onto a main WITHOUT this one. Stop rather than silently
            # processing the rest against a stale base.
            if ! wait_for_pr "$pr"; then
                red "PR #$pr did not merge — stopping so later PRs don't rebase onto a main missing it."
                red "Resolve PR #$pr (or re-run with PR=<n> per PR), then re-run for the remainder."
                break
            fi
        fi
    fi
    echo
done

git checkout main >/dev/null 2>&1
git pull origin main --quiet

green "Done. Final open Dependabot PRs:"
gh pr list --state open --author "app/dependabot" 2>/dev/null || true
