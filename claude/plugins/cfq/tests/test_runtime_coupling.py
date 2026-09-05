"""Migrated from test-runtime-coupling.sh.

Regression guard: the cross-skill-preflight batch's own new/modified scripts must never read
Claude-Code-runtime state directly — everything Claude-Code-specific goes through cfq-runtime.sh.
Scoped to this batch's own files only, not the whole codebase.
"""

import re
import unittest

from cfq_testlib import SCRIPTS_DIR, CfqTestCase

# cfq-runtime.sh itself is the one allowed adapter and is deliberately excluded here.
FILES = [
    SCRIPTS_DIR / "cfq-pfq-preflight.sh",
    SCRIPTS_DIR / "cfq-ifq-preflight.sh",
    SCRIPTS_DIR / "cfq_report.py",
    SCRIPTS_DIR / "cfq-scan.sh",
]

FORBIDDEN = re.compile(r"CLAUDE_CODE_SESSION_ID|\.claude/\.ctx|\.claude/projects")


class RuntimeCouplingTest(CfqTestCase):
    def test_no_direct_runtime_coupling(self):
        for f in FILES:
            if not f.is_file():
                continue
            hits = [line for line in f.read_text().splitlines() if FORBIDDEN.search(line)]
            self.assertEqual(
                hits, [], msg=f"runtime-coupling literal found in {f}:\n" + "\n".join(hits)
            )


if __name__ == "__main__":
    unittest.main()
