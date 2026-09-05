"""Migrated from test-render.sh.

Self-test for the render modes: scripts/cfq-dash.sh's `render` mode and
scripts/cfq_report.py's `index --text` mode. Both are additive terminal renders over the same
JSON the default invocation returns -- this file pins that one-code-path guarantee explicitly.
"""

import subprocess
import unittest

from cfq_testlib import CfqTestCase


class TestRender(CfqTestCase):
    def _plain_repo(self, path):
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)

    # --- fixture builders, each returns the rendered text; shared by their own test and the
    # no-HTML-entity sweep below ---

    def _dash_two_repos_json_and_text(self):
        tmp = self._repos_dir / "dashroot"
        repo_a = tmp / "repo-a"
        (repo_a / ".claude" / "cfq" / "impl" / "2026-01-01-demo" / "done").mkdir(parents=True)
        self._plain_repo(repo_a)
        (repo_a / ".claude" / "cfq" / "impl" / "2026-01-01-demo" / "01-a.md").touch()
        (repo_a / ".claude" / "cfq" / "impl" / "2026-01-01-demo" / "done" / "00-x.md").touch()
        repo_b = tmp / "repo-b"
        (repo_b / ".claude" / "cfq").mkdir(parents=True)
        self._plain_repo(repo_b)

        env = {"CFQ_SCAN_ROOTS": str(tmp)}
        json_out = self.json_out(self.run_cfq("dash", str(tmp), env=env))
        text = self.run_cfq("dash", "render", str(tmp), env=env).stdout
        return json_out, text

    def _dash_render_empty_registry(self):
        # Fresh HOME, not self.home -- cfq-scan.sh registers every scanned repo into the
        # registry, so reusing self.home after another fixture's dash call would still find those
        # repos via the registry union, regardless of CFQ_SCAN_ROOTS.
        empty_home = self._repos_dir / "empty-registry-home"
        empty_home.mkdir()
        empty_root = self._repos_dir / "empty-root"
        empty_root.mkdir()
        return self.run_cfq(
            "dash", "render", str(empty_root), home=empty_home, env={"CFQ_SCAN_ROOTS": str(empty_root)},
        ).stdout

    def _dash_render_multiple_in_progress(self):
        mip_home = self._repos_dir / "mip-home"
        mip_home.mkdir()
        mip_tmp = self._repos_dir / "mip-root"
        mip_repo = mip_tmp / "repo-mip"
        for name in ("2026-01-01-a", "2026-01-02-b"):
            (mip_repo / ".claude" / "cfq" / "impl" / name / "done").mkdir(parents=True)
            (mip_repo / ".claude" / "cfq" / "impl" / name / "01-a.md").touch()
            (mip_repo / ".claude" / "cfq" / "impl" / name / "done" / "00-x.md").touch()
        self._plain_repo(mip_repo)
        return self.run_cfq(
            "dash", "render", str(mip_repo), home=mip_home, env={"CFQ_SCAN_ROOTS": str(mip_tmp)},
        ).stdout

    def _dash_render_ponytail_full(self):
        pony_tmp = self._repos_dir / "pony-root"
        pony_home = self._repos_dir / "pony-home"
        (pony_home / ".claude" / "plugins" / "cache" / "ponytail" / "ponytail" / "4.8.4").mkdir(parents=True)
        repo_p = pony_tmp / "repo-p"
        (repo_p / ".claude" / "cfq").mkdir(parents=True)
        self._plain_repo(repo_p)
        return self.run_cfq(
            "dash", "render", str(pony_tmp), home=pony_home, env={"CFQ_SCAN_ROOTS": str(pony_tmp)},
        ).stdout

    def _dash_render_ponytail_off(self):
        pony_off_tmp = self._repos_dir / "pony-off-root"
        pony_off_home = self._repos_dir / "pony-off-home"
        (pony_off_home / ".claude" / "plugins" / "cache" / "ponytail" / "ponytail" / "4.8.4").mkdir(parents=True)
        (pony_off_home / ".config" / "ponytail").mkdir(parents=True)
        (pony_off_home / ".config" / "ponytail" / "config.json").write_text('{"defaultMode":"off"}')
        repo_q = pony_off_tmp / "repo-q"
        (repo_q / ".claude" / "cfq").mkdir(parents=True)
        self._plain_repo(repo_q)
        return self.run_cfq(
            "dash", "render", str(pony_off_tmp), home=pony_off_home, env={"CFQ_SCAN_ROOTS": str(pony_off_tmp)},
        ).stdout

    def _report_index_text_red_row(self):
        rep_home = self._repos_dir / "rep-home"
        rep_home.mkdir()
        rep_tmp = self._repos_dir / "rep-root"
        rep_batch = rep_tmp / "repo-r" / ".claude" / "cfq" / "impl" / "2026-01-01-demo"
        rep_batch.mkdir(parents=True)
        self._plain_repo(rep_tmp / "repo-r")
        self.run_cfq(
            "report", "append", str(rep_batch),
            '{"phase":"01-a","status":"red","finished":"2026-01-01T10:00:00+01:00","summary":"boom",'
            '"deviations":[],"errors":["x"],"verification":"FAIL","commit":""}',
            home=rep_home,
        )
        env = {"CFQ_SCAN_ROOTS": str(rep_tmp)}
        rep_json = self.json_out(self.run_cfq("report", "index", home=rep_home, env=env))
        rep_text = self.run_cfq("report", "index", "--text", home=rep_home, env=env).stdout
        return rep_json, rep_text

    def _report_index_text_empty(self):
        empty_rep_home = self._repos_dir / "empty-rep-home"
        empty_rep_home.mkdir()
        empty_rep_root = self._repos_dir / "empty-rep-root"
        empty_rep_root.mkdir()
        return self.run_cfq(
            "report", "index", "--text", home=empty_rep_home, env={"CFQ_SCAN_ROOTS": str(empty_rep_root)},
        ).stdout

    # --- tests ---

    def test_dash_json_and_text_agree_on_repo_count(self):
        json_out, text = self._dash_two_repos_json_and_text()

        with self.subTest(mode="text"):
            self.assertIn(
                "| repo-a | 0 | 0 | 1/0 |", text, f"repo-a row missing/wrong in QUEUES:\n{text}",
            )
            self.assertIn(
                "| repo-b | 0 | 0 | 0/0 | OK |", text, f"repo-b row missing/wrong in QUEUES:\n{text}",
            )

        # Equivalence: render and the JSON default agree on repo count for the same fixture.
        with self.subTest(mode="json"):
            self.assertEqual(len(json_out["repos"]), 2, "JSON repos length")
        with self.subTest(mode="text"):
            render_repo_lines = sum(1 for line in text.splitlines() if line.startswith("| repo-"))
            self.assertEqual(render_repo_lines, 2, f"render repo row count = {render_repo_lines}")

    def test_dash_render_empty_registry_plain_sentence(self):
        empty_text = self._dash_render_empty_registry()
        self.assertIn("No repos with a queue yet.", empty_text, f"empty registry sentence missing:\n{empty_text}")
        self.assertNotIn("| Repo |", empty_text, "empty registry still rendered a table")

    def test_dash_render_multiple_in_progress(self):
        mip_text = self._dash_render_multiple_in_progress()
        self.assertIn("MULTIPLE_IN_PROGRESS", mip_text, f"MULTIPLE_IN_PROGRESS not surfaced:\n{mip_text}")
        self.assertIn("2026-01-01-a", mip_text, f"batch a dropped from MULTIPLE_IN_PROGRESS render:\n{mip_text}")
        self.assertIn("2026-01-02-b", mip_text, f"batch b dropped from MULTIPLE_IN_PROGRESS render:\n{mip_text}")

    def test_dash_render_ponytail_full_mode_warns(self):
        pony_text = self._dash_render_ponytail_full()
        self.assertIn("mode: full", pony_text, f"ponytail full mode not surfaced:\n{pony_text}")
        self.assertIn("cfq expects off", pony_text, f"ponytail warning text missing:\n{pony_text}")
        self.assertIn("⚠️ Plugins", pony_text, f"ponytail mode warning should force the ⚠️ icon:\n{pony_text}")

    def test_dash_render_ponytail_off_mode_no_warning(self):
        pony_off_text = self._dash_render_ponytail_off()
        self.assertIn("mode: off", pony_off_text, f"ponytail off mode not surfaced:\n{pony_off_text}")
        self.assertNotIn("cfq expects off", pony_off_text, "mode off should not carry a warning")

    def test_report_index_text_red_row_zero_cost_equivalence(self):
        rep_json, rep_text = self._report_index_text_red_row()

        with self.subTest(mode="json"):
            self.assertEqual(len(rep_json), 1, "rep_json length")
            self.assertEqual(rep_json[0]["cost"]["outputTokens"], 0, "fixture cost not 0")
        with self.subTest(mode="text"):
            self.assertIn(" – |", rep_text, f"zero cost did not render as –:\n{rep_text}")
            self.assertNotIn("0k", rep_text, "zero cost rendered as 0k")
            self.assertIn("**RED**", rep_text, f"RED row not visibly marked:\n{rep_text}")
            self.assertIn("2026-01-01-demo", rep_text, "batch missing from --text output")

    def test_report_index_text_empty_index_plain_sentence(self):
        empty_rep_text = self._report_index_text_empty()
        self.assertIn("v0.2", empty_rep_text, f"empty index sentence missing:\n{empty_rep_text}")

    def test_no_html_entity_in_any_rendered_terminal_output(self):
        _, text = self._dash_two_repos_json_and_text()
        empty_text = self._dash_render_empty_registry()
        mip_text = self._dash_render_multiple_in_progress()
        pony_text = self._dash_render_ponytail_full()
        pony_off_text = self._dash_render_ponytail_off()
        _, rep_text = self._report_index_text_red_row()
        empty_rep_text = self._report_index_text_empty()

        all_output = "\n".join(
            [text, empty_text, mip_text, pony_text, pony_off_text, rep_text, empty_rep_text],
        )
        for entity in ("&nbsp;", "&amp;", "&#"):
            self.assertNotIn(entity, all_output, f"HTML entity {entity!r} found in rendered terminal output")


if __name__ == "__main__":
    unittest.main()
