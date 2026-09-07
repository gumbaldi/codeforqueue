#!/usr/bin/env bash
# Single read-only aggregator for the /cfq dashboard: bundles plugin status (installed + the two
# switches), the cross-repo scan rolled up per repo, and this repo's (or, outside a repo, the
# global) settings-with-sources — the four separate calls the skill used to make — into one JSON
# object. Usage: cfq-dash.sh [render] [--all] [<cwd>]
# Default mode emits the JSON object, unaffected by `--all`. `render` formats the identical
# aggregation as the terminal text the /cfq dashboard prints — same data, no second aggregator.
# `--all` (render only) lists every batch this repo ever had instead of only its open ones.
set -eu

command -v jq >/dev/null 2>&1 || { echo "cfq-dash.sh: jq is required" >&2; exit 1; }

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cfq="$script_dir/../bin/cfq"
# shellcheck source=cfq-paths.sh
. "$script_dir/cfq-paths.sh"
mode="json"
if [ "${1:-}" = "render" ]; then
  mode="render"
  shift
fi
all=false
rest=()
for a in "$@"; do
  if [ "$a" = "--all" ]; then
    all=true
  else
    rest+=("$a")
  fi
done
cwd="${rest[0]:-$(pwd)}"

runtime_json=$("$cfq" runtime plugins)
if [ "$(jq -r '.status' <<<"$runtime_json")" != "OK" ]; then
  if [ "$mode" = "render" ]; then
    printf 'PRECHECKS\n'
    printf '⚠️ %-16s%s\n' "Dash" "runtime degraded · $(jq -r '.code // .cap // "see detail"' <<<"$runtime_json")"
    printf '➖ %-16s%s\n' "Plugins" "unknown · runtime degraded"
  else
    jq -n --argjson rt "$runtime_json" \
      '{status: "RUNTIME_DEGRADED", runtimeDiagnostic: $rt, plugins: null, repos: [], thisRepo: null, settings: []}'
  fi
  exit 0
fi
plugins_list=$(jq -c '.plugins' <<<"$runtime_json")
pony_mode=$(jq -r '.ponytailMode' <<<"$runtime_json")

repo=$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null || true)
repo_args=(); [ -n "$repo" ] && repo_args=(--repo "$repo")

settings_src=$("$cfq" settings list "${repo_args[@]}" --sources)

# One env-stripped re-read of the same tiers, only when at least one key is actually
# env-overridden — gives every masked file value in one call, never one per key.
masked_src="{}"
if [ "$(jq '[.[] | select(.source | startswith("env"))] | length' <<<"$settings_src")" -gt 0 ]; then
  masked_src=$(env -i HOME="$HOME" PATH="$PATH" "$cfq" settings list "${repo_args[@]}" --sources)
fi

