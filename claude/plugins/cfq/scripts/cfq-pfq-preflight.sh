#!/usr/bin/env bash
# Single read-only preflight aggregator for plan-for-queue's Step 1: batches every
# cfq-settings.sh/cfq-scan.sh/cfq-registry.sh/cfq-maintenance.sh call the skill used to issue
# separately (Steps 1, 3, 5, 7, 9a, 11) into one JSON object, one process invocation of
# cfq-settings.sh covering every policy/language key at once. Security (Step 8) stays a separate
# call on purpose — this only reports capability (a security backend reachable at all), never live
# finding counts, which need a network round-trip.
# Usage: cfq-pfq-preflight.sh <repo-root>
set -eu

command -v jq >/dev/null 2>&1 || { echo "cfq-pfq-preflight.sh: jq is required" >&2; exit 1; }

repo="${1:?usage: cfq-pfq-preflight.sh <repo-root>}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cfq="$script_dir/../bin/cfq"

resolved_root=$(git -C "$repo" rev-parse --show-toplevel 2>/dev/null) || {
  jq -n --arg repo "$repo" '{status: "NO_REPO", repo: {root: $repo, known: false}}'
  exit 0
}
repo="$resolved_root"

settings=$("$cfq" settings list --repo "$repo")

known="false"
"$cfq" registry list 2>/dev/null | grep -qxF "$repo" && known="true"

batches=$("$cfq" scan | jq -c --arg repo "$repo" \
  '[(.repos[]? | select(.path == $repo) | .batches[]?
     | select(.archived == false and .open > 0)
     | {name, priority, open, dependsOn})]')

maint_raw=$("$cfq" maintenance due "$repo")
maint_status=$(awk '{print $1}' <<<"$maint_raw")
maint_n=$(awk '{print $2}' <<<"$maint_raw")
maint_n_json="null"; [ -n "$maint_n" ] && maint_n_json="$maint_n"

sec_available="false"
{ command -v gh >/dev/null 2>&1 || command -v tea >/dev/null 2>&1; } && sec_available="true"

jq -n \
  --argjson settings "$settings" \
  --arg repo "$repo" \
  --argjson known "$known" \
  --argjson batches "$batches" \
  --arg maintStatus "$maint_status" \
  --argjson maintN "$maint_n_json" \
  --argjson secAvailable "$sec_available" \
  '{
    status: "OK",
    repo: {root: $repo, known: $known},
    planningPolicy: {
      planModels: $settings.planModels,
      allowAnyModel: $settings.allowAnyModel,
      planExploreModel: $settings.planExploreModel,
      planExploreModelComplex: $settings.planExploreModelComplex,
      planBlockedPlugins: $settings.planBlockedPlugins,
      grillMode: $settings.grillMode,
      useMattpocockGrilling: $settings.useMattpocockGrilling,
      usePonytailAudit: $settings.usePonytailAudit
    },
    language: {
      codeLanguage: $settings.codeLanguage,
      docLanguages: $settings.docLanguages,
      docLevel: $settings.docLevel
    },
    queue: {openBatches: $batches},
    maintenance: {status: $maintStatus, n: $maintN},
    security: {available: $secAvailable},
    reporting: {reportDir: $settings.reportDir, htmlReport: $settings.htmlReport}
  }'
