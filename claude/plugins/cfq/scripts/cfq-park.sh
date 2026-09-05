#!/usr/bin/env bash
# Parks a batch directory: creates it, writes .priority/.dependsOn, ensures the local git-exclude
# entry, and registers the repo. Idempotent — safe to re-run with the same arguments. Does not
# write phase files; only the planning session that knows their content does that.
# Usage: cfq-park.sh <repo-root> <batch-dir-name> <high|normal> [<dependsOn-entry>...]
set -eu

command -v jq >/dev/null 2>&1 || { echo "cfq-park.sh: jq is required" >&2; exit 1; }

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cfq="$script_dir/../bin/cfq"
# shellcheck source=cfq-paths.sh
. "$script_dir/cfq-paths.sh"

repo_root="${1:?usage: cfq-park.sh <repo-root> <batch-dir-name> <high|normal> [<dependsOn-entry>...]}"
batch_name="${2:?usage: cfq-park.sh <repo-root> <batch-dir-name> <high|normal> [<dependsOn-entry>...]}"
priority="${3:?usage: cfq-park.sh <repo-root> <batch-dir-name> <high|normal> [<dependsOn-entry>...]}"
shift 3
depends=("$@")

case "$priority" in
  high|normal) : ;;
  *) echo "cfq-park.sh: priority must be high|normal, got '$priority'" >&2; exit 1 ;;
esac

"$cfq" layout ensure "$repo_root" >/dev/null

dir="$(impl_dir "$repo_root")/$batch_name"
mkdir -p "$dir"
# .planning is written on creation, idempotent (a re-run during the same pfq session refreshes
# the timestamp as a heartbeat), and removed by plan-for-queue's lint step once the batch is
# complete — this is what keeps ifq from picking up a batch pfq is still writing.
date -Iseconds > "$dir/.planning"

case "$priority" in
  high)   printf 'high\n' > "$dir/.priority" ;;
  normal) rm -f "$dir/.priority" ;;
esac

if [ "${#depends[@]}" -gt 0 ]; then
  printf '%s\n' "${depends[@]}" > "$dir/.dependsOn"
fi

"$cfq" registry add "$repo_root" >/dev/null

printf '%s\n' "$dir"
