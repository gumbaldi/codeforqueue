"""Migrated from test-changelog.sh.

Self-test for scripts/cfq_changelog.py -- the YAML changelog itself: init/finish lifecycle,
numbered-batch reserve, Git-trailer bootstrap, legacy migration, commit-message trailers,
branch-for lookup and the gitStatePolicy exclusion.
"""

import json
import subprocess
import textwrap
import unittest

from cfq_testlib import CfqTestCase


class ChangelogTest(CfqTestCase):
    def _plain_repo(self, name):
        d = self._repos_dir / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _target(self, repo):
        return repo / ".claude/cfq/changelog.yml"

    def _write_report(self, batch_dir, data):
        batch_dir.mkdir(parents=True, exist_ok=True)
        (batch_dir / "report.json").write_text(json.dumps(data))

    def _commit(self, repo, message):
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", message], check=True,
        )

    def test_default_changelog_file_setting(self):
        resolved = self.run_cfq("settings", "get", "changelogFile").stdout.strip()
        self.assertEqual(
            resolved, ".claude/cfq/changelog.yml",
            f"default changelogFile = {resolved}, want .claude/cfq/changelog.yml",
        )

    def test_init_creates_file_legacy_no_version(self):
        repo = self._plain_repo("repo")
        target = self._target(repo)
        self.run_cfq(
            "changelog", "init", str(repo), "v0.1-example-topic", "main", "2026-01-01-example-topic",
            check=True,
        )
        self.assertTrue(target.is_file(), f"init did not create the changelog file at {target}")
        text = target.read_text()
        self.assertEqual(
            len([l for l in text.splitlines() if l.startswith("- batchNumber:")]), 1,
            "init block count, want 1",
        )
        self.assertIn("  status: in-progress\n", text, "init block missing status: in-progress")
        self.assertIn("- batchNumber: null\n", text, "legacy init did not write batchNumber: null")
        self.assertIn("  legacy: true\n", text, "legacy init did not write legacy: true")
        self.assertNotIn("  version:", text, "version field leaked into the new schema")

        # init twice appends, does not overwrite
        self.run_cfq(
            "changelog", "init", str(repo), "v0.2-second-topic", "v0.1-example-topic",
            "2026-01-02-second-topic", check=True,
        )
        text = target.read_text()
        self.assertEqual(
            len([l for l in text.splitlines() if l.startswith("- batchNumber:")]), 2,
            "after second init, block count, want 2",
        )
        self.assertIn(
            "2026-01-01-example-topic", text, "second init overwrote the first block",
        )

        # finish turns the matching in-progress block into status: done with a finished date/phases
        batch = self._repos_dir / "2026-01-02-second-topic"
        self._write_report(batch, {
            "started": "2026-01-02T09:00:00+01:00",
            "phases": [
                {"phase": "01-first-step", "status": "green", "summary": "Added the setting and its test"},
                {"phase": "02-second-step", "status": "green", "summary": "Reworked the skill section"},
            ],
        })
        self.run_cfq("changelog", "finish", str(repo), "v0.2-second-topic", str(batch), check=True)
        text = target.read_text()
        self.assertEqual(
            len([l for l in text.splitlines() if l.startswith("- batchNumber:")]), 2,
            "finish changed the block count, want 2",
        )
        self.assertIn("  status: done\n", text, "finish did not set status: done")
        self.assertIn("  finished: ", text, "finish did not add a finished date")
        self.assertEqual(text.count('phase: "01-first-step"'), 1, "finish did not carry over phase 01")
        self.assertEqual(text.count('phase: "02-second-step"'), 1, "finish did not carry over phase 02")
        self.assertIn(
            "2026-01-01-example-topic", text, "finish clobbered the first (unrelated) block",
        )

        # finish for a branch with no matching in-progress block appends instead of corrupting
        orphan = self._repos_dir / "2026-01-03-orphan-topic"
        self._write_report(orphan, {
            "started": "2026-01-03T09:00:00+01:00",
            "phases": [{"phase": "01-only-step", "status": "red", "summary": "failed"}],
        })
        before_n = len([l for l in target.read_text().splitlines() if l.startswith("- batchNumber:")])
        self.run_cfq("changelog", "finish", str(repo), "v0.9-never-inited", str(orphan), check=True)
        text = target.read_text()
        after_n = len([l for l in text.splitlines() if l.startswith("- batchNumber:")])
        self.assertEqual(after_n, before_n + 1, f"orphan finish block count, want {before_n + 1}")
        self.assertEqual(
            text.count("  status: done\n"), 2,
            "orphan finish must not turn the second-topic block into done again",
        )

    def test_empty_changelog_file_disables_init_finish_reserve(self):
        empty_repo = self._plain_repo("empty-repo")
        self.run_cfq("settings", "set", "changelogFile", "", check=True)
        try:
            self.run_cfq(
                "changelog", "init", str(empty_repo), "v0.1-noop", "main", "2026-01-01-noop", check=True,
            )
            self.assertFalse(
                (empty_repo / ".claude/cfq/changelog.yml").exists(),
                "init wrote a file despite empty changelogFile",
            )
            noop_batch = self._repos_dir / "2026-01-04-noop"
            self._write_report(noop_batch, {"phases": []})
            self.run_cfq(
                "changelog", "finish", str(empty_repo), "v0.1-noop", str(noop_batch), check=True,
            )
            self.assertFalse(
                (empty_repo / ".claude/cfq/changelog.yml").exists(),
                "finish wrote a file despite empty changelogFile",
            )
            # reserve must refuse to silently no-op: a disabled changelog can't back numbered allocation
            proc = self.run_cfq("changelog", "reserve", str(empty_repo), "1", "001-2026-01-01-noop")
            self.assertNotEqual(proc.returncode, 0, "reserve succeeded despite empty changelogFile")
        finally:
            self.run_cfq("settings", "set", "changelogFile", ".claude/cfq/changelog.yml", check=True)

    def test_summary_with_colon_and_quote_round_trips(self):
        quoty_repo = self._plain_repo("quoty-repo")
        self.run_cfq(
            "changelog", "init", str(quoty_repo), "v0.1-quoty", "main", "2026-01-05-quoty", check=True,
        )
        quoty_batch = self._repos_dir / "2026-01-05-quoty"
        self._write_report(quoty_batch, {
            "phases": [{"phase": "01-tricky", "status": "green", "summary": 'said: "hi" # not a comment'}],
        })
        self.run_cfq("changelog", "finish", str(quoty_repo), "v0.1-quoty", str(quoty_batch), check=True)
        text = (quoty_repo / ".claude/cfq/changelog.yml").read_text()
        raw = next(
            line[len("      summary: "):] for line in text.splitlines()
            if line.startswith("      summary: ")
        )
        parsed = json.loads(raw)
        self.assertEqual(
            parsed, 'said: "hi" # not a comment', f"summary did not survive the round trip, got: {parsed}",
        )

    def test_numbered_reserve_init_finish_lifecycle(self):
        numbered_repo = self._plain_repo("numbered-repo")
        target = self._target(numbered_repo)
        numbered_batch = "001-2026-02-01-example"
        self.run_cfq("changelog", "reserve", str(numbered_repo), "1", numbered_batch, check=True)
        text = target.read_text()
        self.assertEqual(
            len([l for l in text.splitlines() if l.startswith("- batchNumber:")]), 1,
            "reserve did not create exactly one block",
        )
        self.assertIn("  status: parked\n", text, "reserve did not write status: parked")

        self.run_cfq(
            "changelog", "init", str(numbered_repo), f"cfq/{numbered_batch}", "main", numbered_batch,
            check=True,
        )
        text = target.read_text()
        self.assertEqual(
            len([l for l in text.splitlines() if l.startswith("- batchNumber:")]), 1,
            "init on a reserved batch created a duplicate block instead of transitioning it",
        )
        self.assertIn(
            "- batchNumber: 1\n", text, "init did not preserve the reserved batchNumber",
        )
        self.assertIn(
            "  status: in-progress\n", text, "init did not transition parked -> in-progress",
        )
        self.assertIn(
            "  legacy: false\n", text, "numbered batch was not marked legacy: false",
        )

        numbered_batch_dir = self._repos_dir / numbered_batch
        self._write_report(numbered_batch_dir, {"phases": []})
        self.run_cfq(
            "changelog", "finish", str(numbered_repo), f"cfq/{numbered_batch}", str(numbered_batch_dir),
            check=True,
        )
        text = target.read_text()
        self.assertEqual(
            len([l for l in text.splitlines() if l.startswith("- batchNumber:")]), 1,
            "finish on a numbered batch created a duplicate block instead of transitioning it",
        )
        self.assertIn(
            "- batchNumber: 1\n", text, "finish did not preserve the batchNumber through to done",
        )
        self.assertIn(
            "  status: done\n", text, "finish did not set status: done on the numbered batch",
        )
        self.assertNotIn("  version:", text, "version field leaked into the numbered workflow")

    def test_reserve_rejects_duplicate_identities(self):
        dup_repo = self._plain_repo("dup-repo")
        self.run_cfq("changelog", "reserve", str(dup_repo), "5", "002-2026-02-02-dup", check=True)
        proc = self.run_cfq("changelog", "reserve", str(dup_repo), "5", "003-2026-02-03-other")
        self.assertNotEqual(proc.returncode, 0, "reserve accepted a duplicate batchNumber")
        proc = self.run_cfq("changelog", "reserve", str(dup_repo), "6", "002-2026-02-02-dup")
        self.assertNotEqual(proc.returncode, 0, "reserve accepted a duplicate batch")

        # ledger/queue invariant, changelog half: reserve only ever writes the ledger side, never a
        # queue directory -- pairing the two is cfq-batch-id.sh allocate's job, not this script's.
        self.assertFalse(
            (dup_repo / ".claude/cfq/impl/002-2026-02-02-dup").exists(),
            "reserve created a queue directory; that pairing must stay cfq-batch-id.sh's job",
        )

    def test_max_batch_number_ignores_legacy_and_null(self):
        maxnum_repo = self._plain_repo("maxnum-repo")
        self.run_cfq("changelog", "reserve", str(maxnum_repo), "2", "001-2026-03-01-a", check=True)
        self.run_cfq("changelog", "reserve", str(maxnum_repo), "9", "002-2026-03-02-b", check=True)
        self.run_cfq(
            "changelog", "init", str(maxnum_repo), "branch-x", "main", "2026-03-03-legacy-c", check=True,
        )
        got_max = self.run_cfq("changelog", "max-batch-number", str(maxnum_repo)).stdout.strip()
        self.assertEqual(got_max, "9", f"max-batch-number = {got_max}, want 9")

        empty_max_repo = self._plain_repo("empty-max-repo")
        got_empty_max = self.run_cfq("changelog", "max-batch-number", str(empty_max_repo)).stdout.strip()
        self.assertEqual(
            got_empty_max, "0", f"max-batch-number on a missing ledger = {got_empty_max}, want 0",
        )

    def test_ensure_bootstraps_from_git_trailers_once(self):
        git_repo = self.make_repo("git-repo")
        self._commit(git_repo, "first\n\nCFQ-Batch-Number: 7\nCFQ-Batch: 007-2026-01-01-a\n")
        self._commit(git_repo, "second\n\nCFQ-Batch-Number: 42\nCFQ-Batch: 042-2026-01-02-b\n")
        self._commit(git_repo, "third\n\nCFQ-Batch-Number: 9\nCFQ-Batch: 009-2026-01-03-c\n")
        self._commit(git_repo, "noise\n\nCFQ-Batch-Number: not-a-number\n")
        self._commit(git_repo, "zero\n\nCFQ-Batch-Number: 0\n")
        ensure_out = self.json_out(self.run_cfq("changelog", "ensure", str(git_repo)))
        self.assertEqual(
            ensure_out["source"], "git-trailer", f"ensure source = {ensure_out['source']}, want git-trailer",
        )
        self.assertEqual(ensure_out["max"], 42, f"ensure recovered max = {ensure_out['max']}, want 42")
        recovered_max = self.run_cfq("changelog", "max-batch-number", str(git_repo)).stdout.strip()
        self.assertEqual(recovered_max, "42", f"max-batch-number after bootstrap = {recovered_max}, want 42")
        self.assertIn(
            "042-2026-01-02-b", (git_repo / ".claude/cfq/changelog.yml").read_text(),
            "recovered entry did not retain the matching CFQ-Batch as context",
        )

        # existing ledger -> ensure performs zero Git-history scans even if newer trailers exist
        self._commit(git_repo, "later\n\nCFQ-Batch-Number: 999\n")
        ensure_again = self.json_out(self.run_cfq("changelog", "ensure", str(git_repo)))
        self.assertEqual(
            ensure_again["source"], "exists", "second ensure did not report source: exists",
        )
        still_max = self.run_cfq("changelog", "max-batch-number", str(git_repo)).stdout.strip()
        self.assertEqual(
            still_max, "42", f"second ensure rescanned Git history (max = {still_max}, want unchanged 42)",
        )

    def test_ensure_empty_bootstrap_without_trailers(self):
        no_trailer_repo = self.make_repo("no-trailer-repo")
        self._commit(no_trailer_repo, "plain commit, no trailers")
        empty_ensure_out = self.json_out(self.run_cfq("changelog", "ensure", str(no_trailer_repo)))
        self.assertEqual(
            empty_ensure_out["source"], "empty",
            f"ensure with no trailers reported source {empty_ensure_out['source']}, want empty",
        )
        self.assertEqual(
            empty_ensure_out["max"], 0, "ensure with no trailers reported nonzero max",
        )

    def test_migrate_old_root_file_to_local(self):
        migrate_repo = self._plain_repo("migrate-repo")
        old_file = migrate_repo / "cfq.changelog.yml"
        old_file.write_text(textwrap.dedent("""\
            - version: v0.5
              branch: v0.5-old-topic
              base: main
              batch: 2025-12-01-old-topic
              started: 2025-12-01
              finished: 2025-12-02
              status: done
              phases:
                - phase: "01-step"
                  status: "green"
                  summary: "did the thing"
            """))
        old_before = old_file.read_text()
        self.run_cfq("changelog", "migrate", str(migrate_repo), check=True)
        new_file = migrate_repo / ".claude/cfq/changelog.yml"
        self.assertTrue(new_file.is_file(), "migrate did not create the new local changelog")
        self.assertEqual(old_file.read_text(), old_before, "migrate modified the old root file")
        new_text = new_file.read_text()
        self.assertIn(
            "2025-12-01-old-topic", new_text, "migrate did not carry over the batch identity",
        )
        self.assertIn("  legacy: true\n", new_text, "migrated entry was not marked legacy: true")
        self.assertIn(
            "- batchNumber: null\n", new_text, "migrated entry was not given batchNumber: null",
        )
        self.assertNotIn("  version:", new_text, "migrated version: v0.5 survived into the new schema")
        self.assertIn('phase: "01-step"', new_text, "migrate dropped the phases array")

        # old + new file -> no duplicate entries; repeated migration is idempotent (byte-identical)
        after_first_migrate = new_file.read_text()
        self.run_cfq("changelog", "migrate", str(migrate_repo), check=True)
        new_text2 = new_file.read_text()
        self.assertEqual(
            len([l for l in new_text2.splitlines() if l.startswith("- batchNumber:")]), 1,
            "repeated migration created a duplicate entry",
        )
        self.assertEqual(
            new_text2, after_first_migrate, "repeated migration was not byte-identical",
        )

    def test_commit_message_numbered_batch_appends_trailers(self):
        # numbered batch appends CFQ-* trailers to the existing trailer block via
        # git interpret-trailers -- unpadded batchNumber, existing trailers (Co-Authored-By)
        # preserved, human body untouched, and a "key: value"-shaped line inside the body (not the
        # trailing block) is never mistaken for a real trailer.
        trailer_repo = self.make_repo("trailer-repo")
        msg_file = self._repos_dir / "msg1"
        msg_file.write_text(
            "Add deterministic CFQ batch allocation\n\n"
            "Explains that CFQ-Batch-Number: 999 would be the wrong way to write this by hand.\n\n"
            "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>\n"
        )
        composed = self.run_cfq(
            "changelog", "commit-message", str(trailer_repo),
            "042-2026-08-19-batch-id-refactor", "02-batch-number-allocation", "green", str(msg_file),
            check=True,
        ).stdout
        composed_file = self._repos_dir / "composed1"
        composed_file.write_text(composed)
        subprocess.run(
            ["git", "-C", str(trailer_repo), "commit", "-q", "--allow-empty", "-F", str(composed_file)],
            check=True,
        )

        def trailer(key):
            return subprocess.run(
                ["git", "-C", str(trailer_repo), "log", "-1",
                 f"--format=%(trailers:key={key},valueonly)"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()

        self.assertEqual(
            trailer("CFQ-Batch-Number"), "42",
            "commit-message trailer value, want unpadded 42 (not 999 from the body, not padded 042)",
        )
        self.assertEqual(
            trailer("CFQ-Batch"), "042-2026-08-19-batch-id-refactor",
            "commit-message CFQ-Batch trailer missing/wrong",
        )
        self.assertEqual(
            trailer("CFQ-Phase"), "02-batch-number-allocation",
            "commit-message CFQ-Phase trailer missing/wrong",
        )
        self.assertEqual(
            trailer("CFQ-Phase-Status"), "green", "commit-message CFQ-Phase-Status trailer missing/wrong",
        )
        self.assertEqual(
            trailer("Co-Authored-By"), "Claude Sonnet 5 <noreply@anthropic.com>",
            "commit-message dropped the existing Co-Authored-By trailer",
        )
        body = subprocess.run(
            ["git", "-C", str(trailer_repo), "log", "-1", "--format=%B"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn(
            "Explains that CFQ-Batch-Number: 999", body, "commit-message lost the human body",
        )

        # legacy (unnumbered) batch passes the message through unchanged, no fake number
        msg_file2 = self._repos_dir / "msg2"
        msg_file2.write_text("Fix a thing\n\nCo-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>\n")
        composed2 = self.run_cfq(
            "changelog", "commit-message", str(trailer_repo), "2026-08-19-legacy-batch", "01-fix",
            "green", str(msg_file2), check=True,
        ).stdout
        self.assertEqual(
            composed2, msg_file2.read_text(), "legacy commit-message altered the message",
        )
        composed_file2 = self._repos_dir / "composed2"
        composed_file2.write_text(composed2)
        subprocess.run(
            ["git", "-C", str(trailer_repo), "commit", "-q", "--allow-empty", "-F", str(composed_file2)],
            check=True,
        )
        self.assertEqual(
            trailer("CFQ-Batch-Number"), "", "legacy commit-message invented a CFQ-Batch-Number",
        )

        # commit-message rejects a non-green status
        msg_file3 = self._repos_dir / "msg3"
        msg_file3.write_text("x\n")
        proc = self.run_cfq(
            "changelog", "commit-message", str(trailer_repo), "042-2026-08-19-x", "01-a", "red",
            str(msg_file3),
        )
        self.assertNotEqual(proc.returncode, 0, "commit-message accepted status other than green")

    def test_branch_for_lookup(self):
        branchfor_repo = self._plain_repo("branchfor-repo")
        self.run_cfq(
            "changelog", "init", str(branchfor_repo), "cfq/2026-08-19-lookup", "main",
            "2026-08-19-lookup", check=True,
        )
        got_branch = self.run_cfq(
            "changelog", "branch-for", str(branchfor_repo), "2026-08-19-lookup",
        ).stdout.strip()
        self.assertEqual(
            got_branch, "cfq/2026-08-19-lookup", f"branch-for = '{got_branch}', want cfq/2026-08-19-lookup",
        )
        self.assertEqual(
            self.run_cfq(
                "changelog", "branch-for", str(branchfor_repo), "2026-08-19-unknown-batch",
            ).stdout.strip(),
            "", "branch-for should be empty for an unknown batch",
        )
        no_such_repo = self._repos_dir / "no-such-repo"
        self.assertEqual(
            self.run_cfq("changelog", "branch-for", str(no_such_repo), "2026-08-19-lookup").stdout.strip(),
            "", "branch-for should be empty when the changelog file doesn't exist",
        )

    def test_git_state_policy_exclusion(self):
        policy_repo = self._plain_repo("policy-repo")
        subprocess.run(["git", "-C", str(policy_repo), "init", "-q"], check=True)
        self.run_cfq("layout", "ensure", str(policy_repo), check=True)
        exclude_file = policy_repo / ".git/info/exclude"
        exclude_text = exclude_file.read_text()
        self.assertIn(
            ".claude/cfq/changelog.yml\n", exclude_text,
            "gitStatePolicy=local did not exclude changelog.yml",
        )
        self.assertNotIn(
            ".claude/cfq/settings.json\n", exclude_text,
            "gitStatePolicy=local must not exclude settings.json",
        )

        # gitStatePolicy=trackable removes the managed exclusion
        self.run_cfq("settings", "set", "--repo", str(policy_repo), "gitStatePolicy", "trackable", check=True)
        self.run_cfq("layout", "sync-git-policy", str(policy_repo), check=True)
        exclude_text = exclude_file.read_text()
        self.assertNotIn(
            ".claude/cfq/changelog.yml", exclude_text,
            "gitStatePolicy=trackable did not remove the managed exclusion",
        )


if __name__ == "__main__":
    unittest.main()
