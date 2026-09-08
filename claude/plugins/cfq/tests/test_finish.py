"""Migrated from test-finish.sh.

Self-test for scripts/cfq-finish.sh -- the batch-done sequence: move into impl/done, release the
lock unconditionally (even on a mid-sequence changelog failure), skip cleanly when the changelog is
disabled or no planning snapshot exists, and gate report.html rendering on htmlReport.
"""

import json
import subprocess
import unittest

from cfq_testlib import CfqTestCase


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


if __name__ == "__main__":
    unittest.main()
