"""Serverless App Lambda function using AWS Lambda Powertools.

This module implements a serverless API endpoint that returns a greeting message.
It demonstrates the use of Powertools utilities including structured logging,
X-Ray tracing, CloudWatch metrics, idempotency, SSM parameters, feature flags,
Pydantic-backed request/response validation (with an OpenAPI spec generated
at documentation-build time — see scripts/generate_openapi.py), and Event Source
Data Classes.
"""

import os
from typing import Annotated, Any, cast

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.event_handler import APIGatewayRestResolver
from aws_lambda_powertools.event_handler.api_gateway import CORSConfig
from aws_lambda_powertools.event_handler.exceptions import InternalServerError
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.data_classes import APIGatewayProxyEvent
from aws_lambda_powertools.utilities.feature_flags import AppConfigStore, FeatureFlags
from aws_lambda_powertools.utilities.feature_flags.exceptions import (
    ConfigurationStoreError,
    SchemaValidationError,
    StoreClientError,
)
from aws_lambda_powertools.utilities.idempotency import (
    DynamoDBPersistenceLayer,
    idempotent,
)
from aws_lambda_powertools.utilities.idempotency.config import IdempotencyConfig
from aws_lambda_powertools.utilities.idempotency.exceptions import IdempotencyKeyError
from aws_lambda_powertools.utilities.parameters import SSMProvider
from aws_lambda_powertools.utilities.parameters.exceptions import GetParameterError
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.config import Config
from pydantic import BaseModel, Field, PositiveInt


class EnvVars(BaseModel):
    """Typed view of the environment variables this handler requires.

    Validated once at module load (below). A missing or malformed env var
    (table name, profile name, a non-numeric cache age) would otherwise only
    surface deep inside boto3 as an opaque parameter-validation error; failing
    at import time with pydantic's field-by-field report makes the
    misconfiguration obvious in CloudWatch on the very first invocation.
    Validation is stricter than a presence check: empty strings are rejected
    and the cache age must parse as a positive integer.
    """

    IDEMPOTENCY_TABLE_NAME: Annotated[str, Field(min_length=1)]
    GREETING_PARAM_NAME: Annotated[str, Field(min_length=1)]
    APPCONFIG_APP_NAME: Annotated[str, Field(min_length=1)]
    APPCONFIG_ENV_NAME: Annotated[str, Field(min_length=1)]
    APPCONFIG_PROFILE_NAME: Annotated[str, Field(min_length=1)]
    # In-memory TTL for the fetched feature-flag configuration. Defaults to the
    # same 300s the SSM read uses (see ssm_provider.get below) so the two
    # config-fetch paths share one caching posture; override per environment
    # via the Lambda environment block in CDK when flags must propagate faster.
    APPCONFIG_MAX_AGE_SECONDS: PositiveInt = 300


# Module-level so a bad deployment fails the cold start, not the Nth request.
# Extra keys in os.environ are ignored by pydantic's default model config.
_ENV = EnvVars.model_validate(dict(os.environ))


logger = Logger()
tracer = Tracer()
metrics = Metrics()

# Shared botocore retry config applied to every AWS SDK client this handler uses
# (SSM, AppConfig, DynamoDB). botocore retries transient failures by default, but
# pinning the policy here makes the posture explicit and tunable rather than
# implicit in the SDK default — the same "visible in code, not implicit in the
# runtime default" rationale behind setting recursive_loop="Terminate" on the
# function in CDK. "adaptive" mode adds client-side rate limiting on top of
# exponential backoff with jitter, which backs off proactively when a dependency
# starts returning throttling (429) responses; its token-bucket state is useful
# here because the clients are module-scoped (constructed once below) and reused
# across every warm invocation, so the limiter persists for the container's life.
# max_attempts=4 is the TOTAL attempt budget in standard/adaptive modes (1 initial
# request + up to 3 retries; only legacy mode counts retries-after-initial) —
# comfortably inside the function's 10s timeout. Retrying is
# safe because the write path (DynamoDB via @idempotent) is idempotent on the
# client-supplied Idempotency-Key, and the SSM/AppConfig calls are reads. This
# implements the AWS "retry with backoff" prescriptive-guidance pattern without
# hand-rolling a retry loop — see README "Patterns deliberately not used".
boto_config = Config(retries={"mode": "adaptive", "max_attempts": 4})

