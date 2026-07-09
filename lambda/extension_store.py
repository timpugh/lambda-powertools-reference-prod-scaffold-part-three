"""Feature-flag store backed by the AWS AppConfig Lambda extension.

The extension (a Lambda layer wired in infrastructure/backend_app.py) polls
AppConfig in the background and serves cached configuration over
``http://localhost:2772`` — cutting per-invocation AppConfig API spend and
cold-path latency versus SDK polling (the AWS-recommended pattern for Lambda:
see "Using AWS AppConfig Agent with AWS Lambda" in the AppConfig user guide).

This module adapts that local endpoint to Powertools' ``StoreProvider``
interface so ``FeatureFlags`` consumes it unchanged. Any failure — endpoint
down (e.g. running outside Lambda), HTTP error, malformed body — raises
``ConfigurationStoreError``, which the service layer's fallback already
handles (default flag values + the ``FeatureFlagEvaluationFailure`` metric).

A small monotonic-clock TTL cache mirrors the ``max_age`` posture of the SDK
store this replaces; the extension caches too, so this mostly saves the
localhost round-trip on hot paths. This is a freshness-over-latency trade,
deliberately without stale-serve or backoff: once the TTL lapses, every
evaluation pays the full 2s urlopen timeout while the extension endpoint is
down, before falling through to the service layer's default (fail → raise →
default flag values).
"""

import http.client
import json
import time
import urllib.request
from typing import Any

from aws_lambda_powertools.utilities.feature_flags import StoreProvider
from aws_lambda_powertools.utilities.feature_flags.exceptions import ConfigurationStoreError


class AppConfigExtensionStore(StoreProvider):
    """Powertools feature-flag store reading from the AppConfig extension endpoint."""

    def __init__(
        self,
        *,
        application: str,
        environment: str,
        name: str,
        max_age: int = 300,
        endpoint: str = "http://localhost:2772",
    ) -> None:
        super().__init__()
        self._url = f"{endpoint}/applications/{application}/environments/{environment}/configurations/{name}"
        self._max_age = max_age
        self._cached: dict[str, Any] | None = None
        self._fetched_at = 0.0

    @property
    def max_age(self) -> int:
        """The configured TTL (seconds) for the in-memory cache."""
        return self._max_age

    def get_configuration(self) -> dict[str, Any]:
        """Return the flag configuration, served from the TTL cache when fresh."""
        now = time.monotonic()
        if self._cached is not None and (now - self._fetched_at) < self._max_age:
            return self._cached
        try:
            # nosec B310 / noqa: S310 — bandit and ruff both flag urlopen for
            # arbitrary-scheme risk, but self._url is built entirely in
            # __init__ from the fixed http://localhost:2772 extension
            # endpoint plus this Lambda's own AppConfig identifiers, never
            # from request/caller input, so there's no injection surface.
            with urllib.request.urlopen(self._url, timeout=2) as response:  # nosec B310  # noqa: S310
                body = response.read()
            config = json.loads(body)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            # BadStatusLine/IncompleteRead are http.client.HTTPException, not
            # OSError (CPython only wraps OSError into URLError) — they must
            # not escape the store's failure contract.
            raise ConfigurationStoreError(f"Unable to fetch configuration from the AppConfig extension: {exc}") from exc
        if not isinstance(config, dict):
            raise ConfigurationStoreError("AppConfig extension returned a non-object configuration document")
        self._cached = config
        self._fetched_at = now
        return config

    @property
    def get_raw_configuration(self) -> dict[str, Any]:
        """Raw configuration — same document; required by the StoreProvider ABC."""
        return self.get_configuration()
