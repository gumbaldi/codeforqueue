"""Migrated from test-branch.sh (bin/cfq branch — branch-per-batch resolution)."""

import unittest

from cfq_testlib import CfqTestCase


class BranchTest(CfqTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self._repos_dir / "repo"
        self.repo.mkdir()
        self.run_clean("git", "init", "-q", "-b", "main", cwd=self.repo)
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", "init", cwd=self.repo,
        )

    def _plan(self, batch, repo=None):
        return self.run_cfq("branch", "plan", str(repo or self.repo), batch, check=True)

    def test_no_existing_branch_legacy_slug(self):
        # No existing branch, legacy-style batch dir -> new, branch is cfq/<batch-dir>, base
        # main.
        out = self.json_out(self._plan("2026-01-01-mytopic"))
        self.assertEqual(out["mode"], "new", msg=f"mode -> {out}")
        self.assertEqual(out["branch"], "cfq/2026-01-01-mytopic", msg=f"branch -> {out}")
        self.assertEqual(out["batch"], "2026-01-01-mytopic", msg=f"batch -> {out}")
        self.assertIsNone(out["batchNumber"], msg=f"legacy batchNumber should be null -> {out}")
        self.assertEqual(out["base"], "main", msg=f"base -> {out}")
        self.assertEqual(out["candidates"], [], msg=f"candidates -> {out}")

    def test_numbered_batch_dir(self):
        # Numbered batch dir -> new, branch is cfq/<numbered-dir>, batchNumber extracted.
        out = self.json_out(self._plan("001-2026-01-01-numbered"))
        self.assertEqual(out["mode"], "new", msg=f"numbered mode -> {out}")
        self.assertEqual(
            out["branch"], "cfq/001-2026-01-01-numbered", msg=f"numbered branch -> {out}"
        )
        self.assertEqual(out["batchNumber"], 1, msg=f"numbered batchNumber -> {out}")

    def test_stray_version_branches_ignored(self):
        # Stray vX.Y branches in the repo never influence the new branch name (no version
        # scanning).
        self.run_clean("git", "branch", "v0.9-a", cwd=self.repo)
        self.run_clean("git", "branch", "v0.10-b", cwd=self.repo)
        self.run_clean("git", "branch", "v0.48-x", cwd=self.repo)
        out = self.json_out(self._plan("2026-01-01-mytopic"))
        self.assertEqual(
            out["branch"], "cfq/2026-01-01-mytopic",
            msg=f"stray vX.Y branches affected the new branch -> {out}",
        )
        self.assertNotIn(
            "v0.9-a", out["candidates"], msg=f"stray non-ahead vX.Y branches leaked into candidates -> {out}"
        )
        self.assertNotIn("v0.10-b", out["candidates"], msg=f"stray vX.Y branches leaked -> {out}")
        self.assertNotIn("v0.48-x", out["candidates"], msg=f"stray vX.Y branches leaked -> {out}")

    def test_existing_legacy_branch_continues(self):
        # Existing legacy vX.Y-<slug> branch -> continue, resumable without special-casing.
        self.run_clean("git", "branch", "v0.3-mytopic", cwd=self.repo)
        out = self.json_out(self._plan("2026-01-01-mytopic"))
        self.assertEqual(out["mode"], "continue", msg=f"continue mode -> {out}")
        self.assertEqual(out["branch"], "v0.3-mytopic", msg=f"continue branch -> {out}")

    def test_remote_only_branch_continues(self):
        # Remote-only branch for the slug -> same continue result.
        self.run_clean("git", "branch", "v0.48-x", cwd=self.repo)
        remote = self._repos_dir / "remote.git"
        self.run_clean("git", "init", "-q", "--bare", str(remote))
        self.run_clean("git", "remote", "add", "origin", str(remote), cwd=self.repo)
        self.run_clean("git", "push", "-q", "origin", "v0.48-x:v0.3-mytopic", cwd=self.repo)
        self.run_clean("git", "fetch", "-q", "origin", cwd=self.repo)
        out = self.json_out(self._plan("2026-01-01-mytopic"))
        self.assertEqual(out["mode"], "continue", msg=f"remote continue mode -> {out}")
        self.assertEqual(out["branch"], "v0.3-mytopic", msg=f"remote continue branch -> {out}")

    def test_persisted_changelog_branch_wins(self):
        # A branch persisted in the changelog for this batch wins over the suffix-match
        # fallback.
        changelog_repo = self._repos_dir / "changelog-repo"
        changelog_repo.mkdir()
        self.run_clean("git", "init", "-q", "-b", "main", cwd=changelog_repo)
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", "init", cwd=changelog_repo,
        )
        self.run_cfq(
            "changelog", "init", str(changelog_repo), "cfq/2026-02-01-persisted", "main",
            "2026-02-01-persisted", check=True,
        )
        self.run_clean("git", "branch", "cfq/2026-02-01-persisted", cwd=changelog_repo)
        self.run_clean("git", "branch", "some-other-branch-ending-persisted", cwd=changelog_repo)
        out = self.json_out(self._plan("2026-02-01-persisted", repo=changelog_repo))
        self.assertEqual(out["mode"], "continue", msg=f"persisted-branch continue mode -> {out}")
        self.assertEqual(
            out["branch"], "cfq/2026-02-01-persisted", msg=f"persisted branch not preferred -> {out}"
        )

        # Persisted branch since deleted -> falls through to the suffix-match fallback, not a
        # dangling continue.
        self.run_clean("git", "branch", "-q", "-D", "cfq/2026-02-01-persisted", cwd=changelog_repo)
        out = self.json_out(self._plan("2026-02-01-persisted", repo=changelog_repo))
        self.assertEqual(out["mode"], "continue", msg=f"deleted-persisted-branch fallback mode -> {out}")
        self.assertEqual(
            out["branch"], "some-other-branch-ending-persisted",
            msg=f"deleted-persisted-branch fallback did not use suffix match -> {out}",
        )

    def test_branch_per_batch_off(self):
        # branchPerBatch=false -> off (no env var for this key, set it via settings.json).
        cfq_home = self.home / ".claude" / "code-for-queue"
        cfq_home.mkdir(parents=True)
        (cfq_home / "settings.json").write_text('{"branchPerBatch": false}\n')
        out = self.json_out(self._plan("2026-01-01-mytopic"))
        self.assertEqual(out["mode"], "off", msg=f"off mode -> {out}")
        self.assertIsNone(out["branch"], msg=f"off branch should be null -> {out}")

    def test_ahead_branch_appears_in_candidates(self):
        # A branch ahead of main appears in candidates, base is null.
        self.run_clean("git", "checkout", "-q", "-b", "v0.50-ahead", cwd=self.repo)
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", "ahead", cwd=self.repo,
        )
        self.run_clean("git", "checkout", "-q", "main", cwd=self.repo)
        out = self.json_out(self._plan("2026-01-02-newtopic"))
        self.assertEqual(out["mode"], "new", msg=f"candidates-case mode -> {out}")
        self.assertIsNone(out["base"], msg=f"base should be null with candidates -> {out}")
        self.assertIn("v0.50-ahead", out["candidates"], msg=f"v0.50-ahead should be in candidates -> {out}")

    def test_local_main_behind_origin_is_fast_forwarded(self):
        # (a) local main purely behind origin/main -> fast-forwarded, base still "main".
        ff_repo = self._repos_dir / "ff-repo"
        ff_repo.mkdir()
        self.run_clean("git", "init", "-q", "-b", "main", cwd=ff_repo)
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", "init", cwd=ff_repo,
        )
        ff_remote = self._repos_dir / "ff-remote.git"
        self.run_clean("git", "init", "-q", "--bare", "-b", "main", str(ff_remote))
        self.run_clean("git", "remote", "add", "origin", str(ff_remote), cwd=ff_repo)
        self.run_clean("git", "push", "-q", "origin", "main", cwd=ff_repo)
        self.run_clean("git", "checkout", "-q", "-b", "topic", cwd=ff_repo)
        ff_clone = self._repos_dir / "ff-clone"
        self.run_clean("git", "clone", "-q", str(ff_remote), str(ff_clone))
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", "remote-ahead", cwd=ff_clone,
        )
        self.run_clean("git", "push", "-q", "origin", "main", cwd=ff_clone)

        out = self.json_out(self._plan("2026-03-01-fftopic", repo=ff_repo))
        self.assertEqual(out["remoteChecked"], True, msg=f"ff remoteChecked -> {out}")
        self.assertEqual(out["mode"], "new", msg=f"ff mode -> {out}")
        self.assertEqual(out["base"], "main", msg=f"ff base -> {out}")
        self.assertIsNone(out["remoteWarning"], msg=f"ff remoteWarning -> {out}")
        local_main = self.run_clean("git", "rev-parse", "refs/heads/main", cwd=ff_repo).stdout.strip()
        remote_main = self.run_clean(
            "git", "rev-parse", "refs/remotes/origin/main", cwd=ff_repo
        ).stdout.strip()
        self.assertEqual(local_main, remote_main, msg="local main was not fast-forwarded")

    def test_no_origin_configured(self):
        # (b) no origin configured at all -> behaves exactly as before (remoteChecked false).
        noorigin_repo = self._repos_dir / "no-origin-repo"
        noorigin_repo.mkdir()
        self.run_clean("git", "init", "-q", "-b", "main", cwd=noorigin_repo)
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", "init", cwd=noorigin_repo,
        )
        out = self.json_out(self._plan("2026-03-02-nooriginit", repo=noorigin_repo))
        self.assertEqual(out["remoteChecked"], False, msg=f"no-origin remoteChecked -> {out}")
        self.assertEqual(out["mode"], "new", msg=f"no-origin mode -> {out}")
        self.assertEqual(out["base"], "main", msg=f"no-origin base -> {out}")
        self.assertIsNone(out["remoteWarning"], msg=f"no-origin remoteWarning -> {out}")

    def test_local_main_ahead_of_origin_never_auto_resolved(self):
        # (c) local main ahead of origin/main with an unpushed commit -> stop, never
        # auto-resolve.
        ahead_repo = self._repos_dir / "ahead-repo"
        ahead_repo.mkdir()
        self.run_clean("git", "init", "-q", "-b", "main", cwd=ahead_repo)
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", "init", cwd=ahead_repo,
        )
        ahead_remote = self._repos_dir / "ahead-remote.git"
        self.run_clean("git", "init", "-q", "--bare", str(ahead_remote))
        self.run_clean("git", "remote", "add", "origin", str(ahead_remote), cwd=ahead_repo)
        self.run_clean("git", "push", "-q", "origin", "main", cwd=ahead_repo)
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", "unpushed", cwd=ahead_repo,
        )
        out = self.json_out(self._plan("2026-03-03-aheadtopic", repo=ahead_repo))
        self.assertEqual(out["remoteChecked"], True, msg=f"ahead remoteChecked -> {out}")
        self.assertEqual(out["mode"], "new", msg=f"ahead mode -> {out}")
        self.assertIsNone(out["base"], msg=f"ahead base should be null -> {out}")
        self.assertIn("main", out["candidates"], msg=f"main should be in candidates when ahead of origin -> {out}")
        self.assertIsNotNone(out["remoteWarning"], msg=f"ahead remoteWarning should be set -> {out}")
        local_main = self.run_clean("git", "rev-parse", "refs/heads/main", cwd=ahead_repo).stdout.strip()
        remote_main = self.run_clean(
            "git", "rev-parse", "refs/remotes/origin/main", cwd=ahead_repo
        ).stdout.strip()
        self.assertNotEqual(local_main, remote_main, msg="ahead local main must not be auto-resolved")

    def test_persisted_continue_branch_fast_forwarded(self):
        # (d) a persisted continue branch purely behind its own origin/<branch> ->
        # fast-forwarded.
        cont_repo = self._repos_dir / "cont-repo"
        cont_repo.mkdir()
        self.run_clean("git", "init", "-q", "-b", "main", cwd=cont_repo)
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", "init", cwd=cont_repo,
        )
        cont_remote = self._repos_dir / "cont-remote.git"
        self.run_clean("git", "init", "-q", "--bare", "-b", "main", str(cont_remote))
        self.run_clean("git", "remote", "add", "origin", str(cont_remote), cwd=cont_repo)
        self.run_cfq(
            "changelog", "init", str(cont_repo), "cfq/2026-03-04-conttopic", "main",
            "2026-03-04-conttopic", check=True,
        )
        self.run_clean("git", "branch", "cfq/2026-03-04-conttopic", cwd=cont_repo)
        self.run_clean(
            "git", "push", "-q", "origin", "main", "cfq/2026-03-04-conttopic", cwd=cont_repo
        )
        cont_clone = self._repos_dir / "cont-clone"
        self.run_clean("git", "clone", "-q", str(cont_remote), str(cont_clone))
        self.run_clean("git", "checkout", "-q", "cfq/2026-03-04-conttopic", cwd=cont_clone)
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", "remote-ahead-branch", cwd=cont_clone,
        )
        self.run_clean("git", "push", "-q", "origin", "cfq/2026-03-04-conttopic", cwd=cont_clone)

        out = self.json_out(self._plan("2026-03-04-conttopic", repo=cont_repo))
        self.assertEqual(out["mode"], "continue", msg=f"cont mode -> {out}")
        self.assertEqual(out["branch"], "cfq/2026-03-04-conttopic", msg=f"cont branch -> {out}")
        self.assertEqual(out["remoteChecked"], True, msg=f"cont remoteChecked -> {out}")
        self.assertIsNone(out["remoteWarning"], msg=f"cont remoteWarning -> {out}")
        local = self.run_clean(
            "git", "rev-parse", "refs/heads/cfq/2026-03-04-conttopic", cwd=cont_repo
        ).stdout.strip()
        remote = self.run_clean(
            "git", "rev-parse", "refs/remotes/origin/cfq/2026-03-04-conttopic", cwd=cont_repo
        ).stdout.strip()
        self.assertEqual(local, remote, msg="local continue branch was not fast-forwarded")


