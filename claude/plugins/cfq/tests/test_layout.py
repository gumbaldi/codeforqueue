"""Migrated from test-layout.sh (bin/cfq layout)."""

import pathlib
import subprocess
import unittest

from cfq_testlib import CfqTestCase, PLUGIN_ROOT

# Files allowed to still mention the retired repo-local `.claude/code-for-queue` layout, per
# check 8 below.
ALLOWED_LAYOUT_FILES = {
    "scripts/migrations/cfq-layout-v1.sh",
    "tests/test-layout-migration.sh",
    "tests/test-layout.sh",
    "README.md",
}


class LayoutTest(CfqTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self._repos_dir / "repo"
        self.repo.mkdir()
        self.run_clean("git", "init", "-q", cwd=self.repo)

    def test_ensure_and_status(self):
        # 2. ensure creates the canonical dirs and adds exactly one exclude block
        # (gitStatePolicy=local default)
        self.run_cfq("layout", "ensure", str(self.repo), check=True)
        cfq_dir = self.repo / ".claude" / "cfq"
        self.assertTrue(
            (cfq_dir / "plan").is_dir() and (cfq_dir / "impl" / "done").is_dir() and (cfq_dir / "todo").is_dir(),
            msg="ensure did not create canonical dirs",
        )
        gitdir = self.run_clean(
            "git", "rev-parse", "--absolute-git-dir", cwd=self.repo
        ).stdout.strip()
        exclude_file = pathlib.Path(gitdir) / "info" / "exclude"
        text = exclude_file.read_text()
        n = text.count("# BEGIN cfq-managed")
        self.assertEqual(n, 1, msg=f"expected exactly one cfq-managed block, got {n}")
        self.assertNotIn(
            ".claude/cfq/settings.json\n", text, msg="settings.json must not be excluded"
        )
        self.assertIn(".claude/cfq/plan/\n", text, msg="plan/ missing from exclude block")

        # 3. Idempotent: ensure again -> still exactly one block, byte-identical exclude file
        before = exclude_file.read_text()
        self.run_cfq("layout", "ensure", str(self.repo), check=True)
        after = exclude_file.read_text()
        self.assertEqual(before, after, msg="second ensure changed the exclude file")

        # 4. status reports the block as present and policy as local
        out = self.json_out(self.run_cfq("layout", "status", str(self.repo)))
        self.assertEqual(out["excludeBlock"], "present", msg="status excludeBlock")
        self.assertEqual(out["gitStatePolicy"], "local", msg="status gitStatePolicy")

        # 5. Pre-existing unrelated exclude lines survive both a local sync and a switch to
        # trackable
        with exclude_file.open("a") as f:
            f.write("*.log\n")
        self.run_cfq("settings", "set", "--repo", str(self.repo), "gitStatePolicy", "trackable", check=True)
        self.run_cfq("layout", "sync-git-policy", str(self.repo), check=True)
        text = exclude_file.read_text()
        self.assertIn("*.log\n", text, msg="unrelated exclude line lost on trackable switch")
        self.assertNotIn(
            "# BEGIN cfq-managed", text, msg="cfq-managed block still present under trackable"
        )

        # 6. Switching back to local re-adds exactly one block, still keeps the unrelated line
        self.run_cfq("settings", "set", "--repo", str(self.repo), "gitStatePolicy", "local", check=True)
        self.run_cfq("layout", "sync-git-policy", str(self.repo), check=True)
        text = exclude_file.read_text()
        self.assertIn("*.log\n", text, msg="unrelated exclude line lost on local switch-back")
        n = text.count("# BEGIN cfq-managed")
        self.assertEqual(n, 1, msg=f"expected exactly one block after switch-back, got {n}")

        # 7. .gitignore is never touched, nothing gets staged or modified in the index
        gitignore = self.repo / ".gitignore"
        gitignore.write_text("node_modules/\n")
        self.run_clean("git", "add", ".gitignore", cwd=self.repo)
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "-q", "-m", "gitignore", cwd=self.repo,
        )
        gi_before = gitignore.read_text()
        status_before = self.run_clean("git", "status", "--porcelain", cwd=self.repo).stdout
        self.run_cfq("layout", "ensure", str(self.repo), check=True)
        self.assertEqual(gitignore.read_text(), gi_before, msg=".gitignore was modified")
        status_after = self.run_clean("git", "status", "--porcelain", cwd=self.repo).stdout
        self.assertEqual(status_after, status_before, msg="ensure staged/modified tracked files")

    def test_ensure_creates_done_when_impl_exists_without_it(self):
        # Edge case: .claude/cfq/impl already exists (e.g. from a partial prior run) but
        # impl/done does not -- ensure must still create it.
        impl = self.repo / ".claude" / "cfq" / "impl"
        impl.mkdir(parents=True)
        self.run_cfq("layout", "ensure", str(self.repo), check=True)
        self.assertTrue((impl / "done").is_dir(), "ensure did not create impl/done alongside existing impl/")

    def test_ensure_fresh_repo_with_trackable_policy_writes_no_exclude_block(self):
        # The "permissive" gitStatePolicy verdict, exercised through `ensure` itself (not just
        # `sync-git-policy`) on a repo that has no pre-existing exclude file at all.
        self.run_cfq(
            "settings", "set", "--repo", str(self.repo), "gitStatePolicy", "trackable", check=True,
        )
        self.run_cfq("layout", "ensure", str(self.repo), check=True)
        gitdir = self.run_clean(
            "git", "rev-parse", "--absolute-git-dir", cwd=self.repo
        ).stdout.strip()
        exclude_file = pathlib.Path(gitdir) / "info" / "exclude"
        text = exclude_file.read_text() if exclude_file.exists() else ""
        self.assertNotIn(
            "# BEGIN cfq-managed", text, msg="trackable policy must not write a cfq-managed block"
        )
        out = self.json_out(self.run_cfq("layout", "status", str(self.repo)))
        self.assertEqual(out["gitStatePolicy"], "trackable", msg="status gitStatePolicy")
        self.assertEqual(out["excludeBlock"], "absent", msg="status excludeBlock under trackable")

    def test_no_repo_local_layout_leftovers(self):
        # 8. Repo-local `.claude/code-for-queue` literal must not reappear in normal scripts
        # or SKILL.md files — permitted only in the isolated migration utility, its focused
        # test fixture, and README.md's historical migration note that explicitly document the
        # retired layout rather than using it. The global `$HOME/.claude/code-for-queue/` store
        # is a different, still-current path — any line naming HOME/home/~ is that, not this.
        proc = subprocess.run(
            [
                "grep", "-rnE", r"\.claude/code-for-queue", str(PLUGIN_ROOT),
                "--include=*.sh", "--include=*.md", "--include=*.toml",
            ],
            capture_output=True, text=True,
        )
        failures = []
        for line in proc.stdout.splitlines():
            file, lineno, content = line.split(":", 2)
            rel = file[len(str(PLUGIN_ROOT)) + 1:] if file.startswith(str(PLUGIN_ROOT)) else file
            if rel in ALLOWED_LAYOUT_FILES:
                continue
            if "HOME" in content or "home" in content or "~" in content:
                continue
            failures.append(f"{rel}:{lineno}")
        self.assertEqual(
            failures, [], msg=f"repo-local .claude/code-for-queue literal reappeared: {failures}"
        )

        # `.claude/cfq/settings.json` must never end up in cfq's managed local-state exclude
        # block — the settings file is meant to be trackable even under gitStatePolicy=local.
        # Already asserted in test_ensure_and_status above; not re-asserted here.


if __name__ == "__main__":
    unittest.main()
