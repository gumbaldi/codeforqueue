"""Migrated from test-telemetry.sh (bin/cfq telemetry — record/sync)."""

import json
import unittest

from cfq_testlib import CfqTestCase


TRANSCRIPT_TURNS = """\
{"type":"assistant","timestamp":"2026-08-13T10:00:00.000Z","sessionId":"testsid","gitBranch":"v0.2","version":"1.0.0","isSidechain":false,"effort":"high","attributionSkill":"code-for-queue:implement-for-queue","attributionPlugin":"code-for-queue","message":{"model":"claude-sonnet-5","usage":{"input_tokens":100,"output_tokens":50,"cache_read_input_tokens":10,"cache_creation_input_tokens":5},"content":[{"type":"text","text":"GEHEIMER_PROMPT_TEXT shows up in assistant prose here"}]}}
{"type":"assistant","timestamp":"2026-08-13T10:01:00.000Z","sessionId":"testsid","gitBranch":"v0.2","version":"1.0.0","isSidechain":false,"effort":"high","attributionSkill":"code-for-queue:implement-for-queue","attributionPlugin":"code-for-queue","message":{"model":"claude-sonnet-5","usage":{"input_tokens":200,"output_tokens":80,"cache_read_input_tokens":20,"cache_creation_input_tokens":0},"content":[{"type":"tool_use","name":"Bash","input":{"command":"echo GEHEIMER_PROMPT_TEXT"}}]}}
{"type":"assistant","timestamp":"2026-08-13T10:02:00.000Z","sessionId":"testsid","gitBranch":"v0.2","version":"1.0.0","isSidechain":false,"effort":"medium","attributionSkill":"code-for-queue:plan-for-queue","attributionPlugin":"code-for-queue","message":{"model":"claude-opus-5","usage":{"input_tokens":150,"output_tokens":60,"cache_read_input_tokens":15,"cache_creation_input_tokens":0},"content":[{"type":"text","text":"planning turn"}]}}
{"type":"assistant","timestamp":"2026-08-13T10:03:00.000Z","sessionId":"testsid","gitBranch":"v0.2","version":"1.0.0","isSidechain":true,"effort":"medium","attributionSkill":"code-for-queue:plan-for-queue","attributionPlugin":"code-for-queue","message":{"model":"claude-opus-5","usage":{"input_tokens":50,"output_tokens":20,"cache_read_input_tokens":5,"cache_creation_input_tokens":0},"content":[{"type":"text","text":"subagent turn"}]}}
"""

EXTRA_TURN = """\
{"type":"assistant","timestamp":"2026-08-13T10:04:00.000Z","sessionId":"testsid","gitBranch":"v0.2","version":"1.0.0","isSidechain":false,"effort":"high","attributionSkill":"code-for-queue:implement-for-queue","attributionPlugin":"code-for-queue","message":{"model":"claude-sonnet-5","usage":{"input_tokens":10,"output_tokens":5,"cache_read_input_tokens":0,"cache_creation_input_tokens":0},"content":[{"type":"text","text":"one more turn"}]}}
"""


