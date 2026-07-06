"""Service layer — business logic for the greeting API.

Holds the domain logic (fetch the configured greeting from SSM, apply the
``enhanced_greeting`` feature flag, compose the message), separate from the
handler so it is unit-testable on its own and could be shared by a second
handler. The handler (``app.py``) owns the I/O wiring — the Powertools resolver,
the AWS provider clients, and their shared retry config — and passes the
providers it constructs into :func:`build_greeting`, so this layer carries no
module-level AWS client state.

``logger`` and ``metrics`` are instantiated here too: Powertools' utilities are
designed to be created per-module (instances share the same underlying logger by
service name and the same metric buffer), so each layer declares the
observability it uses rather than importing it across the layer boundary.
"""

from typing import Any, cast

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.feature_flags import FeatureFlags
from aws_lambda_powertools.utilities.feature_flags.exceptions import (
    ConfigurationStoreError,
    SchemaValidationError,
    StoreClientError,
)
from aws_lambda_powertools.utilities.parameters import SSMProvider
from aws_lambda_powertools.utilities.parameters.exceptions import GetParameterError

logger = Logger()
metrics = Metrics()


class GreetingUnavailableError(Exception):
    """The greeting could not be fetched from its source of truth (SSM).

    A domain-level failure with no HTTP coupling — the handler catches it and
    maps it to an ``InternalServerError`` (HTTP 500), keeping this service layer
    independent of the web framework.
    """


def build_greeting(
    *,
    ssm_provider: SSMProvider,
    feature_flags: FeatureFlags,
    param_name: str,
    flag_context: dict[str, Any],
) -> str:
    """Build the greeting message: fetch from SSM, apply the feature flag, compose.

    Args:
        ssm_provider: Powertools SSM provider (constructed by the handler with the
            shared retry config) used to read the greeting parameter.
        feature_flags: Powertools FeatureFlags instance backed by AppConfig.
        param_name: SSM parameter name holding the base greeting string.
        flag_context: Evaluation context for the feature-flag rules engine — the
            handler passes the caller's ``source_ip`` and ``user_agent`` so
            AppConfig rules can match on them. Without it the rule engine can
            never see those values to evaluate against.

    Returns:
        The greeting string, suffixed when ``enhanced_greeting`` evaluates true.

    Raises:
        GreetingUnavailableError: when the SSM read fails.
    """
    # Business KPI: count every greeting request, even one that later fails the
    # SSM read (emitted before the fetch, matching the metric's "requests" name).
    metrics.add_metric(name="GreetingRequests", unit=MetricUnit.Count, value=1)

    # Fetch greeting from SSM Parameter Store. Powertools wraps boto3 errors
    # (ClientError, BotoCoreError) as GetParameterError; catch only that and
    # re-raise as a domain error so truly unexpected exceptions propagate to
    # Powertools' default handler and surface with the right type in metrics and
    # X-Ray. max_age=300 raises Powertools' in-memory TTL from its 5-second
    # default so warm containers reuse the value for 5 minutes between SSM calls;
    # the greeting changes via deployment, not at runtime, so a longer TTL is
    # safe and meaningfully reduces SSM API spend at higher RPS.
    try:
        # SSMProvider.get returns str | bytes | dict | None to cover transform/
        # binary cases; this is a plain String parameter with no transform, so it
        # is always str at runtime. cast keeps the downstream message typed as str.
        greeting = cast("str", ssm_provider.get(param_name, max_age=300))
    except GetParameterError as exc:
        logger.exception("Failed to fetch greeting from SSM", param_name=param_name)
        raise GreetingUnavailableError("Failed to fetch greeting") from exc
    logger.info("Greeting fetched from parameter store", greeting=greeting)

    # Check feature flag — non-critical, fall back to default on failure. Catch
    # only the Powertools FeatureFlags exception types — programming errors
    # (TypeError, AttributeError) intentionally propagate so they surface as bugs
    # in metrics rather than being silently absorbed by the fallback path.
    try:
        enhanced = feature_flags.evaluate(name="enhanced_greeting", context=flag_context, default=False)
    except ConfigurationStoreError, SchemaValidationError, StoreClientError:
        # exc_info=True puts the underlying exception in the log record — without
        # it a permanently broken AppConfig integration (bad IAM, bad config, KMS
        # denial) is indistinguishable in CloudWatch from a transient network
        # blip, and the cause is unrecoverable after the fact. Kept at WARNING
        # because the request still succeeds.
        logger.warning("Feature flag evaluation failed, falling back to default", exc_info=True)
        # Emit a metric on the fallback so a bad flag *config* is observable: it's
        # caught and degraded gracefully (the request still returns 200), so it
        # produces no Lambda error or 5xx — this metric is the only signal the
        # config is broken, and the signal a production fork wires an AppConfig
        # deployment monitor to (see infrastructure/backend_app.py).
        metrics.add_metric(name="FeatureFlagEvaluationFailure", unit=MetricUnit.Count, value=1)
        enhanced = False

    if enhanced:
        message = f"{greeting} - enhanced mode enabled"
        logger.info("Enhanced greeting enabled")
    else:
        message = greeting
    return message
