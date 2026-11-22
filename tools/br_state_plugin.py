"""pytest plugin (dev/build tool): fingerprint the state of every sqlglot class and module
(selector.keyrec.state_fingerprint) after collection and again at the end of the session,
and write the holders whose fingerprint changed to $BR_STATE_OUT (json)."""

import json
import os
import sys

sys.path.insert(0, os.environ.get("BR_SELECTOR_PARENT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_BEFORE = {}


def pytest_collection_finish(session):
    from selector import keyrec

    keyrec.EXPR_POSITION[0] = False
    _BEFORE.update(keyrec.state_fingerprint(loaded_roots=("tests",))["state"])


def pytest_sessionfinish(session, exitstatus):
    from selector import keyrec

    after = keyrec.state_fingerprint(loaded_roots=("tests",))["state"]
    changed = sorted(k for k in set(_BEFORE) | set(after) if _BEFORE.get(k) != after.get(k))
    keys = {}
    for k in changed:
        a, b = _BEFORE.get(k) or {}, after.get(k) or {}
        if "d" in a and "d" in b:
            keys[k] = sorted(x for x in set(a["d"]) | set(b["d"]) if a["d"].get(x) != b["d"].get(x))[:50]
    out = os.environ.get("BR_STATE_OUT")
    if out:
        with open(out, "w") as handle:
            json.dump({"changed": changed, "keys": keys, "holders": len(after), "exitstatus": int(exitstatus)}, handle, indent=0)
