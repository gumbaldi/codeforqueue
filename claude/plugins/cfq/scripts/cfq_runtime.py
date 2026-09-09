#!/usr/bin/env python3
# Usage: cfq_runtime.py session-id
#        cfq_runtime.py transcript-path [--repo <path>] [--exact]
#        cfq_runtime.py context
#        cfq_runtime.py model
#        cfq_runtime.py version
#        cfq_runtime.py capabilities
#        cfq_runtime.py plugins
#        cfq_runtime.py plugin-installed <name>
#        cfq_runtime.py diagnose [--repo <path>]
"""Owns everything specific to the Claude Code runtime: session id, transcript path resolution,
context-window usage (statusline payload primary, transcript fallback), model, version, installed
plugins. One file so a future Claude Code interface change only ever touches this one adapter.
Concrete to Claude Code -- not a multi-runtime plugin system.

Ported from cfq-runtime.sh -- a port, not a redesign: the CLI contract (verbs, argument order,
JSON shapes, text output, exit codes) is the invariant this file preserves.
"""

import json
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cfq_lib import render  # noqa: E402

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CFQ_BIN = SCRIPT_DIR.parent / "bin" / "cfq"

STALE_PAYLOAD_SECONDS = 600
PONYTAIL_MODES = ("off", "lite", "full", "ultra")


def arg_error(message):
    print(render.dump_json({"code": "INVALID_ARGUMENT", "message": message}), file=sys.stderr)
    sys.exit(1)


def home_dir():
    return pathlib.Path(os.environ.get("HOME") or str(pathlib.Path.home()))


def mtime(path):
    try:
        return int(os.stat(path).st_mtime)
    except OSError:
        return 0


def slug_for(repo_path):
    if repo_path:
        return repo_path.replace("/", "-")
    return os.getcwd().replace("/", "-")


def resolve_transcript_path(repo_path, exact):
    slug = slug_for(repo_path)
    d = home_dir() / ".claude" / "projects" / slug
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if exact == "1":
        return str(d / f"{sid}.jsonl") if sid else ""
    if sid and (d / f"{sid}.jsonl").is_file():
        return str(d / f"{sid}.jsonl")
    try:
        files = [p for p in d.glob("*.jsonl") if p.is_file()]
    except OSError:
        files = []
    if not files:
        return ""
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return str(files[0])


def ctx_window_limit_for(model):
    proc = subprocess.run(
        [str(CFQ_BIN), "settings", "get", "ctxWindowLimits"], capture_output=True, text=True
    )
    try:
        limits = json.loads(proc.stdout) if proc.returncode == 0 else {}
    except ValueError:
        limits = {}
    large = limits.get("large") or {}
    if model and model in (large.get("models") or []):
        base = large.get("limit", 1000000)
    else:
        base = limits.get("default", 200000)
    override = os.environ.get("CFQ_CTX_LIMIT") or os.environ.get("CLAUDE_CTX_LIMIT")
    return int(override) if override else int(base)


def classify_payload(path):
    """Returns (kind, extra) -- kind is one of ok/not-yet-populated/schema-mismatch/invalid-json.
    extra is a field dict for "ok", a detail string for "schema-mismatch", None otherwise."""
    try:
        data = json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return "invalid-json", None
    if not isinstance(data, dict) or data.get("context_window") is None:
        return "schema-mismatch", "context_window key missing"
    cw = data["context_window"]
    size = cw.get("context_window_size", 0)
    if not isinstance(size, (int, float)):
        size = 0
    if size <= 0:
        return "not-yet-populated", None
    usage = cw.get("current_usage") or {}
    input_tokens = usage.get("input_tokens") or 0
    cache_creation = usage.get("cache_creation_input_tokens") or 0
    cache_read = usage.get("cache_read_input_tokens") or 0
    used = input_tokens + cache_creation + cache_read
    if used <= 0:
        return "schema-mismatch", "current_usage token fields sum to 0"
    used_percentage = cw.get("used_percentage")
    pct = int(used_percentage) if used_percentage is not None else int(used / size * 100)
    rate_limits = data.get("rate_limits") or {}
    five_hour = (rate_limits.get("five_hour") or {}).get("used_percentage")
    seven_day = (rate_limits.get("seven_day") or {}).get("used_percentage")
    return "ok", {
        "pct": pct,
        "used": used,
        "windowSize": size,
        "fresh": input_tokens,
        "cacheCreation": cache_creation,
        "cacheRead": cache_read,
        "fiveHourPct": int(five_hour) if five_hour is not None else -1,
        "sevenDayPct": int(seven_day) if seven_day is not None else -1,
        "model": (data.get("model") or {}).get("id"),
    }


