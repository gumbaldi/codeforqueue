"""The one place a result becomes either JSON or human-readable text — see CLAUDE.md's "No
duplicate renderers" invariant. Every port that emits both forms of the same value goes through
here rather than growing a second, drifting formatter.
"""

import json


def dump_json(obj):
    """Compact JSON, matching `jq -c`'s formatting: no extra whitespace, unicode kept raw."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def dump_json_pretty(obj):
    """2-space indented JSON, matching jq's default (non `-c`) pretty-print byte-for-byte."""
    return json.dumps(obj, indent=2, ensure_ascii=False)


def tostring(value):
    """Mirrors jq's `tostring` filter: a string passes through unquoted, everything else
    becomes its compact JSON form (numbers, bools and null included)."""
    if isinstance(value, str):
        return value
    return dump_json(value)
