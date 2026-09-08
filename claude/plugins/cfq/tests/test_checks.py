"""Migrated from test-checks.sh.

Self-test for scripts/cfq_lint.py, scripts/cfq-security.sh, scripts/cfq-lang.sh and
scripts/cfq_maintenance.py, plus the commands/* short/long-form pairs.
"""

import os
import re
import subprocess
import unittest

from cfq_testlib import CfqTestCase, PLUGIN_ROOT


def _rule_count(output, rule):
    return len(re.findall(rf": {re.escape(rule)}:", output))


class TestLint(CfqTestCase):
    def _write(self, path, content):
        path.write_text(content)

    def test_dirty_batch_fires_every_rule_once(self):
        qdir = self._repos_dir / "lintrepo" / ".claude" / "cfq"
        qdir.mkdir(parents=True)
        dirty = qdir / "2026-01-01-dirty"
        (dirty / "done").mkdir(parents=True)
        target = self._repos_dir / "existing-target"
        target.touch()

        # 01-a: correct -- all headings, one existing absolute path, no issues
        self._write(dirty / "01-a.md", f"""## Size
M
## Context
x
## Affected Files
- `{target}` (ändern)
## Changes
x
## Verification
`bash tests/foo.sh` must exit 0
""")

        # 02-b: sections violation -- missing the Verification heading
        self._write(dirty / "02-b.md", f"""## Size
M
## Context
x
## Affected Files
- `{target}` (ändern)
## Changes
x
""")

        # 03-c: abspath violation -- relative path (existence is not checked for a non-absolute path)
        self._write(dirty / "03-c.md", """## Size
M
## Context
x
## Affected Files
- `relative/path.sh` (ändern)
## Changes
x
## Verification
`bash tests/foo.sh` must exit 0
""")

        # 04-d: missing violation -- absolute path, marked "(ändern)", does not exist
        self._write(dirty / "04-d.md", f"""## Size
M
## Context
x
## Affected Files
- `{self._repos_dir / 'does-not-exist.sh'}` (ändern)
## Changes
x
## Verification
`bash tests/foo.sh` must exit 0
""")

        # 05-e: stale-new violation -- marked "(new)" but the path already exists
        self._write(dirty / "05-e.md", f"""## Size
M
## Context
x
## Affected Files
- `{target}` (new)
## Changes
x
## Verification
`bash tests/foo.sh` must exit 0
""")

        # 08-g: sections violation -- missing the Size heading
        self._write(dirty / "08-g.md", f"""## Context
x
## Affected Files
- `{target}` (ändern)
## Changes
x
## Verification
`bash tests/foo.sh` must exit 0
""")

        # 09-h: sections violation -- the old German "Größe" heading no longer counts as Size
        self._write(dirty / "09-h.md", f"""## Größe
M
## Context
x
## Affected Files
- `{target}` (ändern)
## Changes
x
## Verification
`bash tests/foo.sh` must exit 0
""")

        # 10-i: verification-cmd violation -- prose only, no fenced code block, no backticked command
        self._write(dirty / "10-i.md", f"""## Size
M
## Context
x
## Affected Files
- `{target}` (ändern)
## Changes
x
## Verification
This step must be verified manually by a human tester.
""")

        # 11-j: verification-expect violation -- a command, but no stated expected result
        self._write(dirty / "11-j.md", f"""## Size
M
## Context
x
## Affected Files
- `{target}` (ändern)
## Changes
x
## Verification
```bash
bash tests/foo.sh
```
""")

        # 12-k: changes-empty violation -- heading present, section body empty
        self._write(dirty / "12-k.md", f"""## Size
M
## Context
x
## Affected Files
- `{target}` (ändern)
## Changes
## Verification
`bash tests/foo.sh` must exit 0
""")

        # 13-l: files-empty violation -- heading present, no `- `…`` entry
        self._write(dirty / "13-l.md", """## Size
M
## Context
x
## Affected Files
## Changes
x
## Verification
`bash tests/foo.sh` must exit 0
""")

        # done/07-f: numbering gap (06 skipped), full of content violations too -- proves done/
        # phases are excluded from sections/abspath/missing/stale-new, only numbering must fire.
        self._write(dirty / "done" / "07-f.md", f"""## Context
x
## Affected Files
- `relative/also-bad.sh` (ändern)
- `{self._repos_dir / 'also-does-not-exist.sh'}` (ändern)
## Changes
x
""")

        # invalid .priority value -> priority violation (a missing file is the normal, unflagged case)
        (dirty / ".priority").write_text("medium")

        proc = self.run_cfq("lint", str(dirty))
        self.assertNotEqual(proc.returncode, 0, "dirty batch should exit non-zero")
        out = proc.stdout + proc.stderr

        self.assertEqual(_rule_count(out, "sections"), 3, f"rule sections fired wrong count. Output:\n{out}")
        self.assertEqual(_rule_count(out, "numbering"), 1, f"rule numbering fired wrong count. Output:\n{out}")
        self.assertEqual(_rule_count(out, "abspath"), 1, f"rule abspath fired wrong count. Output:\n{out}")
        self.assertEqual(_rule_count(out, "missing"), 1, f"rule missing fired wrong count. Output:\n{out}")
        self.assertEqual(_rule_count(out, "stale-new"), 1, f"rule stale-new fired wrong count. Output:\n{out}")
        self.assertEqual(_rule_count(out, "priority"), 1, f"rule priority fired wrong count. Output:\n{out}")
        self.assertEqual(_rule_count(out, "batch-context"), 1, f"rule batch-context fired wrong count. Output:\n{out}")
        self.assertEqual(_rule_count(out, "verification-cmd"), 1, f"rule verification-cmd fired wrong count. Output:\n{out}")
        self.assertEqual(_rule_count(out, "verification-expect"), 1, f"rule verification-expect fired wrong count. Output:\n{out}")
        self.assertEqual(_rule_count(out, "changes-empty"), 1, f"rule changes-empty fired wrong count. Output:\n{out}")
        self.assertEqual(_rule_count(out, "files-empty"), 1, f"rule files-empty fired wrong count. Output:\n{out}")

        self.assertIsNone(re.search(r"^01-a\.md:", out, re.MULTILINE), "correct file 01-a.md appears in findings")
        self.assertNotIn("07-f.md:", out, f"done/ file content should never be linted, got: {out}")

    def test_clean_batch_with_dangling_depends(self):
        qdir = self._repos_dir / "lintrepo2" / ".claude" / "cfq"
        qdir.mkdir(parents=True)
        target = self._repos_dir / "existing-target2"
        target.touch()

        clean = qdir / "2026-01-02-clean"
        clean.mkdir()
        (clean / ".priority").write_text("high")
        (clean / ".dependsOn").write_text("gibtsnicht")
        (clean / ".batch-context.md").write_text("# Batch Context\n\n## Goal\nDoes a thing.\n")
        self._write(clean / "01-a.md", f"""## Size
M
## Context
x
## Affected Files
- `{target}` (ändern)
## Changes
x
## Verification
`bash tests/foo.sh` must exit 0
""")

        proc = self.run_cfq("lint", str(clean))
        self.assertEqual(proc.returncode, 0, f"clean batch (dangling depends only) should exit 0, got {proc.returncode}")
        out = proc.stdout + proc.stderr
        self.assertIsNotNone(re.search(r"^OK 1 phases$", out, re.MULTILINE), f"clean batch summary line missing/wrong: {out}")
        self.assertIsNotNone(
            re.search(r"^warn: .*: depends: gibtsnicht does not exist$", out, re.MULTILINE),
            f"clean batch missing depends warning: {out}",
        )

    def test_new_marker_on_existing_path_fires_stale_new(self):
        qdir = self._repos_dir / "lintrepo3" / ".claude" / "cfq"
        qdir.mkdir(parents=True)
        target = self._repos_dir / "existing-target3"
        target.touch()

        newmarker = qdir / "2026-01-03-newmarker"
        newmarker.mkdir()
        self._write(newmarker / "01-a.md", f"""## Size
M
## Context
x
## Affected Files
- `{target}` (new)
## Changes
x
## Verification
`bash tests/foo.sh` must exit 0
""")
        proc = self.run_cfq("lint", str(newmarker))
        self.assertNotEqual(proc.returncode, 0, "English (new) marker on an existing path should fail lint")
        out = proc.stdout + proc.stderr
        self.assertIn(": stale-new:", out, f"English (new) marker did not fire stale-new: {out}")
        self.assertNotIn(": priority:", out, f"batch without .priority should not fire priority: {out}")

    def test_batch_context_checks(self):
        qdir = self._repos_dir / "lintrepo4" / ".claude" / "cfq"
        qdir.mkdir(parents=True)
        target = self._repos_dir / "existing-target4"
        target.touch()

        phase_body = f"""## Size

S

## Context
x

## Affected Files
- `{target}` (ändern)

## Changes
x

## Verification

`bash tests/foo.sh` must exit 0
"""

        noctx = qdir / "2026-01-04-noctx"
        noctx.mkdir()
        (noctx / "01-a.md").write_text(phase_body)
        proc = self.run_cfq("lint", str(noctx))
        self.assertNotEqual(proc.returncode, 0, "missing .batch-context.md should fail lint")
        out = proc.stdout + proc.stderr
        self.assertIn(": batch-context: missing .batch-context.md", out, f"missing .batch-context.md not reported: {out}")

        nogoal = qdir / "2026-01-05-nogoal"
        nogoal.mkdir()
        (nogoal / "01-a.md").write_text(phase_body)
        (nogoal / ".batch-context.md").write_text("# Batch Context\n\n## Decisions\nsomething\n")
        proc = self.run_cfq("lint", str(nogoal))
        self.assertNotEqual(proc.returncode, 0, ".batch-context.md without ## Goal should fail lint")
        out = proc.stdout + proc.stderr
        self.assertIn(
            ": batch-context: .batch-context.md missing ## Goal heading", out,
            f"missing ## Goal heading not reported: {out}",
        )

        emptygoal = qdir / "2026-01-06-emptygoal"
        emptygoal.mkdir()
        (emptygoal / "01-a.md").write_text(phase_body)
        (emptygoal / ".batch-context.md").write_text("# Batch Context\n\n## Goal\n\n## Decisions\nx\n")
        proc = self.run_cfq("lint", str(emptygoal))
        self.assertNotEqual(proc.returncode, 0, "empty ## Goal section should fail lint")
        out = proc.stdout + proc.stderr
        self.assertIn(": batch-context: ## Goal section is empty", out, f"empty ## Goal section not reported: {out}")

        goodctx = qdir / "2026-01-07-goodctx"
        goodctx.mkdir()
        (goodctx / "01-a.md").write_text(phase_body)
        (goodctx / ".batch-context.md").write_text("# Batch Context\n\n## Goal\nDoes a thing.\n")
        proc = self.run_cfq("lint", str(goodctx))
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, f"valid .batch-context.md should pass lint, got {proc.returncode}: {out}")
        self.assertIsNotNone(re.search(r"^OK 1 phases$", out, re.MULTILINE), f"goodctx summary line missing/wrong: {out}")


