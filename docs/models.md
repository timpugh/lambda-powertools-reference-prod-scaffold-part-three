# Data Models

The `models` module in `lambda/models.py` holds the Pydantic data contracts for
the greeting API: the typed environment-variable view the handler validates at
cold start, and the request/response models that drive runtime validation and
the committed OpenAPI spec. It imports no Powertools resolver or AWS client.

## API reference

::: models
