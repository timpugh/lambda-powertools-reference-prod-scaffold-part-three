#!/usr/bin/env python3
"""Render a CloudFormation diff between two synthesized CDK cloud assemblies.

The ``cdk-diff`` CI job (.github/workflows/ci.yml) synthesizes the base branch
and the PR branch into two cloud-assembly directories, then runs this script to
produce a Markdown report of what the PR changes in the synthesized templates —
resources added / removed / modified. The report is posted as a sticky PR
comment (and the job summary) so a reviewer can spot a destructive change to a
stateful resource *before* merge.

It shells out to the pinned ``npx cdk diff`` and needs **no AWS account or
credentials**: for each stack it compares the PR-synthesized template against the
base template file via ``--template``, using ``--app <assembly>`` so nothing is
re-synthesized. Stacks live inside a ``cdk.Stage``, so the real templates are in
the assembly's nested ``assembly-*/`` sub-directory; the stack's hierarchical
display path (read from that sub-assembly's ``manifest.json``) is both the diff
selector and the stable key for matching a stack across the two branches.

Local use::

    npx cdk synth '**' -o /tmp/base.out   # on the base branch / worktree
    npx cdk synth '**' -o /tmp/pr.out     # on your branch
    python scripts/cdk_pr_diff.py --base-out /tmp/base.out --pr-out /tmp/pr.out \
        --base-ref main --output /tmp/cdk-diff.md

Stdlib only — no third-party dependency, so it runs without touching either venv.
"""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404 - only ever invokes the locally pinned `npx cdk`
import sys
from pathlib import Path

# cdk prints one of these when a stack is unchanged (phrasing differs by whether
# a single stack or the whole app is selected); either means "skip this stack".
_NO_DIFF_MARKERS = ("number of stacks with differences: 0", "there were no differences")
# GitHub rejects an issue comment body over 65536 chars; leave headroom for the
# marker + truncation note the workflow prepends/appends.
_MAX_BODY = 60000
_EMPTY_TEMPLATE = '{"Resources": {}}'


def _stacks(assembly: Path) -> dict[str, Path]:
    """Map each CloudFormation stack's display path to its template file.

    Reads the nested Stage sub-assembly manifests so the keys are the
    hierarchical ids (``<stage>/<stack>``) that ``cdk diff`` accepts as a
    selector and that stay stable across branches.
    """
    stacks: dict[str, Path] = {}
    for manifest_path in sorted(assembly.glob("assembly-*/manifest.json")):
        manifest = json.loads(manifest_path.read_text())
        for artifact_id, artifact in manifest.get("artifacts", {}).items():
            if artifact.get("type") != "aws:cloudformation:stack":
                continue
            display = artifact.get("displayName", artifact_id)
            stacks[display] = manifest_path.parent / f"{artifact_id}.template.json"
    return stacks


def _diff(stack: str, app: Path, baseline: Path) -> str:
    """Return ``cdk diff`` output for one stack against a baseline template."""
    proc = subprocess.run(  # nosec B603 B607 - fixed argv, no shell, pinned `npx cdk`
        # --exclusively: diff only this stack, not the dependency stacks selecting
        # it would otherwise pull in (cdk rejects --template with >1 stack).
        ["npx", "cdk", "diff", stack, "--exclusively", "--app", str(app), "--template", str(baseline), "--no-color"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    # Drop synth-time annotation noise (acknowledged warnings, dependency notes)
    # so the report shows only the actual template delta.
    noise = ("[Warning at", "After deploying once", "Including dependency stacks", "[ack:")
    return "\n".join(line for line in proc.stdout.splitlines() if not line.lstrip().startswith(noise)).strip()


def _render(pr_out: Path, base_out: Path, base_ref: str, empty: Path) -> str:
    """Build the Markdown diff report comparing the PR assembly to the base."""
    pr_stacks = _stacks(pr_out)
    base_stacks = _stacks(base_out)
    sections: list[str] = []

    for display in sorted(pr_stacks):
        baseline = base_stacks.get(display, empty)
        text = _diff(display, pr_out, baseline)
        if any(marker in text.lower() for marker in _NO_DIFF_MARKERS):
            continue
        note = " *(new stack)*" if display not in base_stacks else ""
        sections.append(
            f"<details><summary><strong>{display}</strong>{note}</summary>\n\n```\n{text}\n```\n\n</details>"
        )

    for display in sorted(base_stacks):
        if display not in pr_stacks:
            sections.append(f"- ⚠️ **{display}** — stack removed by this PR")

    header = f"## \U0001f3d7️ CDK infra diff — PR vs `{base_ref}`\n"
    body = header + ("\n".join(sections) if sections else "\n_No CloudFormation template changes._\n")
    if len(body) > _MAX_BODY:
        body = body[:_MAX_BODY] + "\n\n_…diff truncated; run `make cdk-diff` locally for the full output._"
    return body


def main() -> int:
    """Parse args, render the diff report, and write it to ``--output``."""
    parser = argparse.ArgumentParser(description="Render a CDK template diff between two cloud assemblies.")
    parser.add_argument("--pr-out", required=True, type=Path, help="PR-branch cloud assembly (cdk synth -o)")
    parser.add_argument("--base-out", required=True, type=Path, help="Base-branch cloud assembly (cdk synth -o)")
    parser.add_argument("--base-ref", default="base", help="Base branch name, for the report heading")
    parser.add_argument("--output", required=True, type=Path, help="Markdown report destination")
    args = parser.parse_args()

    empty = args.output.parent / "_cdk_pr_diff_empty.template.json"
    empty.write_text(_EMPTY_TEMPLATE)
    args.output.write_text(_render(args.pr_out, args.base_out, args.base_ref, empty))
    sys.stderr.write(f"cdk diff report written to {args.output}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
