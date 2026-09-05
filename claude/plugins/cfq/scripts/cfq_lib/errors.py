"""Shared CLI error helpers.

`error_object` builds the {status, detail, action} shape read-only aggregators use (see
CLAUDE.md's "Status Vocabulary"). `die` and `fail` are the two ways a command stops early: plain
text for a script whose CLI contract is plain-text errors (e.g. cfq_settings.py, ported
byte-for-byte from cfq-settings.sh), or that same {status, detail, action} shape for one whose
contract is structured — both to stderr, both exiting with the given code.
"""

import sys

from . import render


def error_object(status, detail=None, action=None):
    obj = {"status": status}
    if detail is not None:
        obj["detail"] = detail
    if action is not None:
        obj["action"] = action
    return obj


def die(message, code=1):
    print(message, file=sys.stderr)
    sys.exit(code)


def fail(status, detail=None, action=None, code=1):
    print(render.dump_json(error_object(status, detail, action)), file=sys.stderr)
    sys.exit(code)
