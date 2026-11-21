"""Owner-typed coverage of the tokenizer core (build-time / offline tool).

See selector/tokcore.py: a tokenizer setting reaches the core as a field, read
on known lines of sqlglot/tokenizer_core.py. For every such line, record per
test the classes of the Tokenizer objects whose cores ran it ("owners"; a core
not created through Tokenizer._init_core is recorded with the owner "?", which
matches every class at decision time).

Like tools/trace_typed.py, each test runs alone in a child forked from a zygote
that never imported sqlglot or the tests. Records of a test that fails or times
out here are dropped; the selector keeps every test without a record.

Usage: python tools/trace_tokcore.py <sqlglot checkout> <tests.json> <out.json> [--jobs N] [--limit S]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import trace_tests  # noqa: E402

from selector import tokcore  # noqa: E402


def child(root, node, lines, out_path, limit):
    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    mon.use_tool_id(tool, "blast-radius-tokcore")
    core_path = os.path.join(root, tokcore.CORE_FILE)
    getframe = sys._getframe
    owner_of: dict = {}
    keep: list = []
    record: dict = {}
    mro: dict = {}

    def note(cls):
        key = f"{cls.__module__}:{cls.__qualname__}"
        if key not in mro:
            mro[key] = sorted({f"{c.__module__}:{c.__qualname__}" for c in cls.__mro__})
        return key

    import sqlglot.tokens as tokens_mod

    original = tokens_mod.Tokenizer._init_core

    def _init_core(self):
        core = original(self)
        owner_of[id(core)] = note(type(self))
        keep.append(core)  # ids stay unique while recorded
        return core

    tokens_mod.Tokenizer._init_core = _init_core

    def on_line(code, line):
        if code.co_filename != core_path or line not in lines:
            return mon.DISABLE
        try:
            obj = getframe(1).f_locals.get("self")
        except Exception:  # noqa: BLE001
            obj = None
        owner = owner_of.get(id(obj), "?")
        seen = record.get(line)
        if seen is None:
            record[line] = {owner}
        else:
            seen.add(owner)
        return None

    mon.register_callback(tool, mon.events.LINE, on_line)
    from selector.runner import run_tests

    mon.set_events(tool, mon.events.LINE)
    try:
        failed, _ = run_tests([node], limit_of=lambda n: limit)
    finally:
        mon.set_events(tool, 0)
    data = {"failed": node in failed, "mro": mro, "lines": {str(k): sorted(v) for k, v in record.items()}}
    with open(out_path, "w") as handle:
        json.dump(data, handle)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("tests_json")
    ap.add_argument("out")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--limit", type=float, default=300.0)
    o = ap.parse_args()
    root = os.path.realpath(o.repo)
    o.out = os.path.abspath(o.out)
    parts = o.out + ".parts"
    os.makedirs(parts, exist_ok=True)
    tests = json.load(open(o.tests_json))["tests"]
    os.chdir(root)
    sys.path.insert(0, root)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    source = open(os.path.join(root, tokcore.CORE_FILE), encoding="utf-8").read()
    lines = {line for obs in tokcore.observers(source).values() for line, _ in obs}
    print(f"{len(lines)} observer lines", file=sys.stderr)
    trace_tests.preload()
    pending = [t for t in sorted(tests, key=lambda t: -t["seconds"])
               if not os.path.exists(os.path.join(parts, f"{t['number']}.json"))]
    running = {}
    done = 0
    while pending or running:
        while pending and len(running) < o.jobs:
            t = pending.pop(0)
            part = os.path.join(parts, f"{t['number']}.json")
            pid = os.fork()
            if pid == 0:
                code = 0
                try:
                    devnull = os.open(os.devnull, os.O_WRONLY)
                    os.dup2(devnull, 1)
                    os.dup2(devnull, 2)
                    child(root, t["id"], lines, part + ".tmp", o.limit)
                    os.rename(part + ".tmp", part)
                except BaseException:  # noqa: BLE001
                    code = 1
                finally:
                    os._exit(code)
            running[pid] = t
        pid, status = os.wait()
        t = running.pop(pid, None)
        done += 1
        if t and status != 0:
            print(f"tokcore trace failed for {t['id']} (status {status})", file=sys.stderr)
        if done % 100 == 0:
            print(f"{done} done, {len(pending)} pending", file=sys.stderr)
    merged, mro, dropped = {}, {}, []
    number_to_id = {str(t["number"]): t["id"] for t in tests}
    for name in os.listdir(parts):
        if not name.endswith(".json"):
            continue
        data = json.load(open(os.path.join(parts, name)))
        node = number_to_id[name[:-5]]
        if data["failed"]:
            dropped.append(node)
            continue
        merged[node] = data["lines"]
        for k, v in data["mro"].items():
            mro.setdefault(k, set()).update(v)
    missing = sorted(set(number_to_id.values()) - set(merged) - set(dropped))
    json.dump({"lines": merged, "mro": {k: sorted(v) for k, v in mro.items()}, "dropped": sorted(dropped),
               "missing": missing}, open(o.out, "w"))
    print(f"tokcore records for {len(merged)} tests; dropped {dropped}; missing {len(missing)}", file=sys.stderr)


if __name__ == "__main__":
    main()
