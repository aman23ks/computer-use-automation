#!/usr/bin/env bash
# Regenerates /evidence end to end. Requires ANTHROPIC_API_KEY (discovery) and both mock tenants running:
#   python -m target_app --port 8601 & python -m target_app --port 8602 --variant lakeshore &
# PLANNER=script runs discovery with the scripted planner instead (clearly labelled in provenance).
set -euo pipefail
cd "$(dirname "$0")/.."
CUA=./.venv/bin/cua
PLANNER=${PLANNER:-claude}
KIND="LLM (Claude) discovery"
[ "$PLANNER" = "script" ] && KIND="Scripted-planner discovery (stand-in for the LLM run)"
BAL=coreone.member.read_primary_savings_balance
SHARE=coreone.member.open_share_account
SHARE_PARAMS=(--param member_number=100234 --param "share_type=Money Market" --param "nickname=Rainy Day" --param opening_deposit=25.00)

rm -rf runs artifacts evidence
mkdir -p evidence
echo "| # | Run | What it shows | Status |" > evidence/INDEX.md
echo "|---|---|---|---|" >> evidence/INDEX.md
n=0

keep() {  # keep <label> <description> <json-output-file>
  n=$((n + 1))
  local label="$1" desc="$2" out="$3"
  local dir status
  dir=$(./.venv/bin/python -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('evidence_dir') or '')" "$out")
  status=$(./.venv/bin/python -c "import json,sys; d=json.load(open(sys.argv[1])); o=d.get('outcome') or {}; e=d.get('error') or {}; print(' '.join(x for x in [d.get('status',''), o.get('code',''), e.get('code','')] if x))" "$out")
  local dest
  dest="evidence/$(printf '%02d' "$n")-${label}"
  if [ -n "$dir" ] && [ -d "$dir" ]; then cp -R "$dir" "$dest"; else mkdir -p "$dest"; fi
  cp "$out" "$dest/cli-output.json"
  echo "| $n | [\`$(basename "$dest")\`]($(basename "$dest")/) | $desc | \`$status\` |" >> evidence/INDEX.md
  echo "[$n] $label: $status"
}

planner_args=()
[ "$PLANNER" = "script" ] && planner_args=(--planner script:config/scripts/read_primary_savings_balance.json)
tmp=$(mktemp -d)

$CUA faults --tenant meridian --clear >/dev/null
$CUA discover config/goals/read_primary_savings_balance.yaml --param member_number=100234 "${planner_args[@]}" > "$tmp/d1.json"
keep discovery-read-balance "$KIND: goal -> recorded capability artifact" "$tmp/d1.json"
$CUA approve $BAL --by "reviewer@example.test" --note "reviewed steps, locators and outputs" > /dev/null

r() { local label="$1" desc="$2"; shift 2; set +e; $CUA replay "$@" > "$tmp/$label.json"; set -e; keep "$label" "$desc" "$tmp/$label.json"; }

r replay-success "Deterministic replay, no LLM: outputs returned" $BAL --param member_number=100234
r replay-member-not-found "Business outcome, not a crash" $BAL --param member_number=555555
r replay-access-restricted "Business outcome: permission denial" $BAL --param member_number=100999
r replay-bad-input "Input rejected by contract before any UI action" $BAL --param member_number=12AB
$CUA faults --add interstitial >/dev/null
r replay-interstitial-recovered "Recoverable: maintenance interstitial acknowledged" $BAL --param member_number=100234
$CUA faults --add session_expire >/dev/null
r replay-session-expired-recovered "Recoverable: session expiry -> re-authenticate and restart (no mutation yet)" $BAL --param member_number=100234
$CUA faults --add app_error >/dev/null
r replay-app-error "Hard failure: HTTP 500 with step, expected/observed, screenshot and DOM evidence" $BAL --param member_number=100234
$CUA faults --add pwd_notice >/dev/null
r replay-unknown-state "Hard failure: state unknown to the profile (no escalation)" $BAL --param member_number=100234

$CUA faults --add pwd_notice >/dev/null
( sleep 1; $CUA operator-bot --click "Remind Me Later" --decision resume --note "Dismissed password-expiry warning; not a blocker." ) &
r replay-human-handoff "Escalation -> operator takes the live session -> hand back -> resume at checkpoint" $BAL --param member_number=100234 --escalate
wait

r tenant-lakeshore-no-overlay "Same artifact on a rebranded tenant: succeeds, reports locator drift" $BAL --tenant lakeshore-raw --param member_number=100234
r tenant-lakeshore-overlay "Tenant overlay restores semantic locators without re-recording" $BAL --tenant lakeshore --param member_number=100234
set +e; $CUA replay $BAL --param member_number=100234 --repeat 5 > "$tmp/stability.json"; set -e
cp "$tmp/stability.json" evidence/stability-meridian.json

share_planner=()
[ "$PLANNER" = "script" ] && share_planner=(--planner script:config/scripts/open_share_account.json)
( sleep 1; $CUA operator-bot --decision approve --note "Verified with member at branch; approved opening deposit." --wait-s 600 ) &
$CUA discover config/goals/open_share_account.yaml "${SHARE_PARAMS[@]}" "${share_planner[@]}" > "$tmp/d2.json"
wait
keep discovery-open-share-with-approval "$KIND of an irreversible flow; operator approves Submit" "$tmp/d2.json"
r replay-share-needs-approval "Irreversible step without caller authorization: stops before Submit" $SHARE --allow-draft "${SHARE_PARAMS[@]}"
r replay-share-authorized "Caller-authorized irreversible replay: confirmation number returned" $SHARE --allow-draft "${SHARE_PARAMS[@]}" --authorize-irreversible
r replay-share-validation-error "Business outcome from app validation (deposit below minimum)" $SHARE --allow-draft --param member_number=100234 --param "share_type=Money Market" --param "nickname=Rainy Day" --param opening_deposit=1.00 --authorize-irreversible

mkdir -p evidence/artifacts
cp -R artifacts/* evidence/artifacts/
$CUA catalog > evidence/catalog.json
$CUA show $BAL > evidence/artifacts/review-read-balance.txt
$CUA show $SHARE > evidence/artifacts/review-open-share.txt
echo "evidence written to ./evidence (see evidence/INDEX.md)"
