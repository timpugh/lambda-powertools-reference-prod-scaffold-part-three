# Pipeline Stack

The `infrastructure.pipeline_stack` module defines the self-mutating CDK
Pipeline, synthesized only in pipeline mode (`-c pipeline=true`). It is
sourced from GitHub `main` via CodeConnections and runs dev → integration
tests → manual approval → prod.

## API reference

::: infrastructure.pipeline_stack
