#!/usr/bin/env python3
# Usage: cfq_branch.py plan <repo-root> <batch-dir-name>
#        cfq_branch.py check <repo-root> <branch-name>
"""Computes the branch/batch-identity decision for a batch go-ahead as one JSON object. Read-only
-- never creates or checks out a branch itself; the caller acts on `mode`. New branches use the
batch's own stable directory name directly (`cfq/<batch-directory-name>`) -- no version scanning,
no pseudo-version increment, no identity derived from Git branch history.
`check` resolves and judges one named branch (for a free-text base-branch answer) -- also
read-only, no dispatcher entry of its own since `branch` is already the noun.

Ported from cfq-branch.sh -- a port, not a redesign: the CLI contract (verbs, argument order, JSON
shapes, exit codes) is the invariant this file preserves. The remote-is-source-of-truth rule
(candidates ranked from `origin`, never local refs) is load-bearing -- see commits
85f424a/d2fec8a/1753599 from batch 015: local-ahead-of-remote is a stop-and-ask, never
auto-resolved.
"""

import argparse
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cfq_lib import errors, render  # noqa: E402

PROG = "cfq_branch.py"

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CFQ_BIN = SCRIPT_DIR.parent / "bin" / "cfq"

# New-format batch directory names are <digits>-<YYYY-MM-DD>-<slug> (the number precedes the
# date); legacy names start directly with the date and never match.
NUMBER_RE = re.compile(r"^([0-9]+)-[0-9]{4}-[0-9]{2}-[0-9]{2}-")


def cfq_run(*args):
    return subprocess.run([str(CFQ_BIN), *args], capture_output=True, text=True)


def git(repo, *args, check=True):
    """`git -C <repo> <args>`, always capture_output/text. check=True (the default) dies the whole
    process on a nonzero exit, mirroring what the ported shell's `set -eu` did for the same bare
    call -- several call sites deliberately swallow failures (`|| true`, `|| echo 0`, an `if`/`&&`
    test); those pass check=False and handle the result themselves, exactly where the shell
    version did and nowhere else."""
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        sys.exit(proc.returncode)
    return proc


def ref_exists(repo, ref):
    return git(repo, "rev-parse", "--verify", "-q", ref, check=False).returncode == 0


def is_remote_checked(repo):
    """Best-effort remote check: no origin, or fetch fails (offline/sandboxed) -> False, and every
    path below behaves exactly as before this was added."""
    if git(repo, "remote", "get-url", "origin", check=False).returncode != 0:
        return False
    return git(repo, "fetch", "-q", "origin", check=False).returncode == 0


def list_remote_branch_names(repo):
    """Short names of every `origin/*` branch, `origin/HEAD` dropped, `origin/` prefix stripped."""
    proc = git(repo, "branch", "-r", "--format=%(refname:short)", check=False)
    names = []
    for line in proc.stdout.splitlines():
        if line.startswith("origin/"):
            name = line[len("origin/"):]
            if name != "HEAD":
                names.append(name)
    return names


def classify_remote_state(ahead, behind):
    """Classifies a bidirectional ahead/behind count pair into the shared remoteState vocabulary --
    used by both `continue` mode and `new` mode so a caller never has to branch on `mode` to read
    it."""
    if ahead > 0 and behind > 0:
        return "diverged"
    if ahead > 0:
        return "ahead"
    if behind > 0:
        return "behind"
    return "synced"


def left_right_counts(repo, left_ref, right_ref):
    """(behind, ahead) via `rev-list --left-right --count <left>...<right>`."""
    out = git(repo, "rev-list", "--left-right", "--count", f"{left_ref}...{right_ref}").stdout
    behind_s, ahead_s = out.split()
    return int(behind_s), int(ahead_s)


def list_unpushed_commits(repo, from_ref, to_ref):
    """"<hash> <subject>" lines for commits reachable from to_ref but not from_ref."""
    proc = git(repo, "log", "--format=%h %s", f"{from_ref}..{to_ref}", check=False)
    return [line for line in proc.stdout.splitlines() if line]


def parse_batch_number(name):
    m = NUMBER_RE.match(name)
    return int(m.group(1)) if m else None


