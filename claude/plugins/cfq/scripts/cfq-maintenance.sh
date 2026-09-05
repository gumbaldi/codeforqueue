#!/usr/bin/env bash
# Due-check and marker for the periodic maintenance run (`maintenanceEvery` commits apart).
# Usage: cfq-maintenance.sh due <repo-root> | stamp <repo-root>
set -eu

command -v jq >/dev/null 2>&1 || { echo "cfq-maintenance.sh: jq is required" >&2; exit 1; }

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cfq="$script_dir/../bin/cfq"
# shellcheck source=cfq-paths.sh
. "$script_dir/cfq-paths.sh"

marker() { maintenance_marker "$1"; }

cmd="${1:-}"
case "$cmd" in
  due)
    repo="${2:?usage: cfq-maintenance.sh due <repo-root>}"

    every=$("$cfq" settings get maintenanceEvery)
    if [ "$every" = "0" ]; then
      echo OFF
      exit 0
    fi

    if ! git -C "$repo" rev-parse --git-dir >/dev/null 2>&1; then
      echo "DUE 0"
      exit 0
    fi

    f="$(marker "$repo")"
    if [ ! -f "$f" ]; then
      echo "DUE 0"
      exit 0
    fi

    sha=$(awk '{print $2}' "$f")
    if [ -z "$sha" ] || ! git -C "$repo" cat-file -e "$sha^{commit}" 2>/dev/null; then
      echo "DUE 0"
      exit 0
    fi

    n=$(git -C "$repo" rev-list --count "$sha..HEAD")
    if [ "$n" -ge "$every" ]; then
      echo "DUE $n"
    else
      echo "NOT_DUE $n"
    fi
    ;;

  stamp)
    repo="${2:?usage: cfq-maintenance.sh stamp <repo-root>}"
    f="$(marker "$repo")"
    mkdir -p "$(dirname "$f")"
    sha=$(git -C "$repo" rev-parse HEAD 2>/dev/null || echo "")
    printf '%s %s\n' "$(date +%F)" "$sha" >"$f.tmp"
    mv "$f.tmp" "$f"
    echo "$f"
    ;;

  *)
    echo "usage: cfq-maintenance.sh due <repo-root> | stamp <repo-root>" >&2
    exit 1
    ;;
esac
