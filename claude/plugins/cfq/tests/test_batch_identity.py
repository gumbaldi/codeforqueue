"""Migrated from test-batch-identity.sh.

Static regression guards for the numbered-batch-identity model (docs/schema/interface level).
Behavioral coverage for allocation, width migration and trailer rendering already lives in
test_batch_id.py, test_changelog.py, test_branch.py and test_layout.py -- this file only guards
against the four regressions Phase 5 exists to prevent: reintroduced version semantics,
alphabetical-sort bugs, a second CFQ-imposed Git policy, and opaque/incomplete commit metadata.
"""

import re
import subprocess
import unittest

from cfq_testlib import PLUGIN_ROOT, SCRIPTS_DIR, CfqTestCase

SKILLS_DIR = PLUGIN_ROOT / "skills"


def grep(pattern, path, flags=re.MULTILINE):
    return re.search(pattern, path.read_text(), flags)


class BatchIdentityTest(CfqTestCase):
    def test_no_version_detection_surface_reintroduced(self):
        self.assertFalse(
            (SCRIPTS_DIR / "cfq-version.sh").exists(),
            msg="cfq-version.sh exists -- CFQ must not resolve application/plugin versions",
        )
        self.assertIsNone(
            grep(r'"(versionSource|appVersion|cfqVersion|versionDrift|batchSeq)":', SCRIPTS_DIR / "cfq_settings.py"),
            msg="cfq_settings.py schema defines a version-identity setting key",
        )

    def test_changelog_never_carries_version_field(self):
        self.assertIsNone(
            grep(r'"  (version|appVersion|cfqVersion|versionSource): ', SCRIPTS_DIR / "cfq_changelog.py"),
            msg="cfq_changelog.py renders a version-shaped changelog field",
        )

    def test_no_second_branch_version_increment_mechanism(self):
        self.assertIsNone(
            grep(r"v\[0-9\]", SCRIPTS_DIR / "cfq-branch.sh"),
            msg="cfq-branch.sh still contains vX.Y-style version-increment matching",
        )

    def test_settings_json_never_excluded(self):
        text = (SCRIPTS_DIR / "cfq-layout.sh").read_text()
        m = re.search(r"BLOCK_ENTRIES=\((?:\n.*){0,10}", text)
        block = m.group(0) if m else ""
        self.assertNotIn(
            "settings.json", block,
            msg="cfq-layout.sh's managed exclude block includes settings.json",
        )

    def test_gitstatepolicy_stays_single_switch(self):
        forbidden = re.compile(r"GIT_STATE_POLICY|GITPOLICY")
        for f in SCRIPTS_DIR.glob("*.sh"):
            self.assertIsNone(
                forbidden.search(f.read_text()),
                msg="a second Git-state-policy mechanism exists alongside gitStatePolicy",
            )

    def test_history_scan_keys_off_batch_number_only(self):
        text = (SCRIPTS_DIR / "cfq_changelog.py").read_text()
        for line in text.splitlines():
            if "--all" in line and "trailers:key=CFQ-Batch," in line:
                self.fail(
                    f"a history-wide scan keys off CFQ-Batch (human context) instead of "
                    f"CFQ-Batch-Number: {line}"
                )

    def test_phase_commit_has_required_trailers(self):
        lines = (SCRIPTS_DIR / "cfq_changelog.py").read_text().splitlines()
        block_lines = []
        capturing = False
        for line in lines:
            if '"interpret-trailers"' in line:
                capturing = True
            if capturing:
                block_lines.append(line)
                if "message_file" in line and "--trailer" not in line:
                    break
        block = "\n".join(block_lines)
        for t in ("CFQ-Batch-Number", "CFQ-Batch", "CFQ-Phase", "CFQ-Phase-Status"):
            self.assertIn(
                f'"{t}=', block, msg=f"commit-message trailer rendering is missing {t}"
            )

    def test_pfq_never_hand_rolls_batch_number_padding(self):
        skill = (SKILLS_DIR / "plan-for-queue" / "SKILL.md").read_text()
        self.assertIn(
            'bin/cfq" batch allocate', skill,
            msg="plan-for-queue/SKILL.md no longer calls bin/cfq batch allocate",
        )
        pattern = re.compile(r"printf '%0[0-9]+d'|printf \"%0[0-9]+d\"")
        for f in (SKILLS_DIR / "plan-for-queue").rglob("*"):
            if f.is_file():
                self.assertIsNone(
                    pattern.search(f.read_text(errors="ignore")),
                    msg="plan-for-queue hand-rolls zero-padding instead of delegating to the batch noun",
                )

    def test_fixed_width_numbered_names_sort_numerically(self):
        proc = subprocess.run(
            "printf '%s\\n' 100-x 001-x 999-x 010-x | sort",
            shell=True, capture_output=True, text=True, check=True,
        )
        expected = "001-x\n010-x\n100-x\n999-x\n"
        self.assertEqual(
            proc.stdout, expected,
            msg="fixed-width numbered identifiers do not sort in numeric order",
        )


if __name__ == "__main__":
    unittest.main()
