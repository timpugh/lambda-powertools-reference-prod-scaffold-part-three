"""Shared test fixtures for the Serverless App project."""

import importlib.util
import os
import sys

import pytest

# Path to the Lambda handler module. We load it by absolute file path inside
# the lambda_app_module fixture rather than relying on sys.path, because
# pytest re-prepends the project rootdir to sys.path after conftest.py runs,
# which would shadow lambda/app.py with the root-level CDK entry point app.py.
# Loading by file path is unambiguous and avoids that collision entirely.
LAMBDA_APP_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "lambda", "app.py"))


@pytest.fixture
def apigw_event():
    """Generates API GW Event for GET /greeting."""
    return {
        "body": None,
        "resource": "/greeting",
        "path": "/greeting",
        "httpMethod": "GET",
        "isBase64Encoded": False,
        "queryStringParameters": {"foo": "bar"},
        "requestContext": {
            "resourceId": "123456",
            "apiId": "1234567890",
            "resourcePath": "/greeting",
            "httpMethod": "GET",
            "requestId": "c6af9ac6-7b61-11e6-9a41-93e8deadbeef",
            "accountId": "123456789012",
            "identity": {
                "sourceIp": "127.0.0.1",
                "userAgent": "Custom User Agent String",
            },
            "stage": "prod",
        },
        "headers": {
            "Host": "1234567890.execute-api.us-east-1.amazonaws.com",
            "User-Agent": "Custom User Agent String",
            # The Lambda's idempotency layer keys on Idempotency-Key. Missing
            # the header surfaces as a 400 in the handler — the test fixture
            # ships one by default; tests that exercise the missing-header
            # path delete it explicitly.
            "Idempotency-Key": "test-idempotency-key-default",
        },
        "pathParameters": None,
        "stageVariables": None,
    }


@pytest.fixture
def lambda_context(mocker):
    """Mock Lambda context using pytest-mock.

    ``get_remaining_time_in_millis`` returns a concrete int because Powertools'
    idempotency layer calls it to compute a remaining-execution timedelta;
    a MagicMock would propagate into ``timedelta(milliseconds=...)`` and raise.
    """
    context = mocker.MagicMock()
    context.function_name = "ApiFunction"
    context.memory_limit_in_mb = 128
    context.invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:ApiFunction"
    context.aws_request_id = "test-request-id"
    context.get_remaining_time_in_millis.return_value = 30_000
    return context


@pytest.fixture
def lambda_app_module():
    """Provide the Lambda app module for direct access in tests.

    Loaded lazily by absolute file path so test environments without
    aws_lambda_powertools installed (e.g. the cdk-check CI job) can still
    collect tests that don't depend on this fixture without an ImportError
    at conftest load time. Cached in sys.modules under "lambda_app" so that
    mocker.patch.object() sees a consistent module identity across fixtures.

    The handler imports boto3 and Powertools, which live only in
    ``.venv-lambda``. When this fixture runs under ``.venv`` (the CDK-side
    interpreter the root-folder Testing panel uses — including "Run with
    Coverage"), skip rather than error, matching the integration and schema
    suites' module-level ``importorskip`` guards. Collection already stays
    clean here because the import is lazy; this makes the *run* clean too.
    """
    pytest.importorskip("boto3", reason="boto3 not installed — unit tests run in .venv-lambda")
    pytest.importorskip("aws_lambda_powertools", reason="Powertools not installed — unit tests run in .venv-lambda")
    if "lambda_app" in sys.modules:
        return sys.modules["lambda_app"]
    spec = importlib.util.spec_from_file_location("lambda_app", LAMBDA_APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["lambda_app"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # Never leave a half-initialized module cached. Without this, a failed
        # exec (e.g. a missing runtime dep) leaves the broken module in
        # sys.modules, and every later test that uses this fixture hits the
        # cache and fails with a confusing AttributeError (no 'ssm_provider')
        # instead of the real import error.
        del sys.modules["lambda_app"]
        raise
    return module
