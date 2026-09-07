#!/usr/bin/env bash
# Prints the batch briefing block shown before a batch is offered for implementation, or (with
# --phase <NN>) a single-phase announcement block, or (with --with-done) the same batch briefing
# with done phases listed first, ticked. Read-only.
# Usage: cfq-brief.sh <batch-dir> [--phase <NN>|--with-done]
set -eu

dir="${1:?usage: cfq-brief.sh <batch-dir> [--phase <NN>|--with-done]}"
dir="${dir%/}"
[ -d "$dir" ] || { echo "cfq-brief.sh: no such batch directory: $dir" >&2; exit 1; }

brief_awk='
  /^# / && !t            { sub(/^# +/, ""); t = $0; next }
  /^## Size/             { g = 1; next }
  g && NF                { size = $1; g = 0; next }
  /^## Context/          { k = 1; next }
  k && NF                { ctx = ctx $0 " "; if (++n >= 2) k = 0; next }
  /^## Affected Files/   { af = 1; next }
  /^## Verification/     { af = 0; vf = 1; next }
  /^## /                 { af = 0; vf = 0; next }
  af && /^- `/           { p = $0; sub(/^- `/, "", p); sub(/`.*$/, "", p)
                           ns = split(p, seg, "/"); files = files (files == "" ? "" : ", ") seg[ns]; next }
  vf && /^```/           { incode = !incode; next }
  vf && incode && NF && !check { check = $0; next }
  END {
    if (mode == "phase") {
      goal = substr(ctx, 1, 220); sub(/ +$/, "", goal)
      printf "PHASE %s · %s · Size %s\n", num, t, (size ? size : "M")
      printf "  Goal     %s\n", goal
      if (files != "") printf "  Files    %s\n", files
      if (check != "") printf "  Check    %s\n", check
    } else {
      printf "%s  %s  [%s]  %s\n", num, t, (size ? size : "M"), substr(ctx, 1, 220)
    }
  }
'

case "${2:-}" in
  "") with_done=0 ;;
  --with-done) with_done=1 ;;
  --phase)
    phase="${3:?usage: cfq-brief.sh <batch-dir> --phase <NN>}"
    shopt -s nullglob
    matches=("$dir/$phase"-*.md "$dir/done/$phase"-*.md)
    shopt -u nullglob
    f="${matches[0]:-}"
    [ -n "$f" ] || { echo "cfq-brief.sh: no phase $phase in $dir" >&2; exit 1; }
    num=$(basename "$f" | sed -n 's/^\([0-9][0-9]\)-.*/\1/p')
    awk -v num="$num" -v mode="phase" "$brief_awk" "$f"
    exit 0
    ;;
  *)
    echo "cfq-brief.sh: unknown flag: ${2}" >&2
    exit 1
    ;;
esac

name="$(basename "$dir")"
priority=$(cat "$dir/.priority" 2>/dev/null || true)

shopt -s nullglob
files=("$dir"/[0-9][0-9]-*.md)
shopt -u nullglob

if [ "$priority" = high ]; then
  printf '%s  priority=high  phases=%s\n' "$name" "${#files[@]}"
else
  printf '%s  phases=%s\n' "$name" "${#files[@]}"
fi

if [ -f "$dir/.dependsOn" ]; then
  while IFS= read -r d; do
    [ -n "$d" ] && printf 'dependsOn: %s\n' "$d"
  done < "$dir/.dependsOn"
fi

if [ "$with_done" -eq 1 ]; then
  shopt -s nullglob
  done_files=("$dir/done"/[0-9][0-9]-*.md)
  shopt -u nullglob
  for f in "${done_files[@]}"; do
    num=$(basename "$f" | sed -n 's/^\([0-9][0-9]\)-.*/\1/p')
    line=$(awk -v num="$num" -v mode="brief" "$brief_awk" "$f")
    printf '✔ %s\n' "$line"
  done
fi

for f in "${files[@]}"; do
  num=$(basename "$f" | sed -n 's/^\([0-9][0-9]\)-.*/\1/p')
  awk -v num="$num" -v mode="brief" "$brief_awk" "$f"
done