def find_suffix_match(repo, slug):
    """Highest-priority (alphabetically first) local-or-remote branch whose name ends in
    `-<slug>` -- the fallback used when the changelog doesn't (yet, or any more) know a branch for
    this batch."""
    proc = git(repo, "branch", "-a", "--format=%(refname:short)", check=False)
    names = set()
    for line in proc.stdout.splitlines():
        if not line:
            continue
        names.add(line[len("origin/"):] if line.startswith("origin/") else line)
    pattern = re.compile("-" + re.escape(slug) + "$")
    matches = sorted(n for n in names if pattern.search(n))
    return matches[0] if matches else ""


# ---- check --------------------------------------------------------------------------------------

def cmd_check(args):
    repo = args.repo
    branch_name = args.branch_name
    rchecked = is_remote_checked(repo)

    origin_ref = f"refs/remotes/origin/{branch_name}"
    local_ref = f"refs/heads/{branch_name}"
    origin_exists = ref_exists(repo, origin_ref)
    local_exists = ref_exists(repo, local_ref)

    if not origin_exists and not local_exists:
        print(render.dump_json({
            "status": "UNRESOLVED", "name": branch_name, "ref": None, "remoteChecked": rchecked,
        }))
        return

    if origin_exists:
        ref, local_only = origin_ref, False
    else:
        ref, local_only = local_ref, True

    ahead, behind = 0, 0
    if origin_exists and local_exists:
        behind, ahead = left_right_counts(repo, origin_ref, local_ref)

    last_commit = git(repo, "log", "-1", "--format=%cI", ref).stdout.strip()
    ref_epoch = int(git(repo, "log", "-1", "--format=%ct", ref).stdout.strip())

    # Same "newest commit wins" rule the `new`-mode candidate list applies.
    newer_name, newer_epoch = "", 0
    for rb in list_remote_branch_names(repo):
        proc = git(repo, "log", "-1", "--format=%ct", f"refs/remotes/origin/{rb}", check=False)
        if proc.returncode != 0:
            continue
        epoch = int(proc.stdout.strip())
        if epoch > ref_epoch and epoch > newer_epoch:
            newer_epoch, newer_name = epoch, rb

    newer_candidate = None
    if newer_name:
        newer_iso = git(repo, "log", "-1", "--format=%cI", f"refs/remotes/origin/{newer_name}").stdout.strip()
        newer_candidate = {"name": newer_name, "lastCommit": newer_iso}

    print(render.dump_json({
        "status": "OK", "name": branch_name, "ref": ref, "localOnly": local_only,
        "ahead": ahead, "behind": behind, "lastCommit": last_commit,
        "newerCandidate": newer_candidate, "remoteChecked": rchecked,
    }))


# ---- plan ---------------------------------------------------------------------------------------

def cmd_plan(args):
    repo = args.repo
    batch_name = args.batch
    rchecked = is_remote_checked(repo)

    number = parse_batch_number(batch_name)
    slug = re.sub(r"^[0-9]+-[0-9]{4}-[0-9]{2}-[0-9]{2}-", "", batch_name)
    slug = re.sub(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}-", "", slug)

    branch_per_batch = cfq_run("settings", "get", "branchPerBatch").stdout.strip()
    if branch_per_batch == "false":
        print(render.dump_json({
            "mode": "off", "batch": batch_name, "batchNumber": number, "branch": None,
            "base": None, "candidates": [], "remoteChecked": False, "remoteWarning": None,
        }))
        return

    # Prefer the branch already persisted in the CFQ changelog for this exact batch --
    # authoritative, since it is the branch that was actually checked out at init time -- but only
    # once it's confirmed to still exist; a deleted branch falls through to the suffix match below
    # rather than being handed to the caller as an unresolvable "continue".
    existing = cfq_run("changelog", "branch-for", repo, batch_name).stdout.strip()
    if existing and not ref_exists(repo, f"refs/heads/{existing}") \
            and not ref_exists(repo, f"refs/remotes/origin/{existing}"):
        existing = ""
    if not existing:
        existing = find_suffix_match(repo, slug)

    if existing:
        _emit_continue(repo, batch_name, number, existing, rchecked)
        return

    _emit_new(repo, batch_name, number, rchecked)


