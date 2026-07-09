"""Unit tests for the AppConfig-extension-backed feature-flag store."""

import io
import json

import pytest
from aws_lambda_powertools.utilities.feature_flags.exceptions import ConfigurationStoreError
from extension_store import AppConfigExtensionStore


@pytest.fixture
def store() -> AppConfigExtensionStore:
    return AppConfigExtensionStore(application="app", environment="env", name="flags", max_age=300)


def _stub_urlopen(monkeypatch, payload: bytes):
    calls: list[str] = []

    def fake_urlopen(url, timeout):
        calls.append(url)
        return io.BytesIO(payload)

    monkeypatch.setattr("extension_store.urllib.request.urlopen", fake_urlopen)
    return calls


def test_fetches_flags_from_extension_endpoint(monkeypatch, store):
    flags = {"enhanced_greeting": {"default": False}}
    calls = _stub_urlopen(monkeypatch, json.dumps(flags).encode())
    assert store.get_configuration() == flags
    assert calls == ["http://localhost:2772/applications/app/environments/env/configurations/flags"]


def test_result_is_cached_within_ttl(monkeypatch, store):
    calls = _stub_urlopen(monkeypatch, b"{}")
    store.get_configuration()
    store.get_configuration()
    assert len(calls) == 1, "second call within max_age must hit the in-memory cache"


def test_http_error_raises_store_error(monkeypatch, store):
    def boom(url, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr("extension_store.urllib.request.urlopen", boom)
    with pytest.raises(ConfigurationStoreError):
        store.get_configuration()


def test_bad_json_raises_store_error(monkeypatch, store):
    _stub_urlopen(monkeypatch, b"<html>not json</html>")
    with pytest.raises(ConfigurationStoreError):
        store.get_configuration()


def test_non_dict_json_raises_store_error(monkeypatch, store):
    """A syntactically valid but non-object JSON body (e.g. a bare list) must
    raise ConfigurationStoreError rather than handing FeatureFlags a shape it
    cannot evaluate rules against."""
    _stub_urlopen(monkeypatch, b"[1, 2, 3]")
    with pytest.raises(ConfigurationStoreError):
        store.get_configuration()


def test_raw_configuration_property(monkeypatch, store):
    _stub_urlopen(monkeypatch, b'{"a": 1}')
    assert store.get_raw_configuration == {"a": 1}
