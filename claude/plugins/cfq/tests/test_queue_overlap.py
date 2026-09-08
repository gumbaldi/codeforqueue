"""Migrated from test-queue-overlap.sh (bin/cfq overlap — ## Affected Files extraction)."""

import unittest

from cfq_testlib import CfqTestCase


class QueueOverlapTest(CfqTestCase):
    def test_overlap_extraction(self):
        tmp = self._repos_dir / "tmp"
        tmp.mkdir()

        # batch-a and batch-b each have one open phase; both list shared.txt plus one path of
        # their own.
        batch_a = tmp / ".claude" / "cfq" / "impl" / "2026-01-01-batch-a"
        batch_b = tmp / ".claude" / "cfq" / "impl" / "2026-01-01-batch-b"
        batch_a.mkdir(parents=True)
        batch_b.mkdir(parents=True)
        (batch_a / "01-a.md").write_text(
            "## Size\nS\n\n## Affected Files\n\n"
            "- `/repo/shared.txt`\n- `/repo/a-only.txt`\n\n## Changes\n"
        )
        (batch_b / "01-b.md").write_text(
            "## Size\nS\n\n## Affected Files\n\n"
            "- `/repo/shared.txt`\n- `/repo/b-only.txt`\n\n## Changes\n"
        )

        # batch-c has one open phase with no ## Affected Files section at all — must yield an
        # empty files array, not an omitted batch entry.
        batch_c = tmp / ".claude" / "cfq" / "impl" / "2026-01-01-batch-c"
        batch_c.mkdir(parents=True)
        (batch_c / "01-c.md").write_text("## Size\nS\n\n## Changes\n")

        # a phase already moved to done/ must never be scanned (excluded by maxdepth 1)
        done_dir = batch_a / "done"
        done_dir.mkdir()
        (done_dir / "00-old.md").write_text(
            "## Affected Files\n\n- `/repo/should-not-appear.txt`\n"
        )

        # batch-d's "## Affected Files" section is the last heading in the file -- no trailing
        # "## " heading follows it, so the sed range's end pattern never matches and the
        # extraction must run to end-of-file instead of yielding nothing.
        batch_d = tmp / ".claude" / "cfq" / "impl" / "2026-01-01-batch-d"
        batch_d.mkdir(parents=True)
        (batch_d / "01-d.md").write_text(
            "## Size\nS\n\n## Affected Files\n\n- `/repo/d-only.txt`\n- `/repo/d-second.txt`\n"
        )

        proc = self.run_cfq("overlap", str(tmp))
        out = self.json_out(proc)

        got_d = sorted(
            next(b for b in out["batches"] if b["batch"] == "2026-01-01-batch-d")["files"]
        )
        self.assertEqual(
            got_d, ["/repo/d-only.txt", "/repo/d-second.txt"],
            msg=f"batch-d (Affected Files as last heading) files = {got_d}",
        )

        got_a = sorted(
            next(b for b in out["batches"] if b["batch"] == "2026-01-01-batch-a")["files"]
        )
        self.assertEqual(
            got_a, ["/repo/a-only.txt", "/repo/shared.txt"], msg=f"batch-a files = {got_a}"
        )

        got_b = sorted(
            next(b for b in out["batches"] if b["batch"] == "2026-01-01-batch-b")["files"]
        )
        self.assertEqual(
            got_b, ["/repo/b-only.txt", "/repo/shared.txt"], msg=f"batch-b files = {got_b}"
        )

        got_c = next(b for b in out["batches"] if b["batch"] == "2026-01-01-batch-c")["files"]
        self.assertEqual(got_c, [], msg=f"batch-c files = {got_c}, want []")

        self.assertEqual(
            len(out["batches"]), 4, msg=f"batches count = {len(out['batches'])}, want 4"
        )

    def test_empty_repo_never_fails(self):
        empty = self._repos_dir / "empty"
        empty.mkdir()
        proc = self.run_cfq("overlap", str(empty))
        out = self.json_out(proc)
        self.assertEqual(out["batches"], [], msg=f"empty repo batches = {out}")


if __name__ == "__main__":
    unittest.main()
