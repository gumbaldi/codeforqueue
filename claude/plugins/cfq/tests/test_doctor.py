"""Migrated from test-doctor.sh.

Self-test for scripts/cfq_doctor.py: the host dependency doctor. Deliberately jq-free itself, so
every case that restricts PATH uses an explicit env= per call (never a mutated os.environ) and
parses JSON output with the stdlib json module rather than requiring jq to be on PATH.
"""

import json
import pathlib
import re
import tempfile
import unittest

from cfq_testlib import CfqTestCase, PLUGIN_ROOT

CORE_BINS = [
    "bash", "python3", "git", "timeout", "head", "ls", "date", "stat", "printf", "mkdir", "tr",
    "pwd", "sed", "grep", "cat", "dirname", "mv", "rm", "find",
]


class TestDoctor(CfqTestCase):
    def test_healthy_path_hook_mode_is_silent(self):
        healthy_dir = self.minimal_path(*CORE_BINS, "jq", "gh", "tea", "npm")
        proc = self.run_cfq("doctor", "hook", env={"PATH": healthy_dir})
        self.assertEqual(proc.stdout, "", f"healthy hook mode produced output: {proc.stdout}")
        self.assertEqual(proc.returncode, 0, f"healthy hook mode exit = {proc.returncode}")

    def test_missing_jq_names_jq_in_hook_output(self):
        nojq_dir = self.minimal_path(*CORE_BINS, "gh", "tea", "npm")
        proc = self.run_cfq("doctor", "hook", env={"PATH": nojq_dir})
        out = self.json_out(proc)
        self.assertIn("systemMessage", out, "no systemMessage when jq missing")
        sysmsg = out["systemMessage"]
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("jq", sysmsg, f"systemMessage does not name jq: {sysmsg}")
        self.assertIn("jq", ctx, f"additionalContext does not name jq: {ctx}")

    def test_check_json_works_without_jq_in_path(self):
        nojq_dir = self.minimal_path(*CORE_BINS, "gh", "tea", "npm")
        proc = self.run_cfq("doctor", "check", "--json", env={"PATH": nojq_dir})
        out = self.json_out(proc)
        self.assertIn("jq", out["missingRequired"], f"check --json does not list jq missing: {out}")
        self.assertFalse(out["ok"], "check --json ok should be false when jq missing")

    def test_missing_optional_providers_does_not_fail_core_health(self):
        nooptional_dir = self.minimal_path(*CORE_BINS, "jq")
        proc = self.run_cfq("doctor", "check", "--json", env={"PATH": nooptional_dir})
        out = self.json_out(proc)
        self.assertTrue(out["ok"], f"missing only optional providers should still be ok=true: {out}")
        self.assertEqual(
            sorted(out["missingOptional"]), ["gh", "npm", "tea"],
            f"missingOptional should list gh,npm,tea: {out}",
        )
        proc = self.run_cfq("doctor", "hook", env={"PATH": nooptional_dir})
        self.assertEqual(
            proc.stdout, "", f"missing-optional-only PATH should still be hook-silent: {proc.stdout}",
        )

    def test_dependency_inventory_matches_required_and_optional(self):
        inventory = (PLUGIN_ROOT / "config" / "dependencies.txt").read_text()
        for must in ("bash", "git", "jq", "python3"):
            self.assertRegex(
                inventory, rf"(?m)^{re.escape(must)}\|required\|",
                f"dependencies.txt missing required entry for {must}",
            )
        self.assertRegex(
            inventory, r"(?m)^timeout,gtimeout\|alternative\|",
            "dependencies.txt missing timeout/gtimeout alternative group",
        )
        for opt in ("gh", "tea", "npm"):
            self.assertRegex(
                inventory, rf"(?m)^{re.escape(opt)}\|optional\|",
                f"dependencies.txt missing optional entry for {opt}",
            )

    def test_package_manager_is_never_invoked(self):
        nojq_dir = self.minimal_path(*CORE_BINS, "gh", "tea", "npm")
        marker = self._repos_dir / "pm-marker"
        marker.mkdir(parents=True)
        for pm in ("apt-get", "brew", "dnf", "apk"):
            stub = marker / pm
            stub.write_text(
                "#!/usr/bin/env bash\necho \"INVOKED\" >> \"" + str(marker / "invoked.log") + "\"\n"
            )
            stub.chmod(0o755)
        pm_path = f"{marker}:{nojq_dir}"
        self.run_cfq("doctor", "check", env={"PATH": pm_path})
        self.run_cfq("doctor", "hook", env={"PATH": pm_path})
        self.assertFalse((marker / "invoked.log").exists(), "a package manager was actually invoked")

    def test_missing_python3_reports_named_guard_message(self):
        nopython_bins = [b for b in CORE_BINS if b != "python3"]
        nopython_dir = self.minimal_path(*nopython_bins, "jq")
        proc = self.run_cfq("doctor", "check", env={"PATH": nopython_dir})
        self.assertEqual(proc.returncode, 127, f"no-python3 exit != 127: {proc.returncode}")
        self.assertIn(
            "cfq: python3 is required for 'doctor' but was not found on PATH.", proc.stderr,
            f"missing named python3-guard message: {proc.stderr}",
        )

    def test_hook_config_uses_plugin_root_variable(self):
        hooks_json = PLUGIN_ROOT / "hooks" / "hooks.json"
        try:
            data = json.loads(hooks_json.read_text())
        except json.JSONDecodeError as e:
            self.fail(f"hooks/hooks.json is not valid JSON: {e}")
        cmd = data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertTrue(
            cmd.startswith("${CLAUDE_PLUGIN_ROOT}"),
            f"hook command does not use ${{CLAUDE_PLUGIN_ROOT}}: {cmd}",
        )
        self.assertFalse(cmd.startswith("/"), f"hook command is an absolute path: {cmd}")

    def test_ponytail_advisory_full_mode_does_not_affect_ok(self):
        healthy_dir = self.minimal_path(*CORE_BINS, "jq", "gh", "tea", "npm")
        pony_home = self._new_pony_home()
        proc = self.run_cfq("doctor", "check", "--json", home=pony_home, env={"PATH": healthy_dir})
        self.assertEqual(
            proc.returncode, 0, f"check --json exit != 0 with ponytail advisory present: {proc.returncode}",
        )
        out = self.json_out(proc)
        self.assertTrue(out["ok"], f"ponytail advisory should not affect ok: {out}")
        self.assertIn("full", out["ponytailAdvisory"], f"ponytailAdvisory missing/wrong mode: {out}")

        text = self.run_cfq("doctor", "check", home=pony_home, env={"PATH": healthy_dir}).stdout
        self.assertRegex(text, r"optional advisory:.*full", f"text mode missing ponytail advisory: {text}")

    def test_ponytail_advisory_mode_off_suppresses_advisory(self):
        healthy_dir = self.minimal_path(*CORE_BINS, "jq", "gh", "tea", "npm")
        pony_home = self._new_pony_home()
        (pony_home / ".config" / "ponytail").mkdir(parents=True)
        (pony_home / ".config" / "ponytail" / "config.json").write_text('{"defaultMode":"off"}')

        proc = self.run_cfq("doctor", "check", "--json", home=pony_home, env={"PATH": healthy_dir})
        out = self.json_out(proc)
        self.assertIsNone(out["ponytailAdvisory"], f"mode off should suppress the advisory: {out}")

        text = self.run_cfq("doctor", "check", home=pony_home, env={"PATH": healthy_dir}).stdout
        self.assertNotIn(
            "optional advisory:", text, f"mode off should not print an advisory line: {text}",
        )

    def test_ponytail_not_installed_no_advisory(self):
        healthy_dir = self.minimal_path(*CORE_BINS, "jq", "gh", "tea", "npm")
        pony_home = self._repos_dir / "pony-not-installed"
        pony_home.mkdir(parents=True)
        proc = self.run_cfq("doctor", "check", "--json", home=pony_home, env={"PATH": healthy_dir})
        out = self.json_out(proc)
        self.assertIsNone(out["ponytailAdvisory"], f"not-installed should suppress the advisory: {out}")

    def _new_pony_home(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        home = pathlib.Path(d.name)
        (home / ".claude" / "plugins" / "cache" / "ponytail" / "ponytail" / "4.8.4").mkdir(parents=True)
        return home


if __name__ == "__main__":
    unittest.main()
