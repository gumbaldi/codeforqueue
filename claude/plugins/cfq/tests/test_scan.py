"""Migrated from test-scan.sh (bin/cfq scan — cross-repo queue discovery)."""

import json
import os
import time
import unittest

from cfq_testlib import CfqTestCase


class ScanTest(CfqTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = self._repos_dir

    def _scan(self, *args):
        proc = self.run_cfq("scan", *args, env={"CFQ_SCAN_ROOTS": str(self.tmp)})
        return proc.stdout

    def test_scan_fixtures(self):
        tmp = self.tmp

        # repo-a: one open batch, 2 open phases + 1 done phase, priority high
        demo = tmp / "repo-a" / ".claude" / "cfq" / "impl" / "2026-01-01-demo"
        (demo / "done").mkdir(parents=True)
        (demo / ".priority").write_text("high")
        (demo / "01-a.md").touch()
        (demo / "02-b.md").touch()
        (demo / "done" / "00-x.md").touch()
        # a stray dotfile in the batch root must never be counted as an open phase (regression test)
        (demo / ".batch-context.md").touch()

        # repo-b: one batch fully moved to done/ (archived), no .priority
        archived = tmp / "repo-b" / ".claude" / "cfq" / "impl" / "done" / "2026-01-02-demo"
        archived.mkdir(parents=True)
        (archived / "01-a.md").touch()
        (archived / "02-b.md").touch()
        (archived / ".batch-context.md").touch()

        # repo-c: no .claude/cfq/impl/ at all — must not show up
        (tmp / "repo-c").mkdir(parents=True)

        # repo-a has a report.json — must not affect phase counts, only the report flag
        (demo / "report.json").write_text(
            json.dumps({"repo": "x", "batch": "2026-01-01-demo", "started": "t", "phases": []})
        )

        # repo-e: dependsOn fixtures — target-open (still open) and target-done (archived) are
        # the dependency targets; b-blocked/b-free/b-unknown are the batches exercising each
        # outcome.
        e_impl = tmp / "repo-e" / ".claude" / "cfq" / "impl"
        (e_impl / "2026-01-10-target-open").mkdir(parents=True)
        (e_impl / "done" / "2026-01-10-target-done").mkdir(parents=True)
        (e_impl / "2026-01-10-b-blocked").mkdir(parents=True)
        (e_impl / "2026-01-10-b-free").mkdir(parents=True)
        (e_impl / "2026-01-10-b-unknown").mkdir(parents=True)
        (e_impl / "2026-01-10-target-open" / "01-a.md").touch()
        (e_impl / "done" / "2026-01-10-target-done" / "01-a.md").touch()
        (e_impl / "2026-01-10-b-blocked" / "01-a.md").touch()
        (e_impl / "2026-01-10-b-free" / "01-a.md").touch()
        (e_impl / "2026-01-10-b-unknown" / "01-a.md").touch()
        (e_impl / "2026-01-10-b-blocked" / ".dependsOn").write_text("2026-01-10-target-open\n")
        (e_impl / "2026-01-10-b-free" / ".dependsOn").write_text("2026-01-10-target-done\n")
        (e_impl / "2026-01-10-b-unknown" / ".dependsOn").write_text("gibtsnicht\n")

        # repo-f: no impl/ batches at all, only plan/ and todo/ orders — plan and todo must
        # count only the open entries (2 and 1), never the ones already moved to their done/
        f_cfq = tmp / "repo-f" / ".claude" / "cfq"
        (f_cfq / "plan" / "done").mkdir(parents=True)
        (f_cfq / "todo" / "done").mkdir(parents=True)
        (f_cfq / "plan" / "2026-01-04-a.md").touch()
        (f_cfq / "plan" / "2026-01-05-b.md").touch()
        (f_cfq / "plan" / "done" / "2026-01-01-old.md").touch()
        (f_cfq / "todo" / "2026-01-06-c.md").touch()
        (f_cfq / "todo" / "done" / "2026-01-02-old.md").touch()

        # repo-g: .planning fixtures — b-fresh has a just-written marker (planning:true),
        # b-stale has one backdated past the 30-minute staleness threshold (planning:false)
        g_impl = tmp / "repo-g" / ".claude" / "cfq" / "impl"
        (g_impl / "2026-01-11-b-fresh").mkdir(parents=True)
        (g_impl / "2026-01-11-b-stale").mkdir(parents=True)
        (g_impl / "2026-01-11-b-fresh" / "01-a.md").touch()
        (g_impl / "2026-01-11-b-stale" / "01-a.md").touch()
        (g_impl / "2026-01-11-b-fresh" / ".planning").write_text("2026-01-01T00:00:00+00:00\n")
        stale_marker = g_impl / "2026-01-11-b-stale" / ".planning"
        stale_marker.touch()
        stale_time = time.time() - 3600
        os.utime(stale_marker, (stale_time, stale_time))

        # repo-h: stale pre-476aa60 .priority value — must not crash the scan, must read back
        # empty
        h_batch = tmp / "repo-h" / ".claude" / "cfq" / "impl" / "2026-01-12-legacy"
        h_batch.mkdir(parents=True)
        (h_batch / "01-a.md").touch()
        (h_batch / ".priority").write_text("medium")

        # repo-i: a foreign directory under impl/ (not a YYYY-MM-DD-slug batch) must not be
        # collected
        i_impl = tmp / "repo-i" / ".claude" / "cfq" / "impl"
        (i_impl / "todo").mkdir(parents=True)
        (i_impl / "2026-01-13-real-batch").mkdir(parents=True)
        (i_impl / "todo" / "leftover.md").touch()
        (i_impl / "2026-01-13-real-batch" / "01-a.md").touch()

        # repo-j: numbered-format batch dir (new naming, digits precede the date) must be
        # found too
        j_batch = tmp / "repo-j" / ".claude" / "cfq" / "impl" / "001-2026-01-14-numbered"
        j_batch.mkdir(parents=True)
        (j_batch / "01-a.md").touch()

        out = self._scan()
        data = json.loads(out)

        def repo_entry(path):
            return next(r for r in data["repos"] if r["path"] == str(tmp / path))

        a_batches = repo_entry("repo-a")["batches"]
        self.assertEqual(
            a_batches,
            [
                {
                    "name": "2026-01-01-demo",
                    "priority": "high",
                    "open": 2,
                    "done": 1,
                    "archived": False,
                    "report": True,
                    "dependsOn": [],
                    "blocked": False,
                    "unknownDeps": [],
                    "inProgress": True,
                    "planning": False,
                }
            ],
            msg=f"repo-a batches = {a_batches}",
        )

        b_batches = repo_entry("repo-b")["batches"]
        self.assertEqual(
            b_batches,
            [
                {
                    "name": "2026-01-02-demo",
                    "priority": "",
                    "open": 0,
                    "done": 2,
                    "archived": True,
                    "report": False,
                    "dependsOn": [],
                    "blocked": False,
                    "unknownDeps": [],
                    "inProgress": False,
                    "planning": False,
                }
            ],
            msg=f"repo-b batches (unflagged -> empty priority) = {b_batches}",
        )

        c_entries = [r for r in data["repos"] if r["path"] == str(tmp / "repo-c")]
        self.assertEqual(c_entries, [], msg=f"repo-c should not appear, got {c_entries}")

        e_batches = {b["name"]: b for b in repo_entry("repo-e")["batches"]}
        e_blocked = {
            "blocked": e_batches["2026-01-10-b-blocked"]["blocked"],
            "dependsOn": e_batches["2026-01-10-b-blocked"]["dependsOn"],
        }
        self.assertEqual(
            e_blocked,
            {"blocked": True, "dependsOn": ["2026-01-10-target-open"]},
            msg=f"b-blocked = {e_blocked}",
        )
        e_free = {
            "blocked": e_batches["2026-01-10-b-free"]["blocked"],
            "dependsOn": e_batches["2026-01-10-b-free"]["dependsOn"],
        }
        self.assertEqual(
            e_free,
            {"blocked": False, "dependsOn": ["2026-01-10-target-done"]},
            msg=f"b-free = {e_free}",
        )
        e_unknown = {
            "blocked": e_batches["2026-01-10-b-unknown"]["blocked"],
            "unknownDeps": e_batches["2026-01-10-b-unknown"]["unknownDeps"],
        }
        self.assertEqual(
            e_unknown,
            {"blocked": False, "unknownDeps": ["gibtsnicht"]},
            msg=f"b-unknown = {e_unknown}",
        )

        f_entry = repo_entry("repo-f")
        f = {"plan": f_entry["plan"], "todo": f_entry["todo"], "batches": f_entry["batches"]}
        self.assertEqual(f, {"plan": 2, "todo": 1, "batches": []}, msg=f"repo-f plan/todo counts = {f}")

        g_batches = {b["name"]: b for b in repo_entry("repo-g")["batches"]}
        self.assertTrue(
            g_batches["2026-01-11-b-fresh"]["planning"],
            msg=f"b-fresh planning = {g_batches['2026-01-11-b-fresh']['planning']}",
        )
        self.assertFalse(
            g_batches["2026-01-11-b-stale"]["planning"],
            msg=f"b-stale planning = {g_batches['2026-01-11-b-stale']['planning']}",
        )

        h_priority = repo_entry("repo-h")["batches"][0]["priority"]
        self.assertEqual(h_priority, "", msg=f"repo-h legacy priority should read back empty, got {h_priority!r}")

        i_names = [b["name"] for b in repo_entry("repo-i")["batches"]]
        self.assertEqual(
            i_names, ["2026-01-13-real-batch"],
            msg=f"repo-i batches should exclude non-date-prefixed dirs, got {i_names}",
        )

        j_names = [b["name"] for b in repo_entry("repo-j")["batches"]]
        self.assertEqual(
            j_names, ["001-2026-01-14-numbered"],
            msg=f"repo-j numbered-format batch not found, got {j_names}",
        )

        # --format=json (explicit) must be byte-identical to the no-flag default — no
        # regression for existing callers.
        out_json_flag = self._scan("--format=json")
        self.assertEqual(out_json_flag, out, msg="--format=json must be byte-identical to no-flag output")

        # --format=md: valid Markdown table, one row per batch, Status reflects
        # blocked/planning/inProgress.
        out_md = self._scan("--format=md")
        md_lines = out_md.splitlines()
        md_header = md_lines[0]
        self.assertEqual(
            md_header, "| Repo | Batch | Priority | Open/Done | Status |", msg=f"md header = {md_header}"
        )
        md_rows = len(md_lines) - 2
        total_batches = sum(len(r["batches"]) for r in data["repos"])
        self.assertEqual(md_rows, total_batches, msg=f"md row count {md_rows} != total batches {total_batches}")

        def md_status(needle):
            for line in md_lines:
                if needle in line:
                    return line.split("|")[5].strip()
            return None

        self.assertEqual(
            md_status("2026-01-01-demo"), "IN_PROGRESS",
            msg="md status for repo-a's open+priority batch",
        )
        self.assertEqual(md_status("2026-01-10-b-blocked"), "BLOCKED", msg="md status for b-blocked")
        self.assertEqual(md_status("2026-01-11-b-fresh"), "PLANNING", msg="md status for b-fresh")

        # --format=tsv: same field set as md, tab-separated, one line per batch (no header).
        out_tsv = self._scan("--format=tsv")
        tsv_lines = out_tsv.splitlines()
        self.assertEqual(
            len(tsv_lines), total_batches, msg=f"tsv row count {len(tsv_lines)} != total batches {total_batches}"
        )
        tsv_fields = next(len(line.split("\t")) for line in tsv_lines if "2026-01-01-demo" in line)
        self.assertEqual(tsv_fields, 5, msg=f"tsv field count = {tsv_fields}")

        # --format=overview: one row per repo, Batches is open/done batch counts (not phase
        # counts), Status the most severe status among that repo's own batches.
        out_overview = self._scan("--format=overview")
        ov_lines = out_overview.splitlines()
        ov_header = ov_lines[0]
        self.assertEqual(
            ov_header, "| Repo | Plan | Todo | Batches | Status |", msg=f"overview header = {ov_header}"
        )
        ov_repo_a = next((line for line in ov_lines if line.startswith("| repo-a ")), None)
        self.assertEqual(
            ov_repo_a, "| repo-a | 0 | 0 | 1/0 | IN_PROGRESS |", msg=f"overview repo-a row = {ov_repo_a}"
        )
        ov_repo_f = next((line for line in ov_lines if line.startswith("| repo-f ")), None)
        self.assertEqual(ov_repo_f, "| repo-f | 2 | 1 | 0/0 | OK |", msg=f"overview repo-f row = {ov_repo_f}")

        # Unknown --format value: clear error, exit 1.
        proc = self.run_cfq("scan", "--format=bogus", env={"CFQ_SCAN_ROOTS": str(tmp)})
        self.assertNotEqual(proc.returncode, 0, "unknown --format value should exit non-zero")
        self.assertIn(
            "unknown --format value", proc.stderr, "unknown --format value should print a clear error"
        )

    def test_registry_entry_with_missing_repo_path_does_not_crash(self):
        # A repo the registry still knows about, but whose path is gone -- must not crash the
        # scan and must not surface in the output (its .claude/cfq dir cannot be read either).
        missing = str(self.tmp / "repo-vanished")
        self.run_cfq("registry", "add", missing, check=True)
        proc = self.run_cfq("scan", env={"CFQ_SCAN_ROOTS": "/nonexistent-scan-root-xyz"})
        self.assertEqual(proc.returncode, 0, msg=f"scan should exit 0, stderr={proc.stderr}")
        data = json.loads(proc.stdout)
        self.assertNotIn(
            missing, [r["path"] for r in data["repos"]],
            msg="a repo with a missing path must not appear in the scan output",
        )

    def test_nonexistent_scan_root_does_not_crash(self):
        # CFQ_SCAN_ROOTS pointing at a directory that doesn't exist at all -- still a valid,
        # empty-ish result, never a crash.
        proc = self.run_cfq("scan", env={"CFQ_SCAN_ROOTS": "/nonexistent-scan-root-xyz"})
        self.assertEqual(proc.returncode, 0, msg=f"scan should exit 0, stderr={proc.stderr}")
        data = json.loads(proc.stdout)
        self.assertEqual(data.get("repos"), [], msg=f"expected no repos, got {data}")


class ScanNextTest(CfqTestCase):
    """`bin/cfq scan --format=next` — the ranking `/ifq` would pick, moved out of
    cfq_ifq_preflight.py so `/cfq`'s dashboard can show the same answer."""

    def setUp(self):
        super().setUp()
        self.tmp = self._repos_dir

    def _scan_next(self):
        proc = self.run_cfq("scan", "--format=next", env={"CFQ_SCAN_ROOTS": str(self.tmp)})
        return json.loads(proc.stdout)

    def _repo(self, data, name):
        return next(r for r in data["repos"] if r["path"] == str(self.tmp / name))

    def test_routine_lower_numbered_wins(self):
        impl = self.tmp / "repo-routine" / ".claude" / "cfq" / "impl"
        (impl / "2026-02-01-alpha").mkdir(parents=True)
        (impl / "2026-02-02-beta").mkdir(parents=True)
        (impl / "2026-02-01-alpha" / "01-a.md").touch()
        (impl / "2026-02-02-beta" / "01-b.md").touch()

        r = self._repo(self._scan_next(), "repo-routine")
        self.assertEqual(r["next"], "2026-02-01-alpha", msg=r)
        self.assertEqual(r["reason"], "order", msg=r)
        self.assertEqual(r["blocked"], [], msg=r)
        self.assertEqual(r["planning"], [], msg=r)

    def test_priority_flagged_wins(self):
        impl = self.tmp / "repo-priority" / ".claude" / "cfq" / "impl"
        (impl / "2026-02-01-alpha").mkdir(parents=True)
        (impl / "2026-02-02-beta").mkdir(parents=True)
        (impl / "2026-02-01-alpha" / "01-a.md").touch()
        (impl / "2026-02-02-beta" / "01-b.md").touch()
        (impl / "2026-02-02-beta" / ".priority").write_text("high")

        r = self._repo(self._scan_next(), "repo-priority")
        self.assertEqual(r["next"], "2026-02-02-beta", msg=r)
        self.assertEqual(r["reason"], "priority", msg=r)

    def test_in_progress_wins_over_flagged(self):
        impl = self.tmp / "repo-inprogress" / ".claude" / "cfq" / "impl"
        (impl / "2026-02-01-running" / "done").mkdir(parents=True)
        (impl / "2026-02-01-running" / "01-a.md").touch()
        (impl / "2026-02-01-running" / "done" / "00-x.md").touch()
        (impl / "2026-02-02-flagged").mkdir(parents=True)
        (impl / "2026-02-02-flagged" / "01-a.md").touch()
        (impl / "2026-02-02-flagged" / ".priority").write_text("high")

        r = self._repo(self._scan_next(), "repo-inprogress")
        self.assertEqual(r["next"], "2026-02-01-running", msg=r)
        self.assertEqual(r["reason"], "inProgress", msg=r)

    def test_blocked_batch_yields_no_next(self):
        impl = self.tmp / "repo-blocked" / ".claude" / "cfq" / "impl"
        # target exists under impl/ (so the dependency is "not finished") but has no open
        # phases of its own, so it never competes as a candidate itself.
        (impl / "2026-02-01-target").mkdir(parents=True)
        (impl / "2026-02-02-b-blocked").mkdir(parents=True)
        (impl / "2026-02-02-b-blocked" / "01-a.md").touch()
        (impl / "2026-02-02-b-blocked" / ".dependsOn").write_text("2026-02-01-target\n")

        r = self._repo(self._scan_next(), "repo-blocked")
        self.assertIsNone(r["next"], msg=r)
        self.assertEqual(r["blocked"], ["2026-02-02-b-blocked"], msg=r)

    def test_planning_batch_yields_no_next(self):
        impl = self.tmp / "repo-planning" / ".claude" / "cfq" / "impl"
        (impl / "2026-02-01-fresh").mkdir(parents=True)
        (impl / "2026-02-01-fresh" / "01-a.md").touch()
        (impl / "2026-02-01-fresh" / ".planning").touch()

        r = self._repo(self._scan_next(), "repo-planning")
        self.assertIsNone(r["next"], msg=r)
        self.assertEqual(r["planning"], ["2026-02-01-fresh"], msg=r)

    def test_stale_planning_is_selectable_again(self):
        impl = self.tmp / "repo-stale-planning" / ".claude" / "cfq" / "impl"
        (impl / "2026-02-01-stale").mkdir(parents=True)
        (impl / "2026-02-01-stale" / "01-a.md").touch()
        marker = impl / "2026-02-01-stale" / ".planning"
        marker.touch()
        stale_time = time.time() - 3600
        os.utime(marker, (stale_time, stale_time))

        r = self._repo(self._scan_next(), "repo-stale-planning")
        self.assertEqual(r["next"], "2026-02-01-stale", msg=r)
        self.assertEqual(r["planning"], [], msg=r)

    def test_archived_only_yields_no_next(self):
        archived = self.tmp / "repo-archived-only" / ".claude" / "cfq" / "impl" / "done" / "2026-02-01-old"
        archived.mkdir(parents=True)
        (archived / "01-a.md").touch()

        r = self._repo(self._scan_next(), "repo-archived-only")
        self.assertIsNone(r["next"], msg=r)
        self.assertEqual(r["reason"], None, msg=r)
        self.assertEqual(r["blocked"], [], msg=r)
        self.assertEqual(r["planning"], [], msg=r)

    def test_several_repos_have_independent_winners(self):
        impl1 = self.tmp / "repo-multi-1" / ".claude" / "cfq" / "impl"
        (impl1 / "2026-02-01-alpha").mkdir(parents=True)
        (impl1 / "2026-02-02-beta").mkdir(parents=True)
        (impl1 / "2026-02-01-alpha" / "01-a.md").touch()
        (impl1 / "2026-02-02-beta" / "01-b.md").touch()

        impl2 = self.tmp / "repo-multi-2" / ".claude" / "cfq" / "impl"
        (impl2 / "2026-02-01-alpha").mkdir(parents=True)
        (impl2 / "2026-02-02-beta").mkdir(parents=True)
        (impl2 / "2026-02-01-alpha" / "01-a.md").touch()
        (impl2 / "2026-02-02-beta" / "01-b.md").touch()
        (impl2 / "2026-02-02-beta" / ".priority").write_text("high")

        data = self._scan_next()
        self.assertEqual(self._repo(data, "repo-multi-1")["next"], "2026-02-01-alpha", msg=data)
        self.assertEqual(self._repo(data, "repo-multi-2")["next"], "2026-02-02-beta", msg=data)


if __name__ == "__main__":
    unittest.main()
