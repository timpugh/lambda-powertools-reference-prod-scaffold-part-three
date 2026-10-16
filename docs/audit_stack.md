# Audit Stack

The `infrastructure.audit_stack` module defines the audit-trail data store:
the CloudTrail object-level S3 data-event trail, its log bucket, and a
dedicated CMK. It audits the frontend buckets one-way, and `retain_data`
lifts the log bucket into its compliance tier (versioning, Object Lock,
tiering).

## API reference

::: infrastructure.audit_stack
