"""Edits that may leak state from one test to later tests in the same process.

The function-edit rule ("a test that never runs a changed line cannot change its
result") holds for a test run alone. pytest runs the whole suite in one process (one
full-suite pytest run in tests.json order reproduces the kit sample's answers exactly),
so an edit can also change what *later* tests see, through an object that outlives a
test: a class attribute or table, a module global, a registry. A test that never runs
the edit can then fail. Measured: a run-time `type(self).LIMIT_FETCH = "FETCH"` in a
DuckDB generator helper fails 10 tests that never run it (each passes alone); M22 left
all 10 out. Verification has the same blind spot: it runs candidates in a fresh process,
so a candidate that fails only after another candidate ran passes there unless the two
run in that order in the same child.

classify() sorts an edit of a .py file:
* hard: an edited function's original body already writes to an object that outlives a
  test (9 functions in sqlglot at the pinned commit, e.g. the parser's CONNECT BY
  helper that sets and pops a parser-table entry). Every test is included.
* soft: the edit introduces such a write, or changes copying or aliasing (a changed
  line mentions `copy`, a container copy or an in-place operator is added or removed),
  `global`/`nonlocal` declarations, a mutable default or a caching decorator. The
  commit is narrowed as usual, then every candidate runs in one verification child in
  pytest order, and the state of every sqlglot class and module (and every loaded test
  module) is fingerprinted before and after the run (state_leaks). Any change the
  pinned suite itself never makes, a candidate too expensive to run, or a check that
  cannot run, includes every test.
"""

from __future__ import annotations

import ast
import re

MUTATORS = {"pop", "popitem", "update", "setdefault", "append", "extend", "insert", "remove", "clear", "sort",
            "reverse", "add", "discard", "set", "replace", "__setitem__", "__delitem__", "append_arg", "__setattr__"}
SHARED_CALLS = {"type", "getattr", "vars", "globals", "super"}
COPY = re.compile(r"\.copy\(\)|deepcopy\(|copy=True|\bdict\(|\blist\(|\bset\(|\[:\]|\bcopy\.copy\(")


def _walk_own(node):
    """ast.walk without entering nested functions, lambdas or classes."""
    todo = [node]
    while todo:
        n = todo.pop()
        yield n
        for c in ast.iter_child_nodes(n):
            if not isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                todo.append(c)


def _locals(func) -> set:
    a = func.args
    names = {x.arg for x in a.args + a.kwonlyargs + a.posonlyargs}
    names |= {x.arg for x in (a.vararg, a.kwarg) if x is not None}
    glob = set()
    for n in _walk_own(func):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            names.add(n.id)
        if isinstance(n, ast.Global):
            glob |= set(n.names)
        if isinstance(n, (ast.For, ast.comprehension)) and isinstance(getattr(n, "target", None), ast.Name):
            names.add(n.target.id)
    return names - glob


class _Func:
    def __init__(self, func, outer_locals):
        self.func = func
        self.locals = _locals(func) | outer_locals
        self.aliases: set = set()  # locals bound to a shared object
        for n in _walk_own(func):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) \
                    and self.shared(n.value):
                self.aliases.add(n.targets[0].id)

    def shared(self, node) -> bool:
        """Can `node` evaluate to an object that outlives one test?"""
        chain = []
        while isinstance(node, (ast.Attribute, ast.Subscript)):
            if isinstance(node, ast.Attribute):
                chain.append(node.attr)
            node = node.value
        if isinstance(node, ast.Call):
            return isinstance(node.func, ast.Name) and node.func.id in SHARED_CALLS
        if not isinstance(node, ast.Name):
            return False
        if node.id == "cls":
            return True
        if node.id == "self":
            # class-level tables (UPPERCASE), the class itself, the (possibly cached) dialect
            return any(a.isupper() or a.startswith("__") or a == "dialect" for a in chain)
        if node.id in self.aliases:
            return True
        return node.id not in self.locals

    def mutates_shared(self) -> bool:
        for n in _walk_own(self.func):
            if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
                targets = n.targets if isinstance(n, (ast.Assign, ast.Delete)) else [n.target]
                for t in targets:
                    if isinstance(t, (ast.Attribute, ast.Subscript)) and self.shared(t.value):
                        return True
                    if isinstance(t, ast.Name) and t.id not in self.locals and not isinstance(n, ast.AnnAssign):
                        return True  # a global rebound (`global` declared)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in MUTATORS \
                    and self.shared(n.func.value):
                return True
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("setattr", "delattr") \
                    and n.args and self.shared(n.args[0]):
                return True
        return False