class TestSecurity(CfqTestCase):
    def _plain_repo(self, name):
        d = self._repos_dir / name
        d.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
        return d

    def test_forge_detection(self):
        secrepo = self._plain_repo("secrepo")

        subprocess.run(
            ["git", "remote", "add", "origin", "git@github.com:acme/widget.git"], cwd=secrepo, check=True,
        )
        proc = self.run_cfq("security", str(secrepo), env={"CFQ_SECURITY_DETECT_ONLY": "1"})
        self.assertEqual(proc.stdout.strip(), "github github.com acme/widget", f"git@ form -> {proc.stdout}")

        subprocess.run(
            ["git", "remote", "set-url", "origin", "https://github.com/acme/widget.git"], cwd=secrepo, check=True,
        )
        proc = self.run_cfq("security", str(secrepo), env={"CFQ_SECURITY_DETECT_ONLY": "1"})
        self.assertEqual(proc.stdout.strip(), "github github.com acme/widget", f"https form -> {proc.stdout}")

        subprocess.run(
            ["git", "remote", "set-url", "origin", "ssh://git@forge.example:10022/team/widget.git"],
            cwd=secrepo, check=True,
        )
        proc = self.run_cfq("security", str(secrepo), env={"CFQ_SECURITY_DETECT_ONLY": "1"})
        self.assertEqual(proc.stdout.strip(), "unknown forge.example team/widget", f"ssh form (no login) -> {proc.stdout}")
        proc = self.run_cfq(
            "security", str(secrepo),
            env={"CFQ_SECURITY_DETECT_ONLY": "1", "CFQ_TEA_LOGIN_HOSTS": "forge.example"},
        )
        self.assertEqual(proc.stdout.strip(), "gitea forge.example team/widget", f"ssh form (with login) -> {proc.stdout}")

        subprocess.run(
            ["git", "remote", "set-url", "origin", "http://forge.example/team/widget.git"], cwd=secrepo, check=True,
        )
        proc = self.run_cfq("security", str(secrepo), env={"CFQ_SECURITY_DETECT_ONLY": "1"})
        self.assertEqual(proc.stdout.strip(), "unknown forge.example team/widget", f"http form -> {proc.stdout}")

        subprocess.run(["git", "remote", "remove", "origin"], cwd=secrepo, check=True)
        proc = self.run_cfq("security", str(secrepo), env={"CFQ_SECURITY_DETECT_ONLY": "1"})
        self.assertEqual(proc.stdout.strip(), "none - -", f"no remote -> {proc.stdout}")

    def test_full_run_no_sources(self):
        secrepo = self._plain_repo("secrepo")
        proc = self.run_cfq("security", str(secrepo))
        self.assertEqual(proc.returncode, 0, f"security exit code = {proc.returncode}, want 0")
        out = self.json_out(proc)
        self.assertFalse(out["available"], f"no-source security output = {out}")
        self.assertEqual(len(out["sources"]), 0, f"no-source security output = {out}")
        self.assertTrue(len(out["hint"]) > 0, f"no-source security output = {out}")

    def test_manifest_only_no_lockfile(self):
        mfrepo = self._plain_repo("mfrepo")
        (mfrepo / "package.json").write_text('{"name":"x","version":"1.0.0"}')
        proc = self.run_cfq("security", str(mfrepo))
        self.assertEqual(proc.returncode, 0, f"manifest-only security exit code = {proc.returncode}, want 0")
        out = self.json_out(proc)
        self.assertIsInstance(out, dict, f"manifest-only security output is not valid JSON: {proc.stdout}")

    def test_security_findings_cap(self):
        # securityFindingsCap actually reaches cfq-security.sh's behavior: a synthetic npm-audit
        # fixture producing more findings than a lowered cap gets truncated to it.
        capfixture = self._repos_dir / "capfixture"
        (capfixture / "bin").mkdir(parents=True)
        (capfixture / "package.json").write_text('{"name":"x","version":"1.0.0"}')
        npm_stub = capfixture / "bin" / "npm"
        npm_stub.write_text("""#!/usr/bin/env bash
if [ "$1" = "audit" ]; then
  jq -n '{vulnerabilities: {a:{severity:"high",fixAvailable:false}, b:{severity:"high",fixAvailable:false},
    c:{severity:"low",fixAvailable:false}, d:{severity:"low",fixAvailable:false}}}'
  exit 0
fi
exit 0
""")
        npm_stub.chmod(0o755)

        self.run_cfq("settings", "set", "securityFindingsCap", "2")
        proc = self.run_cfq(
            "security", str(capfixture),
            env={"PATH": f"{capfixture / 'bin'}:{os.environ.get('PATH', '')}"},
        )
        self.assertEqual(proc.returncode, 0, f"capped security exit code = {proc.returncode}, want 0")
        out = self.json_out(proc)
        self.assertEqual(len(out["findings"]), 2, f"securityFindingsCap=2 not applied, findings = {out['findings']}")


