#!/usr/bin/env bash
# Single read-only preflight aggregator for implement-for-queue's Steps 1-2 (model/plugin policy),
# 3a (batch selection), 3b's read-only half (briefing) and 4a/4b (failed-attempt lookup, context
# gate) — batches every cfq-settings.sh/cfq-scan.sh/cfq-brief.sh/cfq-resume.sh/cfq-report.sh
# last-failure/ctx-usage.sh gate call the skill used to issue separately into one JSON object.
# Mutations (cfq-lock.sh acquire, git checkout, cfq-changelog.sh init, and cfq-branch.sh plan's
# post-checkout re-confirm on new-mode) stay explicit skill-level steps, never hidden in here — the
# `branch` field below comes from cfq-resume.sh's own internal cfq-branch.sh call (one process
# invocation total on the continue/off path), not a second direct call.
# Usage: cfq-ifq-preflight.sh <repo-root> [--select <batch>]
set -eu

command -v jq >/dev/null 2>&1 || { echo "cfq-ifq-preflight.sh: jq is required" >&2; exit 1; }

repo="${1:?usage: cfq-ifq-preflight.sh <repo-root> [--select <batch>]}"
shift
select_batch=""
if [ "${1:-}" = "--select" ]; then
  select_batch="${2:?usage: cfq-ifq-preflight.sh <repo-root> [--select <batch>]}"
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cfq="$script_dir/../bin/cfq"

resolved_root=$(git -C "$repo" rev-parse --show-toplevel 2>/dev/null) || {
  jq -n --arg repo "$repo" '{status: "NO_REPO", repo: {root: $repo}}'
  exit 0
}
repo="$resolved_root"

settings=$("$cfq" settings list --repo "$repo")
policy=$(jq -c '{implModels, allowAnyModel, implBlockedPlugins, onePhasePerSession, implExploreModel, implExploreModelComplex}' <<<"$settings")
reporting=$(jq -c '{reportDir, htmlReport}' <<<"$settings")

candidates=$("$cfq" scan | jq -c --arg repo "$repo" \
  '[(.repos[]? | select(.path == $repo) | .batches[]?
     | select(.archived == false and .open > 0))]')

planning_json=$(jq -c '[.[] | select(.planning == true) | .name]' <<<"$candidates")
blocked_json=$(jq -c '[.[] | select(.planning != true and .blocked == true)
  | {name, dependsOn, unknownDeps}]' <<<"$candidates")
eligible=$(jq -c '[.[] | select(.planning != true and .blocked != true)]' <<<"$candidates")

inprogress_names=$(jq -c '[.[] | select(.inProgress == true) | .name]' <<<"$eligible")
inprogress_count=$(jq 'length' <<<"$inprogress_names")

if [ "$inprogress_count" -gt 1 ]; then
  jq -n --arg repo "$repo" --argjson policy "$policy" --argjson reporting "$reporting" --argjson names "$inprogress_names" \
    '{status: "MULTIPLE_IN_PROGRESS", repo: {root: $repo}, policy: $policy, reporting: $reporting,
      selection: {selectable: [], blocked: [], planning: [], inProgress: null, multipleInProgress: $names},
      batch: null, nextPhase: null, branch: null, resume: null, contextGate: null}'
  exit 0
fi

if [ "$inprogress_count" -eq 1 ]; then
  inprogress_name=$(jq -r '.[0]' <<<"$inprogress_names")
  selectable=$(jq -c --arg n "$inprogress_name" \
    '[.[] | select(.name != $n) | {name, priority, open, done}]' <<<"$eligible")
