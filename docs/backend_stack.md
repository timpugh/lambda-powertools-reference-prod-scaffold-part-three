# Backend CDK Stack

The `infrastructure.backend_stack` module defines the backend stack: API
Gateway, the Lambda function, SSM parameter, and AppConfig feature flag.
The DynamoDB idempotency table it consumes is owned by
[`DataStack`](data_stack.md) and handed in cross-stack via
`idempotency_table=`.

## API reference

::: infrastructure.backend_stack