settings_json=$(jq -c --argjson masked "$masked_src" '
  def marker:
    if . == "default" then "D" elif . == "global" then "G" elif . == "repo" then "R" else "E" end;
  [ to_entries[] | . as $e | {
      key: $e.key, value: $e.value.value, source: $e.value.source, marker: ($e.value.source | marker)
    } + (if ($e.value.source | startswith("env")) then
           {maskedValue: $masked[$e.key].value, maskedSource: $masked[$e.key].source}
         else {} end)
  ]
' <<<"$settings_src")

use_grill=$(jq -r '.useMattpocockGrilling.value' <<<"$settings_src")
use_pony=$(jq -r '.usePonytailAudit.value' <<<"$settings_src")
has_mp=$(jq -c 'index("mattpocock-skills") != null' <<<"$plugins_list")
has_pt=$(jq -c 'index("ponytail") != null' <<<"$plugins_list")
plugins_obj=$(jq -n --argjson mp "$has_mp" --argjson pt "$has_pt" --argjson g "$use_grill" --argjson p "$use_pony" \
  --arg pm "$pony_mode" \
  '{mattpocock: $mp, ponytail: $pt, useMattpocockGrilling: $g, usePonytailAudit: $p, ponytailMode: $pm}')

scan_json=$("$cfq" scan)

# Repo-level rollup: batch counts (open = not yet archived, done = moved to impl/done/) plus the
# most severe status among the repo's batches — aggregated from counters the scan already
# computed, never recounted from disk.
repos_json=$(jq -c '
  def rst($b):
    if ([$b[] | .blocked] | any) then "BLOCKED"
    elif ([$b[] | .planning] | any) then "PLANNING"
    elif ([$b[] | .inProgress] | any) then "IN_PROGRESS"
    else "OK" end;
  [ .repos[] | {
      path, name: (.path | split("/") | last), plan, todo,
      open: ([.batches[] | select(.archived == false)] | length),
      done: ([.batches[] | select(.archived == true)] | length),
      status: rst(.batches)
    } ]
' <<<"$scan_json")

this_repo_json="null"
if [ -n "$repo" ]; then
  this_repo_json=$(jq -c --arg p "$repo" '
    def bst:
      if .blocked then "BLOCKED" elif .planning then "PLANNING"
      elif .inProgress then "IN_PROGRESS" else "OK" end;
    ([.repos[] | select(.path == $p)][0]) as $r
    | if $r == null then null
      else { path: $r.path, name: ($r.path | split("/") | last),
             batches: [ $r.batches[] | {name, priority, open, done, status: bst} ] }
      end
  ' <<<"$scan_json")
fi

status="OK"
if [ "$this_repo_json" != "null" ] \
   && [ "$(jq '[.batches[] | select(.status == "IN_PROGRESS")] | length' <<<"$this_repo_json")" -gt 1 ]; then
  status="MULTIPLE_IN_PROGRESS"
elif [ "$(jq 'length' <<<"$repos_json")" -eq 0 ]; then
  status="NO_REPO"
fi

if [ "$mode" != "render" ]; then
  jq -n --arg status "$status" --argjson plugins "$plugins_obj" --argjson repos "$repos_json" \
    --argjson thisRepo "$this_repo_json" --argjson settings "$settings_json" \
    '{status: $status, plugins: $plugins, repos: $repos, thisRepo: $thisRepo, settings: $settings}'
  exit 0
fi

# --- render mode: same aggregation, formatted as the terminal text /cfq's dashboard prints. ---

# Next-batch expansion: the batch `/ifq` would pick next (`bin/cfq scan --format=next`, phase 01)
# rendered to its individual phases (`bin/cfq brief --with-done`, batch 015) — same lines as
# /ifq's own Step 4 briefing. Only computed here, in render mode, never for the json branch above.
next_expanded=""
next_header=""
next_note=""
if [ -n "$repo" ]; then
  next_json=$("$cfq" scan --format=next)
  next_row=$(jq -c --arg p "$repo" \
    '(.repos[] | select(.path == $p)) // {next: null, reason: null, blocked: [], planning: []}' \
    <<<"$next_json")
  next_name=$(jq -r '.next // empty' <<<"$next_row")
  next_reason=$(jq -r '.reason // empty' <<<"$next_row")
  if [ -n "$next_name" ]; then
    case "$next_reason" in
      inProgress) reason_text="in progress" ;;
      priority)   reason_text="priority high" ;;
      order)      reason_text="next in order" ;;
      *)          reason_text="$next_reason" ;;
    esac
    next_expanded=$("$cfq" brief "$(impl_dir "$repo")/$next_name" --with-done)
    next_header="$next_name ($reason_text)"
  else
    blocked_name=$(jq -r '.blocked[0] // empty' <<<"$next_row")
    if [ -n "$blocked_name" ]; then
      deps=$(jq -r --arg p "$repo" --arg n "$blocked_name" \
        '[(.repos[] | select(.path == $p) | .batches[] | select(.name == $n) | .dependsOn[])] | join(", ")' \
        <<<"$scan_json")
      next_note="Next batch blocked: $blocked_name waiting on $deps"
    fi
  fi
fi

dash_line=$(jq -rn --arg status "$status" --argjson repos "$repos_json" --argjson thisRepo "$this_repo_json" '
  if $status == "MULTIPLE_IN_PROGRESS" then
    "⚠️\tMULTIPLE_IN_PROGRESS in " + $thisRepo.name + ": "
    + ([$thisRepo.batches[] | select(.status == "IN_PROGRESS") | .name] | join(", "))
    + " — invariant violation, resolve manually"
  else
    "✅\t" + ($repos | length | tostring) + " repos · "
    + ([$repos[] | select(.open > 0)] | length | tostring) + " with open work"
  end
'
)
plugins_line=$(jq -rn --argjson p "$plugins_obj" '
  $p as $p
  | (if $p.ponytail and $p.ponytailMode != "off" then
       "ponytail default mode: " + $p.ponytailMode + " · cfq expects off"
     else null end) as $modeClause
  | (if ($p.mattpocock == false) and ($p.ponytail == false) then
      {icon:"➖", text:"mattpocock-skills/ponytail not installed"}
    elif ($p.mattpocock == true) and ($p.ponytail == true) then
      ([ (if $p.useMattpocockGrilling then empty else "grill: classic off" end),
         (if $p.usePonytailAudit then empty else "maintenance audit: off" end) ]) as $off
      | if ($off | length) == 0 then
          {icon:"✅", text:"mattpocock-skills and ponytail installed · classic grill on · maintenance audit: on"}
        else
          {icon:"➖", text: ("installed · " + ($off | join(", ")))}
        end
    else
      (if $p.mattpocock then
         {missing: "ponytail", state: (if $p.useMattpocockGrilling then "classic grill on" else "classic grill off" end)}
       else
         {missing: "mattpocock-skills", state: (if $p.usePonytailAudit then "maintenance audit: on" else "maintenance audit: off" end)}
       end) as $m
      | {icon:"➖", text: ($m.missing + " not installed · " + $m.state)}
    end) as $base
  | (if $modeClause == null then $base.text else $base.text + " · " + $modeClause end) as $text
  | (if $modeClause != null then "⚠️" else $base.icon end) as $icon
  | $icon + "\t" + $text
'
)