def _emit_continue(repo, batch_name, number, existing, rchecked):
    continue_warning = None
    remote_state = "unknown"
    pushable = False
    unpushed = []

    if rchecked and ref_exists(repo, f"refs/heads/{existing}") \
            and ref_exists(repo, f"refs/remotes/origin/{existing}"):
        behind, ahead = left_right_counts(
            repo, f"refs/remotes/origin/{existing}", f"refs/heads/{existing}"
        )
        remote_state = classify_remote_state(ahead, behind)

        if remote_state == "behind":
            # `update-ref` can't move the ref of the branch that is currently checked out --
            # fall back to a fast-forward merge there, and never mutate anything against a dirty
            # tree.
            checked_out = git(repo, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
            if checked_out != existing:
                git(repo, "update-ref", f"refs/heads/{existing}", f"refs/remotes/origin/{existing}")
            elif git(repo, "status", "--porcelain").stdout == "":
                git(repo, "merge", "-q", "--ff-only", f"refs/remotes/origin/{existing}")
            else:
                continue_warning = (
                    f"local {existing} is behind origin/{existing} but the working tree is "
                    "dirty — resolve before continuing"
                )
        elif remote_state == "ahead":
            pushable = True
            unpushed = list_unpushed_commits(
                repo, f"refs/remotes/origin/{existing}", f"refs/heads/{existing}"
            )
        elif remote_state == "diverged":
            continue_warning = (
                f"local {existing} is {ahead} commit(s) ahead of and {behind} commit(s) behind "
                f"origin/{existing} — a push would be rejected"
            )

    print(render.dump_json({
        "mode": "continue", "batch": batch_name, "batchNumber": number, "branch": existing,
        "base": None, "candidates": [],
        "remoteChecked": rchecked, "remoteWarning": continue_warning,
        "remoteState": remote_state, "pushable": pushable, "unpushed": unpushed,
    }))


def _collect_candidates(repo, rchecked):
    """(candidate_names, cand_ref, cand_local_only, main_ref) -- `origin/*` is the source of truth
    once remote-checked; local-only branches (no remote counterpart) are still offered, explicitly
    marked. Offline falls back to local branches only, every one implicitly local-only."""
    candidate_names = []
    cand_ref = {}
    cand_local_only = {}

    if rchecked:
        for rb in list_remote_branch_names(repo):
            candidate_names.append(rb)
            cand_ref[rb] = f"refs/remotes/origin/{rb}"
            cand_local_only[rb] = False

        proc = git(repo, "branch", "--format=%(refname:short)", check=False)
        for lb in proc.stdout.splitlines():
            if not lb or lb == "main":
                continue
            if lb not in cand_ref:
                candidate_names.append(lb)
                cand_ref[lb] = f"refs/heads/{lb}"
                cand_local_only[lb] = True

        main_ref = "refs/remotes/origin/main"
    else:
        proc = git(repo, "branch", "--format=%(refname:short)", check=False)
        for lb in proc.stdout.splitlines():
            if not lb or lb == "main":
                continue
            candidate_names.append(lb)
            cand_ref[lb] = f"refs/heads/{lb}"
            cand_local_only[lb] = True

        main_ref = "refs/heads/main"

    return candidate_names, cand_ref, cand_local_only, main_ref


def _highest_cfq_branch(candidate_names):
    """Highest-numbered cfq/<NNN>-... branch among all candidates, found before the aheadOfMain
    filter below so a fully-merged batch branch is never silently dropped -- it's always offered
    as an alternative, per the batch context's "last batch is always offered" rule."""
    highest_name, highest_num = "", -1
    for name in candidate_names:
        if not name.startswith("cfq/"):
            continue
        num = parse_batch_number(name[len("cfq/"):])
        if num is None:
            continue
        if num > highest_num:
            highest_num, highest_name = num, name
    return highest_name


def _emit_new(repo, batch_name, number, rchecked):
    branch = f"cfq/{batch_name}"

    candidate_names, cand_ref, cand_local_only, main_ref = _collect_candidates(repo, rchecked)
    highest_name = _highest_cfq_branch(candidate_names)

    cand_objs = []
    for name in candidate_names:
        ref = cand_ref[name]
        local_only = cand_local_only[name]

        ahead_of_main = 0
        if ref_exists(repo, ref) and ref_exists(repo, main_ref):
            proc = git(repo, "rev-list", "--count", f"{main_ref}..{ref}", check=False)
            if proc.returncode == 0 and proc.stdout.strip():
                ahead_of_main = int(proc.stdout.strip())

        is_highest = name == highest_name
        if not (ahead_of_main > 0 or is_highest):
            continue

        merged = False
        if ahead_of_main == 0 and ref_exists(repo, main_ref) \
                and git(repo, "merge-base", "--is-ancestor", ref, main_ref, check=False).returncode == 0:
            merged = True

        behind_remote, ahead_remote = 0, 0
        if not local_only and ref_exists(repo, f"refs/heads/{name}"):
            behind_remote, ahead_remote = left_right_counts(repo, ref, f"refs/heads/{name}")

        last_commit = git(repo, "log", "-1", "--format=%cI", ref).stdout.strip()
        last_epoch = int(git(repo, "log", "-1", "--format=%ct", ref).stdout.strip())

        cand_objs.append({
            "name": name, "ref": ref, "aheadOfMain": ahead_of_main,
            "behindRemote": behind_remote, "aheadRemote": ahead_remote,
            "localOnly": local_only, "highestBatch": is_highest,
            "mergedIntoOriginMain": merged, "lastCommit": last_commit, "_lastEpoch": last_epoch,
        })

    # jq's `sort_by(.lastEpoch) | reverse`: stable ascending sort, then the whole list reversed --
    # not a descending sort, ties among equal lastEpoch also flip order. Replicated as two steps
    # rather than one `sort(reverse=True)`, which would leave ties in their original order.
    cand_objs.sort(key=lambda c: c["_lastEpoch"])
    cand_objs.reverse()
    for c in cand_objs:
        del c["_lastEpoch"]

    if not cand_objs:
        base_name, base_ref, base_local_only = "main", main_ref, False
    else:
        base_name = cand_objs[0]["name"]
        base_ref = cand_objs[0]["ref"]
        base_local_only = cand_objs[0]["localOnly"]

    base_local_ref = f"refs/heads/{base_name}"
    base_origin_ref = f"refs/remotes/origin/{base_name}"

    # remoteState/pushable/unpushed for the *chosen* base -- same vocabulary and helpers as
    # `continue` mode. Local main ahead of/diverged from origin/main (or a candidate ahead
    # of/diverged from its own origin counterpart) no longer influences candidate selection --
    # `baseRef` already points into `refs/remotes/` -- but is still a real state worth a push
    # offer.
    remote_state = "unknown"
    pushable = False
    unpushed = []
    new_warning = None
    if rchecked:
        if base_local_only:
            if ref_exists(repo, base_local_ref):
                remote_state = "ahead"
                pushable = True
                proc = git(repo, "log", "--format=%h %s", base_local_ref, check=False)
                unpushed = [line for line in proc.stdout.splitlines() if line]
        elif ref_exists(repo, base_local_ref) and ref_exists(repo, base_origin_ref):
            behind, ahead = left_right_counts(repo, base_origin_ref, base_local_ref)
            remote_state = classify_remote_state(ahead, behind)
            if remote_state == "ahead":
                pushable = True
                unpushed = list_unpushed_commits(repo, base_origin_ref, base_local_ref)
                new_warning = (
                    f"local {base_name} is {ahead} commit(s) ahead of origin/{base_name} — a "
                    "push before continuing would include them"
                )
            elif remote_state == "diverged":
                new_warning = (
                    f"local {base_name} has diverged from origin/{base_name} — resolve before "
                    "basing new work on it"
                )

    print(render.dump_json({
        "mode": "new", "batch": batch_name, "batchNumber": number, "branch": branch,
        "base": base_name, "baseRef": base_ref, "candidates": cand_objs,
        "remoteChecked": rchecked, "remoteWarning": new_warning,
        "remoteState": remote_state, "pushable": pushable, "unpushed": unpushed,
    }))


# ---- argument parsing -----------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(prog=PROG, add_help=True)
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("plan")
    p.add_argument("repo")
    p.add_argument("batch")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("check")
    p.add_argument("repo")
    p.add_argument("branch_name")
    p.set_defaults(func=cmd_check)

    return parser


def main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        errors.die(f"usage: {PROG} plan <repo-root> <batch-dir-name> | check <repo-root> <branch-name>")
    func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
