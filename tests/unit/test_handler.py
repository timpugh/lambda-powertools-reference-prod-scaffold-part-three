"""Unit tests for the Lambda handler."""

import json

import pytest


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

    AppConfig failures are non-critical — the handler catches the Powertools
    FeatureFlags exception types (StoreClientError covers boto3 / network
    errors against the AppConfig data plane) and uses the default value
    (False) rather than failing the whole request.
    """
    mocker.patch.object(
        lambda_app_module.feature_flags,
        "evaluate",
        side_effect=lambda_app_module.StoreClientError("AppConfig unavailable"),
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


def test_lowercase_idempotency_key_accepted(apigw_event, lambda_context, lambda_app_module, monkeypatch, mocker):
    """The JMESPath also matches a lowercase 'idempotency-key' header.

    HTTP headers are case-insensitive; API Gateway preserves the casing the
    caller sent. The OR fallback in the JMESPath covers the lowercase form.
    POWERTOOLS_IDEMPOTENCY_DISABLED is unset for this test so the @idempotent
    decorator actually evaluates the JMESPath rather than short-circuiting —
    otherwise the test passes trivially regardless of which header is present.
    The persistence layer's mutating methods are mocked to no-ops so the test
    never touches DynamoDB; ``_get_remaining_time_in_millis`` is also patched
    so Powertools doesn't try to compute a timedelta from a MagicMock context.
    """
    monkeypatch.delenv("POWERTOOLS_IDEMPOTENCY_DISABLED", raising=False)
    mocker.patch(
        "aws_lambda_powertools.utilities.idempotency.base.IdempotencyHandler._get_remaining_time_in_millis",
        return_value=30_000,
    )
    mocker.patch.object(lambda_app_module.persistence_layer, "_put_record", return_value=None)
    mocker.patch.object(lambda_app_module.persistence_layer, "_get_record", side_effect=Exception("not found"))
    mocker.patch.object(lambda_app_module.persistence_layer, "_update_record", return_value=None)
    mocker.patch.object(lambda_app_module.persistence_layer, "_delete_record", return_value=None)
    del apigw_event["headers"]["Idempotency-Key"]
    apigw_event["headers"]["idempotency-key"] = "test-idempotency-key-lower"

    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)

    assert ret["statusCode"] == 200


def test_require_env_raises_when_missing(lambda_app_module, monkeypatch):
    """_require_env raises RuntimeError naming the missing variable.

    The function runs at import time on real deploys so a missing var fails
    the cold start with a clear message; this test pins that contract.
    """
    monkeypatch.delenv("UNIT_TEST_ABSENT_VAR", raising=False)

    with pytest.raises(RuntimeError, match="UNIT_TEST_ABSENT_VAR"):
        lambda_app_module._require_env("UNIT_TEST_ABSENT_VAR")


def test_persistence_layer_error_propagates(apigw_event, lambda_context, lambda_app_module, monkeypatch, mocker):
    """A DynamoDB-side persistence failure does not get masked as a 400.

    The outer handler intentionally only catches ``IdempotencyKeyError``
    (which has a meaningful 400 mapping); persistence-layer failures
    propagate up to the Lambda runtime instead, so the original exception
    type surfaces in CloudWatch metrics and X-Ray rather than being silently
    flattened into the generic 400 path. We assert the exception escapes
    rather than being absorbed.
    """
    from aws_lambda_powertools.utilities.idempotency.exceptions import IdempotencyPersistenceLayerError

    monkeypatch.delenv("POWERTOOLS_IDEMPOTENCY_DISABLED", raising=False)
    mocker.patch(
        "aws_lambda_powertools.utilities.idempotency.base.IdempotencyHandler._get_remaining_time_in_millis",
        return_value=30_000,
    )
    mocker.patch.object(
        lambda_app_module.persistence_layer,
        "_put_record",
        side_effect=IdempotencyPersistenceLayerError("DDB throttled", Exception("orig")),
    )

    with pytest.raises(IdempotencyPersistenceLayerError):
        lambda_app_module.lambda_handler(apigw_event, lambda_context)
