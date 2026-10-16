---
icon: lucide/book-open
---

# Lambda Powertools Reference

A reference serverless API built on AWS Lambda Powertools, deployed behind
API Gateway, CloudFront, and AWS WAF. This site covers two audiences:

## Code reference (for developers)

Autodoc-rendered pages for every module under `infrastructure/` and
`lambda/`, generated from the Google-style docstrings in the source via
[mkdocstrings](https://mkdocstrings.github.io/):

- [Lambda handler](lambda_handler.md) — the Powertools route handler and cross-cutting concerns (models and service logic have their own pages below).
- [Data models](models.md) — Pydantic request/response contracts and the typed env-var view.
- [Service layer](service.md) — greeting business logic: SSM lookup, feature flag, message composition.
- [AppConfig extension store](extension_store.md) — feature-flag store reading the AppConfig Lambda extension's local endpoint.
- [App stage](app_stage.md) — the `cdk.Stage` grouping all five stacks; stack naming and the environment dimension.
- [Data stack](data_stack.md) — DynamoDB idempotency table + its dedicated CMK, `retain_data`-gated.
- [Backend application construct](backend_app.md) — `BackendApp`: the domain construct that owns every backend resource.
- [Backend stack](backend_stack.md) — thin wrapper composing `BackendApp` and attaching stack-level cdk-nag suppressions.
- [WAF stack](waf_stack.md) — us-east-1 WebACL attached to CloudFront.
- [Frontend stack](frontend_stack.md) — CloudFront, S3 access logs, Glue + Athena analytics.
- [Audit stack](audit_stack.md) — CloudTrail data-event trail + its log bucket + dedicated CMK.
- [Pipeline stack](pipeline_stack.md) — the self-mutating CD pipeline (`-c pipeline=true`).
- [NAG utilities](nag_utils.md) — cdk-nag suppression helpers shared across stacks.
- [Validation aspects](validation_aspects.md) — synth-time checks for explicit log retention and removal policies.

## API reference (for callers)

A standalone [Scalar](https://github.com/scalar/scalar) API Reference page
that renders the OpenAPI spec in the browser:

- [**HTTP API Reference**](api.html) — paths, request / response schemas, status codes, and an interactive request sandbox.

The OpenAPI spec itself is published as [openapi.json](openapi.json) if a
caller wants to point their own tooling at it. Both files are regenerated
from the live Pydantic models in `lambda/app.py` on every docs build, so
what you see here always reflects the code currently on `main`.
