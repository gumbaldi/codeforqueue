#!/usr/bin/env python3
# Usage: cfq_lang.py <repo-root> [--changed <ref>]
#        cfq_lang.py prose <repo-root> <ref>
"""Language/doc-tree drift reporter for a target repo. Never writes, never fails: always prints
one JSON object on stdout and exits 0 -- the caller (a skill) decides what the numbers mean.

Ported from cfq-lang.sh -- a port, not a redesign: the CLI contract (verbs, argument order, JSON
shape, exit codes) is the invariant this file preserves.
"""

import argparse
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cfq_lib import render  # noqa: E402

PROG = "cfq_lang.py"

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CFQ_BIN = SCRIPT_DIR.parent / "bin" / "cfq"

# Caps kept as named constants so the sample size is tunable in one place.
PROSE_MAX_LINES = 200
PROSE_MAX_BYTES = 8192


def cfq_run(*args):
    return subprocess.run([str(CFQ_BIN), *args], capture_output=True, text=True)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def capture(proc):
    """Mirrors `$(cmd 2>/dev/null || true)`: whatever the command printed to stdout, trailing
    newlines stripped, regardless of exit code."""
    return proc.stdout.rstrip("\n")


def csv_to_list(csv):
    return [p for p in csv.split(",") if p] if csv else []


def get_settings():
    code_language = capture(cfq_run("settings", "get", "codeLanguage"))
    doc_languages = csv_to_list(capture(cfq_run("settings", "get", "docLanguages")))
    doc_level = capture(cfq_run("settings", "get", "docLevel"))
    i18n_patterns = csv_to_list(capture(cfq_run("settings", "get", "i18nExcludePatterns")))
    return code_language, doc_languages, doc_level, i18n_patterns


def is_git_repo(repo):
    return git(repo, "rev-parse", "--git-dir").returncode == 0


def verify_ref(repo, ref):
    return git(repo, "rev-parse", "--verify", ref).returncode == 0


def normalize_i18n_pathspec(pattern):
    """A leading "*/" in a stored pattern only matches nested paths under git's glob pathspec
    magic (single "*" doesn't cross "/"), so it's normalized to "**/" here to also match a
    top-level directory of the same name."""
    if pattern.startswith("*/"):
        p = pattern[2:]
    elif pattern.startswith("/"):
        p = pattern[1:]
    else:
        p = pattern
    return f":(exclude,glob)**/{p}"


def count_lines(s):
    """Mirrors `grep -c '^'` on a string with no guaranteed trailing newline."""
    if s == "":
        return 0
    n = s.count("\n")
    return n if s.endswith("\n") else n + 1


def cmd_prose(args):
    repo = args.repo
    ref = args.ref
    code_language, _doc_languages, _doc_level, i18n_patterns = get_settings()

    sample = ""
    truncated = False
    note = ""

    i18n_pathspecs = [normalize_i18n_pathspec(p) for p in i18n_patterns]

    if is_git_repo(repo) and verify_ref(repo, ref):
        commits = capture(git(repo, "log", "--format=%B", f"{ref}..HEAD"))
        diff_out = capture(git(repo, "diff", f"{ref}...HEAD", "--", ".", *i18n_pathspecs))
        added_lines = [
            line[1:] for line in diff_out.split("\n") if line.startswith("+") and not line.startswith("+++")
        ]
        added = "\n".join(added_lines)
        combined = f"{commits}\n{added}"

        if not re.search(r"\S", combined):
            note = f"no changes between {ref} and HEAD"
        else:
            combined_lines = combined.split("\n")
            total_lines = count_lines(combined)
            by_lines = "\n".join(combined_lines[:PROSE_MAX_LINES])
            by_lines_bytes = len(by_lines.encode())
            if by_lines_bytes > PROSE_MAX_BYTES:
                sample = by_lines.encode()[:PROSE_MAX_BYTES].decode(errors="ignore")
                truncated = True
            else:
                sample = by_lines
                if total_lines > PROSE_MAX_LINES:
                    truncated = True
    else:
        note = f"no git repo or unknown ref '{ref}'"

    lines_count = count_lines(sample)

    result = {
        "mode": "prose",
        "codeLanguage": code_language,
        "ref": ref,
        "truncated": truncated,
        "lines": lines_count,
        "sample": sample,
        "note": note,
    }
    print(render.dump_json(result))


def scoped(paths, scope, changed_set):
    """Restricts a list of repo-relative paths to the changed set when scoped; passthrough
    otherwise."""
    if scope != "changed":
        return paths
    changed = set(p for p in changed_set if p)
    return [p for p in sorted(set(paths)) if p in changed]


def cmd_tree(args):
    repo = args.repo
    changed_ref = args.changed
    code_language, doc_languages, doc_level, _i18n_patterns = get_settings()

    scope = "repo"
    note = ""
    changed_set = []

    if changed_ref:
        if is_git_repo(repo) and verify_ref(repo, changed_ref):
            out = capture(git(repo, "diff", "--name-only", f"{changed_ref}...HEAD", "--", "."))
            changed_set = out.split("\n") if out else []
            scope = "changed"
        else:
            note = f"no git repo or unknown ref '{changed_ref}', falling back to repo scope"

    missing_list = []
    stray_list = []
    unfiled_list = []

    repo_path = pathlib.Path(repo)
    docs_dir = repo_path / "docs"

    if doc_level == "minimal" and not docs_dir.is_dir():
        note = "docLevel=minimal, no docs tree expected"
    elif docs_dir.is_dir():
        all_md = sorted(
            str(p.relative_to(repo_path)) for p in docs_dir.rglob("*.md") if p.is_file()
        )

        unfiled_list = scoped(
            [f for f in all_md if re.match(r"^docs/[^/]+\.md$", f)], scope, changed_set,
        )

        code_prefix = f"docs/{code_language}/"
        code_files = scoped([f for f in all_md if f.startswith(code_prefix)], scope, changed_set)
        for f in code_files:
            rel = f[len(code_prefix):]
            for lang in doc_languages:
                counterpart = f"docs/{lang}/{rel}"
                if not (repo_path / counterpart).is_file():
                    missing_list.append(counterpart)

        for lang in doc_languages:
            lang_prefix = f"docs/{lang}/"
            lang_files = scoped([f for f in all_md if f.startswith(lang_prefix)], scope, changed_set)
            for f in lang_files:
                rel = f[len(lang_prefix):]
                original = f"docs/{code_language}/{rel}"
                if not (repo_path / original).is_file():
                    stray_list.append(f)

    result = {
        "codeLanguage": code_language,
        "docLanguages": doc_languages,
        "docLevel": doc_level,
        "scope": scope,
        "missing": missing_list,
        "stray": stray_list,
        "unfiled": unfiled_list,
        "note": note,
    }
    print(render.dump_json(result))


def main(argv):
    if argv and argv[0] == "prose":
        parser = argparse.ArgumentParser(prog=f"{PROG} prose", add_help=True)
        parser.add_argument("repo")
        parser.add_argument("ref")
        args = parser.parse_args(argv[1:])
        cmd_prose(args)
        return

    parser = argparse.ArgumentParser(prog=PROG, add_help=True)
    parser.add_argument("repo")
    parser.add_argument("--changed")
    args = parser.parse_args(argv)
    cmd_tree(args)


if __name__ == "__main__":
    main(sys.argv[1:])