else
  inprogress_name=""
  selectable=$(jq -c \
    '[.[] | {name, priority, open, done}]
     | sort_by([(if .priority == "high" then 0 else 1 end), .name])' <<<"$eligible")
fi

chosen=""
if [ -n "$select_batch" ]; then
  jq -e --arg n "$select_batch" '[.[] | select(.name == $n)] | length > 0' <<<"$eligible" >/dev/null \
    && chosen="$select_batch"
elif [ -n "$inprogress_name" ]; then
  chosen="$inprogress_name"
elif [ "$(jq 'length' <<<"$selectable")" -eq 1 ]; then
  chosen=$(jq -r '.[0].name' <<<"$selectable")
fi

if [ -z "$chosen" ]; then
  status="OK"
  if [ "$(jq 'length' <<<"$selectable")" -eq 0 ]; then
    if [ "$(jq 'length' <<<"$blocked_json")" -gt 0 ]; then status="BLOCKED"; else status="NO_BATCH"; fi
  fi
  jq -n --arg repo "$repo" --argjson policy "$policy" --argjson reporting "$reporting" --arg status "$status" \
    --argjson selectable "$selectable" --argjson blocked "$blocked_json" --argjson planning "$planning_json" \
    '{status: $status, repo: {root: $repo}, policy: $policy, reporting: $reporting,
      selection: {selectable: $selectable, blocked: $blocked, planning: $planning, inProgress: null, multipleInProgress: []},
      batch: null, nextPhase: null, branch: null, resume: null, contextGate: null}'
  exit 0
fi

qdir="$repo/.claude/cfq/impl"
batch_dir="$qdir/$chosen"
brief_text=$("$cfq" brief "$batch_dir")
cand=$(jq -c --arg n "$chosen" '.[] | select(.name == $n)' <<<"$candidates")

resume_json=$("$cfq" resume "$repo" "$batch_dir")
branch_json=$(jq -c '.branch' <<<"$resume_json")
resume_only=$(jq -c 'del(.branch)' <<<"$resume_json")

next=$(jq -c '.phasesOpen[0] // empty' <<<"$resume_json")
next_phase="OK"
if [ -n "$next" ]; then
  next_slug=$(jq -r '.slug' <<<"$next")
  next_size=$(jq -r '.size' <<<"$next")
  failed=$("$cfq" report last-failure "$batch_dir" "$next_slug")
  next_phase_json=$(jq -c --argjson f "$failed" '. + {failedAttempt: $f}' <<<"$next")

  gate_line=$("$script_dir/ctx-usage.sh" gate "$next_size")
  gate_json=$(jq -n --arg l "$gate_line" '
    ($l | try capture("^USED=(?<used>[^ ]+) SIZE=(?<size>[A-Z]) LIMIT=(?<limit>-?[0-9]+) (?<verdict>START|WARN|HANDOFF) REASON=(?<reason>[a-zA-Z]+) \\((?<note>.*)\\)$") catch null)
    | if . == null then null else
        {used: (if .used == "?" then null else (.used | tonumber) end), size,
         limit: (.limit | tonumber), verdict, reason, note}
      end')
  if [ "$gate_json" = "null" ]; then
    jq -n --arg l "$gate_line" \
      '{status: "GATE_PARSE_FAILED", detail: $l,
        action: "ctx-usage.sh emitted a gate line that does not match the expected USED/SIZE/LIMIT/verdict/REASON grammar; check ctx-usage.sh and cfq-ifq-preflight.sh for drift"}'
    exit 1
  fi
else
  next_phase_json="null"
  gate_json="null"
fi

jq -n --arg repo "$repo" --argjson policy "$policy" --argjson reporting "$reporting" \
  --argjson selectable "$selectable" --argjson blocked "$blocked_json" --argjson planning "$planning_json" \
  --arg inprog "$inprogress_name" \
  --argjson cand "$cand" --arg briefText "$brief_text" \
  --argjson nextPhase "$next_phase_json" --argjson branch "$branch_json" \
  --argjson resume "$resume_only" --argjson gate "$gate_json" '
  {
    status: "OK",
    repo: {root: $repo},
    policy: $policy,
    reporting: $reporting,
    selection: {selectable: $selectable, blocked: $blocked, planning: $planning,
                inProgress: (if $inprog == "" then null else $inprog end), multipleInProgress: []},
    batch: {name: $cand.name, priority: $cand.priority, phaseCount: $cand.open,
            dependsOn: $cand.dependsOn, briefText: $briefText},
    nextPhase: $nextPhase,
    branch: $branch,
    resume: $resume,
    contextGate: $gate
  }'
