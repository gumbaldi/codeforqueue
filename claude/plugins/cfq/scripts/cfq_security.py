#!/usr/bin/env python3
# Usage: cfq_security.py <repo-root>
"""Security snapshot for a target repo. Never fails: always prints one JSON object on stdout and
exits 0, even when a forge is unreachable or unauthenticated. GitHub gets real findings via `gh`
(Dependabot + code-scanning); Gitea has no such API (checked against its Swagger definition), so
only reachability/login are confirmed there and the local npm audit is the actual source. A local
audit runs in addition on every repo with a package.json, never only as a substitute.

Detection-only mode (CFQ_SECURITY_DETECT_ONLY=1) prints "<forge> <host> <owner/name>" for the
origin remote and exits -- for testing the string parsing without network or credentials.

Ported from cfq-security.sh -- a port, not a redesign: the CLI contract (argument order, JSON
shape, exit codes) is the invariant this file preserves, with one deliberate exception: bounding
external calls no longer depends on the external `timeout`/`gtimeout` binary --
`subprocess.run(..., timeout=...)` replaces it, so the old "timeout/gtimeout not found" hint case
no longer exists (see batch 017 phase 08's plan).
"""

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cfq_lib import render  # noqa: E402

PROG = "cfq_security.py"

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CFQ_BIN = SCRIPT_DIR.parent / "bin" / "cfq"

RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def cfq_run(*args):
    return subprocess.run([str(CFQ_BIN), *args], capture_output=True, text=True)


def capture(proc):
    return proc.stdout.rstrip("\n")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def run_bounded(cmd, cwd=None, timeout=None):
    """Bounds an external call the way the shell version used `timeout <n>`: any hang becomes a
    failed/empty result instead of blocking forever."""
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 1, "", "")
    except OSError:
        return subprocess.CompletedProcess(cmd, 1, "", "")


def detect_forge(repo, sec_timeout):
    """Returns (forge, host, owner_name). forge is one of github|gitea|unknown|none. The gitea
    branch never guesses: only a host that appears in the tea login list
    (CFQ_TEA_LOGIN_HOSTS overrides it for tests) is trusted -- otherwise tea would silently query
    the wrong instance."""
    url = capture(git(repo, "remote", "get-url", "origin"))
    if not url:
        return "none", "-", "-"

    if url.startswith("git@") and ":" in url:
        rest = url[len("git@"):]
        host, _, owner_name = rest.partition(":")
    elif url.startswith("ssh://"):
        rest = url[len("ssh://"):]
        if "@" in rest:
            rest = rest.split("@", 1)[1]
        m = re.search(r"[:/]", rest)
        host = rest[: m.start()] if m else rest
        idx = rest.find("/")
        owner_name = rest[idx + 1:] if idx != -1 else ""
    elif url.startswith("http://"):
        rest = url[len("http://"):]
        idx = rest.find("/")
        host = rest[:idx] if idx != -1 else rest
        owner_name = rest[idx + 1:] if idx != -1 else ""
    elif url.startswith("https://"):
        rest = url[len("https://"):]
        idx = rest.find("/")
        host = rest[:idx] if idx != -1 else rest
        owner_name = rest[idx + 1:] if idx != -1 else ""
    else:
        return "unknown", "-", "-"

    if owner_name.endswith(".git"):
        owner_name = owner_name[: -len(".git")]

    if host == "github.com":
        forge = "github"
    else:
        env_hosts = os.environ.get("CFQ_TEA_LOGIN_HOSTS", "")
        if env_hosts:
            hosts = env_hosts.split()
        elif shutil.which("tea"):
            proc = run_bounded(["tea", "login", "list", "--output", "simple"], timeout=sec_timeout)
            hosts = []
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    parts = line.split()
                    if len(parts) >= 2:
                        h = re.sub(r"^[a-zA-Z]+://", "", parts[1])
                        h = re.sub(r"[:/].*$", "", h)
                        hosts.append(h)
        else:
            hosts = []
        forge = "gitea" if host in hosts else "unknown"

    return forge, host, owner_name


def _json_or_none(text):
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def group_counts(items, pred=None):
    counts = {}
    for f in items:
        if pred and not pred(f):
            continue
        sev = f["severity"]
        counts[sev] = counts.get(sev, 0) + 1
    return {k: counts[k] for k in sorted(counts)}


