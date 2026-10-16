# Service Layer

The `service` module in `lambda/service.py` holds the greeting API's business
logic: fetch the configured greeting from SSM, apply the `enhanced_greeting`
feature flag, and compose the message. The handler constructs the AWS providers
and passes them into `build_greeting`, so this layer carries no module-level AWS
client state.

## API reference

::: service
