"""Migrated from test-dispatcher.sh (bin/cfq, the single entrypoint).

The 21 scripts under scripts/ stay directly callable and unchanged (every other migrated test
here still calls them that way) — this file only asserts the dispatcher routes to them correctly,
one behaviour per case, all in one run.
"""

import re
import subprocess
import unittest

from cfq_testlib import CFQ_BIN, SCRIPTS_DIR, CfqTestCase


class DispatcherTest(CfqTestCase):
    def test_01_routine_byte_identical_stdout(self):
        # JSON array, JSON object and plain text — three different output shapes.
        repo1 = self.make_repo("repo1")
        a = subprocess.run(
            [str(CFQ_BIN), "settings", "list", "--repo", str(repo1)],
            capture_output=True, text=True,
        )
        b = subprocess.run(
            ["python3", str(SCRIPTS_DIR / "cfq_settings.py"), "list", "--repo", str(repo1)],
            capture_output=True, text=True,
        )
        self.assertEqual(a.stdout, b.stdout, msg="settings list differs between dispatcher and direct call")

        home1 = self._repos_dir / "home1"
        home1.mkdir()
        scan_env = {"HOME": str(home1), "CFQ_SCAN_ROOTS": "/nonexistent-scan-root"}
        a = self.run_clean(str(CFQ_BIN), "scan", env=scan_env)
        b = self.run_clean("bash", str(SCRIPTS_DIR / "cfq-scan.sh"), env=scan_env)
        self.assertEqual(a.stdout, b.stdout, msg="scan differs between dispatcher and direct call")

        home2 = self._repos_dir / "home2"
        home2.mkdir()
        doctor_env = {"HOME": str(home2)}
        a = self.run_clean(str(CFQ_BIN), "doctor", "check", env=doctor_env)
        b = self.run_clean("python3", str(SCRIPTS_DIR / "cfq_doctor.py"), "check", env=doctor_env)
        self.assertEqual(a.stdout, b.stdout, msg="doctor check differs between dispatcher and direct call")

    def test_02_argument_passthrough(self):
        repo2 = self.make_repo("repo2")
        tricky = "a value with a space and a $dollar sign"
        self.run_cfq("settings", "set", "--repo", str(repo2), "changelogFile", tricky, check=True)
        got = self.run_cfq("settings", "get", "--repo", str(repo2), "changelogFile").stdout.strip()
        self.assertEqual(got, tricky, msg=f"argument passthrough mangled the value: got '{got}'")

    def test_03_exit_codes_propagate(self):
        rc_dispatcher = self.run_cfq("batch", "allocate", "/no-such-repo-path").returncode
        rc_direct = subprocess.run(
            ["bash", str(SCRIPTS_DIR / "cfq-batch-id.sh"), "allocate", "/no-such-repo-path"],
            capture_output=True, text=True,
        ).returncode
        self.assertNotEqual(rc_dispatcher, 0, msg="dispatcher exit 0 on a failing subcommand")
        self.assertEqual(
            rc_dispatcher, rc_direct,
            msg=f"dispatcher exit ({rc_dispatcher}) != direct exit ({rc_direct})",
        )

    def test_04_unknown_noun(self):
        proc = self.run_cfq("nosuchthing")
        self.assertNotEqual(proc.returncode, 0, msg="unknown noun exited 0")
        self.assertIn("nouns:", proc.stderr, msg="unknown noun did not print the noun list to stderr")

    def test_05_no_arguments(self):
        proc = self.run_cfq()
        self.assertNotEqual(proc.returncode, 0, msg="no-args invocation exited 0")
        self.assertIn("nouns:", proc.stderr, msg="no-args invocation did not print the noun list")

    def test_06_help_fallback(self):
        proc = self.run_cfq("--help")
        self.assertEqual(proc.returncode, 0, msg=f"cfq --help exit = {proc.returncode}")
        out = proc.stdout + proc.stderr
        self.assertIn(" settings ", out, msg="cfq --help does not name the settings noun")

        proc = self.run_cfq("settings", "--help")
        self.assertEqual(proc.returncode, 0, msg=f"cfq settings --help exit = {proc.returncode}")
        out = (proc.stdout + proc.stderr).lower()
        self.assertIn("list", out, msg="cfq settings --help does not name its verbs")

        proc = self.run_cfq("batch", "--help")
        self.assertEqual(proc.returncode, 0, msg=f"cfq batch --help exit = {proc.returncode}")
        out = (proc.stdout + proc.stderr).lower()
        self.assertTrue(
            "allocate" in out or "next" in out,
            msg="cfq batch --help (fallback path) does not name its verbs",
        )

    def test_07_completeness_every_script_reachable(self):
        # Extracted straight from bin/cfq's own table, not retyped here, so this stays a
        # structural check rather than a copy that can silently drift from the real map. Scripts
        # may be shell or Python (batch 014); scripts/cfq_lib/ is a package, not a command, and
        # a non-recursive glob already excludes it without needing to say so.
        text = CFQ_BIN.read_text()
        pattern = re.compile(r"^\s*\[[a-z-]+\]=(cfq[a-z_-]+\.(?:sh|py)|ctx-usage\.sh)$", re.MULTILINE)
        mapped = sorted({m.group(1) for m in pattern.finditer(text)})
        on_disk = sorted(
            f.name
            for f in list(SCRIPTS_DIR.glob("*.sh")) + list(SCRIPTS_DIR.glob("*.py"))
            if f.name != "cfq-paths.sh"
        )
        self.assertEqual(
            mapped, on_disk,
            msg="dispatcher routing table and scripts/*.sh + scripts/*.py (minus cfq-paths.sh) disagree",
        )

        # Exactly one noun per script (no script mapped twice).
        all_mapped = [m.group(1) for m in pattern.finditer(text)]
        dupes = sorted({s for s in all_mapped if all_mapped.count(s) > 1})
        self.assertEqual(dupes, [], msg=f"script(s) mapped by more than one noun: {dupes}")


if __name__ == "__main__":
    unittest.main()
