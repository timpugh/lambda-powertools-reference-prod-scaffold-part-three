"""Unit tests for the Lambda handler."""

import json


def test_lambda_handler(apigw_event, lambda_context, lambda_app_module):
    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)
    data = json.loads(ret["body"])

    assert ret["statusCode"] == 200
    assert "message" in ret["body"]
    assert data["message"] == "hello world"


def test_lambda_handler_returns_valid_json(apigw_event, lambda_context, lambda_app_module):
    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)
    body = json.loads(ret["body"])
    assert isinstance(body, dict)


def test_lambda_handler_status_code(apigw_event, lambda_context, lambda_app_module):
    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)
    assert ret["statusCode"] == 200


def test_enhanced_greeting_feature_flag(apigw_event, lambda_context, lambda_app_module, mocker):
    """Test that enhanced greeting feature flag changes the response."""
    mocker.patch.object(lambda_app_module.feature_flags, "evaluate", return_value=True)

    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)
    data = json.loads(ret["body"])

    assert "enhanced mode enabled" in data["message"]


def test_ssm_failure_returns_500(apigw_event, lambda_context, lambda_app_module, mocker):
    """Test that an SSM parameter fetch failure returns a 500 response.

    The handler catches Powertools' GetParameterError and raises
    InternalServerError, which becomes a 500 API Gateway response. Truly
    unexpected exception types intentionally propagate to Powertools' default
    handler so they surface correctly in metrics and X-Ray.
    """
    mocker.patch.object(
        lambda_app_module,
        "get_parameter",
        side_effect=lambda_app_module.GetParameterError("SSM unavailable"),
    )

    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)

    assert ret["statusCode"] == 500


def test_feature_flag_failure_falls_back_to_default(apigw_event, lambda_context, lambda_app_module, mocker):
    """Test that a feature flag evaluation failure falls back gracefully.

    AppConfig failures are non-critical — the handler logs a warning and
    uses the default value (False) rather than failing the whole request.
    """
    mocker.patch.object(
        lambda_app_module.feature_flags,
        "evaluate",
        side_effect=Exception("AppConfig unavailable"),
    )

    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)
    data = json.loads(ret["body"])

    assert ret["statusCode"] == 200
    assert data["message"] == "hello world"


def test_unknown_route_returns_404(apigw_event, lambda_context, lambda_app_module):
    """Test that a request to an unknown route returns 404."""
    apigw_event["path"] = "/unknown"
    apigw_event["resource"] = "/unknown"

    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)

    assert ret["statusCode"] == 404


def test_unsupported_method_returns_404(apigw_event, lambda_context, lambda_app_module):
    """Test that an unsupported HTTP method returns 404.

    Powertools APIGatewayRestResolver returns 404 (not 405) for method+path
    combinations that have no registered route handler.
    """
    apigw_event["httpMethod"] = "POST"

    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)

    assert ret["statusCode"] == 404


def test_missing_idempotency_key_returns_400(apigw_event, lambda_context, lambda_app_module, monkeypatch):
    """A request without an Idempotency-Key header is rejected with 400.

    The header is a hard requirement — without it Powertools' @idempotent
    layer raises IdempotencyKeyError, which the handler converts to a 400
    response so callers see a meaningful error instead of an unhandled 500.

    POWERTOOLS_IDEMPOTENCY_DISABLED is normally set in pytest_env so the
    other tests don't hit DynamoDB; for this assertion specifically we
    re-enable the layer so the missing-key path actually executes.
    """
    monkeypatch.delenv("POWERTOOLS_IDEMPOTENCY_DISABLED", raising=False)
    del apigw_event["headers"]["Idempotency-Key"]

    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)

    assert ret["statusCode"] == 400
    assert "Idempotency-Key" in ret["body"]


def test_lowercase_idempotency_key_accepted(apigw_event, lambda_context, lambda_app_module):
    """The JMESPath also matches a lowercase 'idempotency-key' header.

    HTTP headers are case-insensitive; API Gateway preserves the casing the
    caller sent. The OR fallback in the JMESPath covers the lowercase form.
    """
    del apigw_event["headers"]["Idempotency-Key"]
    apigw_event["headers"]["idempotency-key"] = "test-idempotency-key-lower"

    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)

    assert ret["statusCode"] == 200
