"""Migrated from test-ifq-preflight.sh (scripts/cfq-ifq-preflight.sh).

The stub renames `cfq-branch.sh` and shadows it by filename. When that script is ported to
Python, this stub has to shadow `cfq_branch.py` instead — see batch `014` phase 02.
"""

import json
import shutil
import time
import unittest

from cfq_testlib import CfqTestCase, PLUGIN_ROOT


class IfqPreflightTest(CfqTestCase):
    def setUp(self):
        super().setUp()
        # Copies the whole scripts/ dir so cfq-ifq-preflight.sh's own script_dir resolution
        # (and every sibling script it shells out to, e.g. cfq-resume.sh -> cfq-branch.sh)
        # resolves inside the copy, then swaps cfq-branch.sh for a wrapper that logs every
        # invocation before delegating to the real binary. bin/ is copied alongside scripts/
        # (same relative layout as the real plugin) because internal sibling calls now route
        # through bin/cfq, which resolves its own NOUN_SCRIPT table relative to itself.
        self.scripts_copy = self._repos_dir / "scripts"
        shutil.copytree(PLUGIN_ROOT / "scripts", self.scripts_copy)
        shutil.copytree(PLUGIN_ROOT / "bin", self._repos_dir / "bin")
        real = self.scripts_copy / "cfq-branch-real.sh"
        (self.scripts_copy / "cfq-branch.sh").rename(real)
        self.count_log = self._repos_dir / "branch-calls.log"
        self.count_log.write_text("")
        stub = self.scripts_copy / "cfq-branch.sh"
        stub.write_text(f"""#!/usr/bin/env bash
set -eu
d="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
echo call >> "{self.count_log}"
exec "$d/cfq-branch-real.sh" "$@"
""")
        stub.chmod(0o755)
        self.pf = self.scripts_copy / "cfq-ifq-preflight.sh"

    def _run_pf(self, *args, home=None):
        return self.run_clean(
            "bash", str(self.pf), *args,
            env={"HOME": str(home if home is not None else self.home)},
        )

    def _calls(self):
        return len(self.count_log.read_text().splitlines())

    def _setup_repo(self, name):
        repo = self._repos_dir / name
        self.run_clean("git", "init", "-q", "-b", "main", str(repo))
        self.run_clean(
            "git", "-C", str(repo), "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "-q", "--allow-empty", "-m", "init",
        )
        self.run_clean(
            "bash", str(self.scripts_copy / "cfq-registry.sh"), "add", str(repo),
        )
        return repo

    def test_no_repo(self):
        out = self.json_out(self._run_pf(str(self._repos_dir / "does-not-exist")))
        self.assertEqual(out["status"], "NO_REPO", msg=f"non-git status = {out}")

    def test_continue_mode_calls_branch_once(self):
        repo1 = self._setup_repo("continue-repo")
        batch = repo1 / ".claude" / "cfq" / "impl" / "2026-01-01-solo"
        batch.mkdir(parents=True)
        (batch / "01-a.md").write_text("# T\n\n## Size\n\nS\n")
        self.run_clean("git", "-C", str(repo1), "branch", "cfq/2026-01-01-solo")

        self.count_log.write_text("")
        out = self.json_out(self._run_pf(str(repo1)))
        self.assertEqual(out["status"], "OK", msg=f"continue-mode status = {out}")
        self.assertEqual(out["branch"]["mode"], "continue", msg=f"expected continue mode = {out}")
        self.assertEqual(self._calls(), 1, msg=f"continue-mode cfq-branch.sh calls = {self._calls()}, want 1")
        self.assertTrue(
            "implExploreModel" in out["policy"] and "implExploreModelComplex" in out["policy"],
            msg=f"policy missing implExploreModel/implExploreModelComplex: {out}",
        )
        self.assertTrue(
            "reportDir" in out["reporting"] and "htmlReport" in out["reporting"],
            msg=f"missing reporting object: {out}",
        )

    def test_new_mode_calls_branch_twice_across_sequence(self):
        # new mode: preflight (1) + mutation's post-checkout confirm (1) = 2, combined across
        # the sequence.
        repo2 = self._setup_repo("new-repo")
        batch = repo2 / ".claude" / "cfq" / "impl" / "2026-01-01-fresh"
        batch.mkdir(parents=True)
        (batch / "01-a.md").write_text("# T\n\n## Size\n\nL\n")

        self.count_log.write_text("")
        out = self.json_out(self._run_pf(str(repo2)))
        self.assertEqual(out["branch"]["mode"], "new", msg=f"expected new mode = {out}")
        branch = out["branch"]["branch"]
        self.run_clean("git", "-C", str(repo2), "checkout", "-q", "-b", branch)
        self.run_clean(
            "bash", str(self.scripts_copy / "cfq-branch.sh"), "plan", str(repo2), "2026-01-01-fresh",
            env={"HOME": str(self.home)},
        )
        self.assertEqual(self._calls(), 2, msg=f"new-mode cfq-branch.sh calls = {self._calls()}, want 2")

    def test_selection_filters(self):
        repo3 = self._setup_repo("multi-repo")
        qdir = repo3 / ".claude" / "cfq" / "impl"
        for name in ["2026-01-01-alpha", "2026-01-02-beta", "2026-01-03-blocked", "2026-01-04-planning"]:
            (qdir / name).mkdir(parents=True)
        (qdir / "2026-01-01-alpha" / "01-a.md").touch()
        (qdir / "2026-01-02-beta" / "01-b.md").touch()
        (qdir / "2026-01-03-blocked" / "01-c.md").touch()
        (qdir / "2026-01-04-planning" / "01-d.md").touch()
        (qdir / "2026-01-03-blocked" / ".dependsOn").write_text("2026-01-01-alpha\n")
        (qdir / "2026-01-04-planning" / ".planning").touch()

        out = self.json_out(self._run_pf(str(repo3)))
        self.assertEqual(out["status"], "OK", msg=f"multi status = {out}")
        self.assertIsNone(out["batch"], msg=f"2+ selectable should leave batch null: {out}")
        self.assertIsNone(out["nextPhase"], msg=f"2+ selectable should leave nextPhase null: {out}")
        got = sorted(b["name"] for b in out["selection"]["selectable"])
        self.assertEqual(got, ["2026-01-01-alpha", "2026-01-02-beta"], msg=f"selectable = {got}")
        self.assertEqual(
            [b["name"] for b in out["selection"]["blocked"]], ["2026-01-03-blocked"],
            msg=f"blocked = {out}",
        )
        self.assertEqual(
            out["selection"]["planning"], ["2026-01-04-planning"], msg=f"planning = {out}"
        )

        # --select round-trip resolves one of the ambiguous batches
        out = self.json_out(self._run_pf(str(repo3), "--select", "2026-01-02-beta"))
        self.assertEqual(
            out["batch"]["name"], "2026-01-02-beta", msg=f"--select did not resolve batch: {out}"
        )
        self.assertEqual(out["nextPhase"]["slug"], "01-b", msg=f"--select nextPhase: {out}")

    def test_blocked_only_auto_selects_unblocked_dep(self):
        # only blocked batches left -> BLOCKED never happens if an unblocked dep exists (a real,
        # unfinished dependency actually blocks)
        repo4 = self._setup_repo("blocked-only")
        (repo4 / ".claude" / "cfq" / "impl" / "2026-01-01-dep").mkdir(parents=True)
        (repo4 / ".claude" / "cfq" / "impl" / "2026-01-02-b").mkdir(parents=True)
        (repo4 / ".claude" / "cfq" / "impl" / "2026-01-01-dep" / "01-x.md").touch()
        (repo4 / ".claude" / "cfq" / "impl" / "2026-01-02-b" / "01-x.md").touch()
        (repo4 / ".claude" / "cfq" / "impl" / "2026-01-02-b" / ".dependsOn").write_text("2026-01-01-dep\n")
        out = self.json_out(self._run_pf(str(repo4)))
        self.assertEqual(out["status"], "OK", msg=f"blocked-only status = {out}")
        self.assertEqual(
            out["batch"]["name"], "2026-01-01-dep",
            msg=f"should auto-select the unblocked dep batch: {out}",
        )
        got = [b["name"] for b in out["selection"]["blocked"]]
        self.assertEqual(got, ["2026-01-02-b"], msg=f"blocked list = {got}")

    def test_unknown_dependency_never_blocks(self):
        # an unresolvable dependency name is surfaced but never blocks
        repo4b = self._setup_repo("unknown-dep")
        (repo4b / ".claude" / "cfq" / "impl" / "2026-01-01-b").mkdir(parents=True)
        (repo4b / ".claude" / "cfq" / "impl" / "2026-01-01-b" / "01-x.md").touch()
        (repo4b / ".claude" / "cfq" / "impl" / "2026-01-01-b" / ".dependsOn").write_text("does-not-exist\n")
        out = self.json_out(self._run_pf(str(repo4b)))
        self.assertEqual(out["status"], "OK", msg=f"unknown-dep should not block: {out}")
        self.assertEqual(
            out["batch"]["name"], "2026-01-01-b", msg=f"unknown-dep batch should be selectable: {out}"
        )

    def test_no_batch(self):
        # nothing at all -> NO_BATCH
        repo5 = self._setup_repo("empty-repo")
        out = self.json_out(self._run_pf(str(repo5)))
        self.assertEqual(out["status"], "NO_BATCH", msg=f"empty-repo status = {out}")

    def test_multiple_in_progress(self):
        repo6 = self._setup_repo("multi-inprogress")
        qdir6 = repo6 / ".claude" / "cfq" / "impl"
        (qdir6 / "2026-01-01-a" / "done").mkdir(parents=True)
        (qdir6 / "2026-01-02-b" / "done").mkdir(parents=True)
        (qdir6 / "2026-01-01-a" / "01-x.md").touch()
        (qdir6 / "2026-01-01-a" / "done" / "00-y.md").touch()
        (qdir6 / "2026-01-02-b" / "01-x.md").touch()
        (qdir6 / "2026-01-02-b" / "done" / "00-y.md").touch()
        out = self.json_out(self._run_pf(str(repo6)))
        self.assertEqual(out["status"], "MULTIPLE_IN_PROGRESS", msg=f"multi-inprogress status = {out}")
        got = sorted(out["selection"]["multipleInProgress"])
        self.assertEqual(got, ["2026-01-01-a", "2026-01-02-b"], msg=f"multipleInProgress = {got}")

    def test_single_in_progress_auto_selects(self):
        repo7 = self._setup_repo("single-inprogress")
        qdir7 = repo7 / ".claude" / "cfq" / "impl"
        (qdir7 / "2026-01-01-inprog" / "done").mkdir(parents=True)
        (qdir7 / "2026-01-02-other").mkdir(parents=True)
        (qdir7 / "2026-01-01-inprog" / "01-a.md").touch()
        (qdir7 / "2026-01-01-inprog" / "done" / "00-x.md").touch()
        (qdir7 / "2026-01-02-other" / "01-b.md").touch()
        out = self.json_out(self._run_pf(str(repo7)))
        self.assertEqual(out["status"], "OK", msg=f"single-inprogress status = {out}")
        self.assertEqual(
            out["selection"]["inProgress"], "2026-01-01-inprog", msg=f"inProgress field = {out}"
        )
        self.assertEqual(
            out["batch"]["name"], "2026-01-01-inprog", msg=f"auto-selected batch = {out}"
        )

    def test_failed_attempt(self):
        repo8 = self._setup_repo("failed-attempt")
        batch = repo8 / ".claude" / "cfq" / "impl" / "2026-01-01-solo"
        batch.mkdir(parents=True)
        (batch / "01-a.md").write_text("# T\n\n## Size\n\nM\n")
        out = self.json_out(self._run_pf(str(repo8)))
        self.assertFalse(out["nextPhase"]["failedAttempt"]["found"], msg=f"no red entry yet: {out}")

        self.run_clean(
            "python3", str(self.scripts_copy / "cfq_report.py"), "append", str(batch),
            '{"phase":"01-a","status":"red","finished":"2026-01-01T00:00:00+00:00","summary":"boom",'
            '"deviations":[],"errors":["x"],"verification":"x","commit":""}',
            env={"HOME": str(self.home)},
        )
        out = self.json_out(self._run_pf(str(repo8)))
        self.assertTrue(out["nextPhase"]["failedAttempt"]["found"], msg=f"red entry not found: {out}")
        self.assertEqual(out["nextPhase"]["failedAttempt"]["note"], "boom", msg=f"failedAttempt note: {out}")

        # no report.json at all anywhere -> {"found": false}, no crash
        fresh_batch = repo8.parent / "fresh-no-report" / "2026-01-01-solo"
        fresh_batch.mkdir(parents=True)
        proc = self.run_clean(
            "python3", str(self.scripts_copy / "cfq_report.py"), "last-failure",
            str(fresh_batch), "01-a", env={"HOME": str(self.home)},
        )
        self.assertFalse(
            json.loads(proc.stdout)["found"],
            msg="last-failure on batch with no report.json should be found=false",
        )

    def test_context_gate_defaults_to_m(self):
        # contextGate defaults to M when ## Size is missing
        repo9 = self._setup_repo("no-size")
        batch = repo9 / ".claude" / "cfq" / "impl" / "2026-01-01-nosize"
        batch.mkdir(parents=True)
        (batch / "01-a.md").write_text("# T\n")
        out = self.json_out(self._run_pf(str(repo9)))
        self.assertEqual(out["contextGate"]["size"], "M", msg=f"missing ## Size should default to M: {out}")
        direct_gate = self.run_clean(
            "bash", str(self.scripts_copy / "ctx-usage.sh"), "gate", "M",
            env={"HOME": str(self.home)},
        ).stdout
        # The original Bash test only ever looked for START/HANDOFF here, silently missing WARN
        # (the verdict ctx-usage.sh returns when no statusline payload/transcript is reachable
        # under an isolated $HOME, as in this sandbox) -- broadened to all three verdicts so the
        # comparison is meaningful regardless of whether a live payload is resolvable.
        direct_verdict = next(
            (tok for tok in direct_gate.split() if tok in ("START", "HANDOFF", "WARN")), None
        )
        self.assertEqual(
            out["contextGate"]["verdict"], direct_verdict,
            msg="contextGate.verdict != ctx-usage.sh gate's own verdict",
        )

    def test_deterministic_and_read_only(self):
        repo8 = self._setup_repo("failed-attempt-2")
        batch = repo8 / ".claude" / "cfq" / "impl" / "2026-01-01-solo"
        batch.mkdir(parents=True)
        (batch / "01-a.md").write_text("# T\n\n## Size\n\nM\n")

        out1 = self._run_pf(str(repo8)).stdout
        out2 = self._run_pf(str(repo8)).stdout
        self.assertEqual(out1, out2, msg="two runs on the same fixture produced different output")

        marker = self._repos_dir / "marker"
        marker.touch()
        time.sleep(1)
        self._run_pf(str(repo8))
        changed = self.run_clean("find", str(repo8), "-newer", str(marker)).stdout
        self.assertEqual(changed, "", msg=f"run modified files under the fixture repo: {changed}")


if __name__ == "__main__":
    unittest.main()