class TestLang(CfqTestCase):
    def _plain_repo(self, name):
        d = self._repos_dir / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_doc_tree_drift(self):
        langrepo = self._plain_repo("langrepo")
        (langrepo / "docs" / "en").mkdir(parents=True)
        (langrepo / "docs" / "en" / "a.md").write_text("# a\n")

        proc = self.run_cfq("lang", str(langrepo), env={"CFQ_DOC_LANGUAGES": "de"})
        out = self.json_out(proc)
        self.assertEqual(out["missing"], ["docs/de/a.md"], f"missing translation not reported: {out}")

        (langrepo / "docs" / "de").mkdir()
        (langrepo / "docs" / "de" / "a.md").write_text("# a\n")
        proc = self.run_cfq("lang", str(langrepo), env={"CFQ_DOC_LANGUAGES": "de"})
        out = self.json_out(proc)
        self.assertEqual(out["missing"], [], f"missing should be empty once translated: {out}")

        (langrepo / "docs" / "de" / "nur-hier.md").write_text("# nur hier\n")
        proc = self.run_cfq("lang", str(langrepo), env={"CFQ_DOC_LANGUAGES": "de"})
        out = self.json_out(proc)
        self.assertEqual(out["stray"], ["docs/de/nur-hier.md"], f"stray file not reported: {out}")

        (langrepo / "docs" / "intro.md").write_text("# intro\n")
        proc = self.run_cfq("lang", str(langrepo), env={"CFQ_DOC_LANGUAGES": "de"})
        out = self.json_out(proc)
        self.assertEqual(out["unfiled"], ["docs/intro.md"], f"unfiled file not reported: {out}")

    def test_minimal_doc_level(self):
        minimalrepo = self._plain_repo("minimalrepo")
        proc = self.run_cfq("lang", str(minimalrepo), env={"CFQ_DOC_LEVEL": "minimal"})
        self.assertEqual(proc.returncode, 0, f"minimal/no-docs should exit 0, got {proc.returncode}")
        out = self.json_out(proc)
        self.assertEqual(out["missing"], [], f"minimal/no-docs output wrong: {out}")
        self.assertEqual(out["stray"], [], f"minimal/no-docs output wrong: {out}")
        self.assertEqual(out["unfiled"], [], f"minimal/no-docs output wrong: {out}")
        self.assertTrue(len(out["note"]) > 0, f"minimal/no-docs output wrong: {out}")

    def test_changed_ref_non_git_dir(self):
        minimalrepo = self._plain_repo("minimalrepo")
        proc = self.run_cfq("lang", str(minimalrepo), "--changed", "HEAD")
        self.assertEqual(proc.returncode, 0, f"--changed in a non-git dir should exit 0, got {proc.returncode}")
        out = self.json_out(proc)
        self.assertEqual(out["scope"], "repo", f"--changed in a non-git dir should fall back to scope repo: {out}")

    def test_prose_mode(self):
        proserepo = self._plain_repo("proserepo")
        subprocess.run(["git", "init", "-q"], cwd=proserepo, check=True)
        (proserepo / "f.txt").write_text("line one\nline two\n")
        subprocess.run(["git", "add", "f.txt"], cwd=proserepo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q", "-m", "base commit"],
            cwd=proserepo, check=True,
        )
        base_ref = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=proserepo, check=True, capture_output=True, text=True,
        ).stdout.strip()

        lines = (proserepo / "f.txt").read_text().splitlines()[1:]
        lines.append("added line")
        (proserepo / "f.txt").write_text("\n".join(lines) + "\n")
        subprocess.run(["git", "add", "f.txt"], cwd=proserepo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q", "-m",
             "small change with added line"],
            cwd=proserepo, check=True,
        )

        proc = self.run_cfq("lang", "prose", str(proserepo), base_ref)
        out = self.json_out(proc)
        self.assertFalse(out["truncated"], f"small commit should not be truncated: {out}")
        self.assertIn("added line", out["sample"], f"added line missing from sample: {out}")
        self.assertNotIn("line one", out["sample"], f"removed line leaked into sample: {out}")
        self.assertIn(
            "small change with added line", out["sample"], f"commit message missing from sample: {out}",
        )

        small_ref = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=proserepo, check=True, capture_output=True, text=True,
        ).stdout.strip()
        padding = "\n".join(
            f"padding line number {i} to blow the cap with some extra filler text" for i in range(1, 401)
        )
        with (proserepo / "f.txt").open("a") as fh:
            fh.write(padding + "\n")
        subprocess.run(["git", "add", "f.txt"], cwd=proserepo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q", "-m", "big change"],
            cwd=proserepo, check=True,
        )

        proc = self.run_cfq("lang", "prose", str(proserepo), small_ref)
        out = self.json_out(proc)
        self.assertTrue(out["truncated"], f"big commit should be truncated: {out}")
        self.assertLessEqual(len(out["sample"].encode()), 8192, "sample exceeds byte cap")

        nogitdir = self._plain_repo("nogitdir")
        proc = self.run_cfq("lang", "prose", str(nogitdir), "HEAD")
        self.assertEqual(proc.returncode, 0, f"prose on non-git dir should exit 0, got {proc.returncode}")
        out = self.json_out(proc)
        self.assertEqual(out["sample"], "", f"non-git prose output wrong: {out}")
        self.assertTrue(len(out["note"]) > 0, f"non-git prose output wrong: {out}")

        proc = self.run_cfq("lang", str(proserepo))
        out = self.json_out(proc)
        self.assertEqual(
            sorted(out.keys()),
            sorted(["codeLanguage", "docLanguages", "docLevel", "missing", "note", "scope", "stray", "unfiled"]),
            f"unchanged invocation key set changed: {out}",
        )