def list_plugins():
    d = home_dir() / ".claude" / "plugins" / "cache"
    if not (d.is_dir() and os.access(d, os.R_OK)):
        return []
    names = set()
    for marketplace in d.iterdir():
        if not marketplace.is_dir():
            continue
        for plugin in marketplace.iterdir():
            if plugin.is_dir():
                names.add(plugin.name)
    return sorted(names)


def plugins_capability_code():
    d = home_dir() / ".claude" / "plugins" / "cache"
    return "" if (d.is_dir() and os.access(d, os.R_OK)) else "CAPABILITY_UNAVAILABLE"


def ponytail_mode():
    env_mode = os.environ.get("PONYTAIL_DEFAULT_MODE", "").lower()
    if env_mode in PONYTAIL_MODES:
        return env_mode
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(home_dir() / ".config")
    cfg = pathlib.Path(xdg) / "ponytail" / "config.json"
    if cfg.is_file():
        try:
            data = cfg.read_bytes()
            if data.startswith(b"\xef\xbb\xbf"):
                data = data[3:]
            mode = str(json.loads(data.decode("utf-8")).get("defaultMode", "")).lower()
            if mode in PONYTAIL_MODES:
                return mode
        except (OSError, ValueError, UnicodeDecodeError):
            pass
    return "full"


def add_source(sources, name, tried, used, code, detail):
    sources.append({"name": name, "tried": tried, "used": used, "code": code or None, "detail": detail})


def build_diagnostic(observed, fallback_status, fallback_code=None):
    d = {
        "capability": "contextUsage",
        "source": "statusline-payload",
        "expected": "context_window.used_percentage or current_usage with context_window_size",
        "observed": observed,
        "fallbackStatus": fallback_status,
    }
    if fallback_code is not None:
        d["fallbackCode"] = fallback_code
    d["repairScope"] = "cfq-runtime.sh"
    d["researchHint"] = "current Claude Code statusline JSON schema"
    return d


def last_assistant_line(path):
    try:
        text = pathlib.Path(path).read_text()
    except OSError:
        return ""
    last = ""
    for raw in text.splitlines():
        if '"type":"assistant"' in raw and '"isSidechain":true' not in raw:
            last = raw
    return last


def parse_transcript_line(line):
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    usage = (obj.get("message") or {}).get("usage") or {}
    input_tokens = usage.get("input_tokens") or 0
    cache_read = usage.get("cache_read_input_tokens") or 0
    cache_creation = usage.get("cache_creation_input_tokens") or 0
    return {
        "used": input_tokens + cache_read + cache_creation,
        "model": (obj.get("message") or {}).get("model") or "?",
        "fresh": input_tokens,
        "cacheCreation": cache_creation,
        "cacheRead": cache_read,
    }


def last_line(path):
    try:
        text = pathlib.Path(path).read_text()
    except OSError:
        return ""
    lines = text.splitlines()
    return lines[-1] if lines else ""


