"""Migrated from test-batch-id.sh.

Self-test for scripts/cfq_batch_id.py -- allocation, renumbering and reconcile of the
numbered-batch-identity ledger.
"""

import json
import re
import subprocess
import unittest

from cfq_testlib import CfqTestCase, PLUGIN_ROOT, CFQ_BIN

SCRIPTS_DIR = PLUGIN_ROOT / "scripts"


class BatchIdTest(CfqTestCase):
    def _plain_repo(self, name):
        d = self._repos_dir / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _commit(self, repo, message, extra_args=()):
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", message, *extra_args],
            check=True,
        )

    def _reserve(self, repo, number, batch):
        return self.run_cfq("changelog", "reserve", str(repo), str(number), batch)

    def _next(self, repo, date="2026-08-19", slug="example"):
        return self.run_cfq("batch", "next", str(repo), date, slug)

    def _allocate(self, repo, date, slug):
        return self.run_cfq("batch", "allocate", str(repo), date, slug)

    def _status(self, proc):
        return self.json_out(proc)["status"]

    def _set_changelog_file(self, value):
        self.run_cfq("settings", "set", "changelogFile", value, check=True)

    def test_fresh_repo_bootstraps_at_one(self):
        repo1 = self._plain_repo("repo1")
        out = self.json_out(self._next(repo1))
        self.assertEqual(out["status"], "OK", "fresh repo next status")
        self.assertEqual(out["batch"], "001-2026-08-19-example", "fresh repo batch")
        self.assertEqual(out["batchNumber"], 1, "fresh repo batchNumber != 1")
        self.assertEqual(out["width"], 3, "fresh repo width != 3")

    def test_ledger_bootstrap_from_trailer_no_second_scan(self):
        repo2 = self.make_repo("repo2")
        self._commit(repo2, "a\n\nCFQ-Batch-Number: 42\n")
        out = self.json_out(self._next(repo2))
        self.assertEqual(out["formatted"], "043", "trailer bootstrap next")
        # a later, higher trailer must not leak in once the ledger exists -- `next` never
        # reserves, so the repeat call still reports the same unconsumed number, and only a
        # rescan of Git history (which must not happen) could ever surface the 999 trailer here
        self._commit(repo2, "b\n\nCFQ-Batch-Number: 999\n")
        out2 = self.json_out(self._next(repo2, date="2026-08-20", slug="example2"))
        self.assertEqual(
            out2["formatted"], "043",
            "ledger was rescanned from Git after bootstrap, want unchanged 043",
        )

    def test_sequential_reserve_next(self):
        repo3 = self._plain_repo("repo3")
        self._reserve(repo3, 1, "001-2026-08-01-a")
        self._reserve(repo3, 2, "002-2026-08-02-b")
        self._reserve(repo3, 9, "009-2026-08-03-c")
        out = self.json_out(self._next(repo3))
        self.assertEqual(out["formatted"], "010", "sequential reserve next")

    def test_gap_next_no_reuse(self):
        repo4 = self._plain_repo("repo4")
        self._reserve(repo4, 1, "001-2026-08-01-a")
        self._reserve(repo4, 4, "004-2026-08-02-b")
        out = self.json_out(self._next(repo4))
        self.assertEqual(out["formatted"], "005", "gap next")

    def test_two_to_three_digit_boundary(self):
        repo5 = self._plain_repo("repo5")
        self._reserve(repo5, 99, "099-2026-08-01-a")
        out = self.json_out(self._next(repo5))
        self.assertEqual(out["formatted"], "100", "099 next")

    def test_width_exhausted_requires_migration(self):
        repo6 = self._plain_repo("repo6")
        self._reserve(repo6, 999, "999-2026-08-01-a")
        proc = self._next(repo6)
        self.assertNotEqual(proc.returncode, 0, "width-exhausted next exited 0")
        self.assertEqual(self._status(proc), "BATCH_WIDTH_MIGRATION_REQUIRED", "width-exhausted status")

    def test_numeric_max_beats_lexicographic(self):
        repo7 = self._plain_repo("repo7")
        self._reserve(repo7, 9, "009-2026-08-01-a")
        self._reserve(repo7, 10, "010-2026-08-02-b")
        out = self.json_out(self._next(repo7))
        self.assertEqual(out["formatted"], "011", "numeric-max next")

    def test_legacy_unnumbered_dir_ignored(self):
        repo8 = self._plain_repo("repo8")
        (repo8 / ".claude/cfq/impl/2026-08-01-legacy-topic").mkdir(parents=True)
        self._reserve(repo8, 3, "003-2026-08-01-a")
        out = self.json_out(self._next(repo8))
        self.assertEqual(out["formatted"], "004", "legacy dir influenced next")

    def test_old_branch_and_commit_prose_ignored(self):
        repo9 = self.make_repo("repo9")
        self._commit(
            repo9,
            "v0.12-some-old-topic: mentions CFQ-Batch-Number: 500 in prose, not a trailer",
        )
        subprocess.run(["git", "-C", str(repo9), "branch", "v0.12-some-old-topic"], check=True)
        out = self.json_out(self._next(repo9))
        self.assertEqual(out["formatted"], "001", "branch/prose influenced next")

    def test_changelog_number_higher_than_queue_wins(self):
        repo10 = self._plain_repo("repo10")
        (repo10 / ".claude/cfq/impl/002-2026-08-01-a").mkdir(parents=True)
        self._reserve(repo10, 5, "005-2026-08-02-b")
        out = self.json_out(self._next(repo10))
        self.assertEqual(out["formatted"], "006", "changelog-higher next")

    def test_queue_higher_than_changelog_is_ledger_mismatch(self):
        repo11 = self._plain_repo("repo11")
        (repo11 / ".claude/cfq/impl/009-2026-08-01-a").mkdir(parents=True)
        self._reserve(repo11, 5, "005-2026-08-02-b")
        proc = self._next(repo11)
        self.assertNotEqual(proc.returncode, 0, "queue-higher next exited 0")
        self.assertEqual(self._status(proc), "BATCH_LEDGER_MISMATCH", "queue-higher status")

    def test_allocate_creates_ledger_and_queue_dir(self):
        repo12 = self._plain_repo("repo12")
        out = self.json_out(self._allocate(repo12, "2026-08-19", "my-feature"))
        self.assertEqual(out["status"], "OK", "allocate status")
        batch = out["batch"]
        self.assertEqual(batch, "001-2026-08-19-my-feature", "allocate batch")
        target = repo12 / ".claude/cfq/changelog.yml"
        text = target.read_text()
        self.assertEqual(
            len([l for l in text.splitlines() if l.startswith("- batchNumber:")]), 1,
            "allocate did not reserve exactly one changelog entry",
        )
        self.assertIn("  status: parked\n", text, "allocate did not write status: parked")
        self.assertTrue(
            (repo12 / ".claude/cfq/impl" / batch).is_dir(),
            "allocate did not create the queue directory",
        )

        # a second allocate call in the same repo advances past the first
        out2 = self.json_out(self._allocate(repo12, "2026-08-20", "second-feature"))
        self.assertEqual(out2["batch"], "002-2026-08-20-second-feature", "second allocate batch")

    def test_failed_allocate_does_not_leak_number(self):
        # simulated failure after changelog reservation never allows the number to be reused: make
        # the impl directory unwritable so allocate's own mkdir fails after the changelog reserve
        # succeeds. The invariant: a queue directory never exists without its ledger entry, so on
        # this failure the *ledger entry* is the state that may remain (recoverable -- reconcile
        # reports it, a retry moves past it) and the *directory* must not exist (that's the state
        # that breaks numbering).
        repo13 = self._plain_repo("repo13")
        self.run_cfq("layout", "ensure", str(repo13), check=True)
        impl_dir = repo13 / ".claude/cfq/impl"
        impl_dir.chmod(0o555)
        try:
            proc = self._allocate(repo13, "2026-08-19", "blocked")
            self.assertNotEqual(proc.returncode, 0, "colliding allocate exited 0")
        finally:
            impl_dir.chmod(0o755)
        self.assertEqual(self._status(proc), "INTERNAL_ERROR", "colliding allocate status")
        max_out = self.run_cfq("changelog", "max-batch-number", str(repo13))
        self.assertEqual(
            max_out.stdout.strip(), "1", "failed allocate did not consume number 1 (ledger entry lost)",
        )
        self.assertFalse(
            (impl_dir / "001-2026-08-19-blocked").exists(),
            "failed allocate left an orphaned queue directory",
        )
        action13 = self.json_out(proc)["action"]
        self.assertIn(
            ".claude/cfq/changelog.yml", action13,
            "failed-allocate action does not name the resolved changelog path",
        )
        out2 = self.json_out(self._allocate(repo13, "2026-08-19", "retry"))
        self.assertEqual(
            out2["batch"], "002-2026-08-19-retry", "retry after failed allocate reused number 1",
        )

    def test_malformed_numbered_dir_ignored(self):
        repo14 = self._plain_repo("repo14")
        (repo14 / ".claude/cfq/impl/0ab-2026-08-19-bad").mkdir(parents=True)
        self._reserve(repo14, 2, "002-2026-08-01-a")
        out = self.json_out(self._next(repo14))
        self.assertEqual(out["formatted"], "003", "malformed dir influenced next")

    def test_concurrent_allocate_no_collision(self):
        repo15 = self._plain_repo("repo15")
        env = {**self._base_env(), "HOME": str(self.home)}
        procs = [
            subprocess.Popen(
                [str(CFQ_BIN), "batch", "allocate", str(repo15), "2026-08-19", name],
                stdout=subprocess.PIPE, text=True, env=env,
            )
            for name in ("first-race", "second-race")
        ]
        outs = [json.loads(p.communicate()[0]) for p in procs]
        b1, b2 = outs[0]["batch"], outs[1]["batch"]
        self.assertNotEqual(b1, b2, f"two concurrent allocations returned the same batch: {b1}")
        n1, n2 = outs[0]["batchNumber"], outs[1]["batchNumber"]
        self.assertEqual(n1 + n2, 3, f"concurrent allocation numbers were {n1} and {n2}, want 1 and 2")

    def test_invalid_arguments_rejected(self):
        repo16 = self._plain_repo("repo16")
        proc = self._next(repo16, date="not-a-date")
        self.assertNotEqual(proc.returncode, 0, "bad date exited 0")
        self.assertEqual(self._status(proc), "INVALID_ARGUMENT", "bad date status")
        proc = self._next(repo16, slug="Not_A_Slug")
        self.assertNotEqual(proc.returncode, 0, "bad slug exited 0")
        self.assertEqual(self._status(proc), "INVALID_ARGUMENT", "bad slug status")

    def test_helper_never_reads_claude_runtime(self):
        text = (SCRIPTS_DIR / "cfq_batch_id.py").read_text()
        for needle in ("CLAUDE_", "cfq-runtime"):
            self.assertNotIn(
                needle, text, "cfq_batch_id.py references Claude runtime/session state",
            )

    def test_disabled_changelog_refuses_allocate(self):
        repo17 = self._plain_repo("repo17")
        self._set_changelog_file("")
        try:
            proc = self._next(repo17)
            self.assertNotEqual(proc.returncode, 0, "disabled changelog next exited 0")
            self.assertEqual(self._status(proc), "BATCH_CHANGELOG_REQUIRED", "disabled changelog status")
        finally:
            self._set_changelog_file(".claude/cfq/changelog.yml")

    def test_width_migration_blocked_when_queue_active(self):
        repo18 = self._plain_repo("repo18")
        (repo18 / ".claude/cfq/impl/2026-08-01-still-active").mkdir(parents=True)
        self._reserve(repo18, 999, "999-2026-08-01-a")
        (repo18 / ".claude/cfq/impl/done/999-2026-08-01-a").mkdir(parents=True)
        target = repo18 / ".claude/cfq/changelog.yml"
        before = target.read_text()
        proc = self._allocate(repo18, "2026-08-19", "blocked-test")
        self.assertNotEqual(proc.returncode, 0, "width-blocked allocate exited 0")
        out = self.json_out(proc)
        self.assertEqual(out["status"], "BATCH_WIDTH_MIGRATION_BLOCKED", "width-blocked status")
        self.assertEqual(out["currentWidth"], 3, "width-blocked currentWidth")
        self.assertEqual(out["requiredWidth"], 4, "width-blocked requiredWidth")
        self.assertEqual(out["nextNumber"], 1000, "width-blocked nextNumber")
        self.assertEqual(target.read_text(), before, "width-blocked mutated the changelog")
        self.assertTrue(
            (repo18 / ".claude/cfq/impl/done/999-2026-08-01-a").is_dir(),
            "width-blocked renamed the done directory",
        )
        self.assertFalse(
            (repo18 / ".claude/cfq/impl/done/0999-2026-08-01-a").exists(),
            "width-blocked created a migrated destination",
        )
        self.assertFalse(
            (repo18 / ".claude/cfq/impl/1000-2026-08-19-blocked-test").exists(),
            "width-blocked allocated a queue directory",
        )

    def test_width_migration_when_queue_empty_and_idempotent_rerun(self):
        # same boundary with the active queue empty -> every numbered historical identifier
        # becomes width 4 (legacy unnumbered dirs untouched, sort order stays numeric), then
        # 1000-... is allocated
        repo19 = self._plain_repo("repo19")
        self._reserve(repo19, 1, "001-2026-08-01-a")
        self._reserve(repo19, 99, "099-2026-08-02-b")
        self._reserve(repo19, 999, "999-2026-08-03-c")
        done = repo19 / ".claude/cfq/impl/done"
        for name in ("001-2026-08-01-a", "099-2026-08-02-b", "999-2026-08-03-c", "2026-08-01-legacy-topic"):
            (done / name).mkdir(parents=True)
        out = self.json_out(self._allocate(repo19, "2026-08-19", "new-batch"))
        self.assertEqual(out["status"], "OK", "post-migration allocate status")
        self.assertEqual(out["batch"], "1000-2026-08-19-new-batch", "post-migration allocate batch")
        self.assertEqual(out["width"], 4, "post-migration allocate width")
        target19 = (repo19 / ".claude/cfq/changelog.yml").read_text()
        for line in (
            "  batch: 0001-2026-08-01-a",
            "  batch: 0099-2026-08-02-b",
            "  batch: 0999-2026-08-03-c",
        ):
            self.assertIn(line + "\n", target19, f"changelog line not repadded: {line}")
        for name in ("0001-2026-08-01-a", "0099-2026-08-02-b", "0999-2026-08-03-c"):
            self.assertTrue((done / name).is_dir(), f"done dir {name} missing after migration")
        self.assertTrue(
            (done / "2026-08-01-legacy-topic").is_dir(), "legacy done dir was renamed",
        )
        numbered = sorted(
            p.name for p in done.iterdir()
            if re.match(r"^[0-9]{4}-[0-9]{4}-[0-9]{2}-[0-9]{2}-", p.name)
        )
        self.assertEqual(
            numbered,
            ["0001-2026-08-01-a", "0099-2026-08-02-b", "0999-2026-08-03-c"],
            "alphabetical order after migration != numeric order",
        )

        # second migration invocation is idempotent: nothing left to rename, no double-padding
        out2 = self.json_out(self.run_cfq("batch", "migrate-width", str(repo19)))
        self.assertEqual(out2["status"], "OK", "idempotent migrate-width status")
        self.assertEqual(out2["migrated"], 0, "idempotent migrate-width migrated entries, want 0")
        self.assertTrue(
            (done / "0001-2026-08-01-a").is_dir(), "idempotent migrate-width lost batch 1",
        )
        self.assertFalse(
            (done / "00001-2026-08-01-a").exists(), "idempotent migrate-width double-padded batch 1",
        )

    def test_generic_width_migration_4_to_5(self):
        repo20 = self._plain_repo("repo20")
        self._reserve(repo20, 9999, "9999-2026-08-01-a")
        (repo20 / ".claude/cfq/impl/done/9999-2026-08-01-a").mkdir(parents=True)
        out = self.json_out(self.run_cfq("batch", "migrate-width", str(repo20)))
        self.assertEqual(out["status"], "OK", "4->5 migrate-width status")
        self.assertEqual(out["width"], 5, "4->5 migrate-width width")
        target20 = (repo20 / ".claude/cfq/changelog.yml").read_text()
        self.assertIn(
            "  batch: 09999-2026-08-01-a\n", target20, "4->5 changelog batch was not repadded",
        )
        self.assertTrue(
            (repo20 / ".claude/cfq/impl/done/09999-2026-08-01-a").is_dir(),
            "4->5 done dir was not renamed",
        )
        out2 = self.json_out(self._allocate(repo20, "2026-08-19", "next-after-5"))
        self.assertEqual(
            out2["batch"], "10000-2026-08-19-next-after-5", "4->5 allocate after migration",
        )

    def test_migration_collision_before_rename(self):
        repo21 = self._plain_repo("repo21")
        (repo21 / ".claude/cfq/impl/done/0999-2026-08-01-a").mkdir(parents=True)
        self._reserve(repo21, 999, "999-2026-08-01-a")
        target = repo21 / ".claude/cfq/changelog.yml"
        before = target.read_text()
        proc = self._allocate(repo21, "2026-08-19", "collide")
        self.assertNotEqual(proc.returncode, 0, "collision allocate exited 0")
        self.assertEqual(self._status(proc), "BATCH_WIDTH_MIGRATION_COLLISION", "collision status")
        self.assertEqual(target.read_text(), before, "collision mutated the changelog")
        self.assertFalse(
            (repo21 / ".claude/cfq/impl/done/999-2026-08-01-a").exists(),
            "collision unexpectedly created a 999 done dir",
        )
        self.assertTrue(
            (repo21 / ".claude/cfq/impl/done/0999-2026-08-01-a").is_dir(),
            "collision removed the pre-existing destination",
        )

    def test_interrupted_migration_recovers(self):
        repo22 = self._plain_repo("repo22")
        self._reserve(repo22, 1, "0001-2026-08-01-a")
        self._reserve(repo22, 2, "002-2026-08-02-b")
        done = repo22 / ".claude/cfq/impl/done"
        (done / "0001-2026-08-01-a").mkdir(parents=True)
        (done / "002-2026-08-02-b").mkdir(parents=True)
        out = self.json_out(self.run_cfq("batch", "migrate-width", str(repo22)))
        self.assertEqual(out["status"], "OK", "interrupted-recovery migrate-width status")
        target22 = (repo22 / ".claude/cfq/changelog.yml").read_text()
        self.assertIn(
            "  batch: 0002-2026-08-02-b\n", target22,
            "interrupted-recovery did not repad the still-old entry",
        )
        self.assertIn(
            "  batch: 0001-2026-08-01-a\n", target22,
            "interrupted-recovery lost the already-migrated entry",
        )
        self.assertTrue(
            (done / "0002-2026-08-02-b").is_dir(),
            "interrupted-recovery did not rename the still-old done dir",
        )
        self.assertTrue(
            (done / "0001-2026-08-01-a").is_dir(),
            "interrupted-recovery lost the already-migrated done dir",
        )

    def test_width_migration_never_touches_git(self):
        text = (SCRIPTS_DIR / "cfq_batch_id.py").read_text()
        self.assertIsNone(
            re.search(r"(^|[^A-Za-z])git([^A-Za-z]|$)", text),
            "cfq_batch_id.py performs Git operations (width migration must never touch Git)",
        )

    def test_action_names_configured_changelog_path_not_default(self):
        # the reported bug: a non-default changelogFile must appear verbatim in the action, never
        # the hardcoded default -- this is the assertion that fails against the pre-fix code
        repo23 = self._plain_repo("repo23")
        (repo23 / ".claude/cfq/impl/003-2026-08-01-a").mkdir(parents=True)
        self._set_changelog_file("cfq.changelog.yml")
        try:
            proc = self._next(repo23)
            self.assertNotEqual(proc.returncode, 0, "mismatched-path next exited 0")
        finally:
            self._set_changelog_file(".claude/cfq/changelog.yml")
        self.assertEqual(self._status(proc), "BATCH_LEDGER_MISMATCH", "mismatched-path status")
        action23 = self.json_out(proc)["action"]
        self.assertIn(
            "cfq.changelog.yml", action23, "action does not name the configured changelog path",
        )
        self.assertNotIn(
            ".claude/cfq/changelog.yml", action23, "action still names the hardcoded default path",
        )
        self.assertIn(
            "cfq batch reconcile", action23, "action does not name the repair command",
        )

    def test_reconcile_reports_orphan_dir(self):
        repo24 = self._plain_repo("repo24")
        (repo24 / ".claude/cfq/impl/005-2026-08-19-orphan").mkdir(parents=True)
        proc = self.run_cfq("batch", "reconcile", str(repo24))
        self.assertNotEqual(proc.returncode, 0, "reconcile with an orphaned directory exited 0")
        out = self.json_out(proc)
        self.assertEqual(out["status"], "OK", "reconcile status")
        self.assertEqual(len(out["orphanDirs"]), 1, "reconcile orphanDirs count")
        self.assertEqual(out["orphanDirs"][0], "005-2026-08-19-orphan", "reconcile orphanDirs[0]")
        self.assertEqual(out["dirMax"], 5, "reconcile dirMax")

    def test_reconcile_fix_resolves_orphan_dir(self):
        repo25 = self._plain_repo("repo25")
        (repo25 / ".claude/cfq/impl/006-2026-08-19-orphan").mkdir(parents=True)
        out = self.json_out(self.run_cfq("batch", "reconcile", str(repo25), "--fix"))
        self.assertEqual(len(out["orphanDirs"]), 0, f"reconcile --fix left orphanDirs non-empty: {out}")
        target25 = (repo25 / ".claude/cfq/changelog.yml").read_text()
        self.assertIn(
            "  batch: 006-2026-08-19-orphan\n", target25,
            "reconcile --fix did not reserve the orphaned directory",
        )
        self.assertIn("  status: parked\n", target25, "reconcile --fix did not write status: parked")
        second = self.run_cfq("batch", "reconcile", str(repo25))
        self.assertEqual(second.returncode, 0, "second reconcile run should be clean")

    def test_reconcile_nothing_to_do(self):
        repo26 = self._plain_repo("repo26")
        out = self.json_out(self.run_cfq("batch", "reconcile", str(repo26)))
        self.assertEqual(out["status"], "OK", "empty-repo reconcile status")
        self.assertEqual(len(out["orphanDirs"]), 0, "empty-repo reconcile orphanDirs not empty")
        self.assertEqual(len(out["orphanEntries"]), 0, "empty-repo reconcile orphanEntries not empty")

    def test_reconcile_never_fixes_orphan_entry(self):
        # edge: a ledger entry with no directory (an abandoned reservation) is reported but never
        # "fixed" by --fix -- it never deletes anything and never touches this category
        repo27 = self._plain_repo("repo27")
        self._reserve(repo27, 8, "008-2026-08-19-abandoned")
        out = self.json_out(self.run_cfq("batch", "reconcile", str(repo27), "--fix"))
        self.assertEqual(len(out["orphanEntries"]), 1, "abandoned-reservation reconcile orphanEntries count")
        self.assertEqual(
            out["orphanEntries"][0], "008-2026-08-19-abandoned", "abandoned-reservation orphanEntries[0]",
        )
        self.assertFalse(
            (repo27 / ".claude/cfq/impl/008-2026-08-19-abandoned").exists(),
            "reconcile --fix created a directory for an abandoned reservation",
        )
        self.assertEqual(
            len(out["orphanDirs"]), 0, "abandoned-reservation reconcile unexpectedly reports orphanDirs",
        )

    def test_reconcile_error_object_has_required_fields(self):
        repo28 = self._plain_repo("repo28")
        self._set_changelog_file("")
        try:
            proc = self.run_cfq("batch", "reconcile", str(repo28))
            self.assertNotEqual(proc.returncode, 0, "disabled-changelog reconcile exited 0")
        finally:
            self._set_changelog_file(".claude/cfq/changelog.yml")
        out = self.json_out(proc)
        self.assertEqual(out["status"], "BATCH_CHANGELOG_REQUIRED", "disabled-changelog reconcile status")
        for key in ("status", "detail", "action"):
            self.assertTrue(
                out.get(key), f"disabled-changelog reconcile error missing/empty field {key}",
            )


if __name__ == "__main__":
    unittest.main()
