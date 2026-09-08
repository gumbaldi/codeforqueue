"""Migrated from test-ctx-usage.sh.

Self-test for scripts/ctx_usage.py: routine/boundary context-usage gate decisions, and the
five-hour/seven-day rate-limit reasons layered on top of the capacity reason. Boundary
percentages are exact numbers carried over unchanged from the Bash original, grouped into
self.subTest(...) loops so every boundary case reports in one run instead of stopping at the
first one that moves.
"""

import re
import unittest

from cfq_testlib import CfqTestCase

SID = "sid1"

GATE_LINE_RE = re.compile(
    r"^USED=(?P<used>[^ ]+) SIZE=(?P<size>[A-Z]) LIMIT=(?P<limit>-?[0-9]+) "
    r"(?P<verdict>START|WARN|HANDOFF) REASON=(?P<reason>[a-zA-Z]+) \((?P<note>.*)\)$"
)


class TestCtxUsage(CfqTestCase):
    def _run(self, *args, env=None):
        run_env = {"CLAUDE_CODE_SESSION_ID": SID}
        if env:
            run_env.update(env)
        return self.run_cfq("ctx", *args, env=run_env)

    def _write_payload(self, content):
        ctx_dir = self.home / ".claude" / ".ctx"
        ctx_dir.mkdir(parents=True, exist_ok=True)
        (ctx_dir / f"{SID}.json").write_text(content)

    def test_capacity_boundaries(self):
        cases = [
            dict(
                name="under limit gate", mode="gate", size="S",
                env={"CFQ_CTX_TEST_USED": "50000", "CFQ_STOP_USED": "100000"},
                want_all=["USED=50000", "SIZE=S", "LIMIT=100000", "START REASON=none"],
            ),
            dict(
                name="under limit no-arg", mode="noarg",
                env={"CFQ_CTX_TEST_USED": "50000", "CFQ_STOP_USED": "100000"},
                want_eq="USED=50000 OK REASON=none (test override)",
            ),
            dict(
                name="boundary gate (>=, not >)", mode="gate", size="M",
                env={"CFQ_CTX_TEST_USED": "100000", "CFQ_STOP_USED": "100000"},
                want_all=["USED=100000", "HANDOFF REASON=capacity"],
            ),
            dict(
                name="boundary no-arg", mode="noarg",
                env={"CFQ_CTX_TEST_USED": "100000", "CFQ_STOP_USED": "100000"},
                want_eq="USED=100000 STOP REASON=capacity (test override, used 100000 >= 100000)",
            ),
            dict(
                name="one token over", mode="noarg",
                env={"CFQ_CTX_TEST_USED": "100001", "CFQ_STOP_USED": "100000"},
                want_eq="USED=100001 STOP REASON=capacity (test override, used 100001 >= 100000)",
            ),
            dict(
                name="custom stopUsed", mode="gate", size="L",
                env={"CFQ_CTX_TEST_USED": "19999", "CFQ_STOP_USED": "20000"},
                want_all=["USED=19999", "LIMIT=20000", "START REASON=none"],
            ),
            dict(
                name="stopUsed=0 gate bypass (capacity only)", mode="gate", size="S",
                env={"CFQ_CTX_TEST_USED": "99999999", "CFQ_STOP_USED": "0"},
                want_all=["LIMIT=0", "START REASON=none"],
            ),
            dict(
                name="stopUsed=0 no-arg always stops", mode="noarg",
                env={"CFQ_CTX_TEST_USED": "1", "CFQ_STOP_USED": "0"},
                want_eq="USED=1 STOP REASON=capacity (test override, used 1 >= 0)",
            ),
            dict(
                name="stopUsed=-1 gate never capacity", mode="gate", size="L",
                env={"CFQ_CTX_TEST_USED": "99999999", "CFQ_STOP_USED": "-1"},
                want_all=["LIMIT=-1", "START REASON=none"],
            ),
            dict(
                name="stopUsed=-1 no-arg never capacity", mode="noarg",
                env={"CFQ_CTX_TEST_USED": "99999999", "CFQ_STOP_USED": "-1"},
                want_all=["OK REASON=none"],
            ),
        ]
        for case in cases:
            with self.subTest(case=case["name"]):
                args = ("gate", case["size"]) if case["mode"] == "gate" else ()
                out = self._run(*args, env=case["env"]).stdout.strip()
                if "want_eq" in case:
                    self.assertEqual(out, case["want_eq"], f"{case['name']} -> {out}")
                else:
                    for token in case["want_all"]:
                        self.assertIn(token, out, f"{case['name']} -> {out}")

    def test_stopused_zero_bypass_does_not_suppress_unknown_reason(self):
        # A throwaway HOME with no override, no payload, no transcript: an unresolved context
        # reading still surfaces as an advisory WARN even while the capacity bypass is active.
        out = self._run("gate", "S", env={"CFQ_STOP_USED": "0"}).stdout.strip()
        self.assertIn("LIMIT=0", out, out)
        self.assertIn("WARN REASON=unknown", out, out)

    def test_malformed_or_missing_size_defaults_to_m(self):
        env = {"CFQ_CTX_TEST_USED": "1", "CFQ_STOP_USED": "100000"}
        self.assertIn("SIZE=M", self._run("gate", "XL", env=env).stdout.strip())
        self.assertIn("SIZE=M", self._run("gate", env=env).stdout.strip())

    def test_unknown_used_warns_not_blocks(self):
        for args in [("gate", "M"), ()]:
            with self.subTest(mode="gate" if args else "no-arg"):
                out = self._run(*args, env={"CFQ_STOP_USED": "100000"}).stdout.strip()
                self.assertIn("USED=?", out, out)
                self.assertIn("WARN REASON=unknown", out, out)

    def test_malformed_stop_used_env_falls_back_to_schema_default(self):
        out = self._run(env={"CFQ_CTX_TEST_USED": "1", "CFQ_STOP_USED": "abc"}).stdout.strip()
        self.assertEqual(out, "USED=1 OK REASON=none (test override)")

    def test_rate_limit_five_hour_over_threshold_warns_not_blocks(self):
        self._write_payload(
            '{"context_window":{"used_percentage":1,"context_window_size":1000000,'
            '"current_usage":{"input_tokens":1000,"cache_creation_input_tokens":1000,'
            '"cache_read_input_tokens":1000}},'
            '"rate_limits":{"five_hour":{"used_percentage":80},"seven_day":{"used_percentage":10}}}'
        )
        out = self._run("gate", "M", env={"CFQ_STOP_USED": "100000"}).stdout.strip()
        self.assertIn("WARN REASON=fiveHour", out, out)
        self.assertIn("5h 80% >= 70", out, out)
        out = self._run(env={"CFQ_STOP_USED": "100000"}).stdout.strip()
        self.assertIn("WARN REASON=fiveHour", out, out)

    def test_rate_limit_seven_day_over_threshold_warns_not_blocks(self):
        self._write_payload(
            '{"context_window":{"used_percentage":1,"context_window_size":1000000,'
            '"current_usage":{"input_tokens":1000,"cache_creation_input_tokens":1000,'
            '"cache_read_input_tokens":1000}},'
            '"rate_limits":{"five_hour":{"used_percentage":10},"seven_day":{"used_percentage":97}}}'
        )
        out = self._run("gate", "M", env={"CFQ_STOP_USED": "100000"}).stdout.strip()
        self.assertIn("WARN REASON=sevenDay", out, out)
        self.assertIn("7d 97% >= 95", out, out)
        out = self._run(env={"CFQ_STOP_USED": "100000"}).stdout.strip()
        self.assertIn("WARN REASON=sevenDay", out, out)

    def test_rate_limits_below_threshold_capacity_over_stopused_handoffs(self):
        self._write_payload(
            '{"context_window":{"used_percentage":50,"context_window_size":1000000,'
            '"current_usage":{"input_tokens":60000,"cache_creation_input_tokens":30000,'
            '"cache_read_input_tokens":20000}},'
            '"rate_limits":{"five_hour":{"used_percentage":10},"seven_day":{"used_percentage":10}}}'
        )
        out = self._run("gate", "M", env={"CFQ_STOP_USED": "100000"}).stdout.strip()
        self.assertIn("HANDOFF REASON=capacity", out, out)
        self.assertIn("used 110000 >= 100000", out, out)

    def test_both_capacity_and_rate_limit_fire_capacity_wins_with_both_notes(self):
        # The precedence rule most likely to be got wrong: a rate-limit hit evaluated first must
        # not mask a real capacity blocker.
        self._write_payload(
            '{"context_window":{"used_percentage":50,"context_window_size":1000000,'
            '"current_usage":{"input_tokens":60000,"cache_creation_input_tokens":30000,'
            '"cache_read_input_tokens":20000}},'
            '"rate_limits":{"five_hour":{"used_percentage":80},"seven_day":{"used_percentage":10}}}'
        )
        out = self._run("gate", "M", env={"CFQ_STOP_USED": "100000"}).stdout.strip()
        self.assertIn("HANDOFF REASON=capacity", out, out)
        self.assertIn("5h 80% >= 70", out, out)
        self.assertIn("used 110000 >= 100000", out, out)

        out = self._run(env={"CFQ_STOP_USED": "100000"}).stdout.strip()
        self.assertIn("STOP REASON=capacity", out, out)
        self.assertIn("5h 80% >= 70", out, out)
        self.assertIn("used 110000 >= 100000", out, out)

    def test_no_rate_limits_key_behaves_like_context_only(self):
        self._write_payload(
            '{"context_window":{"used_percentage":50,"context_window_size":1000000,'
            '"current_usage":{"input_tokens":10000,"cache_creation_input_tokens":10000,'
            '"cache_read_input_tokens":10000}}}'
        )
        out = self._run("gate", "M", env={"CFQ_STOP_USED": "100000"}).stdout.strip()
        self.assertIn("START REASON=none", out, out)
        self.assertNotIn("5h", out, out)
        self.assertNotIn("7d", out, out)

    def test_stop_five_hour_pct_negative_one_disables_only_that_reason(self):
        self._write_payload(
            '{"context_window":{"used_percentage":1,"context_window_size":1000000,'
            '"current_usage":{"input_tokens":1000,"cache_creation_input_tokens":1000,'
            '"cache_read_input_tokens":1000}},'
            '"rate_limits":{"five_hour":{"used_percentage":99},"seven_day":{"used_percentage":10}}}'
        )
        out = self._run(
            "gate", "M", env={"CFQ_STOP_USED": "100000", "CFQ_STOP_FIVE_HOUR_PCT": "-1"},
        ).stdout.strip()
        self.assertIn("START REASON=none", out, out)

    def test_stop_used_negative_one_quiet_starts(self):
        self._write_payload(
            '{"context_window":{"used_percentage":1,"context_window_size":1000000,'
            '"current_usage":{"input_tokens":1000,"cache_creation_input_tokens":1000,'
            '"cache_read_input_tokens":1000}},'
            '"rate_limits":{"five_hour":{"used_percentage":10},"seven_day":{"used_percentage":10}}}'
        )
        out = self._run("gate", "M", env={"CFQ_STOP_USED": "-1"}).stdout.strip()
        self.assertIn("LIMIT=-1", out, out)
        self.assertIn("START REASON=none", out, out)

    def test_stop_used_negative_one_still_warns_on_rate_hit(self):
        # The regression this phase's short-circuit rewrite exists for: stopUsed=-1 alone must
        # not silently disable the rate check, but it also must no longer escalate a rate-only
        # hit to HANDOFF.
        self._write_payload(
            '{"context_window":{"used_percentage":1,"context_window_size":1000000,'
            '"current_usage":{"input_tokens":1000,"cache_creation_input_tokens":1000,'
            '"cache_read_input_tokens":1000}},'
            '"rate_limits":{"five_hour":{"used_percentage":80},"seven_day":{"used_percentage":10}}}'
        )
        out = self._run("gate", "M", env={"CFQ_STOP_USED": "-1"}).stdout.strip()
        self.assertIn("LIMIT=-1", out, out)
        self.assertIn("WARN REASON=fiveHour", out, out)
        self.assertIn("5h 80% >= 70", out, out)

    def test_all_three_disabled_starts_without_resolving_anything(self):
        disabled = {
            "CFQ_STOP_USED": "-1", "CFQ_STOP_FIVE_HOUR_PCT": "-1", "CFQ_STOP_SEVEN_DAY_PCT": "-1",
        }
        out = self._run("gate", "M", env=disabled).stdout.strip()
        self.assertIn("LIMIT=-1", out, out)
        self.assertIn("START REASON=none", out, out)
        out = self._run(env=disabled).stdout.strip()
        self.assertIn("OK REASON=none", out, out)

    def test_stop_used_zero_bypass_still_warns_on_rate_hit(self):
        self._write_payload(
            '{"context_window":{"used_percentage":1,"context_window_size":1000000,'
            '"current_usage":{"input_tokens":1000,"cache_creation_input_tokens":1000,'
            '"cache_read_input_tokens":1000}},'
            '"rate_limits":{"five_hour":{"used_percentage":80},"seven_day":{"used_percentage":10}}}'
        )
        out = self._run("gate", "M", env={"CFQ_STOP_USED": "0"}).stdout.strip()
        self.assertIn("WARN REASON=fiveHour", out, out)
        self.assertIn("5h 80% >= 70", out, out)

    def test_cache_share_is_display_only_on_start(self):
        self._write_payload(
            '{"context_window":{"used_percentage":1,"context_window_size":1000000,'
            '"current_usage":{"input_tokens":2,"cache_creation_input_tokens":298,'
            '"cache_read_input_tokens":9700}},'
            '"rate_limits":{"five_hour":{"used_percentage":28},"seven_day":{"used_percentage":10}}}'
        )
        out = self._run("gate", "M", env={"CFQ_STOP_USED": "100000"}).stdout.strip()
        self.assertIn("START REASON=none", out, out)
        self.assertIn("cache 97%", out, out)
        self.assertIn("5h 28%", out, out)
        self.assertIn("7d 10%", out, out)

    def test_gate_line_matches_ifq_preflight_capture_regex(self):
        # Same jq capture(...) expression as cfq-ifq-preflight.sh, copied here so the two cannot
        # drift apart.
        self._write_payload(
            '{"context_window":{"used_percentage":19,"context_window_size":1000000,'
            '"current_usage":{"input_tokens":2,"cache_creation_input_tokens":4000,'
            '"cache_read_input_tokens":180000}},'
            '"rate_limits":{"five_hour":{"used_percentage":28},"seven_day":{"used_percentage":10}}}'
        )
        out = self._run("gate", "M", env={"CFQ_STOP_USED": "100000"}).stdout.strip()
        self.assertRegex(out, GATE_LINE_RE, f"gate line failed preflight capture regex -> {out}")


if __name__ == "__main__":
    unittest.main()
