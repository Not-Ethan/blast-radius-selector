"""Key-level narrowing of edits of string-keyed dict entries (see selector/keyrec.py).

For a commit whose class-body or module-level edits change entries of string-keyed
dicts, fingerprint the state of every sqlglot class and module after importing the
pinned tree (twice, two hash seeds; once per run) and the patched tree (once per
commit), each in its own interpreter. If the only differences are entries of string-keyed
dicts that the build-time trace recorded (tools/trace_keys.py), each such site's
candidates are cut down to the tests that looked up a changed key of a changed dict, or
enumerated it, plus every test without a record. Anything else (another value changed,
a dict without records, an import that fails, a timeout) leaves the candidates as they
were.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_PINNED: list = []  # [pinned1, pinned2] or [None] when unavailable
_CODE = ("import sys, json\n"
         "sys.path.insert(0, {parent!r})\n"
         "from selector import keyrec\n"
         "json.dump(keyrec.state_fingerprint(), sys.stdout)\n")


def _start(tree: str, seed: int):
    env = dict(os.environ, PYTHONPATH=tree, PYTHONHASHSEED=str(seed), PYTHONDONTWRITEBYTECODE="1",
               SKIP_INTEGRATION="0")
    code = _CODE.format(parent=os.path.dirname(HERE))
    return subprocess.Popen([sys.executable, "-c", code], cwd=tree, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)


def _finish(proc, timeout):
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def refine(found, changes, root: str, tmp: str, maps, log, timeout: float = 60.0) -> str:
    """Narrow the key-record sites of `found` in place. Returns a note for the log."""
    from .verify import build_overlay

    rec = getattr(maps, "keyrec", None)
    sites = [(f, i) for f in found.files for i in getattr(f, "keyrec_sites", [])]
    if not rec or not sites:
        return ""
    if any(c.binary for c in changes):
        return "keys: binary change"
    files = {}
    for change in changes:
        report = next((f for f in found.files if f.path == change.path), None)
        if change.old_path and change.is_rename:
            files[change.old_path] = None
        if change.is_deleted:
            files[change.old_path] = None
            continue
        if report is None or report.new_text is None:
            return "keys: change without text"
        files[change.path] = report.new_text.encode("utf-8")
    overlay = os.path.join(tmp, "keys_overlay")
    shutil.rmtree(overlay, ignore_errors=True)
    try:
        build_overlay(overlay, root, files)
    except Exception as error:  # noqa: BLE001
        return f"keys: overlay failed ({error!r})"
    procs = {"patched": _start(overlay, 0)}
    if not _PINNED:
        procs["p1"] = _start(root, 1)
        procs["p2"] = _start(root, 2)
    results = {name: _finish(proc, timeout) for name, proc in procs.items()}
    shutil.rmtree(overlay, ignore_errors=True)
    if not _PINNED:
        _PINNED.extend([results.get("p1"), results.get("p2")])
    pinned1, pinned2 = _PINNED[0], _PINNED[1]
    patched = results["patched"]
    if pinned1 is None or pinned2 is None or patched is None:
        return "keys: fingerprint unavailable"
    from .keyrec import changed_entries

    changed_paths = {c.path for c in changes} | {c.old_path for c in changes if c.old_path}
    changed = changed_entries(pinned1, pinned2, patched, changed_paths)
    if changed is None:
        return "keys: other state changed"
    if not changed:
        return "keys: no dict entry changed"
    unrecordable = set(rec.get("unrecordable", ()))
    holders = set(rec.get("holders", ()))
    lookups = rec.get("lookups", {})
    narrowed = 0
    for holder, keys in changed.items():
        if holder in unrecordable or holder not in holders:
            return f"keys: {holder} has no usable records"
        by_key = lookups.get(holder, {})
        for k in set(keys) | {"*"}:
            narrowed |= maps.mask(by_key.get(k))
    untraced = maps.all_mask & ~maps.mask(rec.get("traced"))
    keep = narrowed | untraced
    for report, index in sites:
        report.site_masks[index] &= keep
        imp = 0
        for j in getattr(report, "imp_sites", []):
            imp |= report.site_masks[j]
        report.imp = imp
    return f"keys: {sum(len(k) for k in changed.values())} entries of {len(changed)} dicts"
