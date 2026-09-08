"""Migrated from test-runtime.sh.

Self-test for scripts/cfq-runtime.sh, the one Claude-Code-specific adapter: session id,
transcript path resolution, context-window usage (statusline payload primary, transcript
fallback), model, version, installed plugins, ponytail mode. Fabricates the runtime state --
transcript files, statusline payloads, session ids -- under a fresh HOME per test.
"""

import json
import os
import pathlib
import shutil
import tempfile
import time
import unittest

from cfq_testlib import CfqTestCase

SID = "sid1"
SLUG = str(pathlib.Path.cwd()).replace("/", "-")

CORE_BINS = [
    "bash", "python3", "git", "head", "ls", "date", "stat", "printf", "mkdir", "tr", "pwd",
    "sed", "grep", "cat", "dirname", "mv", "rm", "find", "sort",
]


class TestRuntime(CfqTestCase):
    def _new_home(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        home = pathlib.Path(d.name)
        (home / ".claude" / ".ctx").mkdir(parents=True)
        (home / ".claude" / "projects" / SLUG).mkdir(parents=True)
        return home

    def _pony_home(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        home = pathlib.Path(d.name)
        (home / ".claude" / "plugins" / "cache" / "ponytail" / "ponytail" / "4.8.4").mkdir(parents=True)
        (home / "xdg" / "ponytail").mkdir(parents=True)
        return home

    def _write_payload(self, home, content):
        (home / ".claude" / ".ctx" / f"{SID}.json").write_text(content)

    def _write_transcript(self, home, model, input_tokens, cache_read, cache_creation, version, usable=True):
        usage = (
            {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            }
            if usable
            else {}
        )
        obj = {
            "type": "assistant",
            "isSidechain": False,
            "message": {"model": model, "usage": usage},
            "version": version,
        }
        (home / ".claude" / "projects" / SLUG / f"{SID}.jsonl").write_text(
            json.dumps(obj, separators=(",", ":")) + "\n"
        )

    def _run(self, *args, home, env=None):
        run_env = {"CLAUDE_CODE_SESSION_ID": SID}
        if env:
            run_env.update(env)
        return self.run_cfq("runtime", *args, home=home, env=run_env)

    # -- context: statusline payload primary source -----------------------------------------

    def test_valid_statusline_payload(self):
        h = self._new_home()
        self._write_payload(
            h,
            '{"context_window":{"used_percentage":21,"context_window_size":200000,'
            '"current_usage":{"input_tokens":1,"cache_creation_input_tokens":1,'
            '"cache_read_input_tokens":1}}}',
        )
        out = self.json_out(self._run("context", home=h))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["source"], "payload")
        self.assertEqual(out["pct"], 21)

    def test_payload_missing_used_percentage_is_computed(self):
        h = self._new_home()
        self._write_payload(
            h,
            '{"context_window":{"context_window_size":200000,"current_usage":'
            '{"input_tokens":20000,"cache_creation_input_tokens":10000,'
            '"cache_read_input_tokens":12000}}}',
        )
        out = self.json_out(self._run("context", home=h))
        self.assertEqual(out["source"], "payload")
        self.assertEqual(out["pct"], 21)
        self.assertEqual(out["used"], 42000)

    def test_stale_payload_falls_through_to_transcript(self):
        h = self._new_home()
        self._write_payload(
            h,
            '{"context_window":{"used_percentage":99,"context_window_size":200000,'
            '"current_usage":{"input_tokens":1,"cache_creation_input_tokens":1,'
            '"cache_read_input_tokens":1}}}',
        )
        payload_file = h / ".claude" / ".ctx" / f"{SID}.json"
        stale = time.time() - 700
        os.utime(payload_file, (stale, stale))
        self._write_transcript(h, "claude-sonnet-5", 40000, 1000, 1000, "2.1.0")
        out = self.json_out(self._run("context", home=h))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["source"], "transcript", "stale must fall through")
        self.assertIsNone(out["code"], "staleness is not structural")

    def test_malformed_payload_json_with_transcript_fallback_degrades(self):
        h = self._new_home()
        self._write_payload(h, "not json")
        self._write_transcript(h, "claude-sonnet-5", 40000, 1000, 1000, "2.1.0")
        out = self.json_out(self._run("context", home=h))
        self.assertEqual(out["status"], "degraded")
        self.assertEqual(out["code"], "RUNTIME_PAYLOAD_INVALID")
        self.assertIsNotNone(out["pct"], "pct should be usable")
        self.assertEqual(out["diagnostic"]["repairScope"], "cfq-runtime.sh")

    def test_schema_mismatch_payload_with_transcript_fallback_degrades(self):
        h = self._new_home()
        self._write_payload(h, '{"foo":"bar"}')
        self._write_transcript(h, "claude-sonnet-5", 40000, 1000, 1000, "2.1.0")
        out = self.json_out(self._run("context", home=h))
        self.assertEqual(out["status"], "degraded")
        self.assertEqual(out["code"], "RUNTIME_SCHEMA_MISMATCH")
        self.assertEqual(out["diagnostic"]["repairScope"], "cfq-runtime.sh")
        self.assertTrue(out["diagnostic"]["researchHint"], "researchHint empty")

    def test_missing_or_not_yet_populated_payload_with_transcript_no_false_mismatch(self):
        h = self._new_home()
        self._write_transcript(h, "claude-sonnet-5", 40000, 1000, 1000, "2.1.0")
        out = self.json_out(self._run("context", home=h))
        self.assertEqual(out["status"], "ok")
        self.assertIsNone(out["code"])

        h2 = self._new_home()
        self._write_payload(h2, '{"context_window":{"context_window_size":0}}')
        self._write_transcript(h2, "claude-sonnet-5", 40000, 1000, 1000, "2.1.0")
        out2 = self.json_out(self._run("context", home=h2))
        self.assertEqual(out2["status"], "ok")
        self.assertIsNone(out2["code"])

    def test_negative_context_window_size_falls_through(self):
        h = self._new_home()
        self._write_payload(h, '{"context_window":{"used_percentage":50,"context_window_size":-1}}')
        self._write_transcript(h, "claude-sonnet-5", 40000, 1000, 1000, "2.1.0")
        out = self.json_out(self._run("context", home=h))
        self.assertEqual(out["source"], "transcript")

    def test_no_payload_no_transcript_unavailable(self):
        h = self._new_home()
        out = self.json_out(self._run("context", home=h))
        self.assertEqual(out["status"], "unavailable")
        self.assertIsNone(out["pct"])
        self.assertEqual(out["code"], "RUNTIME_SOURCE_MISSING")

    def test_transcript_without_usable_usage_is_fallback_failed(self):
        h = self._new_home()
        self._write_transcript(h, "claude-sonnet-5", 0, 0, 0, "2.1.0", usable=False)
        out = self.json_out(self._run("context", home=h))
        self.assertEqual(out["status"], "unavailable")
        self.assertIsNone(out["pct"])
        self.assertEqual(out["code"], "FALLBACK_FAILED")

    def test_primary_failure_with_missing_or_failed_fallback_keeps_primary_code(self):
        h = self._new_home()
        self._write_payload(h, '{"foo":"bar"}')
        out = self.json_out(self._run("context", home=h))
        self.assertEqual(out["status"], "unavailable")
        self.assertEqual(out["code"], "RUNTIME_SCHEMA_MISMATCH")
        self.assertEqual(out["diagnostic"]["fallbackCode"], "RUNTIME_SOURCE_MISSING")

        h2 = self._new_home()
        self._write_payload(h2, "not json")
        out2 = self.json_out(self._run("context", home=h2))
        self.assertEqual(out2["status"], "unavailable")
        self.assertEqual(out2["code"], "RUNTIME_PAYLOAD_INVALID")
        self.assertEqual(out2["diagnostic"]["fallbackCode"], "RUNTIME_SOURCE_MISSING")

        h3 = self._new_home()
        self._write_payload(h3, '{"foo":"bar"}')
        self._write_transcript(h3, "claude-sonnet-5", 0, 0, 0, "2.1.0", usable=False)
        out3 = self.json_out(self._run("context", home=h3))
        self.assertEqual(out3["status"], "unavailable")
        self.assertEqual(out3["code"], "RUNTIME_SCHEMA_MISMATCH")
        self.assertEqual(out3["diagnostic"]["fallbackCode"], "FALLBACK_FAILED")

    # -- overrides, model window sizing, version -----------------------------------------------

    def test_ctx_test_pct_short_circuits(self):
        h = self._new_home()
        out = self.json_out(self._run("context", home=h, env={"CFQ_CTX_TEST_PCT": "33"}))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["source"], "test-override")
        self.assertEqual(out["pct"], 33)

    def test_model_window_size_lookup_and_ctx_limit_override(self):
        h = self._new_home()
        self._write_transcript(h, "claude-opus-5", 10000, 0, 0, "2.1.0")
        out = self.json_out(self._run("context", home=h))
        self.assertEqual(out["windowSize"], 1000000, "large-model windowSize")

        h2 = self._new_home()
        self._write_transcript(h2, "claude-unknown-model", 10000, 0, 0, "2.1.0")
        out2 = self.json_out(self._run("context", home=h2))
        self.assertEqual(out2["windowSize"], 200000, "default windowSize")
        out3 = self.json_out(self._run("context", home=h2, env={"CFQ_CTX_LIMIT": "555555"}))
        self.assertEqual(out3["windowSize"], 555555, "CFQ_CTX_LIMIT override")

    def test_version_reads_from_last_transcript_line(self):
        h = self._new_home()
        self._write_transcript(h, "claude-sonnet-5", 10000, 0, 0, "9.9.9")
        proc = self._run("version", home=h)
        self.assertEqual(proc.stdout.strip(), '"9.9.9"')

    # -- plugins / plugin-installed / diagnose -------------------------------------------------

    def test_plugins_and_plugin_installed_with_known_plugins(self):
        h = self._new_home()
        (h / ".claude" / "plugins" / "cache" / "marketA" / "pluginX" / "1.0.0").mkdir(parents=True)
        (h / ".claude" / "plugins" / "cache" / "marketB" / "pluginY" / "2.0.0").mkdir(parents=True)
        out = self.json_out(self._run("plugins", home=h))
        self.assertEqual(out["status"], "OK")
        self.assertEqual(sorted(out["plugins"]), ["pluginX", "pluginY"])
        self.assertTrue(self.json_out(self._run("plugin-installed", "pluginX", home=h))["installed"])
        self.assertFalse(self.json_out(self._run("plugin-installed", "unknown", home=h))["installed"])

    def test_plugins_missing_cache_source_no_crash(self):
        h = self._new_home()
        out = self.json_out(self._run("plugins", home=h))
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["plugins"], [])
        self.assertFalse(self.json_out(self._run("plugin-installed", "anything", home=h))["installed"])
        diag = self.json_out(self._run("diagnose", home=h))
        self.assertEqual(diag["pluginsCacheCode"], "CAPABILITY_UNAVAILABLE")

    def test_ponytail_mode_not_installed_is_unknown(self):
        h = self._pony_home()
        shutil.rmtree(h / ".claude" / "plugins" / "cache" / "ponytail")
        out = self.json_out(self._run(
            "plugins", home=h,
            env={"XDG_CONFIG_HOME": str(h / "xdg"), "PONYTAIL_DEFAULT_MODE": "off"},
        ))
        self.assertEqual(out["ponytailMode"], "unknown")

    def test_ponytail_mode_env_wins_over_config(self):
        h = self._pony_home()
        (h / "xdg" / "ponytail" / "config.json").write_text('{"defaultMode":"off"}')
        out = self.json_out(self._run(
            "plugins", home=h,
            env={"XDG_CONFIG_HOME": str(h / "xdg"), "PONYTAIL_DEFAULT_MODE": "ultra"},
        ))
        self.assertEqual(out["ponytailMode"], "ultra")

    def test_ponytail_mode_garbage_env_falls_through_to_config(self):
        h = self._pony_home()
        (h / "xdg" / "ponytail" / "config.json").write_text('{"defaultMode":"lite"}')
        out = self.json_out(self._run(
            "plugins", home=h,
            env={"XDG_CONFIG_HOME": str(h / "xdg"), "PONYTAIL_DEFAULT_MODE": "bogus"},
        ))
        self.assertEqual(out["ponytailMode"], "lite")

    def test_ponytail_mode_config_off(self):
        h = self._pony_home()
        (h / "xdg" / "ponytail" / "config.json").write_text('{"defaultMode":"off"}')
        out = self.json_out(self._run("plugins", home=h, env={"XDG_CONFIG_HOME": str(h / "xdg")}))
        self.assertEqual(out["ponytailMode"], "off")

    def test_ponytail_mode_config_with_bom(self):
        h = self._pony_home()
        (h / "xdg" / "ponytail" / "config.json").write_bytes(b"\xef\xbb\xbf" + b'{"defaultMode":"lite"}')
        out = self.json_out(self._run("plugins", home=h, env={"XDG_CONFIG_HOME": str(h / "xdg")}))
        self.assertEqual(out["ponytailMode"], "lite")

    def test_ponytail_mode_invalid_config_json_falls_back_to_full(self):
        h = self._pony_home()
        (h / "xdg" / "ponytail" / "config.json").write_text("{not valid")
        out = self.json_out(self._run("plugins", home=h, env={"XDG_CONFIG_HOME": str(h / "xdg")}))
        self.assertEqual(out["ponytailMode"], "full")

    def test_ponytail_mode_no_config_file_is_full(self):
        h = self._pony_home()
        shutil.rmtree(h / "xdg")
        out = self.json_out(self._run("plugins", home=h, env={"XDG_CONFIG_HOME": str(h / "xdg")}))
        self.assertEqual(out["ponytailMode"], "full")

    def test_missing_python3_reports_named_guard_message_on_every_subcommand(self):
        nopython_dir = self.minimal_path(*[b for b in CORE_BINS if b != "python3"])
        nopython_home = self._new_home()
        guard = "cfq: python3 is required for 'runtime' but was not found on PATH."
        for sub in (
            "session-id", "transcript-path", "context", "model", "version", "capabilities",
            "plugins", "diagnose",
        ):
            with self.subTest(sub=sub):
                proc = self._run(sub, home=nopython_home, env={"PATH": nopython_dir})
                self.assertEqual(proc.returncode, 127, f"{sub} exit={proc.returncode} (want 127)")
                self.assertIn(guard, proc.stderr, f"{sub} message")
        proc = self._run("plugin-installed", "pluginX", home=nopython_home, env={"PATH": nopython_dir})
        self.assertEqual(proc.returncode, 127, f"plugin-installed exit={proc.returncode} (want 127)")
        self.assertIn(guard, proc.stderr, "plugin-installed message")

    def test_diagnose_always_well_formed_even_on_total_failure(self):
        h = self._new_home()
        out = self.json_out(self._run("diagnose", home=h))
        self.assertGreaterEqual(len(out["sources"]), 3)
        self.assertIn("result", out)
        self.assertEqual(out["code"], "RUNTIME_SOURCE_MISSING")

    # -- transcript-path ------------------------------------------------------------------------

    def test_transcript_path_pwd_slug_vs_repo_slug(self):
        h = self._new_home()
        (h / ".claude" / "projects" / SLUG / f"{SID}.jsonl").write_text("{}\n")
        otherrepo_dir = tempfile.TemporaryDirectory()
        self.addCleanup(otherrepo_dir.cleanup)
        otherrepo = pathlib.Path(otherrepo_dir.name)
        otherslug = str(otherrepo).replace("/", "-")
        (h / ".claude" / "projects" / otherslug).mkdir(parents=True)
        (h / ".claude" / "projects" / otherslug / f"{SID}.jsonl").write_text("{}\n")

        proc = self._run("transcript-path", home=h)
        self.assertEqual(
            proc.stdout.strip(), str(h / ".claude" / "projects" / SLUG / f"{SID}.jsonl"), "pwd-slug",
        )
        proc = self._run("transcript-path", "--repo", str(otherrepo), home=h)
        self.assertEqual(
            proc.stdout.strip(), str(h / ".claude" / "projects" / otherslug / f"{SID}.jsonl"), "repo-slug",
        )

    # -- model field on the context result --------------------------------------------------

    def test_payload_carries_a_model_context_reports_it(self):
        h = self._new_home()
        self._write_payload(
            h,
            '{"context_window":{"used_percentage":21,"context_window_size":200000,'
            '"current_usage":{"input_tokens":1,"cache_creation_input_tokens":1,'
            '"cache_read_input_tokens":1}},"model":{"id":"claude-opus-5","display_name":"Opus 5"}}',
        )
        out = self.json_out(self._run("context", home=h))
        self.assertEqual(out["source"], "payload")
        self.assertEqual(out["model"], "claude-opus-5")

    def test_payload_without_model_key_degrades_to_none(self):
        h = self._new_home()
        self._write_payload(
            h,
            '{"context_window":{"used_percentage":21,"context_window_size":200000,'
            '"current_usage":{"input_tokens":1,"cache_creation_input_tokens":1,'
            '"cache_read_input_tokens":1}}}',
        )
        out = self.json_out(self._run("context", home=h))
        self.assertEqual(out["source"], "payload")
        self.assertEqual(out["status"], "ok")
        self.assertIsNone(out["model"])

    def test_transcript_branch_reports_its_own_model(self):
        h = self._new_home()
        self._write_transcript(h, "claude-sonnet-5", 40000, 1000, 1000, "2.1.0")
        out = self.json_out(self._run("context", home=h))
        self.assertEqual(out["source"], "transcript")
        self.assertEqual(out["model"], "claude-sonnet-5")

    # -- full real-shape payloads, cache and rate-limit fields -------------------------------

    def test_full_shape_with_cache_split_and_rate_limits(self):
        h = self._new_home()
        self._write_payload(
            h,
            '{"context_window":{"used_percentage":19,"context_window_size":1000000,'
            '"current_usage":{"input_tokens":2,"output_tokens":10,'
            '"cache_creation_input_tokens":4000,"cache_read_input_tokens":180000}},'
            '"rate_limits":{"five_hour":{"used_percentage":28},"seven_day":{"used_percentage":10}}}',
        )
        out = self.json_out(self._run("context", home=h))
        self.assertEqual(out["used"], 184002)
        self.assertEqual(out["pct"], 19)
        self.assertEqual(out["cache"]["fresh"], 2)
        self.assertEqual(out["cache"]["creation"], 4000)
        self.assertEqual(out["cache"]["read"], 180000)
        self.assertEqual(out["rateLimits"]["fiveHourPct"], 28)
        self.assertEqual(out["rateLimits"]["sevenDayPct"], 10)

    def test_pct_computed_from_summed_usage_when_used_percentage_missing(self):
        h = self._new_home()
        self._write_payload(
            h,
            '{"context_window":{"context_window_size":1000000,"current_usage":'
            '{"input_tokens":2,"output_tokens":10,"cache_creation_input_tokens":4000,'
            '"cache_read_input_tokens":180000}},'
            '"rate_limits":{"five_hour":{"used_percentage":28},"seven_day":{"used_percentage":10}}}',
        )
        out = self.json_out(self._run("context", home=h))
        self.assertEqual(out["used"], 184002)
        self.assertEqual(out["pct"], 18)

    def test_invented_schema_keys_summing_to_zero_are_rejected(self):
        h = self._new_home()
        self._write_payload(
            h,
            '{"context_window":{"used_percentage":50,"context_window_size":200000,'
            '"current_usage":{"input":1,"creation":1,"read":1}}}',
        )
        self._write_transcript(h, "claude-sonnet-5", 40000, 1000, 1000, "2.1.0")
        out = self.json_out(self._run("context", home=h))
        self.assertEqual(out["status"], "degraded")
        self.assertEqual(out["code"], "RUNTIME_SCHEMA_MISMATCH")
        self.assertEqual(out["source"], "transcript")
        self.assertIsNotNone(out["diagnostic"], "diagnostic missing")

    def test_current_usage_missing_entirely_no_transcript_unavailable(self):
        h = self._new_home()
        self._write_payload(h, '{"context_window":{"context_window_size":200000}}')
        out = self.json_out(self._run("context", home=h))
        self.assertEqual(out["status"], "unavailable")
        self.assertEqual(out["code"], "RUNTIME_SCHEMA_MISMATCH")

    def test_no_rate_limits_key_stays_null_no_warning(self):
        h = self._new_home()
        self._write_payload(
            h,
            '{"context_window":{"used_percentage":19,"context_window_size":1000000,'
            '"current_usage":{"input_tokens":2,"cache_creation_input_tokens":4000,'
            '"cache_read_input_tokens":180000}}}',
        )
        out = self.json_out(self._run("context", home=h))
        self.assertEqual(out["status"], "ok")
        self.assertIsNone(out["rateLimits"])

    def test_transcript_fallback_carries_cache_split_rate_limits_stays_null(self):
        h = self._new_home()
        self._write_transcript(h, "claude-sonnet-5", 40000, 1000, 2000, "2.1.0")
        out = self.json_out(self._run("context", home=h))
        self.assertEqual(out["source"], "transcript")
        self.assertEqual(out["cache"]["fresh"], 40000)
        self.assertEqual(out["cache"]["creation"], 2000)
        self.assertEqual(out["cache"]["read"], 1000)
        self.assertIsNone(out["rateLimits"])


if __name__ == "__main__":
    unittest.main()