# enable_validation=True wires Pydantic into the resolver. Request bodies and
# response return types are validated against their model annotations, and
# those same models drive the OpenAPI schema read by scripts/generate_openapi.py.
# We deliberately do NOT call app.enable_swagger() here — exposing the spec at
# runtime would publish the full API surface to any caller. The spec is
# instead rendered into Zensical at documentation-build time.
app = APIGatewayRestResolver(
    # allow_headers is only relevant for the response-side CORS Access-Control-
    # Allow-Headers value, but for completeness we list Idempotency-Key here
    # too — keeps the Powertools CORSConfig in sync with API Gateway's preflight
    # configuration declared in CDK.
    cors=CORSConfig(
        allow_origin="*",
        max_age=300,
        allow_headers=[
            "Content-Type",
            "X-Amz-Date",
            "Authorization",
            "X-Api-Key",
            "X-Amzn-Trace-Id",
            "Idempotency-Key",
        ],
    ),
    enable_validation=True,
)

# Idempotency setup.
# Key on the client-supplied "Idempotency-Key" header. HTTP header names are
# case-insensitive (RFC 9110) but JMESPath lookups are exact-match, so
# lambda_handler lowercases all header keys before the @idempotent layer sees
# the event — the JMESPath then only needs the single lowercase form instead of
# enumerating casings. raise_on_no_idempotency_key turns a missing header into
# Powertools' IdempotencyKeyError, which the resolver below converts into a 400
# BadRequest — making the requirement enforced rather than implicit. Keying on a
# client-controlled value (instead of the server-generated
# requestContext.requestId, which changes on every retry) is what actually makes
# the layer deduplicate.
persistence_layer = DynamoDBPersistenceLayer(
    table_name=_ENV.IDEMPOTENCY_TABLE_NAME,
    boto_config=boto_config,
)
idempotency_config = IdempotencyConfig(
    event_key_jmespath='headers."idempotency-key"',
    raise_on_no_idempotency_key=True,
    expires_after_seconds=3600,
)

# Feature Flags setup.
# AppConfigStore is given an explicit appconfigdata client built with the shared
# retry config. Passing boto_config= (or sdk_config=) to AppConfigStore instead
# routes through a parameter that Powertools v3 deprecated and emits a warning,
# so an explicit client is the clean way to apply the retry policy here.
# max_age lifts the fetched-configuration TTL from the Powertools default of
# 5 seconds — which would re-poll the AppConfig data plane every 5s per warm
# container — to the same 300s posture as the SSM read below. Tunable per
# environment via APPCONFIG_MAX_AGE_SECONDS.
app_config_store = AppConfigStore(
    environment=_ENV.APPCONFIG_ENV_NAME,
    application=_ENV.APPCONFIG_APP_NAME,
    name=_ENV.APPCONFIG_PROFILE_NAME,
    max_age=_ENV.APPCONFIG_MAX_AGE_SECONDS,
    boto3_client=boto3.client("appconfigdata", config=boto_config),
)
feature_flags = FeatureFlags(store=app_config_store)

# SSM provider for the greeting parameter. An explicit SSMProvider (rather than
# the module-level get_parameter helper) is used so the shared retry config can
# be injected — the free helper builds its own client and takes no boto_config.
# The in-memory cache is per-provider, so reuse this one instance across
# invocations to preserve warm-container caching (max_age is set per get() call).
ssm_provider = SSMProvider(boto_config=boto_config)

# Greeting parameter name resolved at module load — validated as non-empty by
# the EnvVars model above rather than letting boto3 reject an empty key at runtime.
GREETING_PARAM_NAME = _ENV.GREETING_PARAM_NAME


class GreetingResponse(BaseModel):
    """Response body for GET /greeting."""

    message: str = Field(
        ...,
        description="Greeting from SSM Parameter Store, optionally suffixed when the enhanced_greeting flag is on.",
        examples=["hello world", "hello world - enhanced mode enabled"],
    )


class MissingIdempotencyKeyResponse(BaseModel):
    """Body of the 400 returned when the required Idempotency-Key header is absent.

    Shape must match the hand-built response in ``lambda_handler`` — that 400 is
    constructed outside the Powertools resolver (the idempotency layer rejects
    the request before the resolver runs), so this model exists purely to
    document the contract in the generated OpenAPI spec.
    """

    message: str = Field(
        "Idempotency-Key header is required",
        description="Explanation of the rejected request.",
    )


class InternalErrorResponse(BaseModel):
    """Body of the 500 produced by Powertools' ServiceError handling.

    When ``get_greeting`` raises ``InternalServerError`` (e.g. the SSM read fails),
    the resolver serialises it as ``{"statusCode": 500, "message": ...}`` —
    documented here so spec consumers see the failure shape, not just the 200.
    """

    statusCode: int = Field(500, description="HTTP status code echoed in the body.")  # noqa: N815 — matches the wire format
    message: str = Field(..., description="Error description.", examples=["Failed to fetch greeting"])


