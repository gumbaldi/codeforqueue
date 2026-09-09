"""Characterization tests for bin/cfq registry add|prune|list (scripts/cfq_registry.py).

Written before the Python port existed, against the shell implementation (cfq-registry.sh), to
pin its behavior -- including behavior nobody would design on purpose (no path normalization, no
directory-ness check on add) -- so the port could be verified byte-for-byte against it.
"""

import shutil
import unittest

from cfq_testlib import CfqTestCase


class RegistryTest(CfqTestCase):
    def setUp(self):
        super().setUp()
        self.repo_a = self._repos_dir / "repoA"
        self.repo_a.mkdir()
        self.repo_b = self._repos_dir / "repoB"
        self.repo_b.mkdir()

    def test_add_creates_registry_and_list_prints_one_path_per_line(self):
        self.run_cfq("registry", "add", str(self.repo_a), check=True)
        out = self.run_cfq("registry", "list", check=True).stdout
        self.assertEqual(out, f"{self.repo_a}\n", f"list output wrong: {out!r}")

    def test_add_is_idempotent(self):
        self.run_cfq("registry", "add", str(self.repo_a), check=True)
        self.run_cfq("registry", "add", str(self.repo_a), check=True)
        out = self.run_cfq("registry", "list", check=True).stdout
        self.assertEqual(
            out, f"{self.repo_a}\n", f"adding the same repo twice duplicated it: {out!r}"
        )

    def test_prune_drops_missing_repo_keeps_existing(self):
        # cfq_repo_dir(repo)/.claude/cfq must exist for prune to consider a repo "still there".
        (self.repo_a / ".claude" / "cfq").mkdir(parents=True)
        gone = self._repos_dir / "repoGone"
        gone.mkdir()
        self.run_cfq("registry", "add", str(self.repo_a), check=True)
        self.run_cfq("registry", "add", str(gone), check=True)
        shutil.rmtree(gone)

        prune_out = self.run_cfq("registry", "prune", check=True).stdout
        self.assertEqual(prune_out, f"{gone}\n", f"prune output wrong: {prune_out!r}")

        out = self.run_cfq("registry", "list", check=True).stdout
        self.assertEqual(out, f"{self.repo_a}\n", f"list after prune wrong: {out!r}")

    def test_add_relative_path_stored_verbatim(self):
        # Pins today's behavior -- no normalization to an absolute path.
        proc = self.run_cfq("registry", "add", "relrepo", cwd=str(self._repos_dir))
        self.assertEqual(proc.returncode, 0, f"add with relative path should not fail: {proc.stderr}")
        out = self.run_cfq("registry", "list", check=True).stdout
        self.assertEqual(out, "relrepo\n", f"relative path not stored verbatim: {out!r}")

    def test_add_non_directory_path_accepted(self):
        # Pins today's behavior -- add does not check that the path is a directory.
        not_a_dir = self._repos_dir / "not-a-dir"
        not_a_dir.write_text("x\n")
        proc = self.run_cfq("registry", "add", str(not_a_dir))
        self.assertEqual(proc.returncode, 0, f"add with non-directory path should not fail: {proc.stderr}")
        out = self.run_cfq("registry", "list", check=True).stdout
        self.assertEqual(out, f"{not_a_dir}\n", f"non-directory path not stored: {out!r}")

    def test_list_on_empty_registry_prints_nothing(self):
        out = self.run_cfq("registry", "list", check=True).stdout
        self.assertEqual(out, "", f"fresh registry list should be empty: {out!r}")

    def test_add_sorts_and_dedupes(self):
        self.run_cfq("registry", "add", str(self.repo_b), check=True)
        self.run_cfq("registry", "add", str(self.repo_a), check=True)
        out = self.run_cfq("registry", "list", check=True).stdout
        expected = "\n".join(sorted([str(self.repo_a), str(self.repo_b)])) + "\n"
        self.assertEqual(out, expected, f"list should be sorted: {out!r}")


if __name__ == "__main__":
    unittest.main()
