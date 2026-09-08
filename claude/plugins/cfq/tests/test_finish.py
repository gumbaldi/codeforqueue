"""Migrated from test-finish.sh.

Self-test for scripts/cfq-finish.sh -- the batch-done sequence: move into impl/done, release the
lock unconditionally (even on a mid-sequence changelog failure), skip cleanly when the changelog is
disabled or no planning snapshot exists, and gate report.html rendering on htmlReport.
"""

import json
import shutil
import subprocess
import sys
import unittest

from cfq_testlib import CfqTestCase, PLUGIN_ROOT, SCRIPTS_DIR


class FinishTest(CfqTestCase):
    def _new_repo(self, name):
        # cfq-finish.sh diffs the changed-files check against a branch literally named "main"
        # (cfq_lang.py --changed main), so the fixture needs that branch regardless of this host's
        # git init.defaultBranch.
        repo = self.make_repo(name)
        subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True)
        return repo

    def _new_batch(self, repo, name):
        d = repo / ".claude/cfq/impl" / name
        (d / "done").mkdir(parents=True)
        (d / ".priority").write_text("medium\n")
        (d / "done" / "01-a.md").write_text("# A phase\n")
        return d

    def test_happy_path(self):
        home = self._repos_dir / "home1"
        home.mkdir()
        repo = self._new_repo("repo1")
        batch = self._new_batch(repo, "2026-01-01-happy")
        self.run_cfq("lock", "acquire", str(repo), "2026-01-01-happy", home=home, check=True)

        proc = self.run_cfq("finish", str(repo), str(batch), "v0.1-happy", home=home, check=True)
        out = self.json_out(proc)
        self.assertTrue(
            (repo / ".claude/cfq/impl/done/2026-01-01-happy").is_dir(),
            f"batch not moved into impl/done: {out}",
        )
        self.assertFalse(
            (repo / ".claude/cfq/impl/2026-01-01-happy").exists(),
            "batch still present at its old location",
        )
        self.assertEqual(out["lock"], "released", f"lock field: {out}")
        lockstatus = self.run_cfq("lock", "status", str(repo), home=home).stdout.strip()
        self.assertEqual(lockstatus, "FREE", f"lock not actually released: {lockstatus}")

    def test_mid_sequence_changelog_failure_still_completes(self):
        # Point changelogFile at a path inside a read-only directory -- cfq-changelog.sh finish
        # fails to write, cfq-finish.sh must still complete the rest of the sequence and release
        # the lock.
        home = self._repos_dir / "home2"
        (home / ".claude/code-for-queue").mkdir(parents=True)
        repo = self._new_repo("repo2")
        batch = self._new_batch(repo, "2026-01-01-brokenchangelog")
        self.run_cfq(
            "lock", "acquire", str(repo), "2026-01-01-brokenchangelog", home=home, check=True,
        )

        readonlydir = repo / "readonlydir"
        readonlydir.mkdir()
        (home / ".claude/code-for-queue/settings.json").write_text(
            json.dumps({"changelogFile": "readonlydir/changelog.yml"})
        )
        readonlydir.chmod(0o555)
        try:
            proc = self.run_cfq(
                "finish", str(repo), str(batch), "v0.1-brokenchangelog", home=home, check=True,
            )
        finally:
            readonlydir.chmod(0o755)

        out = self.json_out(proc)
        self.assertTrue(
            (repo / ".claude/cfq/impl/done/2026-01-01-brokenchangelog").is_dir(),
            f"sequence did not complete the move: {out}",
        )
        self.assertEqual(out["lock"], "released", f"lock field on failure path: {out}")
        lockstatus = self.run_cfq("lock", "status", str(repo), home=home).stdout.strip()
        self.assertEqual(
            lockstatus, "FREE", f"lock not released after mid-sequence failure: {lockstatus}",
        )
        self.assertGreater(len(out["errors"]), 0, f"errors should be non-empty: {out}")
        self.assertTrue(
            any(e.startswith("changelog:") for e in out["errors"]),
            f"no changelog error recorded: {out}",
        )

    def test_changelog_file_empty_is_not_an_error(self):
        home = self._repos_dir / "home3"
        (home / ".claude/code-for-queue").mkdir(parents=True)
        repo = self._new_repo("repo3")
        batch = self._new_batch(repo, "2026-01-01-nochangelog")
        self.run_cfq(
            "lock", "acquire", str(repo), "2026-01-01-nochangelog", home=home, check=True,
        )
        (home / ".claude/code-for-queue/settings.json").write_text(json.dumps({"changelogFile": ""}))

        out = self.json_out(
            self.run_cfq("finish", str(repo), str(batch), "v0.1-nochangelog", home=home, check=True)
        )
        self.assertEqual(
            out["changelog"], "changelogFile empty", f"changelog field should say empty: {out}",
        )
        self.assertFalse(
            any(e.startswith("changelog:") for e in out["errors"]),
            f"changelogFile empty must not be an error: {out}",
        )
        self.assertTrue(
            (repo / ".claude/cfq/impl/done/2026-01-01-nochangelog").is_dir(),
            f"sequence did not complete: {out}",
        )

    def test_no_planning_snapshot_is_not_an_error(self):
        home = self._repos_dir / "home4"
        home.mkdir()
        repo = self._new_repo("repo4")
        # No report.json at all -- simulates a batch that predates the planning-time security
        # snapshot.
        batch = self._new_batch(repo, "2026-01-01-nosnapshot")
        self.run_cfq(
            "lock", "acquire", str(repo), "2026-01-01-nosnapshot", home=home, check=True,
        )

        out = self.json_out(
            self.run_cfq("finish", str(repo), str(batch), "v0.1-nosnapshot", home=home, check=True)
        )
        self.assertEqual(
            out["security"]["new"], {}, f"security.new should be empty without a planning snapshot: {out}",
        )
        self.assertFalse(
            any(e.startswith("security:") for e in out["errors"]),
            f"missing planning snapshot must not be an error: {out}",
        )

    def test_html_report_auto_renders_when_enabled(self):
        home = self._repos_dir / "home5"
        (home / ".claude/code-for-queue").mkdir(parents=True)
        repo = self._new_repo("repo5")
        batch = self._new_batch(repo, "2026-01-01-htmlon")
        self.run_cfq("lock", "acquire", str(repo), "2026-01-01-htmlon", home=home, check=True)
        (home / ".claude/code-for-queue/settings.json").write_text(json.dumps({"htmlReport": True}))

        proc = self.run_cfq("finish", str(repo), str(batch), "v0.1-htmlon", home=home, check=True)
        self.json_out(proc)
        self.assertTrue(
            (repo / ".claude/cfq/impl/done/2026-01-01-htmlon/report.html").is_file(),
            "htmlReport=true should auto-render report.html",
        )

    def test_html_report_default_off(self):
        home = self._repos_dir / "home6"
        home.mkdir()
        repo = self._new_repo("repo6")
        batch = self._new_batch(repo, "2026-01-01-htmloff")
        self.run_cfq("lock", "acquire", str(repo), "2026-01-01-htmloff", home=home, check=True)

        self.run_cfq("finish", str(repo), str(batch), "v0.1-htmloff", home=home, check=True)
        self.assertFalse(
            (repo / ".claude/cfq/impl/done/2026-01-01-htmloff/report.html").exists(),
            "default htmlReport=false must not auto-render report.html",
        )

    def test_already_in_impl_done_completes_normally(self):
        # Edge: the batch directory handed in is already inside impl/done/ (batch_dir == moved)
        # -- the move is skipped, the rest of the sequence runs against it in place.
        home = self._repos_dir / "home7"
        home.mkdir()
        repo = self._new_repo("repo7")
        done_dir = repo / ".claude/cfq/impl/done"
        batch = done_dir / "2026-01-01-alreadydone"
        (batch / "done").mkdir(parents=True)
        (batch / "done" / "01-a.md").write_text("# A phase\n")
        self.run_cfq(
            "lock", "acquire", str(repo), "2026-01-01-alreadydone", home=home, check=True,
        )

        out = self.json_out(
            self.run_cfq(
                "finish", str(repo), str(batch), "v0.1-alreadydone", home=home, check=True,
            )
        )
        self.assertTrue(batch.is_dir(), f"batch should still be in place: {out}")
        self.assertEqual(out["lock"], "released", f"lock field: {out}")
        lockstatus = self.run_cfq("lock", "status", str(repo), home=home).stdout.strip()
        self.assertEqual(lockstatus, "FREE", f"lock not released: {lockstatus}")

    def test_hard_failure_before_move_still_releases_lock(self):
        # The trap, made explicit: a failure so early the sequence never even produces JSON on
        # stdout must still release the lock -- the counterpart to
        # test_mid_sequence_changelog_failure_still_completes, which asserts the sequence
        # *completes* despite a failure. This one asserts the lock guarantee holds even when it
        # does not complete at all.
        home = self._repos_dir / "home8"
        home.mkdir()
        repo = self._new_repo("repo8")
        batch = self._new_batch(repo, "2026-01-01-hardfail")
        self.run_cfq(
            "lock", "acquire", str(repo), "2026-01-01-hardfail", home=home, check=True,
        )

        impl_dir = repo / ".claude/cfq/impl"
        impl_dir.chmod(0o555)
        try:
            proc = self.run_cfq(
                "finish", str(repo), str(batch), "v0.1-hardfail", home=home,
            )
        finally:
            impl_dir.chmod(0o755)

        self.assertNotEqual(proc.returncode, 0, f"expected a hard failure: {proc.stdout!r}")
        lockstatus = self.run_cfq("lock", "status", str(repo), home=home).stdout.strip()
        self.assertEqual(
            lockstatus, "FREE", f"lock not released after a hard failure: {lockstatus}",
        )

    def test_nine_subcalls_run_in_expected_order(self):
        # Order: cfq-finish.sh runs its nine sub-nouns as a sequence where later steps depend on
        # earlier state -- wrap each with a logging shim and assert their first-occurrence order.
        home = self._repos_dir / "home9"
        home.mkdir()
        repo = self._new_repo("repo9")
        batch = self._new_batch(repo, "2026-01-01-order")
        self.run_cfq("lock", "acquire", str(repo), "2026-01-01-order", home=home, check=True)

        scripts_copy = self._repos_dir / "scripts"
        shutil.copytree(SCRIPTS_DIR, scripts_copy)
        bin_copy = self._repos_dir / "bin"
        shutil.copytree(PLUGIN_ROOT / "bin", bin_copy)

        log_file = self._repos_dir / "order.log"
        log_file.write_text("")
        noun_to_script = {
            "registry": "cfq_registry.py",
            "lang": "cfq_lang.py",
            "maintenance": "cfq_maintenance.py",
            "security": "cfq_security.py",
            "report": "cfq_report.py",
            "settings": "cfq_settings.py",
            "changelog": "cfq_changelog.py",
            "telemetry": "cfq_telemetry.py",
            "lock": "cfq_lock.py",
        }
        for noun, script_name in noun_to_script.items():
            orig = scripts_copy / script_name
            real = scripts_copy / script_name.replace(".py", "_real.py")
            orig.rename(real)
            orig.write_text(f"""#!/usr/bin/env python3
import subprocess
import sys

with open({str(log_file)!r}, "a") as f:
    f.write({noun!r} + " " + " ".join(sys.argv[1:]) + "\\n")
sys.exit(subprocess.run([sys.executable, {str(real)!r}] + sys.argv[1:]).returncode)
""")
            orig.chmod(0o755)

        run_env = self._base_env()
        run_env["HOME"] = str(home)
        proc = subprocess.run(
            [str(bin_copy / "cfq"), "finish", str(repo), str(batch), "v0.1-order"],
            capture_output=True, text=True, env=run_env,
        )
        self.assertEqual(proc.returncode, 0, f"finish via copy failed: {proc.stderr}")

        # `settings` is used internally by nearly every other script (to read its own config),
        # so its first log line is not necessarily cfq-finish.sh's own direct call -- only its
        # two direct calls (changelogFile, htmlReport) carry those exact argument tails.
        calls = [line for line in log_file.read_text().splitlines() if line]
        first_index = {}
        for i, line in enumerate(calls):
            noun = line.split(" ", 1)[0]
            if noun == "settings" and not line.endswith("changelogFile") \
                    and not line.endswith("htmlReport"):
                continue
            first_index.setdefault(noun, i)
        expected = [
            "registry", "lang", "maintenance", "security", "report", "settings", "changelog",
            "telemetry", "lock",
        ]
        for noun in expected:
            self.assertIn(noun, first_index, f"{noun} never directly called: {calls}")
        actual_order = sorted(expected, key=lambda n: first_index[n])
        self.assertEqual(actual_order, expected, f"call order mismatch, raw log: {calls}")


if __name__ == "__main__":
    unittest.main()
