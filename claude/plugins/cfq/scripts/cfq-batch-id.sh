#!/usr/bin/env bash
# Repository-local CFQ batch-number allocation: a stable, version-free identity assigned once at
# PFQ park time. Never derives a number from a Git branch name or an application/package version;
# the only Git-history fallback is Phase 1's one-time trailer bootstrap inside cfq-changelog.sh
# ensure, for a ledger that does not exist yet. Width is an implementation invariant (see
# BATCH_WIDTH_MIGRATION_REQUIRED below), not a user-facing setting.
# Usage: cfq-batch-id.sh next          <repo-root> <YYYY-MM-DD> <slug>
#        cfq-batch-id.sh allocate      <repo-root> <YYYY-MM-DD> <slug>
#        cfq-batch-id.sh migrate-width <repo-root>
#        cfq-batch-id.sh reconcile     <repo-root> [--fix]
#
# `allocate` performs an automatic width migration itself when the next number needs an extra
# digit and the active queue is empty (BATCH_WIDTH_MIGRATION_BLOCKED otherwise) -- the normal PFQ
# path never needs a separate manual step. `migrate-width` is the same operation exposed directly,
# useful for recovery/testing; it is a no-op (`status: OK`) when no migration is currently needed.
# `reconcile` compares queue directories against the ledger's numbered entries and reports/repairs
# the gap BATCH_LEDGER_MISMATCH refuses to allocate through -- see reconcile() below.
set -eu

command -v jq >/dev/null 2>&1 || { echo "cfq-batch-id.sh: jq is required" >&2; exit 1; }

here="$(dirname "${BASH_SOURCE[0]}")"
cfq="$here/../bin/cfq"
# shellcheck source=cfq-paths.sh
. "$here/cfq-paths.sh"

# New-name grammar: <digits>-<YYYY-MM-DD>-<slug>, digits precede the date. A legacy
# <YYYY-MM-DD>-<slug> name never satisfies this (its own date would have to double as the digit
# run AND be immediately followed by another full date), so legacy directories are ignored, not
# specially cased.
NUMBERED_RE='^([0-9]{3,})-([0-9]{4}-[0-9]{2}-[0-9]{2})-([a-z0-9][a-z0-9-]*)$'
DEFAULT_WIDTH=3

err() { # status detail action
  jq -n --arg s "$1" --arg d "$2" --arg a "${3:-}" '{status:$s, detail:$d, action:$a}'
}

# Prints the digit-run width of a numbered identifier, nothing on no match. Never partially
# parses: a name either satisfies the full grammar or is ignored outright.
numbered_width() {
  [[ "$1" =~ $NUMBERED_RE ]] || return 1
  printf '%s\n' "${#BASH_REMATCH[1]}"
}

numbered_number() {
  [[ "$1" =~ $NUMBERED_RE ]] || return 1
  printf '%d\n' "$((10#${BASH_REMATCH[1]}))"
}

# Widths seen among existing numbered queue directories and changelog `batch:` entries, one per
# line, deduplicated by the caller. More than one distinct width is an unresolvable conflict.
observed_widths() {
  local repo="$1" d p name w target
  d="$(impl_dir "$repo")"
  if [ -d "$d" ]; then
    for p in "$d"/*/; do
      [ -d "$p" ] || continue
      name="$(basename "$p")"
      [ "$name" = "done" ] && continue
      if w="$(numbered_width "$name")"; then printf '%s\n' "$w"; fi
    done
  fi
  target="$(changelog_path "$repo" 2>/dev/null || true)"
  if [ -n "$target" ] && [ -f "$target" ]; then
    while IFS= read -r name; do
      if w="$(numbered_width "$name")"; then printf '%s\n' "$w"; fi
    done < <(grep '^  batch: ' "$target" | sed 's/^  batch: *//')
  fi
}