printf 'PRECHECKS\n'
printf '%s %-16s%s\n' "${dash_line%%$'\t'*}" "Dash" "${dash_line#*$'\t'}"
printf '%s %-16s%s\n' "${plugins_line%%$'\t'*}" "Plugins" "${plugins_line#*$'\t'}"

jq -rn --argjson repos "$repos_json" --argjson thisRepo "$this_repo_json" --argjson settings "$settings_json" \
  --argjson all "$all" --arg expanded "$next_expanded" --arg expandedHeader "$next_header" \
  --arg nextNote "$next_note" '
  def implModel: (($settings[] | select(.key == "implModels") | .value[0]) // "sonnet");
  ( if ($repos | length) == 0 then
      ["", "No repos with a queue yet."]
    else
      ["", "QUEUES", "| Repo | Plan | Todo | Batches | Status |", "|---|---|---|---|---|"]
      + [ $repos[] | "| " + .name + " | " + (.plan|tostring) + " | " + (.todo|tostring) + " | "
          + ((.open|tostring) + "/" + (.done|tostring)) + " | " + .status + " |" ]
    end
  )
  + ( if $thisRepo == null then [] else
      ( [ $thisRepo.batches[] | select($all or .open > 0) ] ) as $rows
      | ( if ($rows | length) > 0 then
            ["", ("THIS REPO · " + $thisRepo.name), "| Batch | Priority | Open/Done | Status |", "|---|---|---|---|"]
            + [ $rows[] | "| " + .name + " | " + (if .priority == "" then "-" else .priority end) + " | "
                + ((.open|tostring) + "/" + (.done|tostring)) + " | " + .status + " |" ]
          else
            ["", ("THIS REPO · " + $thisRepo.name)]
          end
        ) as $table
      | ( if $all then [] else
            [ (([ $thisRepo.batches[] | select(.open == 0) ] | length | tostring)
               + " batches done · full list: bin/cfq dash render --all") ]
          end
        ) as $summary
      | ( if $expanded != "" then ["", $expandedHeader, $expanded]
          elif $nextNote != "" then ["", $nextNote]
          else [] end
        ) as $expansion
      | $table + $summary + $expansion
    end
  )
  + ( if $thisRepo == null then [] else
      ([ $settings[] | select(.marker != "D") ]) as $rows
      | ["", ("CONFIG · " + $thisRepo.name),
         (($rows|length|tostring) + " of " + ($settings|length|tostring) + " keys differ from default")]
      + ( if ($rows|length) == 0 then [] else
          [ $rows[] | "[" + .marker + "] " + .key + "  " + (.value|tostring)
            + (if has("maskedValue") then
                 "\n   └ ⚠ masks " + .maskedSource + " value `" + (.maskedValue|tostring) + "`"
               else "" end) ]
        end)
    end
  )
  + ( if $thisRepo == null then [] else
      def pad27: . + (" " * (27 - length));
      [
        ["flag / unflag priority", "mark a batch high priority"],
        ["delete a batch", "removes the queue directory"],
        ["archive a batch", "moves it to impl/done/"],
        ["clean the registry", "drop repos that no longer exist"],
        ["set / remove a dependency", ".dependsOn between batches"],
        ["work off todo/ entries", "runs their check: commands"],
        ["change a setting", "just say it in plain language"],
        ["full batch list", "bin/cfq dash render --all"],
        ["settings, this repo", "bin/cfq settings list --repo " + $thisRepo.path + " --sources"],
        ["settings, global", "bin/cfq settings list --sources"]
      ] as $actionRows
      | ["", "ACTIONS"] + [ $actionRows[] | (.[0] | pad27) + .[1] ]
    end
  )
  + (
      ( [ $repos[] | select(.status != "BLOCKED" and .open > 0) ] ) as $eligible
      | if ($eligible | length) == 0 then []
        else
          ( if $thisRepo != null then
              ([ $eligible[] | select(.path == $thisRepo.path) ]) + ([ $eligible[] | select(.path != $thisRepo.path) ])
            else $eligible end
          ) as $ordered
          | ["", "NEXT"] + [ $ordered[] | "\ncd " + .path + "\n/model " + implModel + "\n/ifq" ]
        end
    )
  | .[]
'
