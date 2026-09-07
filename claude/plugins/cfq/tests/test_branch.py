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
        self.assertEqual(out["baseRef"], "refs/heads/main", msg=f"baseRef -> {out}")
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
        names = [c["name"] for c in out["candidates"]]
        self.assertNotIn(
            "v0.9-a", names, msg=f"stray non-ahead vX.Y branches leaked into candidates -> {out}"
        )
        self.assertNotIn("v0.10-b", names, msg=f"stray vX.Y branches leaked -> {out}")
        self.assertNotIn("v0.48-x", names, msg=f"stray vX.Y branches leaked -> {out}")

    def test_existing_legacy_branch_continues(self):
        # Existing legacy vX.Y-<slug> branch -> continue, resumable without special-casing.
        self.run_clean("git", "branch", "v0.3-mytopic", cwd=self.repo)
        out = self.json_out(self._plan("2026-01-01-mytopic"))
        self.assertEqual(out["mode"], "continue", msg=f"continue mode -> {out}")
        self.assertEqual(out["branch"], "v0.3-mytopic", msg=f"continue branch -> {out}")
        # No origin at all -> offline, remoteState falls back to "unknown".
        self.assertEqual(out["remoteState"], "unknown", msg=f"offline remoteState -> {out}")
        self.assertFalse(out["pushable"], msg=f"offline pushable -> {out}")
        self.assertEqual(out["unpushed"], [], msg=f"offline unpushed -> {out}")

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
        # A branch ahead of main appears in candidates (as an object) and becomes the recommended
        # base -- `base` is no longer null just because candidates exist.
        self.run_clean("git", "checkout", "-q", "-b", "v0.50-ahead", cwd=self.repo)
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", "ahead", cwd=self.repo,
        )
        self.run_clean("git", "checkout", "-q", "main", cwd=self.repo)
        out = self.json_out(self._plan("2026-01-02-newtopic"))
        self.assertEqual(out["mode"], "new", msg=f"candidates-case mode -> {out}")
        self.assertEqual(out["base"], "v0.50-ahead", msg=f"base -> {out}")
        self.assertEqual(out["baseRef"], "refs/heads/v0.50-ahead", msg=f"baseRef -> {out}")
        names = [c["name"] for c in out["candidates"]]
        self.assertIn("v0.50-ahead", names, msg=f"v0.50-ahead should be in candidates -> {out}")

    def test_local_main_behind_origin_is_not_fast_forwarded(self):
        # (a) local main purely behind origin/main -> no longer read or mutated on the `new`
        # path; base still "main", but baseRef points straight into refs/remotes/origin.
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
        self.assertEqual(out["baseRef"], "refs/remotes/origin/main", msg=f"ff baseRef -> {out}")
        self.assertIsNone(out["remoteWarning"], msg=f"ff remoteWarning -> {out}")
        local_main = self.run_clean("git", "rev-parse", "refs/heads/main", cwd=ff_repo).stdout.strip()
        remote_main = self.run_clean(
            "git", "rev-parse", "refs/remotes/origin/main", cwd=ff_repo
        ).stdout.strip()
        self.assertNotEqual(local_main, remote_main, msg="local main must not be mutated by `plan`")

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
        self.assertEqual(out["baseRef"], "refs/heads/main", msg=f"no-origin baseRef -> {out}")
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
        self.assertEqual(out["base"], "main", msg=f"ahead base -> {out}")
        self.assertEqual(out["baseRef"], "refs/remotes/origin/main", msg=f"ahead baseRef -> {out}")
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
        self.assertEqual(out["remoteState"], "behind", msg=f"cont remoteState -> {out}")
        local = self.run_clean(
            "git", "rev-parse", "refs/heads/cfq/2026-03-04-conttopic", cwd=cont_repo
        ).stdout.strip()
        remote = self.run_clean(
            "git", "rev-parse", "refs/remotes/origin/cfq/2026-03-04-conttopic", cwd=cont_repo
        ).stdout.strip()
        self.assertEqual(local, remote, msg="local continue branch was not fast-forwarded")

    def _make_continue_repo(self, batch, branch_name):
        """origin + a persisted `branch_name` for `batch`, returns (repo, remote)."""
        repo = self._repos_dir / f"{batch}-repo"
        repo.mkdir()
        self.run_clean("git", "init", "-q", "-b", "main", cwd=repo)
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", "init", cwd=repo,
        )
        remote = self._repos_dir / f"{batch}-remote.git"
        self.run_clean("git", "init", "-q", "--bare", "-b", "main", str(remote))
        self.run_clean("git", "remote", "add", "origin", str(remote), cwd=repo)
        self.run_cfq(
            "changelog", "init", str(repo), branch_name, "main", batch, check=True,
        )
        # Mirrors the real flow: by the time `/ifq` reaches `branch plan`, `cfq_lock.py` has
        # already run `layout ensure`, so the queue's own untracked state is Git-excluded and
        # never shows up as a dirty tree on its own.
        self.run_cfq("layout", "ensure", str(repo), check=True)
        self.run_clean("git", "branch", branch_name, cwd=repo)
        self.run_clean("git", "push", "-q", "origin", "main", branch_name, cwd=repo)
        return repo, remote

    def test_continue_behind_checked_out(self):
        # (behind, checked out, clean) -> fast-forward via `merge --ff-only` since `update-ref`
        # cannot move the ref of the branch that is currently HEAD.
        batch = "2026-03-05-checkedout"
        branch_name = f"cfq/{batch}"
        repo, remote = self._make_continue_repo(batch, branch_name)
        self.run_clean("git", "checkout", "-q", branch_name, cwd=repo)
        clone = self._repos_dir / "checkedout-clone"
        self.run_clean("git", "clone", "-q", str(remote), str(clone))
        self.run_clean("git", "checkout", "-q", branch_name, cwd=clone)
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", "remote-ahead", cwd=clone,
        )
        self.run_clean("git", "push", "-q", "origin", branch_name, cwd=clone)

        out = self.json_out(self._plan(batch, repo=repo))
        self.assertEqual(out["mode"], "continue", msg=f"checked-out behind mode -> {out}")
        self.assertEqual(out["remoteState"], "behind", msg=f"checked-out behind remoteState -> {out}")
        self.assertIsNone(out["remoteWarning"], msg=f"checked-out behind remoteWarning -> {out}")
        head = self.run_clean("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()
        origin_ref = self.run_clean(
            "git", "rev-parse", f"refs/remotes/origin/{branch_name}", cwd=repo
        ).stdout.strip()
        self.assertEqual(head, origin_ref, msg="checked-out behind branch was not fast-forwarded")

    def test_continue_behind_checked_out_dirty(self):
        # (behind, checked out, dirty) -> nothing moves, remoteWarning names the dirty tree.
        batch = "2026-03-06-dirtycheckedout"
        branch_name = f"cfq/{batch}"
        repo, remote = self._make_continue_repo(batch, branch_name)
        self.run_clean("git", "checkout", "-q", branch_name, cwd=repo)
        clone = self._repos_dir / "dirty-clone"
        self.run_clean("git", "clone", "-q", str(remote), str(clone))
        self.run_clean("git", "checkout", "-q", branch_name, cwd=clone)
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", "remote-ahead", cwd=clone,
        )
        self.run_clean("git", "push", "-q", "origin", branch_name, cwd=clone)
        (repo / "dirty.txt").write_text("uncommitted\n")

        before_head = self.run_clean("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()
        out = self.json_out(self._plan(batch, repo=repo))
        self.assertEqual(out["mode"], "continue", msg=f"dirty checked-out mode -> {out}")
        self.assertIsNotNone(out["remoteWarning"], msg=f"dirty checked-out remoteWarning -> {out}")
        after_head = self.run_clean("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()
        self.assertEqual(before_head, after_head, msg="dirty checked-out branch ref must not move")

    def test_continue_ahead(self):
        # (ahead) -> remoteState "ahead", pushable, unpushed lists hash + subject.
        batch = "2026-03-07-ahead"
        branch_name = f"cfq/{batch}"
        repo, _remote = self._make_continue_repo(batch, branch_name)
        self.run_clean("git", "checkout", "-q", branch_name, cwd=repo)
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", "unpushed change", cwd=repo,
        )
        self.run_clean("git", "checkout", "-q", "main", cwd=repo)

        out = self.json_out(self._plan(batch, repo=repo))
        self.assertEqual(out["remoteState"], "ahead", msg=f"ahead remoteState -> {out}")
        self.assertTrue(out["pushable"], msg=f"ahead pushable -> {out}")
        self.assertEqual(len(out["unpushed"]), 1, msg=f"ahead unpushed -> {out}")
        self.assertIn("unpushed change", out["unpushed"][0], msg=f"ahead unpushed content -> {out}")

    def test_continue_diverged(self):
        # (diverged) -> remoteState "diverged", not pushable, warning names both counts.
        batch = "2026-03-08-diverged"
        branch_name = f"cfq/{batch}"
        repo, remote = self._make_continue_repo(batch, branch_name)
        clone = self._repos_dir / "diverged-clone"
        self.run_clean("git", "clone", "-q", str(remote), str(clone))
        self.run_clean("git", "checkout", "-q", branch_name, cwd=clone)
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", "remote-side", cwd=clone,
        )
        self.run_clean("git", "push", "-q", "origin", branch_name, cwd=clone)

        self.run_clean("git", "checkout", "-q", branch_name, cwd=repo)
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", "local-side", cwd=repo,
        )
        self.run_clean("git", "checkout", "-q", "main", cwd=repo)

        out = self.json_out(self._plan(batch, repo=repo))
        self.assertEqual(out["remoteState"], "diverged", msg=f"diverged remoteState -> {out}")
        self.assertFalse(out["pushable"], msg=f"diverged pushable -> {out}")
        self.assertIsNotNone(out["remoteWarning"], msg=f"diverged remoteWarning -> {out}")

    def test_offline_candidates_from_local_branches(self):
        # Offline (no origin) -> candidates fall back to local `git branch`, every one
        # localOnly, baseRef points at refs/heads/<base> -- same selection as today, new shape.
        self.run_clean("git", "checkout", "-q", "-b", "cfq/001-2026-01-01-offline", cwd=self.repo)
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", "offline", cwd=self.repo,
        )
        self.run_clean("git", "checkout", "-q", "main", cwd=self.repo)
        out = self.json_out(self._plan("2026-02-08-topic"))
        self.assertEqual(out["remoteChecked"], False, msg=f"offline remoteChecked -> {out}")
        self.assertEqual(out["base"], "cfq/001-2026-01-01-offline", msg=f"offline base -> {out}")
        self.assertEqual(
            out["baseRef"], "refs/heads/cfq/001-2026-01-01-offline", msg=f"offline baseRef -> {out}"
        )
        cand = out["candidates"][0]
        self.assertTrue(cand["localOnly"], msg=f"offline localOnly -> {cand}")