def do_resolve(repo_path):
    state = {
        "status": "", "pct": None, "windowSize": None, "used": None, "model": "", "source": "",
        "note": "", "code": "", "diagnostic": None, "fresh": None, "cacheCreation": None,
        "cacheRead": None, "fiveHourPct": None, "sevenDayPct": None,
    }
    sources = []
    primary_code = None
    primary_detail = None

    test_pct = os.environ.get("CFQ_CTX_TEST_PCT", "")
    if test_pct:
        if not test_pct.isdigit():
            add_source(sources, "test-override", True, False, "", "CFQ_CTX_TEST_PCT not numeric, ignored")
        else:
            state["pct"] = int(test_pct)
            state["source"] = "test-override"
            state["note"] = "test override"
            state["status"] = "ok"
            add_source(sources, "test-override", True, True, "", f"pct={test_pct}")
    else:
        add_source(sources, "test-override", True, False, "", "CFQ_CTX_TEST_PCT not set")

    test_used = os.environ.get("CFQ_CTX_TEST_USED", "")
    if test_used:
        if not test_used.isdigit():
            add_source(sources, "test-override-used", True, False, "", "CFQ_CTX_TEST_USED not numeric, ignored")
        else:
            state["used"] = int(test_used)
            state["source"] = "test-override"
            state["note"] = "test override"
            state["status"] = "ok"
            add_source(sources, "test-override-used", True, True, "", f"used={test_used}")
    else:
        add_source(sources, "test-override-used", True, False, "", "CFQ_CTX_TEST_USED not set")

    primary_needed = state["pct"] is None and state["used"] is None
    if primary_needed:
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        p = home_dir() / ".claude" / ".ctx" / f"{sid}.json"
        if sid and p.is_file():
            age = int(time.time()) - mtime(p)
            if age < STALE_PAYLOAD_SECONDS:
                kind, extra = classify_payload(p)
                if kind == "ok":
                    state["pct"] = extra["pct"]
                    state["used"] = extra["used"]
                    state["windowSize"] = extra["windowSize"]
                    state["fresh"] = extra["fresh"]
                    state["cacheCreation"] = extra["cacheCreation"]
                    state["cacheRead"] = extra["cacheRead"]
                    state["fiveHourPct"] = extra["fiveHourPct"]
                    state["sevenDayPct"] = extra["sevenDayPct"]
                    state["model"] = extra["model"] or ""
                    state["source"] = "payload"
                    state["status"] = "ok"
                    state["note"] = f'{extra["used"]}/{extra["windowSize"]}, src=payload'
                    add_source(sources, "statusline-payload", True, True, "", state["note"])
                elif kind == "not-yet-populated":
                    add_source(sources, "statusline-payload", True, False, "", "context_window_size <= 0, not yet populated")
                elif kind == "invalid-json":
                    primary_code, primary_detail = "RUNTIME_PAYLOAD_INVALID", "payload file is not valid JSON"
                    add_source(sources, "statusline-payload", True, False, primary_code, primary_detail)
                elif kind == "schema-mismatch":
                    primary_code, primary_detail = "RUNTIME_SCHEMA_MISMATCH", extra
                    add_source(sources, "statusline-payload", True, False, primary_code, primary_detail)
            else:
                add_source(sources, "statusline-payload", True, False, "", f"payload stale (age {age}s >= {STALE_PAYLOAD_SECONDS}s)")
        else:
            reason = "no CLAUDE_CODE_SESSION_ID" if not sid else "payload file not found"
            add_source(sources, "statusline-payload", False, False, "", reason)
    else:
        add_source(sources, "statusline-payload", False, False, "", "not needed, test-override already usable")

    transcript_path = resolve_transcript_path(repo_path, "0")
    transcript_available = bool(transcript_path) and os.path.isfile(transcript_path)

    if state["pct"] is None and state["used"] is None:
        if not transcript_available:
            add_source(sources, "transcript", True, False, "RUNTIME_SOURCE_MISSING", "no transcript file found")
            if not primary_code:
                state["status"], state["code"], state["note"] = "unavailable", "RUNTIME_SOURCE_MISSING", "no transcript found"
            else:
                state["status"], state["code"], state["note"] = "unavailable", primary_code, primary_detail
                state["diagnostic"] = build_diagnostic(primary_detail, "unavailable", "RUNTIME_SOURCE_MISSING")
        else:
            line = last_assistant_line(transcript_path)
            parsed = parse_transcript_line(line)
            if parsed is None or parsed["used"] <= 0:
                add_source(sources, "transcript", True, False, "FALLBACK_FAILED", "transcript found but no usable usage data")
                if not primary_code:
                    state["status"], state["code"], state["note"] = "unavailable", "FALLBACK_FAILED", "transcript found but no usable usage data"
                else:
                    state["status"], state["code"], state["note"] = "unavailable", primary_code, primary_detail
                    state["diagnostic"] = build_diagnostic(primary_detail, "unavailable", "FALLBACK_FAILED")
            else:
                window_size = ctx_window_limit_for(parsed["model"])
                state["used"] = parsed["used"]
                state["windowSize"] = window_size
                state["fresh"] = parsed["fresh"]
                state["cacheCreation"] = parsed["cacheCreation"]
                state["cacheRead"] = parsed["cacheRead"]
                if parsed["model"] != "?":
                    state["model"] = parsed["model"]
                state["pct"] = parsed["used"] * 100 // window_size
                note = f'{parsed["used"]}/{window_size}, src=transcript'
                add_source(sources, "transcript", True, True, "", note)
                if not primary_code:
                    state["status"], state["source"], state["note"] = "ok", "transcript", note
                else:
                    state["status"], state["source"], state["code"] = "degraded", "transcript", primary_code
                    state["note"] = f"{note}, runtime=degraded:{primary_code}"
                    state["diagnostic"] = build_diagnostic(primary_detail, "ok")
    else:
        add_source(sources, "transcript", False, False, "", "not needed, primary source already usable")

    state["sources"] = sources
    state["transcript_path"] = transcript_path
    state["transcript_available"] = transcript_available
    return state


