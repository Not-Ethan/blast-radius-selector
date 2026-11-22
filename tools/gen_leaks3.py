"""Generate copy/aliasing edits where a leak between tests is most plausible (dev tool).

Kinds (each site on a line that at least one test runs, pinned coverage map):
* shared_container_copy_removed: `dict(X)` / `list(X)` / `set(X)` / `X.copy()` /
  `copy.copy(X)` / `X[:]` / `{**X}` / `[*X]` -> `X`, where X is a class- or module-level
  object (module global, `self.UPPER`, `self.<class-level name>`, `cls.X`, `Class.X`): the
  function now returns or stores the shared container itself;
* shared_expr_copy_removed: `X.copy()` / `deepcopy(X)` -> `X` on a shared expression
  (templates, module constants);
* copy_kw_false: `copy=True` -> `copy=False` in sqlglot/optimizer, generators, dialects,
  parsers, transforms;
* copy_param_false: `copy=copy` -> `copy=False` in the same places;
* mutable_default: `def f(..., x=None)` whose body starts its use with
  `if x is None: x = <display>` -> default `<display>`, the check replaced by `pass`.
Label with tools/label_pytest.py --all.

Usage: python tools/gen_leaks3.py <sqlglot checkout> <out dir> [seed]
"""

from __future__ import annotations

import ast
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gen_mutants import git_diff  # noqa: E402
from selector import leak  # noqa: E402
from selector.maps import Maps  # noqa: E402

QUOTA = {"shared_container_copy_removed": 20, "shared_expr_copy_removed": 6, "copy_kw_false": 10,
         "copy_param_false": 6, "mutable_default": 8, "default_copy_false": 14, "copy_false_added": 14}
BUILDERS = {"alias_", "column", "select", "where", "from_", "join", "subquery", "and_", "or_", "not_", "cast",
            "paren", "to_identifier", "condition", "maybe_parse", "replace_placeholders", "union", "intersect",
            "except_", "as_", "order_by", "group_by", "limit", "eq", "isin", "having", "with_", "qualify"}
COPY_DIRS = ("sqlglot/optimizer", "sqlglot/generators", "sqlglot/dialects", "sqlglot/parsers",
             "sqlglot/transforms.py", "sqlglot/generator.py", "sqlglot/parser.py")


def funcs_with_info(tree):
    leak.CLASS_LEVEL[0] = leak._class_level_names(tree)
    names = leak._module_names(tree)
    return leak._changed_functions(tree, set(range(1, 10 ** 6)), names)


def is_shared(f, node):
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id[:1].isupper() \
            and node.value.id not in f.locals:
        return True  # Class.ATTR / MODULE.attr
    return f.shared(node)


def sites(rel, text):
    tree = ast.parse(text)
    out = []
    for f in funcs_with_info(tree):
        for node in leak._walk_own(f.func):
            line = getattr(node, "lineno", None)
            if line is None or line != getattr(node, "end_lineno", None):
                continue
            seg = lambda n: ast.get_source_segment(text, n)  # noqa: E731
            # container copies of shared objects
            if isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords:
                fn = ast.unparse(node.func)
                if fn in ("dict", "list", "set", "copy.copy") and is_shared(f, node.args[0]):
                    out.append(("shared_container_copy_removed", node, seg(node.args[0])))
                if fn in ("deepcopy", "copy.deepcopy") and is_shared(f, node.args[0]):
                    out.append(("shared_expr_copy_removed", node, seg(node.args[0])))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "copy" \
                    and not node.args and not node.keywords and is_shared(f, node.func.value):
                kind = "shared_container_copy_removed" if node.func.value and any(
                    isinstance(node.func.value, t) for t in (ast.Name, ast.Attribute)) else None
                # expression templates are UPPERCASE class attributes holding expressions; containers too:
                # decide by name: *_TEMPLATE / module constants of expressions -> expr
                name = seg(node.func.value) or ""
                kind = "shared_expr_copy_removed" if ("TEMPLATE" in name or "_SEQ" in name) else kind
                if kind:
                    out.append((kind, node, seg(node.func.value)))
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice) and node.slice.lower is None \
                    and node.slice.upper is None and node.slice.step is None and isinstance(node.ctx, ast.Load) \
                    and is_shared(f, node.value):
                out.append(("shared_container_copy_removed", node, seg(node.value)))
            if isinstance(node, ast.Dict) and len(node.keys) == 1 and node.keys[0] is None and is_shared(f, node.values[0]):
                out.append(("shared_container_copy_removed", node, seg(node.values[0])))
            if isinstance(node, ast.List) and len(node.elts) == 1 and isinstance(node.elts[0], ast.Starred) \
                    and is_shared(f, node.elts[0].value):
                out.append(("shared_container_copy_removed", node, seg(node.elts[0].value)))
            if rel.startswith(COPY_DIRS) and isinstance(node, ast.Call) and not any(k.arg == "copy" for k in node.keywords) \
                    and not any(k.arg is None for k in node.keywords) \
                    and (node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")) in BUILDERS \
                    and not (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                             and node.func.value.id in ("t", "typing")) \
                    and node.end_col_offset and text.splitlines()[node.lineno - 1][node.end_col_offset - 1:node.end_col_offset] == ")":
                out.append(("copy_false_added", node, None))
            if rel.startswith(COPY_DIRS) and isinstance(node, ast.keyword) and node.arg == "copy":
                if isinstance(node.value, ast.Constant) and node.value.value is True:
                    out.append(("copy_kw_false", node.value, "False"))
                if isinstance(node.value, ast.Name) and node.value.id == "copy":
                    out.append(("copy_param_false", node.value, "False"))
        # copy defaults flipped towards no copy
        a0 = f.func.args
        pos0 = a0.posonlyargs + a0.args
        for arg, d in list(zip(pos0[len(pos0) - len(a0.defaults):], a0.defaults)) + list(zip(a0.kwonlyargs, a0.kw_defaults)):
            if arg.arg == "copy" and isinstance(d, ast.Constant) and d.value is True:
                out.append(("default_copy_false", d, "False"))
        # mutable default: def f(..., x=None) + `if x is None: x = <display>`
        a = f.func.args
        pos = a.posonlyargs + a.args
        pairs = list(zip(pos[len(pos) - len(a.defaults):], a.defaults)) + [
            (k, d) for k, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None]
        for arg, d in pairs:
            if not (isinstance(d, ast.Constant) and d.value is None):
                continue
            for stmt in f.func.body:
                if isinstance(stmt, ast.If) and not stmt.orelse and len(stmt.body) == 1 \
                        and isinstance(stmt.test, ast.Compare) and isinstance(stmt.test.left, ast.Name) \
                        and stmt.test.left.id == arg.arg and isinstance(stmt.test.ops[0], ast.Is) \
                        and isinstance(stmt.body[0], ast.Assign) and isinstance(stmt.body[0].targets[0], ast.Name) \
                        and stmt.body[0].targets[0].id == arg.arg \
                        and isinstance(stmt.body[0].value, (ast.Dict, ast.List, ast.Set, ast.Call)) \
                        and d.lineno == d.end_lineno:
                    display = ast.get_source_segment(text, stmt.body[0].value)
                    if display and "\n" not in display and display in ("{}", "[]", "set()", "dict()", "list()"):
                        out.append(("mutable_default", (d, stmt), display))
    return out


