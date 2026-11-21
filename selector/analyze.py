"""Turn a change to a Python file into "change sites" in the old file.

The old and new modules are parsed and compared statement by statement
(comments and formatting never reach the AST, so they drop out here). Every
difference becomes a Site that says where in the OLD file behaviour can change
and when that code runs:

* scope "function": the change is inside a def/lambda body, so it only matters
  to tests that execute the enclosing statement (line coverage of `lines`) or
  enter the enclosing function (`entries`: co_firstlineno of functions whose
  callers all count, needed when a body shares the def's line, as in lambdas).
* scope "class"/"module": the change runs at import time (class bodies, module
  statements, decorators, base classes). `names` lists what the statement binds.

Statements are matched by their source text first (cheap) and only compared as
ASTs when the text differs, so large files cost little beyond parsing.
"""

from __future__ import annotations

import ast
import difflib
from dataclasses import dataclass, field

BODY_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")
FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def dump(node) -> str:
    return ast.dump(node, include_attributes=False)


def _dump_any(value):
    if isinstance(value, ast.AST):
        return dump(value)
    if isinstance(value, list):
        return [_dump_any(v) for v in value]
    return value


@dataclass
class Site:
    scope: str  # "function" | "class" | "module"
    kind: str
    lines: set[int] = field(default_factory=set)
    entries: set[int] = field(default_factory=set)
    class_qual: str | None = None
    func_qual: str | None = None
    names: set[str] = field(default_factory=set)
    detail: str = ""
    # For a changed entry of a dict literal bound at class/module level: the
    # entry keys involved, as ("str", text) / ("attr", name) / ("name", id) /
    # ("other",). None when the change is not confined to dict entries.
    dict_keys: list | None = None


@dataclass
class PyAnalysis:
    syntax_error: bool = False
    noop: bool = False
    sites: list[Site] = field(default_factory=list)
    error: str | None = None


def _first_line(node) -> int:
    lines = [node.lineno]
    for decorator in getattr(node, "decorator_list", []) or []:
        lines.append(decorator.lineno)
    return min(lines)


def _span(node) -> set[int]:
    start = _first_line(node) if hasattr(node, "decorator_list") else node.lineno
    end = getattr(node, "end_lineno", None) or node.lineno
    return set(range(start, end + 1))


def _is_compound(node) -> bool:
    if isinstance(node, (ast.ClassDef,) + FUNC_TYPES):
        return True
    return any(isinstance(getattr(node, name, None), list) and getattr(node, name) for name in BODY_FIELDS)


def _header_span(node) -> set[int]:
    """Lines of a compound statement up to (not including) its first body statement."""
    start = _first_line(node) if hasattr(node, "decorator_list") else node.lineno
    firsts = []
    for name in BODY_FIELDS:
        value = getattr(node, name, None)
        if isinstance(value, list) and value and hasattr(value[0], "lineno"):
            firsts.append(value[0].lineno)
    if not firsts:
        return _span(node)
    first_body = min(firsts)
    end = first_body - 1 if first_body > node.lineno else node.lineno
    return set(range(start, max(start, end) + 1))


def _exec_span(node) -> set[int]:
    return _header_span(node) if _is_compound(node) else _span(node)


def _is_docstring(node) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _bound_names(node) -> set[str]:
    names: set[str] = set()
    targets: list = []
    for child in ast.walk(node) if not isinstance(node, FUNC_TYPES + (ast.ClassDef,)) else [node]:
        if isinstance(child, ast.Assign):
            targets.extend(child.targets)
        elif isinstance(child, (ast.AnnAssign, ast.AugAssign)):
            targets.append(child.target)
        elif isinstance(child, FUNC_TYPES + (ast.ClassDef,)):
            names.add(child.name)
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in child.names)
        elif isinstance(child, ast.NamedExpr):
            targets.append(child.target)
    for target in targets:
        for child in ast.walk(target):
            if isinstance(child, ast.Name):
                names.add(child.id)
            elif isinstance(child, ast.Attribute):
                names.add(child.attr)
    return names


class _Ctx:
    __slots__ = ("scope", "classes", "funcs", "func_nodes")

    def __init__(self, scope="module", classes=(), funcs=(), func_nodes=()):
        self.scope = scope
        self.classes = tuple(classes)
        self.funcs = tuple(funcs)
        self.func_nodes = tuple(func_nodes)

    def enter_class(self, node):
        scope = "function" if self.scope == "function" else "class"
        return _Ctx(scope, self.classes + (node.name,), self.funcs, self.func_nodes)

    def enter_func(self, node):
        return _Ctx("function", self.classes, self.funcs + (getattr(node, "name", "<lambda>"),), self.func_nodes + (node,))

    @property
    def class_qual(self):
        return ".".join(self.classes) or None

    @property
    def func_qual(self):
        return ".".join(self.funcs) or None