def _changed_functions(tree, changed_lines):
    """_Func for every function whose lines intersect `changed_lines` (nested included).
    Only statements whose span touches a changed line are entered."""
    import bisect

    out = []
    marks = sorted(changed_lines)

    def touches(node) -> bool:
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", []) or []])
        end = getattr(node, "end_lineno", None) or node.lineno
        i = bisect.bisect_left(marks, start)
        return i < len(marks) and marks[i] <= end

    def visit(node, outer_locals):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if touches(child):
                    f = _Func(child, outer_locals)
                    out.append(f)
                    visit(child, f.locals)
            elif isinstance(child, (ast.stmt, ast.excepthandler, ast.match_case)) and not isinstance(child, ast.Lambda):
                if not hasattr(child, "lineno") or touches(child):
                    visit(child, outer_locals)

    if marks:
        visit(tree, set())
    return out


def change_lines(change):
    """(changed old line numbers, changed new line numbers, removed texts, added texts)
    of a FileChange; an insertion also marks its neighbouring old lines."""
    old_lines, new_lines, removed, added = set(), set(), [], []
    for hunk in change.hunks:
        o = hunk.old_start if hunk.old_len > 0 else hunk.old_start + 1
        n = hunk.new_start if hunk.new_len > 0 else hunk.new_start + 1
        for raw in hunk.lines:
            tag, body = raw[:1], raw[1:]
            if tag == " ":
                o += 1
                n += 1
            elif tag == "-":
                old_lines.add(o)
                removed.append(body)
                o += 1
            elif tag == "+":
                new_lines.add(n)
                old_lines |= {o - 1, o}
                added.append(body)
                n += 1
    return old_lines, new_lines, removed, added




# -- classification ------------------------------------------------------------
CONTAINER = re.compile(r"(?<![\w.])(?:dict|list|set|tuple|frozenset|sorted)\(")
AUGMENTED = re.compile(r"(?:\+|-|\||&|\^|\*|/|%|@|<<|>>)=(?!=)")
SCOPE_DECL = re.compile(r"^\s*(?:global|nonlocal)\s+(.+)$", re.M)


COPY_TOKEN = re.compile(r"\bnot\s+copy\b|\bcopy\s*(?::\s*bool\s*)?=\s*(?:True|False)\b|\.copy\(\s*\)|deepcopy\(|\bcopy\.copy\("
                        r"|\[:\]|\bcopy\b")
MORE_COPYING = {("copy=False", "copy=True"), ("copy:False", "copy:True")}


def _copy_tokens(lines):
    out = []
    for line in lines:
        for m in COPY_TOKEN.finditer(line):
            tok = re.sub(r"\s+", "", m.group(0)).replace(":bool=", ":")
            out.append(tok)
    return sorted(out)


def _copying_changed(removed, added):
    """Reason the edit changes copying, or None. Changes elsewhere on a line that
    mentions copy, and edits that only copy more (copy=False -> copy=True, a copy
    added), cannot make two tests share an object they did not share before."""
    old, new = _copy_tokens(removed), _copy_tokens(added)
    if old == new:
        return None
    rest_old, rest_new = list(old), list(new)
    for a, b in MORE_COPYING:
        while a in rest_old and b in rest_new:
            rest_old.remove(a)
            rest_new.remove(b)
    if not rest_old and all(t in (".copy()", "deepcopy(", "copy.copy(", "[:]") for t in rest_new):
        return None
    return "copying changed"


def _code_lines(texts):
    """Changed lines without comment-only lines."""
    return [t for t in texts if t.strip() and not t.lstrip().startswith("#")]


def _qualified_functions(tree):
    """qualname -> function node for every function (nested included)."""
    out = {}

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                if not isinstance(child, ast.ClassDef):
                    out.setdefault(name, child)
                visit(child, name)
            else:
                visit(child, prefix)

    visit(tree, "")
    return out


def _mutable_default(func) -> bool:
    a = func.args
    for d in list(a.defaults) + [x for x in a.kw_defaults if x is not None]:
        if isinstance(d, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp, ast.Call)):
            return True
    return False


