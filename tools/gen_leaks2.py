"""Generate copy/aliasing edits in code that many tests run (dev tool).

Sites on lines that at least --min tests ran (pinned coverage map): `copy=True` ->
`copy=False` in calls, a `copy: bool = True` default -> False, `if copy` / `copy and` /
`copy or` negated, `x.copy()` -> `x`, `deepcopy(x)` -> `x`, and statements whose text
mentions copy replaced by `pass`. Label with tools/label_pytest.py --all.

Usage: python tools/gen_leaks2.py <sqlglot checkout> <out dir> <count> [seed] [--min N]
"""

from __future__ import annotations

import ast
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gen_mutants import git_diff  # noqa: E402
from selector.maps import Maps  # noqa: E402


def sites(text):
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    out = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        a = func.args
        pos = a.posonlyargs + a.args
        for arg, d in list(zip(pos[len(pos) - len(a.defaults):], a.defaults)) + list(zip(a.kwonlyargs, a.kw_defaults)):
            if arg.arg == "copy" and isinstance(d, ast.Constant) and d.value is True:
                out.append(("default_copy_false", d.lineno, d.col_offset, d.end_col_offset, "False", func.lineno))
        for node in ast.walk(func):
            if not hasattr(node, "lineno") or node.lineno != getattr(node, "end_lineno", None):
                continue
            if isinstance(node, ast.keyword) and node.arg == "copy" and isinstance(node.value, ast.Constant) \
                    and node.value.value is True:
                v = node.value
                out.append(("copy_kw_false", v.lineno, v.col_offset, v.end_col_offset, "False", node.lineno))
            if isinstance(node, ast.Name) and node.id == "copy" and isinstance(node.ctx, ast.Load):
                out.append(("copy_negated", node.lineno, node.col_offset, node.end_col_offset, "(not copy)", node.lineno))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "copy" \
                    and not node.args and not node.keywords:
                inner = node.func.value
                out.append(("copy_removed", node.lineno, node.col_offset, node.end_col_offset,
                            ast.get_source_segment(text, inner), node.lineno))
            if isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords \
                    and ast.unparse(node.func) in ("deepcopy", "copy.deepcopy", "copy.copy"):
                out.append(("deepcopy_removed", node.lineno, node.col_offset, node.end_col_offset,
                            ast.get_source_segment(text, node.args[0]), node.lineno))
        for stmt in ast.walk(func):
            if isinstance(stmt, (ast.Assign, ast.Expr, ast.AugAssign)) and stmt.lineno == stmt.end_lineno \
                    and re.search(r"\bcopy\b", lines[stmt.lineno - 1]) and stmt is not func.body[0]:
                line = lines[stmt.lineno - 1]
                indent = len(line) - len(line.lstrip())
                out.append(("copy_stmt_deleted", stmt.lineno, indent, len(line.rstrip("\n").encode()), "pass", stmt.lineno))
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    minimum = 20
    if "--min" in sys.argv:
        minimum = int(sys.argv[sys.argv.index("--min") + 1])
        args = [a for a in args if a != str(minimum)]
    repo, out, count = args[0], args[1], int(args[2])
    seed = int(args[3]) if len(args) > 3 else 9
    rng = random.Random(seed)
    maps = Maps()
    pool = []
    for d, _, fs in os.walk(os.path.join(repo, "sqlglot")):
        for f in fs:
            if not f.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(d, f), repo)
            text = open(os.path.join(repo, rel), encoding="utf-8").read()
            for site in sites(text):
                n = len(maps.tests_of(maps.func_mask(rel, [site[5]]) | maps.entry_mask(rel, [site[5]])))
                if n >= minimum:
                    pool.append((rel,) + site + (n,))
    by_kind = {}
    for p in pool:
        by_kind.setdefault(p[1], []).append(p)
    for v in by_kind.values():
        rng.shuffle(v)
    chosen = []
    while len(chosen) < count and any(by_kind.values()):
        for k in sorted(by_kind):
            if by_kind[k] and len(chosen) < count:
                chosen.append(by_kind[k].pop())
    os.makedirs(os.path.join(out, "commits"), exist_ok=True)
    meta = {}
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
    for rel, kind, lineno, a, b, repl, _, n in chosen:
        text = open(os.path.join(repo, rel), encoding="utf-8").read()
        lines = text.splitlines(keepends=True)
        raw = lines[lineno - 1].encode("utf-8")
        new_line = (raw[:a] + repl.encode() + raw[b:]).decode("utf-8")
        new = "".join(lines[: lineno - 1] + [new_line] + lines[lineno:])
        if new == text:
            continue
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        cid = "m" + "".join(rng.choice(alphabet) for _ in range(3))
        while cid in meta:
            cid = "m" + "".join(rng.choice(alphabet) for _ in range(3))
        with open(os.path.join(out, "commits", cid + ".patch"), "w") as handle:
            handle.write(git_diff(rel, text, new))
        meta[cid] = {"kind": kind, "tests_on_line": n,
                     "desc": [f"{rel}:{lineno} {lines[lineno - 1].strip()!r} -> {new_line.strip()!r}"]}
    json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=1)
    print(f"wrote {len(meta)} patches; pool {len(pool)} sites: "
          + ", ".join(f"{k} {len([p for p in pool if p[1] == k])}" for k in sorted({p[1] for p in pool})), file=sys.stderr)


if __name__ == "__main__":
    main()
