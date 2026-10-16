# AppConfig Extension Store

The `extension_store` module in `lambda/extension_store.py` adapts the AWS
AppConfig Lambda extension's local `http://localhost:2772` endpoint to
Powertools' `StoreProvider` interface, so `FeatureFlags` reads cached flag
data from the extension instead of calling AppConfig through the SDK. Any
endpoint failure raises `ConfigurationStoreError`, which the service layer's
fallback handles.

## API reference

::: extension_store
