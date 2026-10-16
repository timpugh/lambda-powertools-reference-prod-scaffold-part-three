# AppConfig Extension Layer

The `infrastructure.appconfig_extension` module pins the AWS-published AppConfig
Agent Lambda extension layer (arm64) per deploy region and resolves which ARN
[`BackendApp`](backend_app.md) attaches to the function.

Resolution order: the `appconfig_extension_layer_arn` CDK context key, then the
`APPCONFIG_EXTENSION_LAYER_ARN` environment variable (both resolved in `app.py`
and shape-validated in [`app_stage`](app_stage.md)), then the pinned
`APPCONFIG_EXTENSION_ARM64_LAYER_ARNS` map, then a synth-time `ValueError`. An
override must be in the stack's region, because Lambda layers are regional.

## API reference

::: infrastructure.appconfig_extension
