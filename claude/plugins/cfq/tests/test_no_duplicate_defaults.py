"""Migrated from test-no-duplicate-defaults.sh.

Regression guard for config-runtime-refactor's motivating bug shape: a script reading a setting
through cfq-settings.sh, but falling back on failure to a hardcoded literal that happens to copy
that key's schema default. The default then only lives in cfq-settings.sh's schema in the common
case -- but a later change to the schema default silently stops reaching that script, which keeps
using the frozen old value forever (cfq-lang.sh's pre-phase-6 `|| echo en`, and pre-phase-5
cfq-scan.sh's `$HOME/git`, both this exact shape). Scope is deliberately narrow: only a fallback
attached to an actual `cfq-settings.sh ... get` call is this bug shape -- an unrelated `|| echo
false`/`:-0` elsewhere in a script (an mtime fallback, a JSON boolean literal, ...) is not, and is
not linted. Not a general linter; see cfq/CLAUDE.md's "adding a setting" convention.

ctx-usage.sh is excluded alongside cfq-settings.sh itself: unlike every other script it uses
`set -u`, not `set -e` (see its own header comment) specifically so the phase-start size gate
always emits a decision even if the settings subsystem itself is unavailable -- its two
`|| echo <default>` fallbacks are that crash-safety net, not forgotten duplication.

`test_no_duplicate_defaults.py` asserts that no script hardcodes a default that cfq-settings.sh
already owns. After batch 014 ports settings to Python, this guard must also scan `.py` files --
batch 014 phase 02 handles it. The `.sh`-only glob stays in place here on purpose; a guard that
already scanned `.py` files would silently pass in a repo that has none.
"""

import re
import unittest

from cfq_testlib import SCRIPTS_DIR, CfqTestCase

EXEMPT = {"cfq-settings.sh", "ctx-usage.sh", "cfq_settings.py"}

FALLBACK_LINE = re.compile(r"\|\| echo |:-[^}]*\}")
SETTINGS_REF = re.compile(r"cfq-settings\.sh|\$settings_sh")


def default_reprs(schema):
    """One rendered literal per key, in the same spelling a shell fallback would use: int/string
    /enum bare, array CSV-joined, object as compact JSON."""
    reprs = []
    for entry in schema.values():
        default = entry["default"]
        if isinstance(default, list):
            reprs.append(",".join(str(v) for v in default))
        elif isinstance(default, dict):
            import json

            reprs.append(json.dumps(default, separators=(",", ":")))
        elif isinstance(default, bool):
            reprs.append("true" if default else "false")
        elif default is None:
            reprs.append("null")
        else:
            reprs.append(str(default))
    return reprs


def extract_literal(content):
    """Extracts the fallback literal from one candidate line, stripping one layer of matching
    quotes and a trailing `)`/`;`. Empty output means the line didn't carry a recognizable
    literal."""
    literal = ""
    if "|| echo " in content:
        literal = content.rsplit("|| echo ", 1)[1]
        literal = literal.split(";", 1)[0]
        literal = literal.rsplit(")", 1)[0] if literal.endswith(")") else literal
    elif ":-" in content:
        idx = content.rfind(":-")
        rest = content[idx + 2 :]
        literal = rest.split("}", 1)[0]
    if not literal:
        return ""
    if len(literal) >= 2 and literal[0] == literal[-1] and literal[0] in ("'", '"'):
        literal = literal[1:-1]
    return literal


def check_dir(directory, reprs, exempt=(), globs=("*.sh",)):
    """Scans every file matching `globs` directly under `directory` (non-recursive) except
    `exempt` for a settings-read fallback literal that duplicates a schema default. Returns the
    list of FAIL lines. `*.py` joins the scan once settings has a Python implementation (batch
    014 phase 02) -- the `*.sh`-only default stays so a repo with no `.py` files still passes."""
    fails = []
    files = []
    for pattern in globs:
        files.extend(directory.glob(pattern))
    for f in sorted(files):
        if f.name in exempt:
            continue
        for lineno, line in enumerate(f.read_text().splitlines(), start=1):
            if not SETTINGS_REF.search(line):
                continue
            if not FALLBACK_LINE.search(line):
                continue
            literal = extract_literal(line)
            if not literal:
                continue
            if literal in reprs:
                fails.append(
                    f"FAIL: {f.name}:{lineno} duplicates a schema default in its "
                    f"settings-read fallback: '{literal}'"
                )
    return fails


class NoDuplicateDefaultsTest(CfqTestCase):
    def test_no_hardcoded_default_duplicates_schema(self):
        schema = self.json_out(self.run_cfq("settings", "describe", home=self.home))
        reprs = default_reprs(schema)

        fails = check_dir(SCRIPTS_DIR, reprs, EXEMPT, globs=("*.sh", "*.py"))
        self.assertEqual(fails, [], msg="\n".join(fails))

    def test_selfcheck_catches_bug_shape_and_ignores_unrelated_fallback(self):
        schema = self.json_out(self.run_cfq("settings", "describe", home=self.home))
        reprs = default_reprs(schema)

        tmp = self._repos_dir / "selfcheck"
        tmp.mkdir()
        (tmp / "bad.sh").write_text(
            "#!/usr/bin/env bash\n"
            'stop_used=$("$script_dir/cfq-settings.sh" get stopUsed 2>/dev/null || echo 100000)\n'
        )
        (tmp / "good.sh").write_text(
            "#!/usr/bin/env bash\n"
            'marker=$("$script_dir/cfq-settings.sh" get someKey 2>/dev/null || echo not-a-schema-default)\n'
        )

        fails = check_dir(tmp, reprs)
        joined = "\n".join(fails)
        self.assertIn(
            "bad.sh", joined, msg="self-check did not catch the synthetic stopUsed-duplicate fixture"
        )
        self.assertNotIn(
            "good.sh",
            joined,
            msg="self-check false-flagged a fallback literal that isn't a schema default",
        )


if __name__ == "__main__":
    unittest.main()
