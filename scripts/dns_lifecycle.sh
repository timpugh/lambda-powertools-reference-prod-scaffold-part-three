#!/usr/bin/env bash
# scripts/dns_lifecycle.sh — the DNS hibernation lifecycle backing
# `make hibernate-dns` and `make wake-dns`.
#
# The zero-idle-cost stance: a hosted zone bills $0.50/month whether or not
# anything is deployed, and the locked design keeps it OUTSIDE CloudFormation
# (a CDK-managed zone would force a registration NS update on every deploy
# cycle — the treadmill this repo refuses). Hibernation squares the two:
# when parking the project for a long stretch, `hibernate` deletes the zone
# (recurring cost drops to $0.00/month; only the annual domain registration
# remains, and that survives by definition — it IS the domain). On resume,
# `wake` recreates the zone, re-points the domain registration at the new
# zone's name servers, and rewrites HOSTED_ZONE_ID in ./.env.
#
# This is an OPERATOR-INVOKED lifecycle command, once per park/resume cycle —
# deliberately not automated into deploys, and philosophically different from
# the per-deploy NS treadmill: explicit, observable, and rare.
#
# Usage: dns_lifecycle.sh hibernate|wake
#   Requires DOMAIN_NAME in ./.env (the domain's registration home is this
#   account). `wake` also rewrites HOSTED_ZONE_ID in ./.env.
#
# Safety properties:
#   * hibernate refuses while any ServerlessApp* stack is deployed — live
#     stacks hold records in (and certificates validated through) the zone.
#   * hibernate deletes leftover non-apex records defensively before the zone
#     (Route 53 refuses to delete a non-empty zone; post-teardown only NS+SOA
#     should remain, but a straggler must not wedge the parking flow).
#   * wake is idempotent: an existing zone is adopted rather than duplicated.
#   * Registry NS propagation after wake takes minutes to hours — deploy after
#     it settles, or ACM DNS validation will sit pending until it does.

set -euo pipefail

MODE="${1:-}"
[ "$MODE" = "hibernate" ] || [ "$MODE" = "wake" ] || {
  echo "usage: $0 hibernate|wake" >&2; exit 2; }

[ -f .env ] || { echo "ERROR: no ./.env — DOMAIN_NAME must live there (see README 'Custom domain')" >&2; exit 2; }
DOMAIN_NAME=$(sh -c '. ./.env >/dev/null 2>&1; printf "%s" "${DOMAIN_NAME:-}"')
[ -n "$DOMAIN_NAME" ] || { echo "ERROR: DOMAIN_NAME not set in ./.env" >&2; exit 2; }

find_zone_id() {
  aws route53 list-hosted-zones-by-name --dns-name "$DOMAIN_NAME" \
    --query "HostedZones[?Name=='${DOMAIN_NAME}.'].Id | [0]" --output text 2>/dev/null \
    | sed 's|/hostedzone/||' | grep -v '^None$' || true
}

case "$MODE" in
hibernate)
  ZONE_ID=$(find_zone_id)
  if [ -z "$ZONE_ID" ]; then
    echo "already hibernated: no hosted zone exists for $DOMAIN_NAME"; exit 0
  fi

  # Refuse while stacks are up: their records live in this zone, and their
  # ACM certificates renew through validation CNAMEs here. Park the stacks
  # first (make destroy-pipeline / destroy-clean), then hibernate DNS.
  DEPLOYED=$(aws cloudformation list-stacks --region us-east-1 \
    --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE \
    --query "StackSummaries[?starts_with(StackName,'ServerlessApp')].StackName" --output text)
  if [ -n "$DEPLOYED" ]; then
    echo "ERROR: refusing to hibernate DNS while stacks are deployed:" >&2
    printf '  %s\n' $DEPLOYED >&2
    echo "tear down first: make destroy-pipeline && make destroy-clean ENV=dev && make destroy-clean" >&2
    exit 1
  fi

  # Route 53 refuses to delete a zone holding anything beyond the apex
  # NS+SOA. Post-teardown nothing else should remain; sweep defensively.
  aws route53 list-resource-record-sets --hosted-zone-id "$ZONE_ID" --output json \
    | python3 -c '
