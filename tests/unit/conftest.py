"""Unit test fixtures — mocks for AWS dependencies."""

import pytest


@pytest.fixture(autouse=True)
def mock_powertools_externals(mocker, lambda_app_module):
    """Mock external Powertools dependencies so unit tests run without AWS."""
    # Mock SSM parameter fetch (the handler reads via the module's SSMProvider
    # instance so the shared retry config applies; patch its .get method).
    mocker.patch.object(
        lambda_app_module.ssm_provider,
        "get",
        return_value="hello world",
    )
    # Mock feature flags
    mocker.patch.object(
        lambda_app_module.feature_flags,
        "evaluate",
        return_value=False,
    )