def _args_without_annotations(args: ast.arguments) -> str:
    def strip(a):
        return ast.arg(arg=a.arg) if a is not None else None

    clone = ast.arguments(
        posonlyargs=[strip(a) for a in args.posonlyargs],
        args=[strip(a) for a in args.args],
        vararg=strip(args.vararg),
        kwonlyargs=[strip(a) for a in args.kwonlyargs],
        kw_defaults=list(args.kw_defaults),
        kwarg=strip(args.kwarg),
        defaults=list(args.defaults),
    )
    return dump(clone)


def _has_lazy_annotations(tree) -> bool:
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            if any(alias.name == "annotations" for alias in node.names):
                return True
        elif not (isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant)):
            if not isinstance(node, ast.ImportFrom):
                break
    return False


class _Differ:
    lazy_annotations = False

    def __init__(self, old_lines, new_lines, filename):
        self.old_lines = old_lines
        self.new_lines = new_lines
        self.filename = filename
        self.sites: list[Site] = []
        self.compile_errors: list[str] = []

    # -- text keys -----------------------------------------------------------
    @staticmethod
    def _segment(lines, node) -> str:
        """Source text of a statement (col offsets are UTF-8 byte offsets)."""
        decorated = bool(getattr(node, "decorator_list", None))
        start = (_first_line(node) if decorated else node.lineno) - 1
        end = node.end_lineno
        piece = lines[start:end]
        if not piece:
            return ""
        piece = list(piece)

        def cut(text, lo, hi):
            if text.isascii():
                return text[lo:hi]
            return text.encode("utf-8")[lo:hi].decode("utf-8", "replace")

        if len(piece) == 1:
            piece[0] = cut(piece[0], 0 if decorated else node.col_offset, node.end_col_offset)
        else:
            piece[-1] = cut(piece[-1], 0, node.end_col_offset)
            if not decorated:
                piece[0] = cut(piece[0], node.col_offset, None)
        return "\n".join(piece)

    def _check_compiles(self, node):
        try:
            compile(ast.Module(body=[node], type_ignores=[]), self.filename, "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as error:
            self.compile_errors.append(str(error))

    # -- sites ---------------------------------------------------------------
    def _func_entries(self, ctx) -> set[int]:
        entries: set[int] = set()
        if ctx.func_nodes:
            func = ctx.func_nodes[-1]
            if isinstance(func, ast.Lambda):
                entries.add(func.lineno)
            elif func.body and func.body[0].lineno == func.lineno:
                entries.add(_first_line(func))
                entries.add(func.lineno)
        return entries

    def add(self, ctx, kind, lines, names=(), detail="", entries=()):
        self.sites.append(Site(
            scope=ctx.scope,
            kind=kind,
            lines=set(lines),
            entries=set(entries) | (self._func_entries(ctx) if ctx.scope == "function" else set()),
            class_qual=ctx.class_qual,
            func_qual=ctx.func_qual,
            names=set(names),
            detail=detail,
        ))

    # -- statement lists -----------------------------------------------------
    def body(self, old: list, new: list, ctx: _Ctx, parent=None, compile_new=False):
        a = [self._segment(self.old_lines, s) for s in old]
        b = [self._segment(self.new_lines, s) for s in new]
        if a == b:
            return
        matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            if tag == "replace" and i2 - i1 == j2 - j1:
                for o, n in zip(old[i1:i2], new[j1:j2]):
                    self.stmt(o, n, ctx)
                continue
            if tag == "replace":
                self._unequal(old[i1:i2], new[j1:j2], ctx, old, i1, parent)
                continue
            if tag == "delete":
                for o in old[i1:i2]:
                    self.removed(o, ctx, old, i1, parent)
                continue
            if tag == "insert":
                self.inserted(new[j1:j2], old, i1, ctx, parent)

    def _unequal(self, olds, news, ctx, old_all, i1, parent):
        def key(s):
            return (type(s).__name__, getattr(s, "name", None) or tuple(sorted(_bound_names(s))))

        matcher = difflib.SequenceMatcher(None, [key(s) for s in olds], [key(s) for s in news], autojunk=False)
        for tag, a1, a2, b1, b2 in matcher.get_opcodes():
            if tag == "equal":
                for o, n in zip(olds[a1:a2], news[b1:b2]):
                    self.stmt(o, n, ctx)
            elif tag == "delete":
                for o in olds[a1:a2]:
                    self.removed(o, ctx, old_all, i1 + a1, parent)
            elif tag == "insert":
                self.inserted(news[b1:b2], old_all, i1 + a1, ctx, parent)
            else:
                for o in olds[a1:a2]:
                    self.removed(o, ctx, old_all, i1 + a1, parent)
                self.inserted(news[b1:b2], old_all, i1 + a1, ctx, parent)

    def removed(self, o, ctx, old_all=None, at=None, parent=None):
        if ctx.scope == "function":
            if _is_docstring(o) and old_all is not None and old_all and old_all[0] is o:
                return  # a docstring is never executed
            self.add(ctx, "delete", _exec_span(o), detail=f"removed {type(o).__name__}")
        else:
            if _is_docstring(o) and old_all is not None and old_all and old_all[0] is o:
                self.add(ctx, "docstring", set(), detail="docstring removed")
                return
            self.add(ctx, "delete", _span(o), _bound_names(o), f"removed {type(o).__name__}")

    def inserted(self, news, old_all, at, ctx, parent):
        for n in news:
            if ctx.scope != "function" or not ctx.func_nodes or ctx.func_nodes and len(ctx.func_nodes) == 1:
                self._check_compiles(n) if not isinstance(n, ast.Return) and ctx.scope != "function" else None
        lines: set[int] = set()
        neighbours = []
        if at > 0:
            neighbours.append(old_all[at - 1])
        if at < len(old_all):
            neighbours.append(old_all[at])
        for s in neighbours:
            lines |= _exec_span(s)
        entries: set[int] = set()
        if ctx.scope == "function" and ctx.func_nodes:
            func = ctx.func_nodes[-1]
            if not neighbours or all(_is_docstring(s) for s in neighbours):
                entries.add(_first_line(func) if not isinstance(func, ast.Lambda) else func.lineno)
                entries.add(func.lineno)
        if parent is not None and not old_all and hasattr(parent, "lineno"):
            lines |= _header_span(parent)
        names: set[str] = set()
        for n in news:
            names |= _bound_names(n)
        if ctx.scope != "function" and all(_is_docstring(n) for n in news) and at == 0:
            self.add(ctx, "docstring", set(), detail="docstring added")
            return
        self.add(ctx, "insert", lines, names if ctx.scope != "function" else (),
                 "inserted " + ",".join(type(n).__name__ for n in news), entries)

    # -- single statements ---------------------------------------------------
    def stmt(self, o, n, ctx):
        if self._segment(self.old_lines, o) == self._segment(self.new_lines, n):
            return
        if type(o) is not type(n):
            self.removed(o, ctx)
            if ctx.scope != "function":
                self._check_compiles(n)
            return
        if isinstance(o, FUNC_TYPES):
            self._func(o, n, ctx)
            return
        if isinstance(o, ast.ClassDef):
            self._class(o, n, ctx)
            return
        if _is_compound(o) or _is_compound(n):
            self._compound(o, n, ctx)
            return
        if dump(o) == dump(n):
            return  # formatting or comments only
        if isinstance(o, ast.AnnAssign) and isinstance(n, ast.AnnAssign) and (ctx.scope == "function" or self.lazy_annotations):
            # Annotations of locals are never evaluated; with lazy annotations
            # neither are class/module ones (beyond a stored string).
            if dump(o.target) == dump(n.target) and _dump_any(o.value) == _dump_any(n.value) and o.simple == n.simple:
                return
        if _is_docstring(o) and _is_docstring(n):
            if ctx.scope == "function":
                self.add(ctx, "stmt", _span(o), detail="string statement")
            else:
                self.add(ctx, "docstring", set(), detail="docstring")
            return
        if ctx.scope == "function":
            self.add(ctx, "stmt", _span(o), detail=type(o).__name__)
        else:
            self._check_compiles(n)
            self._import_time_stmt(o, n, ctx)

    def _compound(self, o, n, ctx):
        header_same = all(
            _dump_any(getattr(o, f, None)) == _dump_any(getattr(n, f, None))
            for f in o._fields if f not in BODY_FIELDS
        )
        if not header_same:
            if ctx.scope == "function":
                self.add(ctx, "header", _header_span(o), detail=f"{type(o).__name__} header")
            else:
                self._import_time_stmt(o, n, ctx, header_only=True)
        for f in BODY_FIELDS:
            ob, nb = getattr(o, f, None), getattr(n, f, None)
            if not isinstance(ob, list) or not isinstance(nb, list):
                continue
            if f in ("handlers", "cases"):
                if len(ob) != len(nb):
                    lines = _header_span(o)
                    for h in ob:
                        lines |= _header_span(h)
                    names = (_bound_names(o) | _bound_names(n)) if ctx.scope != "function" else ()
                    self.add(ctx, "header", lines, names, f"{f} changed")
                    continue
                for oh, nh in zip(ob, nb):
                    head_same = all(
                        _dump_any(getattr(oh, k, None)) == _dump_any(getattr(nh, k, None))
                        for k in oh._fields if k != "body"
                    )
                    if not head_same:
                        names = (_bound_names(oh) | _bound_names(nh)) if ctx.scope != "function" else ()
                        self.add(ctx, "header", _header_span(oh), names, "handler header")
                    self.body(oh.body, nh.body, ctx, oh)
            elif not ob and nb:
                names = _bound_names(n) if ctx.scope != "function" else ()
                self.add(ctx, "insert", _header_span(o), names, f"new {f}")
            elif ob and not nb:
                names = _bound_names(o) if ctx.scope != "function" else ()
                self.add(ctx, "delete", _header_span(o) | set().union(*[_exec_span(s) for s in ob]), names, f"removed {f}")
            else:
                self.body(ob, nb, ctx, o)

    def _import_time_stmt(self, o, n, ctx, header_only=False):
        """A module/class level statement changed. If every difference sits in a
        lambda body, it is really a function-scope change of that lambda."""
        diffs: list = []
        _diff_nodes(o, n, (), diffs)
        lambdas = []
        for node, ancestors in diffs:
            lam = None
            chain = ancestors + (node,)
            for k in range(len(chain) - 1):
                anc = chain[k]
                if isinstance(anc, ast.Lambda) and chain[k + 1] is anc.body:
                    lam = anc
                    break
            if lam is None:
                lambdas = None
                break
            lambdas.append(lam)
        if lambdas:
            seen = set()
            for lam in lambdas:
                if id(lam) in seen:
                    continue
                seen.add(id(lam))
                fctx = ctx.enter_func(lam)
                self.add(fctx, "lambda", _span(lam.body), detail="lambda body", entries={lam.lineno})
            return
        names = _bound_names(o) | _bound_names(n)
        self.add(ctx, "stmt", _header_span(o) if header_only else _span(o), names, type(o).__name__)
        if not header_only and isinstance(o, (ast.Assign, ast.AnnAssign)) and isinstance(o.value, ast.Dict) \
                and isinstance(n, type(o)) and isinstance(n.value, ast.Dict):
            self.sites[-1].dict_keys = _changed_dict_keys(o.value, n.value, diffs)

    def _func(self, o, n, ctx):
        header_fields = ("name", "args", "decorator_list", "returns", "type_params", "type_comment")
        if self.lazy_annotations:
            # With `from __future__ import annotations` annotations are never
            # evaluated, so annotation-only signature edits change nothing.
            header_fields = ("name", "decorator_list", "type_params")
            header_diff = any(_dump_any(getattr(o, f, None)) != _dump_any(getattr(n, f, None)) for f in header_fields) \
                or _args_without_annotations(o.args) != _args_without_annotations(n.args)
        else:
            header_diff = any(_dump_any(getattr(o, f, None)) != _dump_any(getattr(n, f, None)) for f in header_fields)
        before = len(self.sites)
        if header_diff:
            decorators_same = _dump_any(o.decorator_list) == _dump_any(n.decorator_list)
            if ctx.scope == "function":
                self.add(ctx, "def-header", _header_span(o), detail="nested def header",
                         entries={_first_line(o), o.lineno})
            elif o.name == n.name and decorators_same:
                # Defaults or annotations: only callers of this function notice.
                self.sites.append(Site(scope="function", kind="def-header", lines=set(),
                                       entries={_first_line(o), o.lineno}, class_qual=ctx.class_qual,
                                       func_qual=".".join(ctx.funcs + (o.name,)), names={o.name},
                                       detail="signature"))
            else:
                self.add(ctx, "def-header", _header_span(o), {o.name, n.name}, "def renamed/redecorated")
        self.body(o.body, n.body, ctx.enter_func(o), o)
        if ctx.scope != "function" and len(self.sites) > before:
            self._check_compiles(n)

    def _class(self, o, n, ctx):
        header_fields = ("name", "bases", "keywords", "decorator_list", "type_params")
        if any(_dump_any(getattr(o, f, None)) != _dump_any(getattr(n, f, None)) for f in header_fields):
            self.add(ctx, "class-header", _header_span(o), {o.name, n.name}, "class header")
        self.body(o.body, n.body, ctx.enter_class(o), o)


def _key_descriptor(key):
    if key is None:
        return ("spread",)
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return ("str", key.value)
    if isinstance(key, ast.Attribute):
        return ("attr", key.attr)
    if isinstance(key, ast.Name):
        return ("name", key.id)
    return ("other",)


def _changed_dict_keys(old: ast.Dict, new: ast.Dict, diffs) -> list | None:
    """Keys of the entries that differ between two dict literals, or None."""
    old_entries = [(dump(k) if k is not None else None, dump(v)) for k, v in zip(old.keys, old.values)]
    new_entries = [(dump(k) if k is not None else None, dump(v)) for k, v in zip(new.keys, new.values)]
    if len(old_entries) == len(new_entries):
        changed = []
        for i, (a, b) in enumerate(zip(old_entries, new_entries)):
            if a != b:
                changed.append(_key_descriptor(old.keys[i]))
                if a[0] != b[0]:
                    changed.append(_key_descriptor(new.keys[i]))
        return changed or None
    # Entries added or removed: compare by key.
    old_map = {}
    for (kd, vd), k in zip(old_entries, old.keys):
        old_map.setdefault(kd, []).append((vd, k))
    new_map = {}
    for (kd, vd), k in zip(new_entries, new.keys):
        new_map.setdefault(kd, []).append((vd, k))
    changed = []
    for kd in set(old_map) | set(new_map):
        a = [v for v, _ in old_map.get(kd, [])]
        b = [v for v, _ in new_map.get(kd, [])]
        if a != b:
            node = (old_map.get(kd) or new_map.get(kd))[0][1]
            changed.append(_key_descriptor(node))
    return changed or None


def _diff_nodes(o, n, ancestors, out):
    """Collect minimal differing nodes of two ASTs, with their old ancestors."""
    if type(o) is not type(n) or not isinstance(o, ast.AST):
        anchor = o if isinstance(o, ast.AST) and hasattr(o, "lineno") else (ancestors[-1] if ancestors else o)
        out.append((anchor, ancestors[:-1] if anchor is not o and ancestors else ancestors))
        return
    here = ancestors + (o,)
    for name in o._fields:
        a, b = getattr(o, name, None), getattr(n, name, None)
        if isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                anchor = o if hasattr(o, "lineno") else (ancestors[-1] if ancestors else o)
                out.append((anchor, ancestors if anchor is o else ancestors[:-1]))
                continue
            for x, y in zip(a, b):
                if isinstance(x, ast.AST) and isinstance(y, ast.AST):
                    if dump(x) != dump(y):
                        if hasattr(x, "lineno"):
                            _diff_nodes(x, y, here, out)
                        else:
                            out.append((o, ancestors))
                elif x != y:
                    out.append((o, ancestors))
        elif isinstance(a, ast.AST) and isinstance(b, ast.AST):
            if dump(a) != dump(b):
                if hasattr(a, "lineno"):
                    _diff_nodes(a, b, here, out)
                else:
                    out.append((o, ancestors))
        elif _dump_any(a) != _dump_any(b):
            out.append((o, ancestors))


def analyze_python(old_src: str, new_src: str, filename: str = "<file>") -> PyAnalysis:
    result = PyAnalysis()
    try:
        old_tree = ast.parse(old_src)
    except SyntaxError as error:
        result.error = f"old does not parse: {error}"
        return result
    try:
        new_tree = ast.parse(new_src)
    except (SyntaxError, ValueError) as error:
        result.syntax_error = True
        result.error = str(error)
        return result
    differ = _Differ(old_src.split("\n"), new_src.split("\n"), filename)
    differ.lazy_annotations = _has_lazy_annotations(old_tree) and _has_lazy_annotations(new_tree)
    differ.body(old_tree.body, new_tree.body, _Ctx(), old_tree)
    if differ.compile_errors:
        result.syntax_error = True
        result.error = differ.compile_errors[0]
        return result
    result.sites = differ.sites
    result.noop = not differ.sites
    return result