# Highest number among numbered queue directories only (never legacy ones), 0 if none.
queue_max() {
  local repo="$1" d p name n max=0
  d="$(impl_dir "$repo")"
  if [ -d "$d" ]; then
    for p in "$d"/*/; do
      [ -d "$p" ] || continue
      name="$(basename "$p")"
      [ "$name" = "done" ] && continue
      if n="$(numbered_number "$name")" && [ "$n" -gt "$max" ]; then max="$n"; fi
    done
  fi
  printf '%s\n' "$max"
}

# True (exit 0) when the active queue -- every planning/open/in-progress batch, i.e. every
# directory directly under impl/ other than "done" -- is completely empty. A width migration may
# only run while this holds; archived/completed batches under impl/done/ don't count.
queue_is_empty() {
  local repo="$1" d p
  d="$(impl_dir "$repo")"
  [ -d "$d" ] || return 0
  for p in "$d"/*/; do
    [ -d "$p" ] || continue
    [ "$(basename "$p")" = "done" ] && continue
    return 1
  done
  return 0
}

changelog_path() {
  local repo="$1" rel
  rel="$("$cfq" settings get changelogFile)"
  [ -n "$rel" ] || return 1
  printf '%s/%s\n' "$repo" "$rel"
}

# Computes the next identity. Prints the full {status, ...} result and returns 0 for OK, 1 for any
# structured non-OK status (BATCH_CHANGELOG_REQUIRED / BATCH_LEDGER_MISMATCH /
# BATCH_WIDTH_MIGRATION_REQUIRED). Read-only except for cfq-changelog.sh's own one-time missing-
# ledger bootstrap, which must run before any number can be computed at all.
compute_next() {
  local repo="$1" date="$2" slug="$3" cf changelog_max qmax widths n_widths width next digits formatted

  cf="$("$cfq" settings get changelogFile)"
  if [ -z "$cf" ]; then
    err BATCH_CHANGELOG_REQUIRED "changelogFile is disabled; numbered batch identity needs the CFQ workflow changelog" "set changelogFile before allocating a numbered batch"
    return 1
  fi

  "$cfq" changelog ensure "$repo" >/dev/null
  changelog_max="$("$cfq" changelog max-batch-number "$repo")"
  qmax="$(queue_max "$repo")"

  if [ "$qmax" -gt "$changelog_max" ]; then
    err BATCH_LEDGER_MISMATCH "queue directory number $qmax exceeds changelog max $changelog_max" \
      "reconcile the queue directory against $cf -- \`cfq batch reconcile <repo>\` reports the gap and \`--fix\` can close it"
    return 1
  fi

  widths="$(observed_widths "$repo" | sort -u)"
  n_widths="$(printf '%s\n' "$widths" | grep -c . || true)"
  if [ "$n_widths" -gt 1 ]; then
    err BATCH_LEDGER_MISMATCH "numbered batch identifiers disagree on width: $(printf '%s' "$widths" | tr '\n' ' ')" "run the width migration before allocating"
    return 1
  fi
  width="$widths"
  [ -n "$width" ] || width="$DEFAULT_WIDTH"

  next=$((changelog_max + 1))
  digits=${#next}
  if [ "$digits" -gt "$width" ]; then
    jq -n --argjson n "$next" --argjson w "$width" --argjson rw "$digits" '
      {status:"BATCH_WIDTH_MIGRATION_REQUIRED",
       nextNumber:$n, currentWidth:$w, requiredWidth:$rw,
       detail:("next batch number " + ($n|tostring) + " needs " + ($rw|tostring) + " digits, current width is " + ($w|tostring)),
       action:"run the width migration first, then allocate again"}'
    return 1
  fi

  formatted="$(printf "%0${width}d" "$next")"
  jq -n --argjson n "$next" --argjson w "$width" --arg f "$formatted" --arg date "$date" --arg slug "$slug" \
    '{status:"OK", batchNumber:$n, width:$w, formatted:$f, date:$date, slug:$slug, batch: ($f + "-" + $date + "-" + $slug)}'
}

# Deterministic, idempotent width migration. Only two locations can hold a numbered identifier
# while the active queue is empty (the caller's job to verify -- this function doesn't re-check):
# impl/done/ directory names and changelog.yml `batch:` fields. Never touches impl/ itself (empty
# by precondition), Git history, branch names, or legacy unnumbered batches. Re-derives the
# rewrite set from current disk/changelog state on every call rather than tracking a marker, so a
# repeated or interrupted invocation converges to the same end state: an entry already at the
# target width simply no longer matches and is skipped. `status: OK` with nothing to do is not an
# error -- allocate calls this unconditionally once it decides a migration is needed.
#
# Target width is the max of the default, the digits the next number needs, and every width
# already observed -- not just "the" current width. compute_next refuses to run at all while more
# than one width is observed (BATCH_LEDGER_MISMATCH), but this function must still be able to
# finish an interrupted prior migration on its own (some entries already at the new width, some
# still at the old one), so it never assumes a single incoming width the way compute_next does.
migrate_width() {
  local repo="$1" target d changelog_max next digits to name w num suffix new_name
  target="$(changelog_path "$repo" 2>/dev/null || true)"
  d="$(impl_done_dir "$repo")"

  changelog_max="$("$cfq" changelog max-batch-number "$repo")"
  next=$((changelog_max + 1))
  digits=${#next}
  to="$DEFAULT_WIDTH"
  [ "$digits" -gt "$to" ] && to="$digits"
  for w in $(observed_widths "$repo" | sort -un); do
    [ "$w" -gt "$to" ] && to="$w"
  done

  local -a old_names=()
  if [ -n "$target" ] && [ -f "$target" ]; then
    while IFS= read -r name; do
      [ -n "$name" ] || continue
      w="$(numbered_width "$name" 2>/dev/null)" || continue
      [ "$w" -lt "$to" ] || continue
      old_names+=("$name")
    done < <(grep '^  batch: ' "$target" | sed 's/^  batch: *//')
  fi
  if [ -d "$d" ]; then
    local p
    for p in "$d"/*/; do
      [ -d "$p" ] || continue
      name="$(basename "$p")"
      w="$(numbered_width "$name" 2>/dev/null)" || continue
      [ "$w" -lt "$to" ] || continue
      old_names+=("$name")
    done
  fi
  if [ "${#old_names[@]}" -gt 0 ]; then
    mapfile -t old_names < <(printf '%s\n' "${old_names[@]}" | sort -u)
  fi

  if [ "${#old_names[@]}" -eq 0 ]; then
    jq -n --argjson w "$to" '{status:"OK", width:$w, migrated:0}'
    return 0
  fi

  # Preflight: compute every destination and fail closed on any collision before mutating anything.
  local -a pairs=()
  for name in "${old_names[@]}"; do
    num="$(numbered_number "$name")"
    suffix="$(printf '%s' "$name" | sed -E 's/^[0-9]+-//')"
    new_name="$(printf "%0${to}d" "$num")-$suffix"
    if [ -e "$d/$new_name" ]; then
      err BATCH_WIDTH_MIGRATION_COLLISION "migration destination already exists on disk: $new_name" "resolve the collision under $d before retrying"
      return 1
    fi
    if [ -n "$target" ] && [ -f "$target" ] && grep -qxF "  batch: $new_name" "$target"; then
      err BATCH_WIDTH_MIGRATION_COLLISION "migration destination already exists in the changelog: $new_name" "resolve the collision in $target before retrying"
      return 1
    fi
    pairs+=("$name:$new_name")
  done

  local pair old_n new_n
  for pair in "${pairs[@]}"; do
    old_n="${pair%%:*}"; new_n="${pair#*:}"
    [ -d "$d/$old_n" ] && mv "$d/$old_n" "$d/$new_n"
    if [ -n "$target" ] && [ -f "$target" ]; then
      # Direct sibling call: inside a per-pair loop, where a dispatcher exec per iteration
      # is the more expensive trade (see CLAUDE.md's dispatcher-loop-exception note).
      python3 "$here/cfq_changelog.py" rename-batch "$repo" "$old_n" "$new_n"
    fi
  done

  jq -n --argjson w "$to" --argjson n "${#pairs[@]}" '{status:"OK", width:$w, migrated:$n}'
}

# All numbered directory names under impl/ (excluding "done" itself) and impl/done/, one per
# line -- both count against the ledger, since a finished batch's directory persists there.
numbered_dir_names() {
  local repo="$1" d p name
  for d in "$(impl_dir "$repo")" "$(impl_done_dir "$repo")"; do
    [ -d "$d" ] || continue
    for p in "$d"/*/; do
      [ -d "$p" ] || continue
      name="$(basename "$p")"
      [ "$name" = "done" ] && continue
      numbered_width "$name" >/dev/null 2>&1 && printf '%s\n' "$name"
    done
  done
}

# Numbered ledger entries as "<number>:<batch>" pairs, one per line. Relies on render_block's
# fixed field order in cfq-changelog.sh (batchNumber is always immediately followed by batch).
ledger_numbered_pairs() {
  local target="$1"
  [ -n "$target" ] && [ -f "$target" ] || return 0
  awk '
    /^- batchNumber: / { bn=$0; sub(/^- batchNumber: /,"",bn); next }
    /^  batch: / {
      b=$0; sub(/^  batch: /,"",b)
      if (bn != "" && bn ~ /^[0-9]+$/) print bn":"b
      bn=""
    }
  ' "$target"
}

# Read-only comparison of queue directories against the ledger's numbered entries, plus (fix=1)
# reservation of every orphaned directory, ascending by number, driven through cfq-changelog.sh
# reserve rather than writing YAML here. Never deletes anything, never touches an orphaned ledger
# entry (a reserved-but-abandoned number is a legitimate state, not a gap to close). Prints one
# {status, orphanDirs, orphanEntries, dirMax, ledgerMax} object; returns 1 when orphanDirs is
# non-empty in the final (post-fix, if requested) state, 0 otherwise -- usable as a check.
reconcile() {
  local repo="$1" fix="$2" target
  target="$(changelog_path "$repo" 2>/dev/null || true)"
  if [ -z "$target" ]; then
    err BATCH_CHANGELOG_REQUIRED "changelogFile is disabled; reconcile needs the CFQ workflow changelog" "set changelogFile before reconciling"
    return 1
  fi

  local -a dir_pairs=()
  local name num
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    num="$(numbered_number "$name")"
    dir_pairs+=("$num:$name")
  done < <(numbered_dir_names "$repo" | sort -u)

  local -a ledger_pairs=()
  local line
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    ledger_pairs+=("$line")
  done < <(ledger_numbered_pairs "$target" | sort -u)

  local -a orphan_dirs=()
  local dp dname found lp lname
  for dp in "${dir_pairs[@]}"; do
    dname="${dp#*:}"
    found=0
    for lp in "${ledger_pairs[@]}"; do
      lname="${lp#*:}"
      [ "$lname" = "$dname" ] && { found=1; break; }
    done
    [ "$found" -eq 1 ] || orphan_dirs+=("$dp")
  done

  local -a orphan_entries=()
  for lp in "${ledger_pairs[@]}"; do
    lname="${lp#*:}"
    found=0
    for dp in "${dir_pairs[@]}"; do
      dname="${dp#*:}"
      [ "$dname" = "$lname" ] && { found=1; break; }
    done
    [ "$found" -eq 1 ] || orphan_entries+=("$lname")
  done

  if [ "$fix" = "1" ] && [ "${#orphan_dirs[@]}" -gt 0 ]; then
    local -a ordered=()
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      ordered+=("$line")
    done < <(printf '%s\n' "${orphan_dirs[@]}" | sort -t: -k1,1n)

    local reserve_out
    for dp in "${ordered[@]}"; do
      num="${dp%%:*}"; name="${dp#*:}"
      # Direct sibling call: inside a per-orphan loop, see CLAUDE.md's dispatcher-loop-exception note.
      if ! reserve_out="$(python3 "$here/cfq_changelog.py" reserve "$repo" "$num" "$name" 2>&1)"; then
        err BATCH_ID_CONFLICT "reconcile --fix: reserving $name failed: $reserve_out" "resolve manually, then retry reconcile"
        return 1
      fi
    done
    orphan_dirs=()
  fi

  local dir_max=0 ledger_max
  for dp in "${dir_pairs[@]}"; do
    num="${dp%%:*}"
    [ "$num" -gt "$dir_max" ] && dir_max="$num"
  done
  ledger_max="$("$cfq" changelog max-batch-number "$repo")"

  local -a orphan_dir_names=()
  for dp in "${orphan_dirs[@]}"; do orphan_dir_names+=("${dp#*:}"); done

  local od_json oe_json
  od_json="$(printf '%s\n' "${orphan_dir_names[@]}" | jq -R -s 'split("\n") | map(select(length>0))')"
  oe_json="$(printf '%s\n' "${orphan_entries[@]}" | jq -R -s 'split("\n") | map(select(length>0))')"

  jq -n --argjson od "$od_json" --argjson oe "$oe_json" --argjson dm "$dir_max" --argjson lm "$ledger_max" \
    '{status:"OK", orphanDirs:$od, orphanEntries:$oe, dirMax:$dm, ledgerMax:$lm}'

  [ "${#orphan_dirs[@]}" -eq 0 ]
}

validate_args() {
  local date="$1" slug="$2"
  if [[ ! "$date" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    err INVALID_ARGUMENT "date must be YYYY-MM-DD, got '$date'" ""
    return 1
  fi
  if [[ ! "$slug" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
    err INVALID_ARGUMENT "slug must match [a-z0-9][a-z0-9-]*, got '$slug'" ""
    return 1
  fi
}

# mkdir-based mutex, same staleness spirit as cfq-lock.sh: a lock older than lock_stale_s is
# presumed abandoned (crashed holder) and reclaimed rather than waited on forever. Allocation is
# fast, so a short stale window is enough — this is not a long-lived session lock.
lock_stale_s=10
alloc_lockdir() { printf '%s/.batch-id.lock' "$(cfq_repo_dir "$1")"; }

acquire_alloc_lock() {
  local repo="$1" lockdir age tries=0
  lockdir="$(alloc_lockdir "$repo")"
  mkdir -p "$(dirname "$lockdir")"
  while ! mkdir "$lockdir" 2>/dev/null; do
    age=$(( $(date +%s) - $(stat -c %Y "$lockdir" 2>/dev/null || echo 0) ))
    if [ "$age" -gt "$lock_stale_s" ]; then
      rmdir "$lockdir" 2>/dev/null || true
      continue
    fi
    tries=$((tries + 1))
    [ "$tries" -le 200 ] || return 1
    sleep 0.05
  done
}

release_alloc_lock() { rmdir "$(alloc_lockdir "$1")" 2>/dev/null || true; }

cmd="${1:-}"
case "$cmd" in
  next)
    repo="${2:?usage: cfq-batch-id.sh next <repo-root> <YYYY-MM-DD> <slug>}"
    date="${3:?usage: cfq-batch-id.sh next <repo-root> <YYYY-MM-DD> <slug>}"
    slug="${4:?usage: cfq-batch-id.sh next <repo-root> <YYYY-MM-DD> <slug>}"
    validate_args "$date" "$slug"
    compute_next "$repo" "$date" "$slug"
    ;;

  allocate)
    repo="${2:?usage: cfq-batch-id.sh allocate <repo-root> <YYYY-MM-DD> <slug>}"
    date="${3:?usage: cfq-batch-id.sh allocate <repo-root> <YYYY-MM-DD> <slug>}"
    slug="${4:?usage: cfq-batch-id.sh allocate <repo-root> <YYYY-MM-DD> <slug>}"
    validate_args "$date" "$slug"

    "$cfq" layout ensure "$repo" >/dev/null

    if ! acquire_alloc_lock "$repo"; then
      err INTERNAL_ERROR "could not acquire the allocation lock" "retry once the concurrent allocation finishes"
      exit 1
    fi
    trap 'release_alloc_lock "'"$repo"'"' EXIT

    if ! result="$(compute_next "$repo" "$date" "$slug")"; then
      status="$(jq -r .status <<<"$result")"
      if [ "$status" != "BATCH_WIDTH_MIGRATION_REQUIRED" ]; then
        printf '%s\n' "$result"
        exit 1
      fi

      if ! queue_is_empty "$repo"; then
        jq -n --argjson cw "$(jq -r .currentWidth <<<"$result")" \
              --argjson nn "$(jq -r .nextNumber <<<"$result")" \
              --argjson rw "$(jq -r .requiredWidth <<<"$result")" '
          {status:"BATCH_WIDTH_MIGRATION_BLOCKED",
           currentWidth:$cw, nextNumber:$nn, requiredWidth:$rw,
           action:"finish or clear all active CFQ batches before parking the next batch"}'
        exit 1
      fi

      if ! migrate_out="$(migrate_width "$repo")"; then
        printf '%s\n' "$migrate_out"
        exit 1
      fi

      if ! result="$(compute_next "$repo" "$date" "$slug")"; then
        printf '%s\n' "$result"
        exit 1
      fi
    fi

    number="$(jq -r .batchNumber <<<"$result")"
    batch="$(jq -r .batch <<<"$result")"

    # Invariant: a queue directory never exists without its ledger entry. The reservation is
    # written first and the directory second; a number is never un-reserved once burned (see
    # BATCH_ID_CONFLICT below) -- so on a directory-creation failure below, the ledger entry is
    # the state left standing on purpose. That's the recoverable half: `cfq batch reconcile`
    # reports a ledger entry with no directory and a later allocate simply moves past it, whereas
    # the reverse (a directory with no entry) is exactly what breaks numbering.
    if ! reserve_out="$("$cfq" changelog reserve "$repo" "$number" "$batch" 2>&1)"; then
      err BATCH_ID_CONFLICT "changelog reservation for $batch failed: $reserve_out" "retry; the failed number stays consumed and is never reused"
      exit 1
    fi

    target_dir="$(impl_dir "$repo")/$batch"
    if ! mkdir "$target_dir" 2>/dev/null; then
      cf_target="$(changelog_path "$repo" 2>/dev/null || true)"
      err INTERNAL_ERROR "changelog reserved $batch but the queue directory could not be created; the number stays consumed" "inspect $target_dir and $cf_target manually, then park under a new slug/date"
      exit 1
    fi

    printf '%s\n' "$result"
    ;;

  migrate-width)
    repo="${2:?usage: cfq-batch-id.sh migrate-width <repo-root>}"
    if ! acquire_alloc_lock "$repo"; then
      err INTERNAL_ERROR "could not acquire the allocation lock" "retry once the concurrent allocation finishes"
      exit 1
    fi
    trap 'release_alloc_lock "'"$repo"'"' EXIT
    migrate_width "$repo"
    ;;

  reconcile)
    repo="${2:?usage: cfq-batch-id.sh reconcile <repo-root> [--fix]}"
    fix=0
    case "${3:-}" in
      --fix) fix=1 ;;
      "") : ;;
      *) echo "usage: cfq-batch-id.sh reconcile <repo-root> [--fix]" >&2; exit 1 ;;
    esac
    reconcile "$repo" "$fix"
    ;;

  *)
    echo "usage: cfq-batch-id.sh next <repo-root> <YYYY-MM-DD> <slug> | allocate <repo-root> <YYYY-MM-DD> <slug> | migrate-width <repo-root> | reconcile <repo-root> [--fix]" >&2
    exit 1
    ;;
esac
