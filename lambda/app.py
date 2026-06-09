"""Hello World Lambda function using AWS Lambda Powertools.

This module implements a serverless API endpoint that returns a greeting message.
It demonstrates the use of Powertools utilities including structured logging,
X-Ray tracing, CloudWatch metrics, idempotency, SSM parameters, feature flags,
Pydantic-backed request/response validation (with an OpenAPI spec generated
at documentation-build time — see scripts/generate_openapi.py), and Event Source
Data Classes.
"""

import os
from typing import Any, cast

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.event_handler import APIGatewayRestResolver
from aws_lambda_powertools.event_handler.api_gateway import CORSConfig
from aws_lambda_powertools.event_handler.exceptions import InternalServerError
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
from pydantic import BaseModel, Field


def _require_env(name: str) -> str:
    """Return the env var or raise at import time with a clear message.

    A missing env var (table name, profile name, etc.) only surfaces deep
    inside boto3 as an opaque parameter-validation error. Failing here makes
    the misconfiguration obvious in CloudWatch on the very first invocation.
    """
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name!r} is not set")
    return value


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
# max_attempts=4 is botocore's "retries AFTER the initial request" count, i.e. up
# to 5 total attempts — comfortably inside the function's 10s timeout. Retrying is
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
# Key on the client-supplied "Idempotency-Key" header (case-insensitive lookup
# matches both "Idempotency-Key" and "idempotency-key"). raise_on_no_idempotency_key
# turns a missing header into Powertools' IdempotencyKeyError, which the resolver
# below converts into a 400 BadRequest — making the requirement enforced rather
# than implicit. Keying on a client-controlled value (instead of the server-
# generated requestContext.requestId, which changes on every retry) is what
# actually makes the layer deduplicate.
persistence_layer = DynamoDBPersistenceLayer(
    table_name=_require_env("IDEMPOTENCY_TABLE_NAME"),
    boto_config=boto_config,
)
idempotency_config = IdempotencyConfig(
    event_key_jmespath='headers."Idempotency-Key" || headers."idempotency-key"',
    raise_on_no_idempotency_key=True,
    expires_after_seconds=3600,
)

# Feature Flags setup.
# AppConfigStore is given an explicit appconfigdata client built with the shared
# retry config. Passing boto_config= (or sdk_config=) to AppConfigStore instead
# routes through a parameter that Powertools v3 deprecated and emits a warning,
# so an explicit client is the clean way to apply the retry policy here.
app_config_store = AppConfigStore(
    environment=_require_env("APPCONFIG_ENV_NAME"),
    application=_require_env("APPCONFIG_APP_NAME"),
    name=_require_env("APPCONFIG_PROFILE_NAME"),
    boto3_client=boto3.client("appconfigdata", config=boto_config),
)
feature_flags = FeatureFlags(store=app_config_store)

# SSM provider for the greeting parameter. An explicit SSMProvider (rather than
# the module-level get_parameter helper) is used so the shared retry config can
# be injected — the free helper builds its own client and takes no boto_config.
# The in-memory cache is per-provider, so reuse this one instance across
# invocations to preserve warm-container caching (max_age is set per get() call).
ssm_provider = SSMProvider(boto_config=boto_config)

# Greeting parameter name resolved at module load — fail loudly on
# misconfiguration rather than letting boto3 reject an empty key at runtime.
GREETING_PARAM_NAME = _require_env("GREETING_PARAM_NAME")


class HelloResponse(BaseModel):
    """Response body for GET /hello."""

    message: str = Field(
        ...,
        description="Greeting from SSM Parameter Store, optionally suffixed when the enhanced_greeting flag is on.",
        examples=["hello world", "hello world - enhanced mode enabled"],
    )


@app.get(
    "/hello",
    summary="Return a greeting",
    description=(
        "Returns the greeting string configured in SSM Parameter Store. "
        "When the `enhanced_greeting` AppConfig feature flag is enabled for "
        "the caller's source IP, the response includes the feature flag's "
        "configured suffix."
    ),
    response_description="A JSON object containing the resolved greeting.",
    tags=["Greeting"],
)
@tracer.capture_method
def hello() -> HelloResponse:
    """Handle GET /hello requests.

    Fetches the greeting from SSM Parameter Store, checks the enhanced_greeting
    feature flag, emits a CloudWatch metric, and logs request metadata from
    the API Gateway event.

    Returns:
        HelloResponse: Validated response model with a ``message`` field.
    """
    metrics.add_metric(name="HelloRequests", unit=MetricUnit.Count, value=1)

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
        logger.warning("Feature flag evaluation failed, falling back to default")
        enhanced = False

    if enhanced:
        message = f"{greeting} - enhanced mode enabled"
        logger.info("Enhanced greeting enabled")
    else:
        message = greeting

    return HelloResponse(message=message)


@idempotent(config=idempotency_config, persistence_store=persistence_layer)
def _resolve_with_idempotency(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Inner handler wrapped by @idempotent.

    Split out so the outer handler can catch IdempotencyKeyError (raised by
    @idempotent before this body runs when the request has no Idempotency-Key
    header) and return a 400 instead of letting Lambda surface a 500.

    Caching caveat: @idempotent wraps the whole resolver, and Powertools persists
    whatever this function *returns* (only raised exceptions are not cached). The
    APIGatewayRestResolver returns non-2xx outcomes — 404 (unknown route), 422
    (validation), and the 500 produced when ``hello`` raises InternalServerError —
    as response dicts rather than exceptions, so those are cached under the
    client's Idempotency-Key for ``expires_after_seconds`` (1 hour). A client that
    reuses the same key after fixing the route/payload would get the stale error
    replayed. This is acceptable for this single-GET reference (the documented
    contract is one key per logical request — see README "Idempotency keys"); a
    fork that wants transient errors retried under the same key should move
    @idempotent onto ``hello`` (the success-bearing function) instead of the
    resolver, accepting that the missing-key→400 path then needs a separate guard.
    """
    return cast("dict[str, Any]", app.resolve(event, context))


@logger.inject_lambda_context
@tracer.capture_lambda_handler
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
            "headers": {"Content-Type": "application/json"},
            "body": '{"message":"Idempotency-Key header is required"}',
            "isBase64Encoded": False,
        }
