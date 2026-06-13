# Security Policy

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues, pull requests, or discussions.**

Report privately through GitHub's built-in private vulnerability reporting:

- Open the repository's [**Security** tab](https://github.com/timpugh/lambda-powertools-reference/security), then choose **Report a vulnerability** — or use the direct link:
  [https://github.com/timpugh/lambda-powertools-reference/security/advisories/new](https://github.com/timpugh/lambda-powertools-reference/security/advisories/new)

This routes the report to the maintainers through a private GitHub Security Advisory. Reporting this way (rather than by email) keeps the disclosure confidential until a fix is ready and avoids exchanging any personal contact details. See GitHub's documentation on
[privately reporting a security vulnerability](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
for how the flow works.

To help triage, please include where relevant:

- the affected file, workflow, or component;
- a description of the issue and its impact;
- steps to reproduce, or a proof of concept;
- any suggested remediation.

## What to expect

This is an open-source **reference architecture**, maintained on a best-effort basis — it is not a hosted service with an availability or response-time guarantee. That said, the intent is to:

- acknowledge a valid report once it has been reviewed;
- investigate and, where warranted, fix the issue on the `main` branch;
- coordinate disclosure so a fix is available before details are made public;
- credit reporters who want acknowledgement (entirely optional).

## Supported versions

This project is a template intended to be forked and adapted. Security fixes are applied to the **latest `main`**; tagged releases are point-in-time snapshots and are not separately patched. Forks are responsible for their own security posture once they diverge.

| Version | Supported |
| ------- | --------- |
| `main` (latest) | ✅ |
| Tagged releases / older commits | ❌ (re-base onto the latest `main`) |

## Scope

This policy covers the source in this repository — the CDK infrastructure code, the Lambda handler, the CI/CD workflows, and the build/release tooling. Vulnerabilities in upstream dependencies should be reported to their respective projects; this repo tracks them via Dependabot, `pip-audit`, and the scheduled dependency-audit workflow.