def classify(old_text: str | None, new_text: str | None, removed_text: list[str], added_text: list[str],
             changed_old: set[int], changed_new: set[int], trees: dict | None = None) -> tuple[str | None, str | None]:
    """(hard, soft) reasons the edit may leak state to later tests, or None.

    hard: an edited function's original body already writes to an object that
    outlives a test (a class attribute or table, a module global, a registry); an
    edit there can break the write/restore discipline in ways a check after the run
    cannot see (e.g. a parser table entry set and popped again). Such commits include
    every test.
    soft: the edit introduces such a write, changes copying/aliasing (a `copy`
    mentioned on a changed line, a container copy or an in-place operator added or
    removed), a `global`/`nonlocal` declaration or a mutable default. Such commits
    keep their narrowing but are verified with a state check: every candidate runs in
    one process, in pytest order, and the state of every sqlglot class and module
    must be the same after the run as before it (selector/verify.py)."""
    removed, added = _code_lines(removed_text), _code_lines(added_text)
    soft = None
    # Functions whose own body writes shared state (old version: hard; new only: soft).
    if trees is None:
        trees = {}
        for label, text in (("old", old_text), ("new", new_text)):
            if not text:
                continue
            try:
                trees[label] = ast.parse(text)
            except SyntaxError:
                continue
    trees = {k: v for k, v in trees.items() if v is not None}
    if "old" in trees and changed_old:
        for f in _changed_functions(trees["old"], changed_old):
            if f.mutates_shared():
                return f"{f.func.name} (line {f.func.lineno}) writes to an object that outlives a test", None
    if "new" in trees and changed_new:
        for f in _changed_functions(trees["new"], changed_new):
            if f.mutates_shared():
                soft = soft or f"{f.func.name} (new line {f.func.lineno}) now writes to an object that outlives a test"
            if _mutable_default(f.func):
                soft = soft or f"{f.func.name} has a mutable default"
    why = _copying_changed(removed, added)
    if why:
        soft = soft or why
    if sum(len(CONTAINER.findall(x)) for x in removed) != sum(len(CONTAINER.findall(x)) for x in added):
        soft = soft or "a container copy added or removed"
    if sum(len(AUGMENTED.findall(x)) for x in removed) != sum(len(AUGMENTED.findall(x)) for x in added):
        soft = soft or "an in-place operator added or removed"
    decl_old = sorted(m.group(1).strip() for x in removed for m in SCOPE_DECL.finditer(x))
    decl_new = sorted(m.group(1).strip() for x in added for m in SCOPE_DECL.finditer(x))
    if decl_old != decl_new:
        soft = soft or "global/nonlocal declarations changed"
    if any("cache" in x for x in removed + added if x.lstrip().startswith("@")):
        soft = soft or "a caching decorator changed"
    return None, soft


# -- state check (run in the verification child) ---------------------------------
# State the pinned suite itself changes while it runs (one full pytest run at the
# pinned commit, fingerprints after collection and at the end: tools/br_state_plugin.py):
# lazily filled registries and caches may gain entries (entries present before the
# run must stay as they were); module attributes cached by a lazy module __getattr__
# may appear; __slotnames__ is cached by copy/pickle.
GROWING = {"sqlglot.dialects.dialect:_Dialect\t_classes", "sqlglot.generator\t_DISPATCH_CACHE"}
LAZY_MODULES = {"sqlglot.dialects", "sqlglot.optimizer"}
# Dict entries whose value the pinned suite itself changes at run time: BigQuery's
# REGEXP_EXTRACT_ALL builder closes over one Literal that every parse attaches to its
# tree (and later passes annotate).
MUTATED_ENTRIES = {"sqlglot.parsers.bigquery:BigQueryParser\tFUNCTIONS": {"REGEXP_EXTRACT_ALL"}}
# Test-class fixtures (setUpClass) and module helpers the pinned suite sets while it
# runs; a test that reads them runs the fixture itself when run alone, so its
# coverage already holds what the fixture executes. pytest's unittest support also
# records tearDown_exceptions on each TestCase class.
TEST_STATE = {"sqlglot\tschema",  # test setUp rebinds sqlglot.schema; no sqlglot code reads it
              "tests.test_executor\tget_context", "tests.test_optimizer\tget_context",
              "tests.test_sqlglotc:TestSQLGlotC\tbuild_ext", "tests.test_optimizer:TestOptimizer\tconn",
              "tests.test_optimizer:TestOptimizer\tpool"} | {
    f"tests.test_executor:TestExecutor\t{a}" for a in ("cache", "tpcds_conn", "tpcds_sqls", "tpcds_tables",
                                                       "tpch_conn", "tpch_sqls", "tpch_tables")}


def state_leaks(before: dict, after: dict) -> list[str]:
    """Holders whose fingerprint changed in a way the pinned suite never shows."""
    bad = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        holder, _, attr = key.partition("\t")
        if attr == "__slotnames__":
            continue
        if key in TEST_STATE or (holder.startswith("tests") and attr == "tearDown_exceptions"):
            continue
        if old is None and ":" not in holder and holder in LAZY_MODULES:
            continue
        if old is not None and new is not None and "d" in old and "d" in new:
            changed = {k for k in set(old["d"]) | set(new["d"]) if old["d"].get(k) != new["d"].get(k)}
            if key in GROWING and all(k not in old["d"] for k in changed):
                continue  # only new entries
            if changed <= MUTATED_ENTRIES.get(key, set()):
                continue
        bad.append(key)
    return bad
