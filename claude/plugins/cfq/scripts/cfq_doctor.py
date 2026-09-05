#!/usr/bin/env python3
# Usage: cfq_doctor.py check | cfq_doctor.py check --json | cfq_doctor.py hook
"""Host dependency doctor. Deliberately dependency-light itself (stdlib plus one local import) so
it still works when everything else the plugin needs is missing — the one check every other cfq
script cannot perform on its own behalf. Never installs anything, never invokes a package manager;
it only reports and, for required gaps, offers an installer hint.

Ported from cfq-doctor.sh -- a port, not a redesign: the CLI contract (verbs, message wording,
warning-versus-failure classification, exit codes) is the invariant this file preserves. It must
not import anything from cfq_lib beyond `errors` -- this is the script that runs when the
environment is broken, and every import is a way for it to fail before it can report.
"""

import argparse
import json
import os
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cfq_lib import errors  # noqa: E402

PROG = "cfq_doctor.py"

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
INVENTORY = SCRIPT_DIR.parent / "config" / "dependencies.txt"

PONYTAIL_MODES = ("off", "lite", "full", "ultra")


# ---- dependency inventory ------------------------------------------------------------------

def read_inventory():
    """Yields (names, kind) for every non-comment, non-empty row of config/dependencies.txt."""
    for line in INVENTORY.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("|", 2)
        if len(parts) < 2:
            continue
        yield parts[0], parts[1]


def check_dependencies():
    missing_required = []
    missing_alt_groups = []
    missing_optional = []
    for names, kind in read_inventory():
        if kind == "required":
            if shutil.which(names) is None:
                missing_required.append(names)
        elif kind == "optional":
            if shutil.which(names) is None:
                missing_optional.append(names)
        elif kind == "alternative":
            alts = names.split(",")
            if not any(shutil.which(a) for a in alts):
                missing_alt_groups.append(f"[{names}]")
    return missing_required, missing_alt_groups, missing_optional


def install_hint(name):
    """Best-effort, environment-owner action only -- never executed here."""
    if shutil.which("apt-get"):
        return f"apt-get install -y {name}"
    if shutil.which("brew"):
        return f"brew install {name}"
    if shutil.which("dnf"):
        return f"dnf install -y {name}"
    if shutil.which("apk"):
        return f"apk add {name}"
    return f"install {name} via your platform's package manager"


# ---- ponytail advisory ---------------------------------------------------------------------

def ponytail_advisory():
    """cfq only ever uses ponytail for one thing (the optional maintenance-run audit, a one-shot
    skill invocation) and expects it dormant otherwise. Mirrors, at grep precision, the tier
    order ponytail/hooks/ponytail-config.js's getDefaultMode() itself uses: env var, then config
    file, then "full"."""
    home = pathlib.Path(os.environ.get("HOME", ""))
    cache = home / ".claude" / "plugins" / "cache"
    if not _ponytail_installed(cache):
        return None

    mode = os.environ.get("PONYTAIL_DEFAULT_MODE", "").lower()
    if mode not in PONYTAIL_MODES:
        mode = ""
    if not mode:
        xdg_config = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
        pony_cfg = pathlib.Path(xdg_config) / "ponytail" / "config.json"
        if pony_cfg.is_file():
            try:
                data = json.loads(pony_cfg.read_text())
                mode = str(data.get("defaultMode", "")).lower()
            except (json.JSONDecodeError, OSError):
                mode = ""
        if mode not in PONYTAIL_MODES:
            mode = ""
    if not mode:
        mode = "full"
    if mode == "off":
        return None
    return f"ponytail mode: {mode} (cfq expects off) -- fix: /ponytail default off"


def _ponytail_installed(cache_dir):
    """True if cache_dir/<L1>/<L2> has a directory named "ponytail" at depth 2 -- mirrors
    `find "$cache_dir" -mindepth 2 -maxdepth 2 -type d -name ponytail`."""
    if not cache_dir.is_dir():
        return False
    for l1 in cache_dir.iterdir():
        if not l1.is_dir():
            continue
        for l2 in l1.iterdir():
            if l2.is_dir() and l2.name == "ponytail":
                return True
    return False


# ---- verbs ----------------------------------------------------------------------------------

def cmd_check(args):
    missing_required, missing_alt_groups, missing_optional = check_dependencies()
    advisory = ponytail_advisory()
    if args.json:
        ok = not missing_required and not missing_alt_groups
        obj = {
            "ok": ok,
            "missingRequired": missing_required,
            "missingAlternativeGroups": missing_alt_groups,
            "missingOptional": missing_optional,
            "ponytailAdvisory": advisory,
        }
        print(json.dumps(obj, separators=(",", ":")))
        return

    print("cfq dependency check")
    if not missing_required and not missing_alt_groups:
        print("  required: ok")
    else:
        if missing_required:
            print(f"  missing required: {' '.join(missing_required)}")
        if missing_alt_groups:
            print(f"  missing alternative group(s): {' '.join(missing_alt_groups)}")
        for name in missing_required:
            print(f"    hint: {install_hint(name)}")
    if missing_optional:
        print(f"  optional (degraded, not blocking): {' '.join(missing_optional)}")
    else:
        print("  optional: ok")
    if advisory:
        print(f"  optional advisory: {advisory}")


def cmd_hook(args):
    missing_required, missing_alt_groups, _missing_optional = check_dependencies()
    if not missing_required and not missing_alt_groups:
        return
    gap = " ".join(missing_required + missing_alt_groups)
    msg = f"cfq: missing required host command(s): {gap} — cfq scripts will fail until this is installed."
    ctx = (
        f"cfq dependency check found missing required command(s): {gap}. Avoid running any cfq "
        "(/pfq, /ifq, /cfq, /rfq) command until this is resolved; the environment owner needs to "
        "install it."
    )
    print(json.dumps({
        "systemMessage": msg,
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx},
    }, separators=(",", ":")))


# ---- argument parsing -----------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(prog=PROG, add_help=True)
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("check")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("hook")
    p.set_defaults(func=cmd_hook)

    return parser


def main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        errors.die(f"usage: {PROG} check | check --json | hook")
    func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
