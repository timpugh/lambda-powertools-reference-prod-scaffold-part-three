# App Stage

The `infrastructure.app_stage` module defines `AppStage`, the `cdk.Stage`
that groups the data, WAF, backend, frontend, and audit stacks as one deploy
unit. Stack naming and the environment dimension (`env_name` validation)
live here, along with the context plumbing for `retain_data`,
`ssm_param_path`, and `appconfig_monitor`.

## API reference

::: infrastructure.app_stage