class BranchPlanRemoteCandidatesTest(CfqTestCase):
    """`bin/cfq branch plan`'s `new`-mode candidate list once `origin` is the source of truth."""

    def setUp(self):
        super().setUp()
        self.repo = self._repos_dir / "repo"
        self.repo.mkdir()
        self.run_clean("git", "init", "-q", "-b", "main", cwd=self.repo)
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", "init", cwd=self.repo,
        )
        self.remote = self._repos_dir / "remote.git"
        self.run_clean("git", "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.run_clean("git", "remote", "add", "origin", str(self.remote), cwd=self.repo)
        self.run_clean("git", "push", "-q", "-u", "origin", "main", cwd=self.repo)

    def _plan(self, batch):
        return self.run_cfq("branch", "plan", str(self.repo), batch, check=True)

    def _branch(self, name, date, push=True):
        self.run_clean("git", "checkout", "-q", "-b", name, "main", cwd=self.repo)
        env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", name, cwd=self.repo, env=env,
        )
        if push:
            self.run_clean("git", "push", "-q", "-u", "origin", name, cwd=self.repo)
        self.run_clean("git", "checkout", "-q", "main", cwd=self.repo)

    def _candidate(self, out, name):
        for c in out["candidates"]:
            if c["name"] == name:
                return c
        self.fail(f"candidate {name} not found in {out['candidates']}")

    def test_routine_two_candidates_ranked_by_newest_commit(self):
        self._branch("cfq/001-2026-01-01-a", "2026-01-01T00:00:00")
        self._branch("cfq/002-2026-01-02-b", "2026-01-02T00:00:00")
        out = self.json_out(self._plan("2026-02-01-topic"))
        self.assertEqual(out["mode"], "new", msg=f"mode -> {out}")
        names = {c["name"] for c in out["candidates"]}
        self.assertEqual(
            names, {"cfq/001-2026-01-01-a", "cfq/002-2026-01-02-b"}, msg=f"candidates -> {out}"
        )
        self.assertEqual(out["base"], "cfq/002-2026-01-02-b", msg=f"base -> {out}")
        self.assertEqual(
            out["baseRef"], "refs/remotes/origin/cfq/002-2026-01-02-b", msg=f"baseRef -> {out}"
        )

    def test_remote_only_candidate_still_listed(self):
        # Branch pushed, local ref then deleted -> still listed, localOnly false, ref points at
        # origin.
        self._branch("topic-remote", "2026-01-01T00:00:00")
        self.run_clean("git", "branch", "-q", "-D", "topic-remote", cwd=self.repo)
        out = self.json_out(self._plan("2026-02-02-topic"))
        cand = self._candidate(out, "topic-remote")
        self.assertFalse(cand["localOnly"], msg=f"remote-only localOnly -> {cand}")
        self.assertEqual(cand["ref"], "refs/remotes/origin/topic-remote", msg=f"remote-only ref -> {cand}")

    def test_local_only_candidate_listed(self):
        # Branch created locally, never pushed -> still listed, localOnly true, ref is local.
        self._branch("topic-local", "2026-01-01T00:00:00", push=False)
        out = self.json_out(self._plan("2026-02-03-topic"))
        cand = self._candidate(out, "topic-local")
        self.assertTrue(cand["localOnly"], msg=f"local-only -> {cand}")
        self.assertEqual(cand["ref"], "refs/heads/topic-local", msg=f"local-only ref -> {cand}")

    def test_newest_commit_wins_over_highest_number(self):
        # The higher-numbered branch was committed first; the lower-numbered one is newer ->
        # base is the newer commit, the higher-numbered one is flagged highestBatch instead.
        self._branch("cfq/002-2026-01-01-earlier-commit-higher-number", "2026-01-01T00:00:00")
        self._branch("cfq/001-2026-01-05-later-commit-lower-number", "2026-01-05T00:00:00")
        out = self.json_out(self._plan("2026-02-04-topic"))
        self.assertEqual(
            out["base"], "cfq/001-2026-01-05-later-commit-lower-number", msg=f"base -> {out}"
        )
        highest = self._candidate(out, "cfq/002-2026-01-01-earlier-commit-higher-number")
        self.assertTrue(highest["highestBatch"], msg=f"highestBatch -> {highest}")

    def test_merged_batch_branch_still_listed(self):
        # Highest-numbered branch fully merged into origin/main -> still listed as an
        # alternative, aheadOfMain 0, mergedIntoOriginMain true.
        self._branch("cfq/001-2026-01-01-merged", "2026-01-01T00:00:00")
        self.run_clean("git", "checkout", "-q", "main", cwd=self.repo)
        self.run_clean(
            "git", "merge", "-q", "--ff-only", "cfq/001-2026-01-01-merged", cwd=self.repo
        )
        self.run_clean("git", "push", "-q", "origin", "main", cwd=self.repo)
        out = self.json_out(self._plan("2026-02-05-topic"))
        cand = self._candidate(out, "cfq/001-2026-01-01-merged")
        self.assertTrue(cand["mergedIntoOriginMain"], msg=f"merged -> {cand}")
        self.assertEqual(cand["aheadOfMain"], 0, msg=f"merged aheadOfMain -> {cand}")

    def test_candidate_behind_its_own_remote(self):
        # Local ref reset back after push -> behindRemote > 0, candidate still offered because
        # baseRef/ref point at origin, not at the local ref.
        self._branch("topic-behind", "2026-01-01T00:00:00")
        self.run_clean("git", "checkout", "-q", "topic-behind", cwd=self.repo)
        self.run_clean(
            "git", "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", "on-remote", cwd=self.repo,
        )
        self.run_clean("git", "push", "-q", "origin", "topic-behind", cwd=self.repo)
        self.run_clean("git", "reset", "-q", "--hard", "HEAD~1", cwd=self.repo)
        self.run_clean("git", "checkout", "-q", "main", cwd=self.repo)
        out = self.json_out(self._plan("2026-02-06-topic"))
        cand = self._candidate(out, "topic-behind")
        self.assertGreater(cand["behindRemote"], 0, msg=f"behindRemote -> {cand}")
        self.assertEqual(cand["ref"], "refs/remotes/origin/topic-behind", msg=f"ref -> {cand}")

    def test_no_candidates_at_all_uses_main(self):
        out = self.json_out(self._plan("2026-02-07-topic"))
        self.assertEqual(out["candidates"], [], msg=f"candidates -> {out}")
        self.assertEqual(out["base"], "main", msg=f"base -> {out}")
        self.assertEqual(out["baseRef"], "refs/remotes/origin/main", msg=f"baseRef -> {out}")

    def test_origin_head_never_a_candidate(self):
        self.run_clean("git", "remote", "set-head", "origin", "main", cwd=self.repo)
        self._branch("cfq/001-2026-01-01-topic", "2026-01-01T00:00:00")
        out = self.json_out(self._plan("2026-02-09-topic"))
        names = {c["name"] for c in out["candidates"]}
        self.assertNotIn("HEAD", names, msg=f"origin/HEAD leaked into candidates -> {out}")


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