class BranchCheckTest(CfqTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self._repos_dir / "repo"
        self.repo.mkdir()
        self.run_clean("git", "init", "-q", "-b", "main", cwd=self.repo)
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", "init", cwd=self.repo,
        )

    def _check(self, name, repo=None):
        return self.run_cfq("branch", "check", str(repo or self.repo), name, check=True)

    def _add_origin(self, repo=None):
        repo = repo or self.repo
        remote = self._repos_dir / f"{repo.name}-remote.git"
        self.run_clean("git", "init", "-q", "--bare", "-b", "main", str(remote))
        self.run_clean("git", "remote", "add", "origin", str(remote), cwd=repo)
        self.run_clean("git", "push", "-q", "origin", "main", cwd=repo)
        return remote

    def _commit(self, repo=None, msg="commit", date=None):
        env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date} if date else None
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", msg, cwd=repo or self.repo, env=env,
        )

    def test_routine_same_commit(self):
        # origin/feature and local feature at the same commit -> OK, ref is the origin ref,
        # ahead/behind both 0.
        self._add_origin()
        self.run_clean("git", "checkout", "-q", "-b", "feature", cwd=self.repo)
        self.run_clean("git", "push", "-q", "-u", "origin", "feature", cwd=self.repo)
        out = self.json_out(self._check("feature"))
        self.assertEqual(out["status"], "OK", msg=f"routine status -> {out}")
        self.assertEqual(out["ref"], "refs/remotes/origin/feature", msg=f"routine ref -> {out}")
        self.assertEqual(out["behind"], 0, msg=f"routine behind -> {out}")
        self.assertEqual(out["ahead"], 0, msg=f"routine ahead -> {out}")
        self.assertFalse(out["localOnly"], msg=f"routine localOnly -> {out}")

    def test_remote_only_resolves_through_origin(self):
        # Branch pushed, local ref deleted -> resolves through origin/, localOnly false.
        self._add_origin()
        self.run_clean("git", "checkout", "-q", "-b", "feature", cwd=self.repo)
        self.run_clean("git", "push", "-q", "-u", "origin", "feature", cwd=self.repo)
        self.run_clean("git", "checkout", "-q", "main", cwd=self.repo)
        self.run_clean("git", "branch", "-q", "-D", "feature", cwd=self.repo)
        out = self.json_out(self._check("feature"))
        self.assertEqual(out["status"], "OK", msg=f"remote-only status -> {out}")
        self.assertFalse(out["localOnly"], msg=f"remote-only localOnly -> {out}")
        self.assertEqual(out["ref"], "refs/remotes/origin/feature", msg=f"remote-only ref -> {out}")

    def test_local_only_never_pushed(self):
        # Branch created locally, never pushed -> localOnly true, ref is the local ref.
        self.run_clean("git", "branch", "feature", cwd=self.repo)
        out = self.json_out(self._check("feature"))
        self.assertEqual(out["status"], "OK", msg=f"local-only status -> {out}")
        self.assertTrue(out["localOnly"], msg=f"local-only localOnly -> {out}")
        self.assertEqual(out["ref"], "refs/heads/feature", msg=f"local-only ref -> {out}")

    def test_behind_origin(self):
        # Local reset one commit back from origin/<name> -> behind 1, ahead 0.
        self._add_origin()
        self.run_clean("git", "checkout", "-q", "-b", "feature", cwd=self.repo)
        self._commit(msg="ahead-on-remote")
        self.run_clean("git", "push", "-q", "-u", "origin", "feature", cwd=self.repo)
        self.run_clean("git", "reset", "-q", "--hard", "HEAD~1", cwd=self.repo)
        out = self.json_out(self._check("feature"))
        self.assertEqual(out["behind"], 1, msg=f"behind -> {out}")
        self.assertEqual(out["ahead"], 0, msg=f"behind-case ahead -> {out}")

    def test_ahead_of_origin(self):
        # One local commit not pushed -> ahead 1, behind 0.
        self._add_origin()
        self.run_clean("git", "checkout", "-q", "-b", "feature", cwd=self.repo)
        self.run_clean("git", "push", "-q", "-u", "origin", "feature", cwd=self.repo)
        self._commit(msg="unpushed")
        out = self.json_out(self._check("feature"))
        self.assertEqual(out["ahead"], 1, msg=f"ahead -> {out}")
        self.assertEqual(out["behind"], 0, msg=f"ahead-case behind -> {out}")

    def test_diverged(self):
        # One local and one remote commit from a common base -> ahead 1, behind 1.
        self._add_origin()
        self.run_clean("git", "checkout", "-q", "-b", "feature", cwd=self.repo)
        self.run_clean("git", "push", "-q", "-u", "origin", "feature", cwd=self.repo)
        clone = self._repos_dir / "clone"
        self.run_clean("git", "clone", "-q", str(self._repos_dir / f"{self.repo.name}-remote.git"), str(clone))
        self.run_clean("git", "checkout", "-q", "feature", cwd=clone)
        self._commit(repo=clone, msg="remote-side")
        self.run_clean("git", "push", "-q", "origin", "feature", cwd=clone)
        self._commit(msg="local-side")
        out = self.json_out(self._check("feature"))
        self.assertEqual(out["ahead"], 1, msg=f"diverged ahead -> {out}")
        self.assertEqual(out["behind"], 1, msg=f"diverged behind -> {out}")

    def test_newer_candidate_reported(self):
        # A second branch with a later committer date is reported as newerCandidate.
        self._add_origin()
        self.run_clean("git", "checkout", "-q", "-b", "feature", cwd=self.repo)
        self.run_clean("git", "push", "-q", "-u", "origin", "feature", cwd=self.repo)
        self.run_clean("git", "checkout", "-q", "-b", "newer-topic", "main", cwd=self.repo)
        self._commit(msg="newer", date="2030-01-01T00:00:00")
        self.run_clean("git", "push", "-q", "-u", "origin", "newer-topic", cwd=self.repo)
        out = self.json_out(self._check("feature"))
        self.assertIsNotNone(out["newerCandidate"], msg=f"newerCandidate -> {out}")
        self.assertEqual(out["newerCandidate"]["name"], "newer-topic", msg=f"newerCandidate name -> {out}")
        self.assertTrue(out["newerCandidate"]["lastCommit"], msg=f"newerCandidate lastCommit -> {out}")

    def test_unknown_name_unresolved(self):
        # A name that exists nowhere -> UNRESOLVED, exit 0, ref null.
        proc = self._check("does-not-exist")
        self.assertEqual(proc.returncode, 0, msg=f"unresolved exit code -> {proc}")
        out = self.json_out(proc)
        self.assertEqual(out["status"], "UNRESOLVED", msg=f"unresolved status -> {out}")
        self.assertIsNone(out["ref"], msg=f"unresolved ref -> {out}")

    def test_offline_no_origin(self):
        # No origin at all -> OK, remoteChecked false, local ref used.
        self.run_clean("git", "branch", "feature", cwd=self.repo)
        out = self.json_out(self._check("feature"))
        self.assertEqual(out["status"], "OK", msg=f"offline status -> {out}")
        self.assertFalse(out["remoteChecked"], msg=f"offline remoteChecked -> {out}")
        self.assertEqual(out["ref"], "refs/heads/feature", msg=f"offline ref -> {out}")


if __name__ == "__main__":
    unittest.main()
