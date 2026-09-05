"""Migrated from test-pfq-preflight.sh (scripts/cfq-pfq-preflight.sh).

The stub renames `cfq_settings.py` and shadows it by filename — batch `014` phase 02 ported
settings to Python, so the stub is a Python script now too (the dispatcher invokes `.py` files
with `python3`).
"""

import os
import pathlib
import shutil
import time
import unittest

from cfq_testlib import CfqTestCase, PLUGIN_ROOT


class PfqPreflightTest(CfqTestCase):
    def setUp(self):
        super().setUp()
        # Copies the whole scripts/ dir so cfq-pfq-preflight.sh's own script_dir resolution (and
        # every sibling script it shells out to, e.g. cfq-maintenance.sh) resolves inside the
        # copy, then swaps cfq_settings.py for a wrapper that logs every subcommand before
        # delegating to the real module. bin/ is copied alongside scripts/ (same relative layout
        # as the real plugin) because internal sibling calls now route through bin/cfq, which
        # resolves its own NOUN_SCRIPT table relative to itself.
        self.scripts_copy = self._repos_dir / "scripts"
        shutil.copytree(PLUGIN_ROOT / "scripts", self.scripts_copy)
        shutil.copytree(PLUGIN_ROOT / "bin", self._repos_dir / "bin")
        real = self.scripts_copy / "cfq_settings_real.py"
        (self.scripts_copy / "cfq_settings.py").rename(real)
        self.count_log = self._repos_dir / "settings-calls.log"
        self.count_log.write_text("")
        stub = self.scripts_copy / "cfq_settings.py"
        stub.write_text(f"""#!/usr/bin/env python3
import pathlib
import subprocess
import sys

d = pathlib.Path(__file__).resolve().parent
with open({str(self.count_log)!r}, "a") as f:
    f.write((sys.argv[1] if len(sys.argv) > 1 else "") + "\\n")
sys.exit(subprocess.run([sys.executable, str(d / "cfq_settings_real.py"), *sys.argv[1:]]).returncode)
""")
        self.pf = self.scripts_copy / "cfq-pfq-preflight.sh"

    def _run_pf(self, *args, env=None):
        run_env = {"HOME": str(self.home)}
        if env:
            run_env.update(env)
        return self.run_clean("bash", str(self.pf), *args, env=run_env)

    def test_fresh_unregistered_repo(self):
        fresh = self._repos_dir / "fresh"
        fresh.mkdir()
        self.run_clean("git", "init", "-q", cwd=fresh)

        out = self.json_out(self._run_pf(str(fresh)))
        self.assertEqual(out["status"], "OK", msg=f"fresh repo status = {out}")
        self.assertFalse(out["repo"]["known"], msg=f"fresh repo should be known=false: {out}")
        self.assertEqual(out["queue"]["openBatches"], [], msg=f"fresh repo openBatches should be []: {out}")

    def test_non_git_path(self):
        out = self.json_out(self._run_pf(str(self._repos_dir / "does-not-exist")))
        self.assertEqual(out["status"], "NO_REPO", msg=f"non-git status = {out}")

    def test_known_repo_and_open_batches(self):
        reg = self._repos_dir / "reg"
        reg.mkdir()
        self.run_clean("git", "init", "-q", cwd=reg)
        self.run_clean(
            "bash", str(self.scripts_copy / "cfq-registry.sh"), "add", str(reg),
            env={"HOME": str(self.home)},
        )

        qdir = reg / ".claude" / "cfq" / "impl"
        (qdir / "2026-02-01-a").mkdir(parents=True)
        (qdir / "2026-02-01-b").mkdir(parents=True)
        (qdir / "done" / "2026-02-01-archived").mkdir(parents=True)
        (qdir / "2026-02-01-empty").mkdir(parents=True)
        (qdir / "2026-02-01-a" / "01-x.md").touch()
        (qdir / "2026-02-01-a" / "02-y.md").touch()
        (qdir / "2026-02-01-b" / "01-x.md").touch()
        (qdir / "2026-02-01-b" / ".priority").write_text("high")
        (qdir / "2026-02-01-b" / ".dependsOn").write_text("2026-02-01-a\n")
        (qdir / "done" / "2026-02-01-archived" / "01-x.md").touch()
        # 2026-02-01-empty: batch dir exists but has zero open NN-*.md phases -> must be excluded
        # (open>0)

        out = self.json_out(self._run_pf(str(reg)))
        self.assertTrue(out["repo"]["known"], msg=f"registered repo should be known=true: {out}")
        reglist = self.run_clean(
            "bash", str(self.scripts_copy / "cfq-registry.sh"), "list", env={"HOME": str(self.home)}
        ).stdout
        self.assertIn(str(reg), reglist.splitlines(), msg=f"test setup broken, registry doesn't list {reg}")

        got = sorted(out["queue"]["openBatches"], key=lambda b: b["name"])
        want = sorted(
            [
                {"name": "2026-02-01-a", "priority": "", "open": 2, "dependsOn": []},
                {"name": "2026-02-01-b", "priority": "high", "open": 1, "dependsOn": ["2026-02-01-a"]},
            ],
            key=lambda b: b["name"],
        )
        self.assertEqual(got, want, msg=f"openBatches = {got}, want {want}")

        # maintenance vocabulary
        self.run_clean(
            "git", "-C", str(reg), "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "--allow-empty", "-q", "-m", "init",
        )

        out = self.json_out(self._run_pf(str(reg), env={"CFQ_MAINTENANCE_EVERY": "0"}))
        self.assertEqual(out["maintenance"]["status"], "OFF", msg=f"maintenance OFF: {out}")
        self.assertIsNone(out["maintenance"]["n"], msg=f"maintenance.n should be null when OFF: {out}")

        out = self.json_out(self._run_pf(str(reg)))
        direct = self.run_clean(
            "bash", str(self.scripts_copy / "cfq-maintenance.sh"), "due", str(reg),
            env={"HOME": str(self.home)},
        ).stdout.strip()
        got_pair = f"{out['maintenance']['status']} {out['maintenance']['n'] if out['maintenance']['n'] is not None else 'null'}"
        self.assertEqual(
            got_pair, direct, msg=f"maintenance field != direct cfq-maintenance.sh output: {out} vs {direct}"
        )

        # security.available
        stubbin = self._repos_dir / "stubbin"
        stubbin.mkdir()
        bindir = self._make_bindir_without(["gh", "tea"])

        out = self.json_out(
            self._run_pf(str(reg), env={"PATH": f"{stubbin}:{bindir}"})
        )
        self.assertFalse(out["security"]["available"], msg=f"no gh/tea on PATH should give security.available=false: {out}")

        gh_stub = stubbin / "gh"
        gh_stub.write_text("#!/usr/bin/env bash\nexit 0\n")
        gh_stub.chmod(0o755)
        out = self.json_out(
            self._run_pf(str(reg), env={"PATH": f"{stubbin}:{bindir}"})
        )
        self.assertTrue(out["security"]["available"], msg=f"gh on PATH should give security.available=true: {out}")

        # one settings.sh call
        self.count_log.write_text("")
        out = self.json_out(self._run_pf(str(reg)))
        list_calls = self.count_log.read_text().splitlines().count("list")
        self.assertEqual(
            list_calls, 1,
            msg=f"expected exactly 1 'list' cfq_settings.py call, got {list_calls} (log: {self.count_log.read_text()!r})",
        )
        for k in [
            "planModels", "allowAnyModel", "planExploreModel", "planExploreModelComplex",
            "planBlockedPlugins", "grillMode", "useMattpocockGrilling", "usePonytailAudit",
            "codeLanguage", "docLanguages", "docLevel",
        ]:
            self.assertTrue(
                k in out["planningPolicy"] or k in out["language"],
                msg=f"batched settings call missing key '{k}': {out}",
            )
        self.assertTrue(
            "reportDir" in out["reporting"] and "htmlReport" in out["reporting"],
            msg=f"batched settings call missing reporting object: {out}",
        )

        # deterministic
        out1 = self._run_pf(str(reg)).stdout
        out2 = self._run_pf(str(reg)).stdout
        self.assertEqual(out1, out2, msg="two runs on the same fixture produced different output")

        # read-only
        marker = self._repos_dir / "marker"
        marker.touch()
        time.sleep(1)
        self._run_pf(str(reg))
        changed = self.run_clean("find", str(reg), "-newer", str(marker)).stdout
        self.assertEqual(changed, "", msg=f"run modified files under the fixture repo: {changed}")

    def _make_bindir_without(self, excluded):
        # Symlink farm of the real PATH minus the given commands, so the rest of the toolchain
        # (bash, jq, git, coreutils, ...) stays reachable while the excluded commands' presence
        # is fully controlled by a separate, empty stub dir.
        bindir = self._repos_dir / "bindir"
        bindir.mkdir()
        seen = set()
        for d in os.environ.get("PATH", "").split(":"):
            if not d or not os.path.isdir(d):
                continue
            try:
                entries = os.listdir(d)
            except OSError:
                continue
            for name in entries:
                if name in excluded or name in seen:
                    continue
                f = pathlib.Path(d) / name
                if f.is_file() and os.access(f, os.X_OK):
                    (bindir / name).symlink_to(f)
                    seen.add(name)
        return bindir


if __name__ == "__main__":
    unittest.main()
