"""Synth-time validation Aspects enforcing this template's own conventions.

These complement cdk-nag rather than duplicate it. cdk-nag enforces broad
security/compliance posture across five rule packs; these Aspects enforce two
project-specific invariants that no rule pack covers and that are easy to
violate by accident when adding resources:

1. **Every CloudWatch log group declares an explicit retention.** The
   CloudWatch Logs default is "never expire", which is both a cost leak
   (logs accumulate forever) and a compliance footgun (no defined audit
   window). This repo sets ``retention=`` on every log group; the Aspect turns
   "forgot to set retention" into a synth error instead of a silently
   unbounded log group.

2. **Every stateful resource declares an explicit removal policy.** L2
   constructs fill in a default (usually ``RETAIN``), but a raw L1 resource
   (``CfnBucket`` / ``CfnTable`` / ``CfnGlobalTable`` / ``CfnKey``) added
   directly carries no ``DeletionPolicy`` at all — which in a template that
   advertises clean teardown silently strands resources on ``cdk destroy``.
   The Aspect requires the lifecycle choice to be explicit on stateful L1s.

Violations are emitted as error-level annotations, so they fail ``cdk synth``
(the authoritative gate) and are asserted absent by the ``tests/cdk``
nag-annotation checks — the same surface cdk-nag findings surface on. The
Aspect is wired for every stack by :func:`hello_world.nag_utils.apply_compliance_aspects`,
so it runs in lockstep with the rule packs.
"""

import jsii
from aws_cdk import Annotations, IAspect
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from constructs import IConstruct

# Stateful L1 resource types that must carry an explicit removal policy. These
# are the resources whose accidental retention (or accidental deletion) has a
# real blast radius: data stores, the bucket holding them, and the CMKs that
# make them readable.
_STATEFUL_CFN_TYPES = (
    s3.CfnBucket,
    dynamodb.CfnTable,
    dynamodb.CfnGlobalTable,
    kms.CfnKey,
)


@jsii.implements(IAspect)
class TemplateConventionChecks:
    """Enforce log-group retention and explicit removal policies tree-wide.

    Implements :class:`aws_cdk.IAspect`; ``visit`` is invoked once per construct
    during the prepare phase, before synthesis. Add one instance per stack via
    ``Aspects.of(stack).add(...)``.
    """

    def visit(self, node: IConstruct) -> None:
        """Validate a single construct against the project conventions."""
        if isinstance(node, logs.CfnLogGroup):
            self._check_log_retention(node)
        elif isinstance(node, _STATEFUL_CFN_TYPES):
            self._check_removal_policy(node)

    @staticmethod
    def _check_log_retention(node: logs.CfnLogGroup) -> None:
        # retention_in_days is None when no retention was set — CloudWatch then
        # keeps the log stream forever.
        if node.retention_in_days is None:
            Annotations.of(node).add_error(
                f"Log group {node.node.path} has no explicit retention "
                "(CloudWatch defaults to never-expire). Set retention= on the LogGroup. "
                "Enforced by hello_world.validation_aspects.TemplateConventionChecks."
            )

    @staticmethod
    def _check_removal_policy(node: IConstruct) -> None:
        # cfn_options.deletion_policy reflects the applied RemovalPolicy. Every
        # _STATEFUL_CFN_TYPES entry is a CfnResource, so cfn_options exists; it
        # is None only when no removal policy was ever applied (a raw L1 added
        # without one — L2 constructs always set a default).
        cfn_options = node.cfn_options  # type: ignore[attr-defined]
        if cfn_options.deletion_policy is None:
            Annotations.of(node).add_error(
                f"Stateful resource {node.node.path} has no explicit DeletionPolicy/RemovalPolicy. "
                "Set removal_policy= (DESTROY for the destroy-friendly default, or RETAIN behind "
                "retain_data for the stateful data stack). "
                "Enforced by hello_world.validation_aspects.TemplateConventionChecks."
            )