@app.get(
    "/greeting",
    summary="Return a greeting",
    description=(
        "Returns the greeting string configured in SSM Parameter Store. "
        "When the `enhanced_greeting` AppConfig feature flag is enabled for "
        "the caller's source IP, the response includes the feature flag's "
        "configured suffix. Requires an `Idempotency-Key` header; requests "
        "without one are rejected with 400 before this route runs."
    ),
    response_description="A JSON object containing the resolved greeting.",
    tags=["Greeting"],
    # Error responses documented explicitly — the generated OpenAPI spec
    # otherwise covers only the happy path, leaving the 400 (missing
    # Idempotency-Key, enforced outside the resolver) and the 500 (SSM
    # failure) invisible to spec consumers and breaking-change tooling.
    responses={
        200: {
            "description": "The resolved greeting.",
            "content": {"application/json": {"model": GreetingResponse}},
        },
        400: {
            "description": "Missing Idempotency-Key header.",
            "content": {"application/json": {"model": MissingIdempotencyKeyResponse}},
        },
        500: {
            "description": "Greeting could not be fetched from SSM Parameter Store.",
            "content": {"application/json": {"model": InternalErrorResponse}},
        },
    },
)
@tracer.capture_method(capture_response=False)
def get_greeting() -> GreetingResponse:
    """Handle GET /greeting requests.

    Fetches the greeting from SSM Parameter Store, checks the enhanced_greeting
    feature flag, emits a CloudWatch metric, and logs request metadata from
    the API Gateway event.

    Returns:
        GreetingResponse: Validated response model with a ``message`` field.
    """
    metrics.add_metric(name="GreetingRequests", unit=MetricUnit.Count, value=1)

    # Access typed event data via Event Source Data Classes
    event: APIGatewayProxyEvent = app.current_event
    source_ip = event.request_context.identity.source_ip
    user_agent = event.request_context.identity.user_agent
    request_id = event.request_context.request_id

    logger.info(
        "Request received",
        source_ip=source_ip,
        user_agent=user_agent,
        request_id=request_id,
    )

    # Fetch greeting from SSM Parameter Store. Powertools wraps boto3 errors
    # (ClientError, BotoCoreError) as GetParameterError; catch only that so
    # truly unexpected exceptions propagate to Powertools' default handler
    # and surface with the right type in metrics and X-Ray.
    # max_age=300 raises Powertools' in-memory TTL from its 5-second default
    # so warm containers reuse the value for 5 minutes between SSM calls.
    # The greeting changes via deployment, not at runtime, so a longer TTL
    # is safe and meaningfully reduces SSM API spend at higher RPS.
    try:
        # SSMProvider.get returns str | bytes | dict | None to cover transform/
        # binary cases; this is a plain String parameter with no transform, so it
        # is always str at runtime. cast keeps the downstream message typed as str.
        greeting = cast("str", ssm_provider.get(GREETING_PARAM_NAME, max_age=300))
    except GetParameterError as exc:
        logger.exception("Failed to fetch greeting from SSM", param_name=GREETING_PARAM_NAME)
        raise InternalServerError("Failed to fetch greeting") from exc
    logger.info("Greeting fetched from parameter store", greeting=greeting)

    # Check feature flag — non-critical, fall back to default on failure.
    # Pass source_ip + user_agent as context so AppConfig rules can match on
    # them (the route's docstring promises IP-based gating; without context
    # the rule engine can never see the values to evaluate against).
    # Catch only the Powertools FeatureFlags exception types — programming
    # errors (TypeError, AttributeError) intentionally propagate so they
    # surface as bugs in metrics rather than being silently absorbed by the
    # fallback path.
    try:
        enhanced = feature_flags.evaluate(
            name="enhanced_greeting",
            context={"source_ip": source_ip, "user_agent": user_agent},
            default=False,
        )
    except (ConfigurationStoreError, SchemaValidationError, StoreClientError):
        # exc_info=True puts the underlying exception in the log record —
        # without it a permanently broken AppConfig integration (bad IAM, bad
        # config, KMS denial) is indistinguishable in CloudWatch from a
        # transient network blip, and the cause is unrecoverable after the
        # fact. Kept at WARNING because the request still succeeds.
        logger.warning("Feature flag evaluation failed, falling back to default", exc_info=True)
        # Emit a metric on the fallback so a bad flag *config* is observable.
        # A bad config is caught here and degraded gracefully (the request still
        # returns 200), so it produces no Lambda error or 5xx — this metric is
        # the only signal that the config is broken. It's the signal a production
        # fork wires an AppConfig deployment monitor to (gradual rollout +
        # auto-rollback — a documented add-on, not shipped; see the AppConfig
        # deployment comment in infrastructure/backend_app.py). Lands in the
        # ServerlessApp namespace with the service dimension Powertools adds.
        metrics.add_metric(name="FeatureFlagEvaluationFailure", unit=MetricUnit.Count, value=1)
        enhanced = False

    if enhanced:
        message = f"{greeting} - enhanced mode enabled"
        logger.info("Enhanced greeting enabled")
    else:
        message = greeting

    return GreetingResponse(message=message)


