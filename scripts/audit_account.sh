#!/usr/bin/env bash
# scripts/audit_account.sh — read-only account audit backing `make audit-account`.
#
# Answers one question: did teardown actually leave a clean, zero-recurring-cost
# account? It enumerates this scaffold's resource footprint across EVERY enabled
# region (the "hidden in another region" trap is real — a resource in a region
# you didn't look at bills silently, and multi-region forks make it likely) and
# cross-checks against Cost Explorer. It DELETES NOTHING — the whole point is to
# look, not touch.
#
# Exit 0 = clean: no unambiguously app-owned resources remain.
# Exit 1 = dirty: app-owned resources still present (each printed with [!] and
#          its region), so this doubles as a post-teardown gate.
# Exit 2 = no usable AWS credentials.
#
# Deliberately NOT failed on — reported for your eyeball because the tool can't
# safely auto-classify them as app-owned vs. intentional:
#   * Route 53 hosted zones ($0.50/mo each),
#   * registered domains — THE one true vampire: an annual auto-renewing charge
#     that lives in Route 53 Domains OUTSIDE CloudFormation, so no `cdk destroy`
#     ever reaches it; disable auto-renew (or transfer/expire) to stop it,
#   * ACM certificates (free, but orphans are worth seeing),
#   * Cost Explorer month-to-date by service — the bill is the only ground truth
#     an orphaned resource cannot hide from; trust the money over any list.

set -uo pipefail

# Matches the names every stack in this scaffold emits: the ServerlessApp* stacks
# and their resources, the aws-waf-logs-* delivery buckets AWS force-prefixes,
# and a lowercase form for any future service-named resource.
APP_RE='ServerlessApp|aws-waf-logs|serverless-app'
# Account-level, one-time prerequisites that are SUPPOSED to survive teardown.
SURVIVORS_RE='CDKToolkit|CdkScaffoldBoundary'

dirty=0
flag() { printf '  [!] %s\n' "$1"; dirty=1; }

acct=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || {
  echo "no usable AWS credentials — configure them and retry" >&2
  exit 2
}
echo "=== Account audit (read-only) — account $acct ==="
echo

regions=$(aws ec2 describe-regions --all-regions \
  --query "Regions[?OptInStatus!='not-opted-in'].RegionName" --output text 2>/dev/null)
echo "scanning $(echo "$regions" | wc -w | tr -d ' ') enabled region(s) for app-owned resources..."
echo

echo "CloudFormation stacks:"
found=0
for r in $regions; do
  for s in $(aws cloudformation list-stacks --region "$r" \
      --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE ROLLBACK_COMPLETE DELETE_FAILED \
      --query "StackSummaries[].StackName" --output text 2>/dev/null); do
    if printf '%s' "$s" | grep -qE "$APP_RE"; then flag "$r: $s"; found=1; fi
  done
done
[ "$found" -eq 0 ] && echo "  none"

echo "S3 buckets (global):"
found=0
for b in $(aws s3api list-buckets --query 'Buckets[].Name' --output text 2>/dev/null); do
  if printf '%s' "$b" | grep -qiE "$APP_RE"; then flag "$b"; found=1; fi
done
[ "$found" -eq 0 ] && echo "  none"

echo "CloudWatch log groups:"
found=0
for r in $regions; do
  for lg in $(aws logs describe-log-groups --region "$r" \
      --query "logGroups[].logGroupName" --output text 2>/dev/null); do
    if printf '%s' "$lg" | grep -qE "$APP_RE"; then flag "$r: $lg"; found=1; fi
  done
done
[ "$found" -eq 0 ] && echo "  none"

echo "CodeDeploy applications:"
found=0
for r in $regions; do
  for a in $(aws deploy list-applications --region "$r" --query 'applications' --output text 2>/dev/null); do
    if printf '%s' "$a" | grep -qE "$APP_RE"; then flag "$r: $a"; found=1; fi
  done
done
[ "$found" -eq 0 ] && echo "  none"

echo "Cognito user pools:"
found=0
for r in $regions; do
  for p in $(aws cognito-idp list-user-pools --max-results 60 --region "$r" \
      --query 'UserPools[].Name' --output text 2>/dev/null); do
    if printf '%s' "$p" | grep -qiE "$APP_RE"; then flag "$r: $p"; found=1; fi
  done
done
[ "$found" -eq 0 ] && echo "  none"

echo
echo "--- eyeball these (never auto-deleted; not counted as dirty) ---"

echo "Route 53 hosted zones (\$0.50/mo each):"
zones=$(aws route53 list-hosted-zones --query 'HostedZones[].Name' --output text 2>/dev/null)
if [ -n "$zones" ]; then for z in $zones; do echo "  $z"; done; else echo "  none"; fi

echo "Registered domains — THE vampire (annual auto-renew, outside CloudFormation):"
domains=$(aws route53domains list-domains --region us-east-1 \
  --query 'Domains[].[DomainName,AutoRenew,Expiry]' --output text 2>/dev/null)
if [ -n "$domains" ]; then
  echo "  name / auto-renew / expiry — set auto-renew false to stop the annual charge:"
  printf '%s\n' "$domains" | sed 's/^/    /'
else
  echo "  none"
fi

echo "ACM certificates (free; orphans harmless but worth seeing):"
found=0
for r in $regions; do
  for c in $(aws acm list-certificates --region "$r" \
      --query 'CertificateSummaryList[].DomainName' --output text 2>/dev/null); do
    echo "  $r: $c"; found=1
  done
done
[ "$found" -eq 0 ] && echo "  none"

echo
echo "--- expected survivors (fine to keep — account-level prerequisites) ---"
for s in $(aws cloudformation list-stacks --region us-east-1 \
    --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
    --query "StackSummaries[].StackName" --output text 2>/dev/null); do
  printf '%s' "$s" | grep -qE "$SURVIVORS_RE" && echo "  stack: $s"
done
for c in $(aws codeconnections list-connections --region us-east-1 \
    --query 'Connections[].ConnectionName' --output text 2>/dev/null); do
  echo "  connection: $c"
done

echo
echo "--- Cost Explorer (month-to-date by service) — the ground truth ---"
start=$(python3 -c "import datetime; print(datetime.date.today().replace(day=1))")
end=$(python3 -c "import datetime; print(datetime.date.today() + datetime.timedelta(days=1))")
aws ce get-cost-and-usage --time-period "Start=$start,End=$end" --granularity MONTHLY \
  --metrics UnblendedCost --group-by Type=DIMENSION,Key=SERVICE --output json 2>/dev/null \
  | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("  (Cost Explorer returned nothing — enable it in the Billing console)"); sys.exit(0)
rows = []
for g in d.get("ResultsByTime", [{}])[0].get("Groups", []):
    amt = float(g["Metrics"]["UnblendedCost"]["Amount"])
    if amt > 0.001:
        rows.append((amt, g["Keys"][0]))
if not rows:
    print("  $0.00 — nothing billing this month"); sys.exit(0)
for amt, name in sorted(rows, reverse=True):
    print(f"  ${amt:8.4f}  {name}")
print("  " + "-" * 30)
print(f"  ${sum(a for a, _ in rows):8.4f}  TOTAL month-to-date")
' || echo "  (Cost Explorer unavailable)"

echo
if [ "$dirty" -eq 0 ]; then
  echo "=== CLEAN: no app-owned resources remain (see eyeball/cost notes above) ==="
else
  echo "=== DIRTY: app-owned resources still present — see the [!] lines ==="
fi
exit "$dirty"
