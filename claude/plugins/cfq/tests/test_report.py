"""Migrated from test-report.sh.

Self-test for scripts/cfq_report.py: append/set-commit/summary/html/last-failure/security plus
the index/detail surface.
"""

import json
import shutil
import subprocess
import unittest

from cfq_testlib import CFQ_BIN, CfqTestCase, PLUGIN_ROOT


class TestReport(CfqTestCase):
    def _batch(self, name):
        d = self._repos_dir / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _report_json(self, batch):
        return json.loads((batch / "report.json").read_text())

    def test_append_and_set_commit(self):
        batch = self._batch("2026-01-01-demo")

        # HOME is an empty dir so append's automatic telemetry call finds no transcript
        # (fail-soft, no-op) -- this batch stays a "report without telemetry" fixture on purpose.
        self.run_cfq(
            "report", "append", str(batch),
            '{"phase":"01-a","status":"green","finished":"2026-01-01T10:00:00+01:00","summary":"ok",'
            '"deviations":["Plan sagte X, gebaut Y"],"errors":[],"verification":"tests -> PASS","commit":"abc1234"}',
        )
        self.run_cfq(
            "report", "append", str(batch),
            '{"phase":"02-b","status":"red","finished":"2026-01-01T11:00:00+01:00","summary":"fehlgeschlagen",'
            '"deviations":[],"errors":["Verifikation rot: 1 Test <failed>"],"verification":"tests -> FAIL","commit":""}',
        )

        self.run_cfq("report", "set-commit", str(batch), "01-a", "def5678")
        c = self._report_json(batch)["phases"][0]["commit"]
        self.assertEqual(c, "def5678", f"set-commit did not update commit = {c}")

        # Unknown phase: set-commit must fail loudly instead of silently returning the file
        # unchanged. Passing the bare number where the report carries the full slug used to exit
        # 0 and leave the commit field null forever -- the whole reason this check exists.
        before = (batch / "report.json").read_text()
        proc = self.run_cfq("report", "set-commit", str(batch), "01", "9999999")
        self.assertNotEqual(proc.returncode, 0, "set-commit with an unknown phase should exit non-zero")
        self.assertTrue(len(proc.stderr) > 0, "set-commit gave no stderr message for an unknown phase")
        after = (batch / "report.json").read_text()
        self.assertEqual(before, after, "set-commit modified report.json despite an unknown phase")

        # A batch whose report.json has an empty phases array hits the same path, not a jq crash.
        emptyph = self._batch("2026-01-04-emptyphases")
        (emptyph / "report.json").write_text(
            '{"repo":"","batch":"2026-01-04-emptyphases","started":"2026-01-04T10:00:00+01:00","phases":[]}\n'
        )
        proc = self.run_cfq("report", "set-commit", str(emptyph), "01-a", "aaa1111")
        self.assertNotEqual(proc.returncode, 0, "set-commit on an empty phases array should exit non-zero")

        # The routine case still works after the change -- same phase, a second overwrite.
        self.run_cfq("report", "set-commit", str(batch), "01-a", "ccc3333")
        c = self._report_json(batch)["phases"][0]["commit"]
        self.assertEqual(c, "ccc3333", f"set-commit no longer updates a known phase = {c}")

        # append validates the phase field before anything is persisted: NN-slug only.
        badph = self._batch("2026-01-05-badphase")

        # bare number -- the batch-009 shape that started this
        proc = self.run_cfq("report", "append", str(badph), '{"phase":"01","status":"green","summary":"x"}')
        self.assertNotEqual(proc.returncode, 0, "append should reject a bare phase number")
        self.assertTrue(len(proc.stderr) > 0, "append gave no stderr message for a bare phase number")
        self.assertFalse((badph / "report.json").exists(), "append created report.json despite rejecting the phase value")

        # missing phase field entirely
        proc = self.run_cfq("report", "append", str(badph), '{"status":"green","summary":"x"}')
        self.assertNotEqual(proc.returncode, 0, "append should reject phase JSON without a phase field")

        # empty phase field
        proc = self.run_cfq("report", "append", str(badph), '{"phase":"","status":"green","summary":"x"}')
        self.assertNotEqual(proc.returncode, 0, "append should reject an empty phase field")

        # routine case still accepted, and it is what creates report.json
        self.run_cfq("report", "append", str(badph), '{"phase":"03-c","status":"green","summary":"x"}')
        self.assertEqual(
            self._report_json(badph)["phases"][-1]["phase"], "03-c",
            "append no longer accepts a well-formed phase slug",
        )

        s = self.run_cfq("report", "summary", str(batch)).stdout.rstrip("\n")
        expected = f"{batch.name}\t2\t1\t1\t1\t2026-01-01T11:00:00+01:00\t0\t0\t0\t\t"
        self.assertEqual(s, expected, f"summary = {s}")

        out = self.run_clean(str(CFQ_BIN), "report", "html", str(batch)).stdout.strip()
        self.assertEqual(out, str(batch / "report.html"), f"html path (default reportDir) = {out}")
        self.assertTrue((batch / "report.html").is_file(), "report.html not created")

        html = (batch / "report.html").read_text()
        self.assertIn("01-a", html, "report.html missing 01-a")
        self.assertIn("02-b", html, "report.html missing 02-b")
        self.assertIn("1 Test &lt;failed&gt;", html, "error text not HTML-escaped")
        self.assertEqual(html.count('class="telemetry"'), 0, "report.html has telemetry markup despite no telemetry data")

        lf = self.json_out(self.run_cfq("report", "last-failure", str(batch), "02-b"))
        self.assertTrue(lf["found"], f"last-failure should find 02-b's red entry: {lf}")
        self.assertEqual(lf["note"], "fehlgeschlagen", f"last-failure note: {lf}")

        lf = self.json_out(self.run_cfq("report", "last-failure", str(batch), "01-a"))
        self.assertFalse(lf["found"], f"01-a is green, last-failure should be false: {lf}")

        noreport = self._batch("2026-01-03-noreport")
        lf = self.json_out(self.run_cfq("report", "last-failure", str(noreport), "01-a"))
        self.assertFalse(lf["found"], f"last-failure on missing report.json should be false, not crash: {lf}")

        self.run_cfq("report", "security", str(batch), '{"status":"ok","critical":0,"high":0}')
        self.run_cfq("report", "security", str(batch), '{"status":"ok","critical":0,"high":1}')
        sec = len(self._report_json(batch)["security"])
        self.assertEqual(sec, 2, f"security snapshots = {sec}, want 2")

        # security on a fresh batch must create report.json -- this is the planning-time path,
        # where no phase has been appended yet.
        fresh = self._batch("2026-01-02-fresh")
        self.run_cfq("report", "security", str(fresh), '{"available":false,"counts":{}}')
        self.assertTrue((fresh / "report.json").exists(), "security did not create report.json")
        fresh_report = self._report_json(fresh)
        self.assertEqual(len(fresh_report["security"]), 1, f"fresh security snapshots = {len(fresh_report['security'])}, want 1")
        self.assertEqual(fresh_report["batch"], "2026-01-02-fresh", f"batch = {fresh_report['batch']}")

    def test_index_and_detail(self):
        # repo-p: alpha (open, all-green) and beta (archived, red with no later retry)
        repo_p = self._repos_dir / "repo-p"
        alpha = repo_p / ".claude" / "cfq" / "impl" / "2026-02-01-alpha"
        beta = repo_p / ".claude" / "cfq" / "impl" / "done" / "2026-02-02-beta"
        alpha.mkdir(parents=True)
        beta.mkdir(parents=True)
        (alpha / "report.json").write_text(json.dumps({
            "repo": "/repo-p", "batch": "2026-02-01-alpha", "started": "2026-02-01T09:00:00+01:00",
            "phases": [
                {"phase": "01-a", "status": "green", "finished": "2026-02-01T10:00:00+01:00", "summary": "ok",
                 "deviations": [], "errors": [], "verification": "tests -> PASS", "commit": "aaa1111"},
                {"phase": "02-b", "status": "green", "finished": "2026-02-01T11:00:00+01:00", "summary": "ok2",
                 "deviations": ["dev1"], "errors": [], "verification": "tests -> PASS", "commit": "aaa2222"},
            ],
        }))
        (beta / "report.json").write_text(json.dumps({
            "repo": "/repo-p", "batch": "2026-02-02-beta", "started": "2026-02-02T08:00:00+01:00",
            "phases": [
                {"phase": "01-a", "status": "red", "finished": "2026-02-02T09:00:00+01:00", "summary": "boom",
                 "deviations": [], "errors": ["stacktrace line"], "verification": "tests -> FAIL", "commit": ""},
            ],
        }))

        # repo-q: gamma (open, phase 01-a red then retried green -> MIXED)
        repo_q = self._repos_dir / "repo-q"
        gamma = repo_q / ".claude" / "cfq" / "impl" / "2026-02-03-gamma"
        gamma.mkdir(parents=True)
        (gamma / "report.json").write_text(json.dumps({
            "repo": "/repo-q", "batch": "2026-02-03-gamma", "started": "2026-02-03T08:00:00+01:00",
            "phases": [
                {"phase": "01-a", "status": "red", "finished": "2026-02-03T09:00:00+01:00", "summary": "first try failed",
                 "deviations": [], "errors": ["err"], "verification": "FAIL", "commit": ""},
                {"phase": "01-a", "status": "green", "finished": "2026-02-03T10:00:00+01:00", "summary": "fixed",
                 "deviations": [], "errors": [], "verification": "PASS", "commit": "ccc3333"},
            ],
        }))

        # Call-counting stub for the N+1-regression guard: index must call cfq-scan.sh exactly
        # once, regardless of how many report-bearing batches exist. cfq_report.py resolves its
        # own SCRIPT_DIR from its own path, so the stub has to be invoked directly by path --
        # this is the one case in this file that cannot go through bin/cfq, since the dispatcher
        # would always exec the real, unstubbed scripts/ directory.
        scan_calls = self._repos_dir / "scan-call-count"
        stub_dir = self._repos_dir / "stub-scripts"
        stub_dir.mkdir()
        scripts_dir = PLUGIN_ROOT / "scripts"
        (stub_dir / "cfq_report.py").write_bytes((scripts_dir / "cfq_report.py").read_bytes())
        shutil.copytree(scripts_dir / "cfq_lib", stub_dir / "cfq_lib")
        scan_calls.write_text("")
        stub_scan = stub_dir / "cfq-scan.sh"
        stub_scan.write_text(f"""#!/usr/bin/env bash
echo x >>"{scan_calls}"
exec bash "{scripts_dir / 'cfq-scan.sh'}" "$@"
""")
        stub_scan.chmod(0o755)

        # Direct invocation of the stubbed copy, matching the Bash original.
        proc = subprocess.run(
            ["python3", str(stub_dir / "cfq_report.py"), "index"],
            capture_output=True, text=True,
            env={**self._base_env(), "HOME": str(self.home), "CFQ_SCAN_ROOTS": str(self._repos_dir)},
        )
        idx = self.json_out(proc)
        self.assertEqual(len(idx), 3, f"index (no filter) length = {len(idx)}")

        calls = len(scan_calls.read_text().splitlines())
        self.assertEqual(calls, 1, f"index should call cfq-scan.sh exactly once, got {calls}")

        st_alpha = next(e["status"] for e in idx if e["batch"] == "2026-02-01-alpha")
        self.assertEqual(st_alpha, "GREEN", f"alpha status = {st_alpha}")
        st_beta = next(e["status"] for e in idx if e["batch"] == "2026-02-02-beta")
        self.assertEqual(st_beta, "RED", f"beta status = {st_beta}")
        st_gamma = next(e["status"] for e in idx if e["batch"] == "2026-02-03-gamma")
        self.assertEqual(st_gamma, "MIXED", f"gamma status = {st_gamma}")

        dev_alpha = next(e["deviations"] for e in idx if e["batch"] == "2026-02-01-alpha")
        self.assertEqual(dev_alpha, 1, f"alpha deviations = {dev_alpha}")

        first = idx[0]["batch"]
        self.assertEqual(first, "2026-02-03-gamma", f"index should sort newest-first, got {first} first")

        idx_repo = self.json_out(self.run_cfq(
            "report", "index", "--repo", "repo-q", env={"CFQ_SCAN_ROOTS": str(self._repos_dir)},
        ))
        self.assertEqual(len(idx_repo), 1, f"--repo filter did not narrow to 1: {idx_repo}")
        self.assertEqual(idx_repo[0]["batch"], "2026-02-03-gamma", f"--repo filter wrong batch: {idx_repo}")

        idx_batch = self.json_out(self.run_cfq(
            "report", "index", "--batch", "alpha", env={"CFQ_SCAN_ROOTS": str(self._repos_dir)},
        ))
        self.assertEqual(len(idx_batch), 1, f"--batch filter did not narrow to 1: {idx_batch}")
        self.assertEqual(idx_batch[0]["batch"], "2026-02-01-alpha", f"--batch filter wrong batch: {idx_batch}")

        # --any matches either field, deduped, no separate merge on the caller's side
        idx_any = self.json_out(self.run_cfq(
            "report", "index", "--any", "repo-q", env={"CFQ_SCAN_ROOTS": str(self._repos_dir)},
        ))
        self.assertEqual(len(idx_any), 1, f"--any filter did not narrow to 1: {idx_any}")

        # --text is additive, not a substitute: same fixture, JSON above is untouched, and
        # RED/MIXED show up visibly marked. Deep render coverage (zero-cost dash, no-HTML-entity)
        # lives in test_render.py, not duplicated here.
        idx_text = self.run_cfq(
            "report", "index", "--text", env={"CFQ_SCAN_ROOTS": str(self._repos_dir)},
        ).stdout
        self.assertIn("**RED**", idx_text, "--text missing marked RED row")
        self.assertIn("**MIXED**", idx_text, "--text missing marked MIXED row")

        # detail on a batch with no report.json -> clear not-found result, no crash
        noreport_batch = repo_p / ".claude" / "cfq" / "impl" / "nope"
        noreport_batch.mkdir(parents=True)
        det_missing = self.json_out(self.run_cfq("report", "detail", str(noreport_batch)))
        self.assertFalse(det_missing["found"], f"detail on missing report.json should be found:false: {det_missing}")

        # detail on gamma: overall status MIXED, both attempts present
        det = self.json_out(self.run_cfq("report", "detail", str(gamma)))
        self.assertTrue(det["found"], "detail found = false for gamma")
        self.assertEqual(det["status"], "MIXED", f"detail status = {det['status']}")
        self.assertEqual(len(det["phases"]), 2, "detail phases length != 2")

        # detail's verification-excerpt bound: a long verification log must not be dumped in full
        long_batch = repo_p / ".claude" / "cfq" / "impl" / "2026-02-04-longlog"
        long_batch.mkdir(parents=True)
        long_verification = "\n".join(f"line {i}" for i in range(200))
        (long_batch / "report.json").write_text(json.dumps({
            "repo": "/repo-p", "batch": "2026-02-04-longlog", "started": "2026-02-04T08:00:00+01:00",
            "phases": [
                {"phase": "01-a", "status": "green", "finished": "2026-02-04T09:00:00+01:00", "summary": "ok",
                 "deviations": [], "errors": [], "verification": long_verification, "commit": "ddd4444"},
            ],
        }))
        det_long = self.json_out(self.run_cfq("report", "detail", str(long_batch)))
        vlines = det_long["phases"][0]["verification"].count("\n")
        self.assertLess(vlines, 200, f"detail should bound verification output, got {vlines} lines")

    def test_html_report_dir_and_index(self):
        rd = self._repos_dir / "reportdir"
        rd.mkdir()
        repo_x = self._repos_dir / "repo-x"
        batch_x_name = "2026-03-01-goaltest"
        batch_x = repo_x / ".claude" / "cfq" / "impl" / "done" / batch_x_name
        (batch_x / "done").mkdir(parents=True)
        (batch_x / "done" / "01-a.md").write_text("""# A phase

## Context

This phase adds the goal-extraction test.
It covers the two-line context excerpt.

## Size

M
""")
        self.run_cfq(
            "report", "append", str(batch_x),
            '{"phase":"01-a","status":"green","finished":"2026-03-01T10:00:00+01:00","summary":"ok",'
            '"deviations":[],"errors":[],"verification":"tests -> PASS","commit":"eee5555"}',
        )

        env = {"CFQ_REPORT_DIR": str(rd), "CFQ_SCAN_ROOTS": str(self._repos_dir)}
        out_x = self.run_cfq("report", "html", str(batch_x), env=env).stdout.strip()
        self.assertEqual(out_x, str(rd / "repo-x" / f"{batch_x_name}.html"), f"reportDir html path = {out_x}")
        self.assertTrue((rd / "repo-x" / f"{batch_x_name}.html").is_file(), f"collected report.html not created: {out_x}")
        self.assertTrue((rd / "index.html").is_file(), "index.html not created")
        html_x = (rd / "repo-x" / f"{batch_x_name}.html").read_text()
        self.assertIn("This phase adds the goal-extraction test", html_x, "report.html missing phase goal text")
        index_html = (rd / "index.html").read_text()
        self.assertIn(batch_x_name, index_html, f"index.html missing batch {batch_x_name}")
        n_links = index_html.count("<a href=")
        self.assertGreaterEqual(n_links, 1, f"index.html has no links: {n_links}")

        # edge: batch whose phase file no longer exists -> phase still renders, goal omitted, no crash
        batch_y_name = "2026-03-02-nogoal"
        batch_y = repo_x / ".claude" / "cfq" / "impl" / "done" / batch_y_name
        (batch_y / "done").mkdir(parents=True)
        self.run_cfq(
            "report", "append", str(batch_y),
            '{"phase":"01-a","status":"green","finished":"2026-03-02T10:00:00+01:00","summary":"ok",'
            '"deviations":[],"errors":[],"verification":"tests -> PASS","commit":"fff6666"}',
        )
        out_y = self.run_cfq("report", "html", str(batch_y), env=env).stdout.strip()
        self.assertTrue(len(out_y) > 0, f"html for missing-plan-file batch not created: {out_y}")
        html_y = (rd / "repo-x" / f"{batch_y_name}.html").read_text()
        self.assertNotIn('class="goal"', html_y, "goal markup present despite no plan file")

        # edge: called twice -> overwritten not duplicated, index still lists the batch once
        out_x2 = self.run_cfq("report", "html", str(batch_x), env=env).stdout.strip()
        self.assertEqual(out_x2, out_x, f"second html call path differs: {out_x2}")
        count_x = len(list((rd / "repo-x").glob(f"{batch_x_name}.html")))
        self.assertEqual(count_x, 1, f"duplicate html file for batch_x: {count_x}")
        index_html = (rd / "index.html").read_text()
        href_count = index_html.count(f"repo-x/{batch_x_name}.html")
        self.assertEqual(href_count, 1, f"index.html links batch_x more than once: {href_count}")

        # must-fall-back: reportDir pointing at a path that cannot be created -> non-zero exit,
        # stderr message, batch-directory file NOT silently written instead
        robase = self._repos_dir / "readonly-parent"
        robase.mkdir()
        badrd = robase / "reports"
        robase.chmod(0o555)
        try:
            proc = self.run_cfq(
                "report", "html", str(batch_x),
                env={"CFQ_REPORT_DIR": str(badrd), "CFQ_SCAN_ROOTS": str(self._repos_dir)},
            )
        finally:
            robase.chmod(0o755)
        self.assertNotEqual(proc.returncode, 0, "html should fail when reportDir cannot be created")
        self.assertTrue(len(proc.stderr) > 0, "no stderr message on reportDir mkdir failure")
        self.assertFalse(
            (batch_x / "report.html").exists(), "fell back to writing batch-dir report.html on reportDir failure",
        )


if __name__ == "__main__":
    unittest.main()
