# Data Stack

The `infrastructure.data_stack` module defines the stateful data layer: the
DynamoDB idempotency table and its dedicated CMK, kept separate from the
stateless compute stack. The `retain_data` switch flips both to `RETAIN`,
enables deletion and termination protection, and provisions AWS Backup.

## API reference

::: infrastructure.data_stack