import json, sys
zone_apex = "'"$DOMAIN_NAME"'."
records = json.load(sys.stdin)["ResourceRecordSets"]
doomed = [r for r in records
          if not (r["Name"] == zone_apex and r["Type"] in ("NS", "SOA"))]
changes = [{"Action": "DELETE", "ResourceRecordSet": r} for r in doomed]
print(json.dumps({"Changes": changes}) if changes else "")' > /tmp/dns-hibernate-changes.json
  if [ -s /tmp/dns-hibernate-changes.json ]; then
    echo "sweeping leftover records before zone deletion..."
    aws route53 change-resource-record-sets --hosted-zone-id "$ZONE_ID" \
      --change-batch "file:///tmp/dns-hibernate-changes.json" >/dev/null
  fi

  aws route53 delete-hosted-zone --id "$ZONE_ID" >/dev/null
  echo "HIBERNATED: hosted zone $ZONE_ID for $DOMAIN_NAME deleted."
  echo "  recurring DNS cost is now \$0.00/month; only the annual registration remains."
  echo "  ./.env HOSTED_ZONE_ID is now stale — 'make wake-dns' rewrites it on resume."
  echo "  verify the account: make audit-account"
  ;;

wake)
  ZONE_ID=$(find_zone_id)
  if [ -n "$ZONE_ID" ]; then
    echo "zone already exists for $DOMAIN_NAME ($ZONE_ID) — adopting it (idempotent wake)"
  else
    ZONE_ID=$(aws route53 create-hosted-zone --name "$DOMAIN_NAME" \
      --caller-reference "wake-$(date -u +%Y%m%dT%H%M%SZ)" \
      --hosted-zone-config Comment="recreated by make wake-dns" \
      --query 'HostedZone.Id' --output text | sed 's|/hostedzone/||')
    echo "created hosted zone $ZONE_ID for $DOMAIN_NAME"
  fi

  # Point the (permanent, non-CFN) registration at the new zone's NS set.
  NS_JSON=$(aws route53 get-hosted-zone --id "$ZONE_ID" \
    --query 'DelegationSet.NameServers' --output json)
  NS_ARGS=$(printf '%s' "$NS_JSON" | python3 -c '
import json, sys
print(" ".join(f"Name={n}" for n in json.load(sys.stdin)))')
  # shellcheck disable=SC2086  # word-splitting NS_ARGS is intentional
  aws route53domains update-domain-nameservers --region us-east-1 \
    --domain-name "$DOMAIN_NAME" --nameservers $NS_ARGS >/dev/null
  echo "registration for $DOMAIN_NAME re-pointed at:"
  printf '%s' "$NS_JSON" | python3 -c 'import json,sys; [print("  " + n) for n in json.load(sys.stdin)]'

  # Rewrite HOSTED_ZONE_ID in ./.env (insert if absent).
  python3 - "$ZONE_ID" <<'PYEOF'
import re, sys
zone_id = sys.argv[1]
with open(".env") as f:
    content = f.read()
if re.search(r"^HOSTED_ZONE_ID=", content, flags=re.M):
    content = re.sub(r"^HOSTED_ZONE_ID=.*$", f"HOSTED_ZONE_ID={zone_id}", content, flags=re.M)
else:
    content = content.rstrip("\n") + f"\nHOSTED_ZONE_ID={zone_id}\n"
with open(".env", "w") as f:
    f.write(content)
print(f"./.env updated: HOSTED_ZONE_ID={zone_id}")
PYEOF

  echo "AWAKE. Registry NS propagation takes minutes to hours — check with:"
  echo "  aws route53domains get-domain-detail --region us-east-1 --domain-name $DOMAIN_NAME --query Nameservers"
  echo "Deploy only after it settles, or ACM DNS validation will sit pending until it does."
  ;;
esac
