"""Migrated from test-resume.sh (bin/cfq resume — state reconstruction)."""

import json
import subprocess
import unittest

from cfq_testlib import CfqTestCase


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check
    )


class ResumeTest(CfqTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = self._repos_dir / "tmp"
        self.tmp.mkdir()
        try:
            git(self.tmp, "init", "-q", "-b", "main")
        except subprocess.CalledProcessError:
            git(self.tmp, "init", "-q")
        git(self.tmp, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")

    def test_fresh_batch(self):
        # Fresh batch: no report.json, no .batch-context.md, one open phase, branch doesn't exist
        # yet.
        batch = self.tmp / ".claude" / "cfq" / "impl" / "2026-01-01-fresh"
        batch.mkdir(parents=True)
        (batch / "01-a.md").write_text("# T\n\n## Size\n\nM\n")

        proc = self.run_cfq("resume", str(self.tmp), str(batch))
        out = self.json_out(proc)
        self.assertEqual(out["phasesDone"], [], msg="fresh phasesDone")
        self.assertIsNone(out["lastCommit"], msg="fresh lastCommit")
        self.assertFalse(out["batchContext"]["exists"], msg="fresh batchContext")
        self.assertEqual(out["branch"]["mode"], "new", msg="fresh branch mode")
        self.assertEqual(out["phasesOpen"][0]["size"], "M", msg="fresh phase size")

    def test_midflight_batch(self):
        # Mid-flight batch: 01-a done with a green report entry carrying a commit, 02-b open with
        # a red entry, a .batch-context.md present, a missing-Size open phase.
        batch1 = self.tmp / ".claude" / "cfq" / "impl" / "2026-01-01-fresh"
        batch1.mkdir(parents=True)
        (batch1 / "01-a.md").write_text("# T\n\n## Size\n\nM\n")

        batch2 = self.tmp / ".claude" / "cfq" / "impl" / "2026-01-02-midflight"
        (batch2 / "done").mkdir(parents=True)
        (batch2 / "done" / "01-a.md").write_text((batch1 / "01-a.md").read_text())
        (batch2 / "02-b.md").write_text("# T2\n")
        (batch2 / ".batch-context.md").write_text("# Batch Context\n\n## Goal\nx\n")

        git(self.tmp, "checkout", "-qb", "v0.1-midflight")
        git(self.tmp, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "phase 1")
        sha = git(self.tmp, "rev-parse", "HEAD").stdout.strip()

        report = {
            "repo": str(self.tmp),
            "batch": "2026-01-02-midflight",
            "started": "2026-01-01T00:00:00+00:00",
            "phases": [
                {
                    "phase": "01-a",
                    "status": "green",
                    "finished": "2026-01-01T01:00:00+00:00",
                    "summary": "ok",
                    "deviations": ["did X instead of Y"],
                    "errors": [],
                    "verification": "ok",
                    "commit": sha,
                },
                {
                    "phase": "02-b",
                    "status": "red",
                    "finished": "2026-01-01T02:00:00+00:00",
                    "summary": "failed",
                    "deviations": [],
                    "errors": ["boom"],
                    "verification": "fail",
                    "commit": "",
                },
            ],
        }
        (batch2 / "report.json").write_text(json.dumps(report))

        proc = self.run_cfq("resume", str(self.tmp), str(batch2))
        out = self.json_out(proc)
        self.assertEqual(out["lastCommit"], sha, msg="midflight lastCommit")
        self.assertEqual(out["lastCommitSource"], "report", msg="midflight source")
        self.assertEqual(
            out["phasesDone"][0],
            {"num": "01", "slug": "01-a", "commit": sha},
            msg="midflight phasesDone",
        )
        self.assertEqual(
            out["deviations"],
            [{"phase": "01-a", "text": "did X instead of Y"}],
            msg="midflight deviations",
        )
        self.assertEqual(out["redPhases"], ["02-b"], msg="midflight redPhases")
        self.assertEqual(out["phasesOpen"][0]["size"], "M", msg="midflight missing-size default")
        self.assertTrue(out["batchContext"]["exists"], msg="midflight batchContext")
        self.assertEqual(
            out["batchContext"]["path"],
            str(batch2 / ".batch-context.md"),
            msg="midflight batchContext path",
        )

    def test_legacy_commit_falls_back_to_branch_tip(self):
        # Old-style green entry with an empty commit -> falls back to the branch tip.
        batch3 = self.tmp / ".claude" / "cfq" / "impl" / "2026-01-03-legacycommit"
        (batch3 / "done").mkdir(parents=True)
        (batch3 / "done" / "01-a.md").write_text("# T3\n")

        git(self.tmp, "checkout", "-qb", "v0.1-legacycommit", "main")
        git(self.tmp, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "legacy phase")
        tip = git(self.tmp, "rev-parse", "HEAD").stdout.strip()

        report = {
            "repo": str(self.tmp),
            "batch": "2026-01-03-legacycommit",
            "started": "t",
            "phases": [
                {
                    "phase": "01-a",
                    "status": "green",
                    "finished": "t",
                    "summary": "ok",
                    "deviations": [],
                    "errors": [],
                    "verification": "ok",
                    "commit": "",
                }
            ],
        }
        (batch3 / "report.json").write_text(json.dumps(report))

        proc = self.run_cfq("resume", str(self.tmp), str(batch3))
        out = self.json_out(proc)
        self.assertEqual(out["lastCommit"], tip, msg="legacy lastCommit")
        self.assertEqual(out["lastCommitSource"], "branch-tip", msg="legacy source")

    def test_two_open_one_done(self):
        # Routine: two open phases plus one already in done/ -- phasesOpen/phasesDone both
        # populated in the same call, field by field.
        batch = self.tmp / ".claude" / "cfq" / "impl" / "2026-01-04-tworeq"
        (batch / "done").mkdir(parents=True)
        (batch / "done" / "01-a.md").write_text("# T1\n\n## Size\n\nS\n")
        (batch / "02-b.md").write_text("# T2\n\n## Size\n\nL\n")
        (batch / "03-c.md").write_text("# T3\n")

        proc = self.run_cfq("resume", str(self.tmp), str(batch))
        out = self.json_out(proc)
        self.assertEqual(
            out["phasesOpen"],
            [
                {"num": "02", "slug": "02-b", "size": "L"},
                {"num": "03", "slug": "03-c", "size": "M"},
            ],
            msg="two-open phasesOpen",
        )
        self.assertEqual(
            out["phasesDone"],
            [{"num": "01", "slug": "01-a", "commit": None}],
            msg="two-open phasesDone, no report.json so no commit",
        )

    def test_planning_marker_is_ignored(self):
        # A .planning marker does not change resume's own output -- filtering batches still
        # planning is a batch-selection concern (cfq_ifq_preflight.py), not resume's.
        batch = self.tmp / ".claude" / "cfq" / "impl" / "2026-01-05-planning"
        batch.mkdir(parents=True)
        (batch / ".planning").write_text("")
        (batch / "01-a.md").write_text("# T\n")

        proc = self.run_cfq("resume", str(self.tmp), str(batch))
        out = self.json_out(proc)
        self.assertEqual(out["phasesOpen"], [{"num": "01", "slug": "01-a", "size": "M"}])

    def test_empty_batch_dir_no_phase_files(self):
        # Edge: the directory exists but holds no phase files at all -- both lists empty, no
        # crash.
        batch = self.tmp / ".claude" / "cfq" / "impl" / "2026-01-06-empty"
        batch.mkdir(parents=True)

        proc = self.run_cfq("resume", str(self.tmp), str(batch))
        out = self.json_out(proc)
        self.assertEqual(out["phasesOpen"], [], msg="empty phasesOpen")
        self.assertEqual(out["phasesDone"], [], msg="empty phasesDone")
        self.assertIsNone(out["lastCommit"], msg="empty lastCommit")
        self.assertFalse(out["batchContext"]["exists"], msg="empty batchContext")

    def test_nonexistent_batch_directory_fails(self):
        # Failure: no such batch directory -- pins the exact plain-text message and exit code,
        # whether or not the repo ever had a queue at all (both collapse to the same check).
        missing = self.tmp / ".claude" / "cfq" / "impl" / "2026-01-07-nope"
        proc = self.run_cfq("resume", str(self.tmp), str(missing))
        self.assertEqual(proc.returncode, 1, msg="missing batch dir exit code")
        self.assertIn(
            f"no such batch directory: {missing}", proc.stderr,
            msg=f"missing batch dir stderr: {proc.stderr!r}",
        )

    def test_no_queue_at_all_same_as_missing_batch(self):
        # A repo that has never had a .claude/cfq directory at all behaves identically -- the
        # check is purely "does this batch directory exist", nothing queue-specific.
        bare = self._repos_dir / "bare"
        bare.mkdir()
        try:
            git(bare, "init", "-q", "-b", "main")
        except subprocess.CalledProcessError:
            git(bare, "init", "-q")
        missing = bare / ".claude" / "cfq" / "impl" / "2026-01-08-nope"

        proc = self.run_cfq("resume", str(bare), str(missing))
        self.assertEqual(proc.returncode, 1, msg="no-queue exit code")
        self.assertIn(
            f"no such batch directory: {missing}", proc.stderr,
            msg=f"no-queue stderr: {proc.stderr!r}",
        )


if __name__ == "__main__":
    unittest.main()