class TestMaintenance(CfqTestCase):
    def test_due_stamp_lifecycle(self):
        maintrepo = self._repos_dir / "maintrepo"
        maintrepo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=maintrepo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "--allow-empty", "-q", "-m", "init"],
            cwd=maintrepo, check=True,
        )

        proc = self.run_cfq("maintenance", "due", str(maintrepo))
        self.assertEqual(proc.stdout.strip(), "DUE 0", f"no marker -> '{proc.stdout.strip()}', want 'DUE 0'")

        self.run_cfq("maintenance", "stamp", str(maintrepo))
        proc = self.run_cfq("maintenance", "due", str(maintrepo))
        self.assertEqual(
            proc.stdout.strip(), "NOT_DUE 0", f"stamp then due -> '{proc.stdout.strip()}', want 'NOT_DUE 0'",
        )

        subprocess.run(
            ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "--allow-empty", "-q", "-m", "c1"],
            cwd=maintrepo, check=True,
        )
        subprocess.run(
            ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "--allow-empty", "-q", "-m", "c2"],
            cwd=maintrepo, check=True,
        )
        proc = self.run_cfq("maintenance", "due", str(maintrepo), env={"CFQ_MAINTENANCE_EVERY": "2"})
        self.assertEqual(proc.stdout.strip(), "DUE 2", f"2 commits, every=2 -> '{proc.stdout.strip()}', want 'DUE 2'")

        proc = self.run_cfq("maintenance", "due", str(maintrepo), env={"CFQ_MAINTENANCE_EVERY": "50"})
        self.assertEqual(
            proc.stdout.strip(), "NOT_DUE 2", f"2 commits, every=50 -> '{proc.stdout.strip()}', want 'NOT_DUE 2'",
        )

        marker = maintrepo / ".claude" / "cfq" / ".maintenance"
        marker.write_text("2020-01-01 0000000\n")
        proc = self.run_cfq("maintenance", "due", str(maintrepo))
        self.assertTrue(
            proc.stdout.strip().startswith("DUE"), f"garbage sha -> '{proc.stdout.strip()}', want DUE*",
        )

        marker.write_text("2020-01-01\n")
        proc = self.run_cfq("maintenance", "due", str(maintrepo))
        self.assertTrue(
            proc.stdout.strip().startswith("DUE"), f"empty sha -> '{proc.stdout.strip()}', want DUE*",
        )

        self.run_cfq("maintenance", "stamp", str(maintrepo))
        before = marker.read_text()
        proc = self.run_cfq("maintenance", "due", str(maintrepo), env={"CFQ_MAINTENANCE_EVERY": "0"})
        self.assertEqual(proc.stdout.strip(), "OFF", f"maintenanceEvery=0 -> '{proc.stdout.strip()}', want 'OFF'")
        after = marker.read_text()
        self.assertEqual(before, after, "OFF must not touch the marker")


class TestCommandPairs(CfqTestCase):
    def test_short_long_forms_byte_identical(self):
        commands_dir = PLUGIN_ROOT / "commands"
        pairs = [
            ("pfq", "plan-for-queue", "toml"),
            ("ifq", "implement-for-queue", "toml"),
            ("cfq", "code-for-queue", "md"),
            ("rfq", "report-for-queue", "md"),
        ]
        for short, long, ext in pairs:
            with self.subTest(pair=f"{short}:{long}"):
                short_bytes = (commands_dir / f"{short}.{ext}").read_bytes()
                long_bytes = (commands_dir / f"{long}.{ext}").read_bytes()
                self.assertEqual(
                    short_bytes, long_bytes,
                    f"commands/{short}.{ext} and commands/{long}.{ext} are not byte-identical",
                )


if __name__ == "__main__":
    unittest.main()
