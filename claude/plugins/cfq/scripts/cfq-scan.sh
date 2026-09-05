#!/usr/bin/env bash
# The single source of numbers for the dashboard. Usage: cfq-scan.sh [--format=json|md|tsv|overview]
# json (default): one JSON object on stdout: { "repos": [ { "path", "plan", "todo", "batches": [
#   {name, priority, open, done, archived, report, dependsOn, blocked, unknownDeps, inProgress,
#    planning} ] } ] } — byte-identical to the no-flag output for every existing caller.
# md/tsv: one row per batch across all repos (Repo, Batch, Priority, Open/Done, Status), status
# one of BLOCKED/PLANNING/IN_PROGRESS/OK per CLAUDE.md's Status Vocabulary (IN_PROGRESS is an
# additive per-batch-row extension, same pattern as RFQ's GREEN/RED/MIXED), sorted open-first,
# then flagged priority first, then name.
# overview: one row per repo (Repo, Plan, Todo, Batches, Status) — Batches is open/done batch
# counts, Status the most severe status among the repo's own batches, same vocabulary as above.
set -eu

format=json
for arg in "$@"; do
  case "$arg" in
    --format=*) format="${arg#--format=}" ;;
    *) echo "cfq-scan.sh: unknown argument: $arg" >&2; exit 1 ;;
  esac
done
case "$format" in
  json|md|tsv|overview) ;;
  *) echo "cfq-scan.sh: unknown --format value '$format' (expected json|md|tsv|overview)" >&2; exit 1 ;;
esac

command -v jq >/dev/null 2>&1 || { echo "cfq-scan.sh: jq is required" >&2; exit 1; }

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cfq="$script_dir/../bin/cfq"
# shellcheck source=cfq-paths.sh
. "$script_dir/cfq-paths.sh"

candidates=$("$cfq" registry list 2>/dev/null || true)

old_ifs=$IFS; IFS=','
for root in $("$cfq" settings get scanRoots); do
  IFS=$old_ifs
  root="${root/#\~/$HOME}"
  [ -d "$root" ] || continue
  while IFS= read -r d; do
    candidates="$candidates
$(dirname "$(dirname "$d")")"
  done < <(find "$root" -mindepth 2 -maxdepth 4 -type d -path '*/.claude/cfq' 2>/dev/null)
  IFS=','
done
IFS=$old_ifs

candidates=$(printf '%s\n' "$candidates" | sed '/^$/d' | sort -u)

for repo in $candidates; do
  # Direct sibling call: inside a per-repo loop, see CLAUDE.md's dispatcher-loop-exception note.
  "$script_dir/cfq-registry.sh" add "$repo" >/dev/null
done