class TelemetryTest(CfqTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self._repos_dir / "repo"
        self.repo.mkdir()
        self.run_clean("git", "init", "-q", cwd=self.repo)
        self.batch = self.repo / ".claude" / "cfq" / "2026-01-01-demo"
        self.batch.mkdir(parents=True)

        slug = str(self.repo).replace("/", "-")
        tdir = self.home / ".claude" / "projects" / slug
        tdir.mkdir(parents=True)
        self.transcript = tdir / "testsid.jsonl"
        self.transcript.write_text(TRANSCRIPT_TURNS)

        self.jsonl = self.repo / ".claude" / "cfq" / "telemetry.jsonl"

        # Seed report.json with a phase entry, as implement-for-queue would via cfq_report.py
        # append.
        self.run_cfq(
            "report", "append", str(self.batch),
            '{"phase":"01-foo","status":"green","summary":"test"}',
            check=True,
        )

    def _record(self, *args, session="testsid"):
        return self.run_cfq(
            "telemetry", "record", *args,
            cwd=self.repo, env={"CLAUDE_CODE_SESSION_ID": session}, check=True,
        )

    def _last_record(self):
        return json.loads(self.jsonl.read_text().splitlines()[-1])

    def test_record_and_sync(self):
        # 1. First record call
        proc = self._record(str(self.batch), "phase", "01-foo")
        self.assertEqual(
            proc.stdout.strip(), "telemetry: 4 turns, 210 out / 555 in",
            msg=f"unexpected record output: {proc.stdout!r}",
        )

        rec1 = self._last_record()

        self.assertEqual(rec1["totals"]["output"], 210, msg="totals.output, want 210 (50+80+60+20)")
        self.assertEqual(rec1["totals"]["cache_read"], 50, msg="totals.cache_read, want 50 (10+20+15+5)")

        by_model = {k: v["turns"] for k, v in rec1["by_model"].items()}
        self.assertEqual(by_model, {"claude-opus-5": 2, "claude-sonnet-5": 2}, msg="by_model")
        by_effort = {k: v["turns"] for k, v in rec1["by_effort"].items()}
        self.assertEqual(by_effort, {"high": 2, "medium": 2}, msg="by_effort")
        by_skill = {k: v["turns"] for k, v in rec1["by_skill"].items()}
        self.assertEqual(
            by_skill,
            {"code-for-queue:implement-for-queue": 2, "code-for-queue:plan-for-queue": 2},
            msg="by_skill",
        )

        self.assertEqual(rec1["tools"], {"Bash": 1}, msg="tools")
        self.assertEqual(rec1["subagent"]["turns"], 1, msg="subagent.turns, want 1")

        # No prompt/tool-argument text ever reaches the stored record.
        n = self.jsonl.read_text().count("GEHEIMER_PROMPT_TEXT")
        self.assertEqual(n, 0, msg=f"telemetry.jsonl leaked prompt text ({n} hits)")

        # Structural whitelist: every leaf field name must be one we deliberately added.
        allowed = {
            "schema", "kind", "repo", "batch", "phase", "session_id", "branch", "cc_version",
            "from", "until", "wallclock_s", "turns", "input", "output", "cache_read",
            "cache_creation", "billable_in", "Bash",
        }
        leaves = set(_leaf_keys(rec1))
        extra = leaves - allowed
        self.assertEqual(extra, set(), msg=f"unexpected leaf field(s) in telemetry record: {extra}")

        # report.json got the telemetry block attached to the last phase.
        report = json.loads((self.batch / "report.json").read_text())
        self.assertEqual(
            report["phases"][-1]["telemetry"], rec1,
            msg="report.json .phases[-1].telemetry does not match recorded telemetry",
        )

        # 2. Second record call of the same session only picks up the new line (window logic)
        with self.transcript.open("a") as f:
            f.write(EXTRA_TURN)
        self._record(str(self.batch), "phase", "01-foo")
        rec2 = self._last_record()
        self.assertEqual(rec2["totals"]["turns"], 1, msg="windowed record turns, want 1")

        # 3. kind=planning writes to .planning instead of .phases[-1].telemetry
        self._record(str(self.batch), "planning")
        rec3 = self._last_record()
        report = json.loads((self.batch / "report.json").read_text())
        self.assertEqual(
            report["planning"], rec3, msg="report.json .planning does not match recorded telemetry"
        )

        # 4. Fail-soft: no transcript found -> exit 0, no telemetry.jsonl written
        repo2 = self._repos_dir / "repo2"
        repo2.mkdir()
        self.run_clean("git", "init", "-q", cwd=repo2)
        batch2 = repo2 / ".claude" / "cfq" / "2026-01-02-empty"
        batch2.mkdir(parents=True)
        home_empty = self._repos_dir / "home-empty"
        home_empty.mkdir()
        proc = self.run_cfq(
            "telemetry", "record", str(batch2), "phase", "02-bar",
            home=home_empty, cwd=repo2, env={"CLAUDE_CODE_SESSION_ID": "nope"},
        )
        self.assertEqual(proc.returncode, 0, msg="record without transcript should exit 0")
        self.assertFalse(
            (repo2 / ".claude" / "cfq" / "telemetry.jsonl").exists(),
            msg="telemetry.jsonl written despite missing transcript",
        )

        # 5. Sync path, no network: local git target without a remote
        target = self._repos_dir / "target"
        target.mkdir()
        self.run_clean("git", "init", "-q", cwd=target)
        self.run_clean("git", "config", "user.email", "test@example.com", cwd=target)
        self.run_clean("git", "config", "user.name", "Test", cwd=target)
        self.run_cfq("settings", "set", "telemetrySyncRepo", str(target), check=True)

        proc = self.run_cfq("telemetry", "sync", str(self.repo))
        self.assertEqual(proc.returncode, 0, msg="sync exited non-zero despite a failed push (must stay non-fatal)")
        out = proc.stdout + proc.stderr
        self.assertIn("non-fatal", out, msg=f"sync output missing non-fatal note: {out}")
        name = f"{self.repo.name}.jsonl"
        lines_target = len((target / name).read_text().splitlines())
        lines_src = len(self.jsonl.read_text().splitlines())
        self.assertEqual(lines_target, lines_src, msg=f"sync copied {lines_target} lines, want {lines_src}")

        proc = self.run_cfq("telemetry", "sync", str(self.repo))
        out = proc.stdout + proc.stderr
        self.assertEqual(out.strip(), "telemetry sync: nothing new", msg=f"second sync = {out!r}")

    def test_bootstrap_kind(self):
        proc = self.run_cfq(
            "telemetry", "record", str(self.batch), "bootstrap", "implement-for-queue", "3", "420",
            check=True,
        )
        self.assertEqual(
            proc.stdout.strip(), "telemetry: bootstrap implement-for-queue 3 calls / 420 ms",
            msg=f"unexpected bootstrap record output: {proc.stdout!r}",
        )

        rec6 = self._last_record()
        self.assertEqual(rec6["kind"], "bootstrap", msg="bootstrap kind")
        self.assertEqual(rec6["skill"], "implement-for-queue", msg="bootstrap skill")
        self.assertEqual(rec6["call_count"], 3, msg="bootstrap call_count")
        self.assertEqual(rec6["duration_ms"], 420, msg="bootstrap duration_ms")

        allowed_bootstrap = {
            "schema", "kind", "repo", "batch", "skill", "call_count", "duration_ms", "timestamp",
        }
        leaves6 = set(_leaf_keys(rec6))
        extra6 = leaves6 - allowed_bootstrap
        self.assertEqual(
            extra6, set(), msg=f"unexpected leaf field(s) in bootstrap telemetry record: {extra6}"
        )

        # Non-numeric callCount/durationMs is rejected, not silently coerced.
        proc = self.run_cfq(
            "telemetry", "record", str(self.batch), "bootstrap", "implement-for-queue",
            "notanumber", "420",
        )
        self.assertNotEqual(proc.returncode, 0, msg="bootstrap accepted a non-numeric callCount")


def _leaf_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                yield from _leaf_keys(v)
            else:
                yield k
    elif isinstance(obj, list):
        for v in obj:
            yield from _leaf_keys(v)


if __name__ == "__main__":
    unittest.main()