def result_json(state):
    cache = None
    if state["fresh"] is not None and state["cacheCreation"] is not None and state["cacheRead"] is not None:
        cache = {"fresh": state["fresh"], "creation": state["cacheCreation"], "read": state["cacheRead"]}
    rate_limits = None
    if (
        state["fiveHourPct"] is not None and state["fiveHourPct"] != -1
        and state["sevenDayPct"] is not None and state["sevenDayPct"] != -1
    ):
        rate_limits = {"fiveHourPct": state["fiveHourPct"], "sevenDayPct": state["sevenDayPct"]}
    return {
        "status": state["status"],
        "pct": state["pct"],
        "windowSize": state["windowSize"],
        "used": state["used"],
        "model": state["model"] or None,
        "source": state["source"] or None,
        "note": state["note"] or None,
        "code": state["code"] or None,
        "diagnostic": state["diagnostic"],
        "cache": cache,
        "rateLimits": rate_limits,
    }


USAGE = (
    "usage: cfq-runtime.sh session-id | transcript-path [--repo <path>] | context | model | "
    "version | capabilities | plugins | plugin-installed <name> | diagnose [--repo <path>]"
)


def main(argv):
    cmd = argv[0] if argv else ""
    rest = argv[1:]

    plugin_name = ""
    if cmd == "plugin-installed":
        plugin_name = rest[0] if rest else ""
        rest = rest[1:]

    repo_path = ""
    exact = "0"
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--repo":
            if i + 1 >= len(rest):
                arg_error("--repo requires a path")
            repo_path = rest[i + 1]
            i += 2
        elif a == "--exact":
            exact = "1"
            i += 1
        else:
            arg_error("unrecognized argument")

    if cmd == "session-id":
        print(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    elif cmd == "transcript-path":
        print(resolve_transcript_path(repo_path, exact))
    elif cmd == "context":
        print(render.dump_json(result_json(do_resolve(""))))
    elif cmd == "model":
        state = do_resolve("")
        print(render.dump_json(state["model"] or None))
    elif cmd == "version":
        tp = resolve_transcript_path("", "0")
        v = None
        if tp and os.path.isfile(tp):
            try:
                v = json.loads(last_line(tp)).get("version") or None
            except ValueError:
                v = None
        print(render.dump_json(v))
    elif cmd == "capabilities":
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        p = home_dir() / ".claude" / ".ctx" / f"{sid}.json"
        statusline = bool(sid and p.is_file() and (int(time.time()) - mtime(p)) < STALE_PAYLOAD_SECONDS)
        tp = resolve_transcript_path("", "0")
        transcript = bool(tp) and os.path.isfile(tp)
        print(render.dump_json({"statuslinePayload": statusline, "transcriptAvailable": transcript}))
    elif cmd == "plugins":
        plugins = list_plugins()
        pony_mode = ponytail_mode() if "ponytail" in plugins else "unknown"
        print(render.dump_json({"status": "OK", "plugins": plugins, "ponytailMode": pony_mode}))
    elif cmd == "plugin-installed":
        if not plugin_name:
            arg_error("plugin-installed requires a plugin name")
        print(render.dump_json({"installed": plugin_name in list_plugins()}))
    elif cmd == "diagnose":
        state = do_resolve(repo_path)
        ccv = None
        if state["transcript_available"]:
            try:
                ccv = json.loads(last_line(state["transcript_path"])).get("version") or None
            except ValueError:
                ccv = None
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        p = home_dir() / ".claude" / ".ctx" / f"{sid}.json"
        sp = bool(sid and p.is_file() and (int(time.time()) - mtime(p)) < STALE_PAYLOAD_SECONDS)
        result = {
            "sessionId": sid or None,
            "transcriptPath": state["transcript_path"] or None,
            "sources": state["sources"],
            "result": result_json(state),
            "capabilities": {"statuslinePayload": sp, "transcriptAvailable": state["transcript_available"]},
            "ccVersion": ccv,
            "code": state["code"] or None,
            "pluginsCacheCode": plugins_capability_code() or None,
        }
        print(render.dump_json(result))
    else:
        arg_error(USAGE)


if __name__ == "__main__":
    main(sys.argv[1:])