@idempotent(config=idempotency_config, persistence_store=persistence_layer)
def _resolve_with_idempotency(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Inner handler wrapped by @idempotent.

    Split out so the outer handler can catch IdempotencyKeyError (raised by
    @idempotent before this body runs when the request has no Idempotency-Key
    header) and return a 400 instead of letting Lambda surface a 500.

    Caching caveat: @idempotent wraps the whole resolver, and Powertools persists
    whatever this function *returns* (only raised exceptions are not cached). The
    APIGatewayRestResolver returns non-2xx outcomes — 404 (unknown route), 422
    (validation), and the 500 produced when ``get_greeting`` raises InternalServerError —
    as response dicts rather than exceptions, so those are cached under the
    client's Idempotency-Key for ``expires_after_seconds`` (1 hour). A client that
    reuses the same key after fixing the route/payload would get the stale error
    replayed. This is acceptable for this single-GET reference (the documented
    contract is one key per logical request — see README "Idempotency keys"); a
    fork that wants transient errors retried under the same key should move
    idempotency onto ``get_greeting`` (the success-bearing function) instead of the
    resolver — Powertools supports this directly via
    ``@idempotent_function(data_keyword_argument=..., ...)`` with a
    ``PydanticSerializer`` output serializer, so the cached record stores the
    validated response model rather than a raw dict — accepting that the
    missing-key→400 path then needs a separate guard.
    """
    return cast("dict[str, Any]", app.resolve(event, context))


# correlation_id_path lifts the API Gateway request id into a correlation_id
# field on every log record of the invocation, so one request's records can be
# joined in Logs Insights without relying on the single line that logs
# request_id by hand. capture_response=False keeps response payloads out of
# X-Ray segments — traces record timing and metadata, not body content.
@logger.inject_lambda_context(correlation_id_path=correlation_paths.API_GATEWAY_REST)
@tracer.capture_lambda_handler(capture_response=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def lambda_handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Lambda entry point.

    Resolves the API Gateway event through the router and returns the result.
    Decorated with Powertools Logger, Tracer, Metrics; the inner function
    handles Idempotency so a missing Idempotency-Key header surfaces as a 400
    instead of an unhandled 500.

    Args:
        event: API Gateway Lambda proxy event.
        context: Lambda runtime context.

    Returns:
        dict: API Gateway Lambda proxy response.
    """
    # Header names are case-insensitive on the wire but the idempotency layer's
    # JMESPath is exact-match, so normalize keys to lowercase once here. The
    # resolver is unaffected — Powertools' event data classes already do
    # case-insensitive header lookups. A copy (not in-place mutation) keeps the
    # caller's event untouched. `or {}` covers manual invocations (console test
    # events, aws lambda invoke) that omit the headers map entirely.
    event = {**event, "headers": {k.lower(): v for k, v in (event.get("headers") or {}).items()}}

    # cast() restores the return type after @idempotent erases it. Powertools'
    # app.resolve() is well-typed in .venv-lambda, but the @idempotent wrapper
    # passes return values through as Any; .venv (CDK side, no Powertools)
    # already sees the function as Any. The cast is a no-op at runtime.
    try:
        return cast("dict[str, Any]", _resolve_with_idempotency(event, context))
    except IdempotencyKeyError:
        logger.warning("Request rejected: missing Idempotency-Key header")
        return {
            "statusCode": 400,
            # This response is built outside the Powertools resolver, so it must
            # carry its own CORS header (the resolver adds one to every response
            # it builds) — without it, cross-origin browsers can't read the 400
            # body at all. Keep in sync with CORSConfig.allow_origin above.
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
            },
            "body": '{"message":"Idempotency-Key header is required"}',
            "isBase64Encoded": False,
        }