def cmd_security(args):
    repo = args.repo

    sec_timeout = int(capture(cfq_run("settings", "get", "securityTimeoutSeconds")))
    sec_cap = int(capture(cfq_run("settings", "get", "securityFindingsCap")))

    if os.environ.get("CFQ_SECURITY_DETECT_ONLY") == "1":
        forge, host, slug = detect_forge(repo, sec_timeout)
        print(f"{forge} {host} {slug}")
        return

    forge, host, slug = detect_forge(repo, sec_timeout)

    findings = []
    sources = []
    hint = ""

    if forge == "github" and shutil.which("gh") and run_bounded(
        ["gh", "api", "user"], timeout=sec_timeout,
    ).returncode == 0:
        hint = (
            "Dependabot and code scanning return nothing for this repo (disabled or never "
            "configured) — local audit is the only source."
        )

        dep = capture(run_bounded(
            ["gh", "api", "--paginate", f"repos/{slug}/dependabot/alerts?state=open"],
            timeout=sec_timeout,
        ))
        dep_json = _json_or_none(dep)
        if isinstance(dep_json, list) and len(dep_json) > 0:
            sources.append("dependabot")
            for item in dep_json:
                dependency = item.get("dependency") or {}
                package = dependency.get("package") or {}
                advisory = item.get("security_advisory") or {}
                vuln = item.get("security_vulnerability") or {}
                findings.append({
                    "id": package.get("name") or "?",
                    "severity": advisory.get("severity") or "unknown",
                    "source": "dependabot",
                    "fix": vuln.get("first_patched_version") is not None,
                })

        cs = capture(run_bounded(
            ["gh", "api", "--paginate", f"repos/{slug}/code-scanning/alerts?state=open"],
            timeout=sec_timeout,
        ))
        cs_json = _json_or_none(cs)
        if isinstance(cs_json, list) and len(cs_json) > 0:
            sources.append("code-scanning")
            for item in cs_json:
                rule = item.get("rule") or {}
                findings.append({
                    "id": rule.get("id") or "?",
                    "severity": rule.get("security_severity_level") or rule.get("severity") or "unknown",
                    "source": "code-scanning",
                    "fix": False,
                })

        if sources:
            hint = ""
    elif forge == "gitea" and shutil.which("tea"):
        if run_bounded(["tea", "api", f"repos/{slug}"], timeout=sec_timeout).returncode == 0:
            sources.append("gitea:none")
            hint = "This forge offers no security-alert API — local audit is the source."
        else:
            hint = f"tea login add --url {host}"

    # Local audit: runs on every repo with a package.json, in addition to any forge source above.
    if (pathlib.Path(repo) / "package.json").is_file() and shutil.which("npm"):
        npm_out = capture(run_bounded(["npm", "audit", "--json"], cwd=repo, timeout=sec_timeout))
        if npm_out:
            npm_json = _json_or_none(npm_out)
            npm_findings = []
            vulns = (npm_json or {}).get("vulnerabilities") if isinstance(npm_json, dict) else None
            if isinstance(vulns, dict):
                for key, value in vulns.items():
                    if not isinstance(value, dict):
                        value = {}
                    severity = value.get("severity")
                    if severity is None or severity is False:
                        severity = "unknown"
                    if severity == "moderate":
                        severity = "medium"
                    raw_fix = value.get("fixAvailable")
                    fix = not (raw_fix is None or raw_fix is False)
                    npm_findings.append({
                        "id": key, "severity": severity, "source": "npm-audit", "fix": fix,
                    })

            existing = findings
            npm_findings = [
                f for f in npm_findings
                if not any(e["id"] == f["id"] and e["severity"] == f["severity"] for e in existing)
            ]
            if npm_findings:
                sources.append("npm-audit")
                findings.extend(npm_findings)

    if not sources:
        if not hint:
            if forge == "github":
                hint = "gh auth login"
            elif forge == "gitea":
                hint = f"tea login add --url {host}"
            else:
                hint = "no supported manifest found"
        result = {
            "available": False, "forge": forge, "sources": [], "counts": {}, "findings": [], "hint": hint,
        }
        print(render.dump_json(result))
        return

    counts = group_counts(findings)
    fixable = group_counts(findings, pred=lambda f: f["fix"] is True)
    top = sorted(findings, key=lambda f: RANK.get(f["severity"], 4))[:sec_cap]
    top_findings = [
        {"id": f["id"], "severity": f["severity"], "source": f["source"], "fix": f["fix"]} for f in top
    ]

    result = {
        "available": True, "forge": forge, "sources": sources, "counts": counts,
        "fixable": fixable, "findings": top_findings,
    }
    if hint:
        result["hint"] = hint
    print(render.dump_json(result))


def build_parser():
    parser = argparse.ArgumentParser(prog=PROG, add_help=True)
    parser.add_argument("repo")
    parser.set_defaults(func=cmd_security)
    return parser


def main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