def f_line(text, line):
    """First line of the def whose header contains `line`."""
    lines = text.splitlines()
    k = line - 1
    while k > 0 and not lines[k].lstrip().startswith(("def ", "async def ")):
        k -= 1
    return k + 1


def apply(text, kind, node, repl):
    lines = text.splitlines(keepends=True)
    if kind == "mutable_default":
        d, stmt = node
        # replace the if-statement lines by `pass` first (they come after the default)
        indent = lines[stmt.lineno - 1][: len(lines[stmt.lineno - 1]) - len(lines[stmt.lineno - 1].lstrip())]
        new_lines = lines[: stmt.lineno - 1] + [indent + "pass\n"] + lines[stmt.end_lineno:]
        raw = new_lines[d.lineno - 1].encode()
        new_lines[d.lineno - 1] = (raw[: d.col_offset] + repl.encode() + raw[d.end_col_offset:]).decode()
        return "".join(new_lines), d.lineno
    raw = lines[node.lineno - 1].encode()
    if kind == "copy_false_added":
        end = node.end_col_offset - 1  # the closing parenthesis
        inner = raw[node.col_offset:end].decode()
        sep = "" if inner.rstrip().endswith("(") else ", "
        lines[node.lineno - 1] = (raw[:end] + (sep + "copy=False").encode() + raw[end:]).decode()
        return "".join(lines), node.lineno
    lines[node.lineno - 1] = (raw[: node.col_offset] + repl.encode() + raw[node.end_col_offset:]).decode()
    return "".join(lines), node.lineno


def main():
    repo, out = sys.argv[1], sys.argv[2]
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else 31
    rng = random.Random(seed)
    maps = Maps()
    pool = {}
    for d, _, fs in os.walk(os.path.join(repo, "sqlglot")):
        for fname in fs:
            if not fname.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(d, fname), repo)
            text = open(os.path.join(repo, rel), encoding="utf-8").read()
            try:
                found = sites(rel, text)
            except Exception as error:  # noqa: BLE001
                print("skip", rel, error, file=sys.stderr)
                continue
            for kind, node, repl in found:
                line = node[0].lineno if kind == "mutable_default" else node.lineno
                body_line = node[1].lineno if kind == "mutable_default" else line
                n = len(maps.tests_of(maps.func_mask(rel, [body_line]) | maps.func_mask(rel, [line])))
                if kind == "default_copy_false":  # a default runs at import; its users call the function
                    n = len(maps.tests_of(maps.entry_mask(rel, [f_line(text, line)])))
                if n >= 1:
                    pool.setdefault(kind, []).append((rel, kind, node, repl, n))
    os.makedirs(os.path.join(out, "commits"), exist_ok=True)
    meta = {}
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
    for kind, items in sorted(pool.items()):
        rng.shuffle(items)
        taken = 0
        seen_lines = set()
        for rel, kind, node, repl, n in items:
            if taken >= QUOTA[kind]:
                break
            text = open(os.path.join(repo, rel), encoding="utf-8").read()
            new, line = apply(text, kind, node, repl)
            if new == text or (rel, line) in seen_lines:
                continue
            try:
                ast.parse(new)
            except SyntaxError:
                continue
            seen_lines.add((rel, line))
            cid = "q" + "".join(rng.choice(alphabet) for _ in range(3))
            while cid in meta:
                cid = "q" + "".join(rng.choice(alphabet) for _ in range(3))
            with open(os.path.join(out, "commits", cid + ".patch"), "w") as handle:
                handle.write(git_diff(rel, text, new))
            old_line = text.splitlines()[line - 1].strip()
            new_line = new.splitlines()[line - 1].strip()
            meta[cid] = {"kind": kind, "tests_on_line": n, "desc": [f"{rel}:{line} {old_line!r} -> {new_line!r}"]}
            taken += 1
    json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=1)
    print(f"wrote {len(meta)}; pool: " + ", ".join(f"{k} {len(v)}" for k, v in sorted(pool.items())), file=sys.stderr)


if __name__ == "__main__":
    main()