trim() { sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$1"; }

read_priority() {
  [ -f "$1/.priority" ] || return 0
  local p; p=$(trim "$1/.priority")
  [ "$p" = high ] && echo high
  return 0
}

read_deps() {
  [ -f "$1/.dependsOn" ] || return 0
  sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$1/.dependsOn" \
    | sed '/^$/d' | paste -sd, -
}

records=$(mktemp)
counts=$(mktemp)
trap 'rm -f "$records" "$counts"' EXIT

while IFS= read -r repo; do
  [ -n "$repo" ] || continue
  qdir="$(cfq_repo_dir "$repo")"
  [ -d "$qdir" ] || continue
  # Direct sibling call: inside a per-repo loop, see CLAUDE.md's dispatcher-loop-exception note.
  stale_s=$(python3 "$script_dir/cfq_settings.py" get --repo "$repo" sessionStaleSeconds)

  plan=$(find "$qdir/plan" -maxdepth 1 -name '*.md' -type f 2>/dev/null | wc -l)
  todo=$(find "$qdir/todo" -maxdepth 1 -name '*.md' -type f 2>/dev/null | wc -l)
  printf '%s\t%s\t%s\n' "$repo" "$plan" "$todo" >>"$counts"

  for b in "$qdir/impl"/*/; do
    [ -d "$b" ] || continue
    name=$(basename "$b")
    [[ "$name" =~ ^([0-9]+-)?[0-9]{4}-[0-9]{2}-[0-9]{2}-.+ ]] || continue
    open=$(find "$b" -maxdepth 1 -name '[0-9][0-9]-*.md' -type f | wc -l)
    donec=$(find "$b/done" -maxdepth 1 -name '*.md' -type f 2>/dev/null | wc -l)
    priority=$(read_priority "$b")
    report="false"; [ -f "$b/report.json" ] && report="true"
    deps=$(read_deps "$b")
    blocked="false"; unknown=""
    if [ -n "$deps" ]; then
      old_ifs=$IFS; IFS=','
      for d in $deps; do
        if [ -d "$qdir/impl/$d" ]; then
          blocked="true"                    # dependency exists and is not finished
        elif [ -d "$qdir/impl/done/$d" ]; then
          :                                 # finished — satisfied
        else
          unknown="${unknown:+$unknown,}$d" # unresolvable: reported, never blocking
        fi
      done
      IFS=$old_ifs
    fi
    planning="false"
    if [ -f "$b/.planning" ]; then
      age=$(( $(date +%s) - $(stat -c %Y "$b/.planning" 2>/dev/null || echo 0) ))
      [ "$age" -lt "$stale_s" ] && planning="true"
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$repo" "$name" "$priority" "$open" "$donec" "false" "$report" "$deps" "$blocked" "$unknown" "$planning" >>"$records"
  done

  if [ -d "$qdir/impl/done" ]; then
    for b in "$qdir/impl/done"/*/; do
      [ -d "$b" ] || continue
      name=$(basename "$b")
      [[ "$name" =~ ^([0-9]+-)?[0-9]{4}-[0-9]{2}-[0-9]{2}-.+ ]] || continue
      donec=$(find "$b" -maxdepth 1 -name '[0-9][0-9]-*.md' -type f | wc -l)
      priority=$(read_priority "$b")
      report="false"; [ -f "$b/report.json" ] && report="true"
      printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$repo" "$name" "$priority" "0" "$donec" "true" "$report" "" "false" "" "false" >>"$records"
    done
  fi
done <<<"$candidates"

scan_json=$(jq -n --rawfile recs "$records" --rawfile cnts "$counts" '
  def parse_tsv: split("\n") | map(select(length > 0)) | map(split("\t"));

  ($recs | parse_tsv | map({
    path: .[0], name: .[1], priority: .[2],
    open: (.[3] | tonumber), done: (.[4] | tonumber),
    archived: (.[5] == "true"), report: (.[6] == "true"),
    dependsOn:   (((.[7] // "") | select(. != "")) // "" | if . == "" then [] else split(",") end),
    blocked:     ((.[8] // "false") == "true"),
    unknownDeps: (((.[9] // "") | select(. != "")) // "" | if . == "" then [] else split(",") end),
    inProgress:  ((.[3] | tonumber) > 0 and (.[4] | tonumber) > 0),
    planning:    ((.[10] // "false") == "true")
  }) | group_by(.path)
     | map({key: .[0].path,
            value: map({name, priority, open, done, archived, report, dependsOn, blocked, unknownDeps, inProgress, planning})})
     | from_entries) as $batchmap
  |
  ($cnts | parse_tsv
     | map({key: .[0], value: {plan: (.[1] | tonumber), todo: (.[2] | tonumber)}})
     | from_entries) as $countmap
  |
  (($batchmap | keys) + ($countmap | keys) | unique) as $paths
  |
  { repos: [ $paths[] | . as $p | {
      path: $p,
      plan: ($countmap[$p].plan // 0),
      todo: ($countmap[$p].todo // 0),
      batches: ($batchmap[$p] // [])
    } ] }
')

# Shared row-projection for md/tsv: one row per batch, Status pre-computed from Phase 1's
# vocabulary (BLOCKED/PLANNING take precedence, IN_PROGRESS is the additive per-row extension),
# sorted open batches first, then flagged priority first, then name.
row_filter='
  def st: if .blocked then "BLOCKED"
          elif .planning then "PLANNING"
          elif .inProgress then "IN_PROGRESS"
          else "OK" end;
  [ .repos[] as $r | $r.batches[] | {
      repo: ($r.path | split("/") | last),
      name,
      priority: (if .priority == "" then "-" else .priority end),
      openDone: ((.open | tostring) + "/" + (.done | tostring)),
      archived,
      status: st
    } ]
  | sort_by(.archived, (if .priority == "high" then 0 else 1 end), .name)
'

case "$format" in
  json)
    printf '%s\n' "$scan_json"
    ;;
  md)
    jq -r "$row_filter"'
      | (["Repo","Batch","Priority","Open/Done","Status"] | "| " + join(" | ") + " |"),
        "|---|---|---|---|---|",
        (.[] | [.repo,.name,.priority,.openDone,.status] | "| " + join(" | ") + " |")
    ' <<<"$scan_json"
    ;;
  tsv)
    jq -r "$row_filter"'
      | .[] | [.repo,.name,.priority,.openDone,.status] | @tsv
    ' <<<"$scan_json"
    ;;
  overview)
    jq -r '
      def st($b):
        if ([$b[] | .blocked] | any) then "BLOCKED"
        elif ([$b[] | .planning] | any) then "PLANNING"
        elif ([$b[] | .inProgress] | any) then "IN_PROGRESS"
        else "OK" end;
      (["Repo","Plan","Todo","Batches","Status"] | "| " + join(" | ") + " |"),
      "|---|---|---|---|---|",
      (.repos[] | [
          (.path | split("/") | last), (.plan|tostring), (.todo|tostring),
          (([.batches[] | select(.archived == false)] | length | tostring) + "/"
            + ([.batches[] | select(.archived == true)] | length | tostring)),
          st(.batches)
        ] | "| " + join(" | ") + " |")
    ' <<<"$scan_json"
    ;;
esac
