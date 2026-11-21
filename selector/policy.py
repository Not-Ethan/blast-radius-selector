"""Decide, per commit, what to include outright and what to verify by running.

Costs are in seconds. Including a set S costs pack(S) (busiest worker + 2 s per
worker); verifying a test costs its CPU time in our container; missing a
failing test costs 300 s. Candidates are a sound superset, so verifying all of
them gives the exact answer; the policy only trades CPU against packing cost.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

from .pack import pack
from .verify import Job, module_name, run_jobs

MISS = 300.0
# Fixed CPU cost of one verification child (fork, overlay, importing sqlglot
# and the test modules). Measured on the target image, see README.
VERIFY_OVERHEAD = float(os.environ.get("BR_VERIFY_OVERHEAD", "0.35"))
# Tests whose verification would cost more than this are never verified.
EXPENSIVE_CPU = float(os.environ.get("BR_EXPENSIVE_CPU", "3.0"))
# Never spend more than this on verifying one commit.
MAX_VERIFY_CPU = float(os.environ.get("BR_MAX_VERIFY_CPU", "20.0"))
PARALLEL = int(os.environ.get("BR_PARALLEL", "2"))
# Expensive candidates are left out only when verification shows the patch is
# inert: every changed expression was evaluated next to its original at least
# PROBE_MIN_COUNT times, by at least PROBE_MIN_TESTS cheap tests, and the two
# never differed, and no cheap test failed.
PROBE_MIN_COUNT = int(os.environ.get("BR_PROBE_MIN_COUNT", "50"))
PROBE_MIN_TESTS = int(os.environ.get("BR_PROBE_MIN_TESTS", "3"))

# Rough probability that a candidate set contains a failure, by reason.
# (Calibrated on the 30 sample commits: about 60% of code edits that reach any
# test break one, a quarter of class-level edits do, and every fixture edit did.)
P_FAIL = {
    "certain": 1.0,
    "data": 0.95,
    "pydata": 0.2,
    "func": 0.6,
    "imp": 0.35,
    "other": 0.5,
}


@dataclass
class Plan:
    cid: str
    include: set[int] = field(default_factory=set)
    verify: list[int] = field(default_factory=list)
    conditional: set[int] = field(default_factory=set)  # expensive tests, dropped if the patch proves inert
    lite_rest: set[int] = field(default_factory=set)  # cheap candidates a light probe left for a second round
    lite_rest_cpu: float = 0.0
    probes: dict = field(default_factory=dict)
    probe_result: dict | None = None
    files: dict = field(default_factory=dict)
    check_modules: list[str] = field(default_factory=list)
    import_fallback: dict = field(default_factory=dict)  # module -> numbers failing if it cannot import
    failed: set[int] | None = None
    import_errors: dict = field(default_factory=dict)
    verify_cpu_estimate: float = 0.0
    note: str = ""

    def final_tests(self) -> set[int]:
        chosen = set(self.include)
        if self.verify:
            if self.failed is None:
                # verification did not happen: include everything it was meant to decide
                chosen |= set(self.verify) | self.conditional
            else:
                chosen |= self.failed
                if self.conditional and not self.inert():
                    chosen |= self.conditional
        else:
            chosen |= self.conditional
        for module in self.import_errors:
            chosen |= set(self.import_fallback.get(module, ()))
        return chosen


    def inert(self) -> bool:
        if self.failed or self.include or self.import_errors or not self.probes or self.probe_result is None:
            return False
        expected = {f"{rel}:{k}" for rel, items in self.probes.items() for k in range(len(items))}
        stats = self.probe_result
        if "__incomplete__" in stats or set(stats) != expected:
            return False
        return all(v["infected"] == 0 and v["count"] >= PROBE_MIN_COUNT and v["tests"] >= PROBE_MIN_TESTS
                   for v in stats.values())


def probe_live_prior(item) -> float:
    """Prior that a probed edit changes some evaluation, from the kind of edit:
    `is` <-> `==` swaps are usually inert, ordering swaps sometimes, anything
    else (boolean operators, negations, constants) almost always changes values."""
    import ast

    old_src, new_src = item[0], item[3] if len(item) > 3 else ""
    try:
        old = ast.parse(old_src, mode="eval").body
        new = ast.parse(new_src, mode="eval").body
    except SyntaxError:
        return 0.8
    if isinstance(old, ast.Compare) and isinstance(new, ast.Compare) and len(old.ops) == len(new.ops) == 1 \
            and ast.dump(old.left) == ast.dump(new.left) and ast.dump(old.comparators[0]) == ast.dump(new.comparators[0]):
        pair = {type(old.ops[0]), type(new.ops[0])}
        if pair in ({ast.Is, ast.Eq}, {ast.IsNot, ast.NotEq}):
            return 0.25  # identity vs equality: differs only for odd __eq__
        if pair in ({ast.Lt, ast.LtE}, {ast.Gt, ast.GtE}):
            return 0.5  # differs only at the boundary
    return 0.9


def default_time_limit(n_commits: int) -> float:
    # 3 minutes for the practice batch (~40 commits), 4.5 for the final (~60).
    limit = 180.0 if n_commits <= 48 else 270.0
    return limit * 0.70


def cpu_estimate(maps, index: int, seconds: float) -> float:
    """CPU seconds our verifier needs for one test on the scoring machine."""
    measured = maps.cpu_estimate(index) if hasattr(maps, "cpu_estimate") else None
    if measured is not None:
        return measured
    return max(seconds, 0.001) * 1.5


def plan_commit(cid, changes, found, maps, map_to_number, seconds_of, unknown_numbers) -> Plan:
    plan = Plan(cid)

    def numbers(mask):
        out = set()
        for i in maps.tests_of(mask):
            n = map_to_number[i]
            if n is not None:
                out.add(n)
        return out

    if found.unknown:
        plan.include = set(seconds_of)
        plan.note = "unknown change: everything"
        return plan

    certain = numbers(found.certain)
    by_reason = {"data": 0, "pydata": 0, "func": 0, "imp": 0, "other": 0}
    for f in found.files:
        by_reason["data"] |= f.data
        by_reason["pydata"] |= f.pydata
        by_reason["func"] |= f.func
        by_reason["imp"] |= f.imp
        by_reason["other"] |= f.other
    possible = numbers(found.possible) - certain
    if unknown_numbers and possible:
        # Tests we have no coverage for: cannot rule them out.
        possible |= set(unknown_numbers)
    plan.include = set(certain)
    if not possible:
        plan.note = "nothing reachable" if not certain else "certain only"
        return plan

    reason_of = {}
    for reason in ("data", "func", "imp", "pydata", "other"):
        for n in numbers(by_reason[reason]):
            reason_of.setdefault(n, reason)
    index_of = {n: i for i, n in enumerate(map_to_number) if n is not None}
    id_of_number = {n: maps.tests[i] for n, i in index_of.items()}
    est = {n: cpu_estimate(maps, index_of[n], seconds_of[n]) if n in index_of else seconds_of[n] * 3 + 1
           for n in possible}
    ms_of = {n: int(seconds_of[n] * 1000 + 0.5) for n in seconds_of}

    expensive = {n for n in possible if est[n] > EXPENSIVE_CPU}
    cheap = possible - expensive
    reasons = {reason_of.get(n, "other") for n in possible}
    p_any = max((P_FAIL[r] for r in reasons), default=0.0)

    def packed(tests) -> float:
        return pack(sorted(tests), ms_of)[1] / 1000.0

    def verify_cpu_of(tests) -> float:
        classes = {"::".join(id_of_number[n].split("::")[:2]) for n in tests if n in id_of_number}
        return VERIFY_OVERHEAD + sum(est[n] for n in tests) + sum(maps.setup_cost.get(c, 0.0) for c in classes)

    def expected(blind, verify):
        """Expected cost of including `blind` outright and running `verify`."""
        if not verify:
            return packed(certain | blind), 0.0
        cpu = verify_cpu_of(verify)
        base = certain | blind
        if base:
            after = packed(base) + p_any * 0.5
        else:
            times = sorted(seconds_of[n] for n in verify)
            after = p_any * (2.0 + 0.5 * times[-1] + 0.5 * times[len(times) // 2])
        return cpu + after, cpu

    options = [("blind", possible, set())]
    if cheap:
        options.append(("verify", expensive, cheap))
        # Tests that cost far more to run here than to include (process-pool
        # tests run serially by us) go in outright; verify the rest.
        heavy = {n for n in cheap if est[n] > 1.5 * seconds_of[n] + 0.05}
        if heavy and cheap - heavy:
            options.append(("mixed", expensive | heavy, cheap - heavy))
    # Probe option: every change is a side-effect-free expression in a function
    # body and nothing else is touched; expensive tests become conditional.
    probe_items = {}
    probe_ok = bool(expensive) and bool(cheap) and not certain
    for f in found.files:
        if f.imp or f.data or f.pydata or f.other or f.certain:
            probe_ok = False
        for site in f.sites or []:
            if site.kind == "docstring":
                continue
            if site.scope != "function" or not site.probes:
                probe_ok = False
                continue
            probe_items.setdefault(f.path, []).extend(site.probes)
    lite_verify = set()
    if probe_items and all(not (f.imp or f.data or f.pydata or f.other or f.certain) for f in found.files) and not certain:
        all_function = all(site.scope == "function" and site.probes for f in found.files for site in (f.sites or []) if site.kind != "docstring")
        if all_function and cheap and max(probe_live_prior(i) for items in probe_items.values() for i in items) <= 0.25:
            # Edits that are usually inert (`is` <-> `==`): run only the cheapest
            # few candidates to watch the edited expressions; if they never
            # differ from the originals, nothing can fail.
            def class_of(n):  # noqa: E306
                return "::".join(id_of_number.get(n, "").split("::")[:2])

            def lite_cost(n):
                return est[n] + maps.setup_cost.get(class_of(n), 0.0)

            budget = 0.0
            paid = set()
            for n in sorted(cheap, key=lambda n: (lite_cost(n), n)):
                if len(lite_verify) >= 3 and budget >= 0.6:
                    break
                lite_verify.add(n)
                budget += est[n] + (0.0 if class_of(n) in paid else maps.setup_cost.get(class_of(n), 0.0))
                paid.add(class_of(n))
            if len(lite_verify) < len(cheap) or expensive:
                options.append(("probe-lite", set(), lite_verify))
    if probe_ok and probe_items:
        options.append(("probe", set(), cheap))
    scored = []
    for name, blind, verify in options:
        cost, cpu = expected(blind, verify)
        if name == "probe":
            live = max(probe_live_prior(item) for items in probe_items.values() for item in items)
            cost = cpu + live * (packed(expensive) + p_any * 0.5) + (1 - live) * p_any * 2.5
        elif name == "probe-lite":
            live = max(probe_live_prior(item) for items in probe_items.values() for item in items)
            cost = cpu + live * packed(possible)
        if cpu <= MAX_VERIFY_CPU:
            scored.append((cost, name, blind, verify, cpu))
    scored.sort(key=lambda x: x[0])
    cost, name, blind, verify, cpu = scored[0]
    summary = ", ".join(f"{n}={c:.2f}" for c, n, *_ in scored)
    plan.include |= blind
    if not verify:
        plan.note = f"blind {len(possible)} ({summary})"
        return plan
    plan.verify = sorted(verify)
    plan.verify_cpu_estimate = cpu
    if name == "probe":
        plan.conditional = set(expensive)
        plan.probes = probe_items
    elif name == "probe-lite":
        plan.conditional = possible - verify
        plan.probes = probe_items
        plan.lite_rest = cheap - verify
        plan.lite_rest_cpu = verify_cpu_of(plan.lite_rest) if plan.lite_rest else 0.0
    plan.note = f"{name}: verify {len(verify)}, include {len(blind)} ({summary})"

    # Files for the overlay and modules whose import we double-check.
    for change in changes:
        report = next((f for f in found.files if f.path == change.path), None)
        if change.old_path and change.is_rename:
            plan.files[change.old_path] = None
        if change.is_deleted:
            plan.files[change.old_path] = None
            continue
        if report is not None and report.new_text is not None:
            plan.files[change.path] = report.new_text.encode("utf-8")
        name = module_name(change.path)
        if name and (change.path.startswith("sqlglot/") or change.path.startswith("tests/")):
            plan.check_modules.append(name)
            imp_mask = maps.import_mask(change.old_path or change.path)
            plan.import_fallback[name] = numbers(imp_mask)
    return plan


def verify_plans(plans: list[Plan], root: str, tmp: str, deadline: float, log, id_of, number_of, seconds_of) -> None:

    jobs = []
    by_key = {}
    for plan in plans:
        if not plan.verify:
            continue
        # tests.json numbers -> node ids through the plan's own mapping
        jobs.append(plan)
    if not jobs:
        return
    work = []
    for plan in jobs:
        ids = [id_of[n] for n in plan.verify]
        limits = {id_of[n]: max(10.0, 6.0 * seconds_of[n] + 5.0) for n in plan.verify}
        budget = max(30.0, 4.0 * plan.verify_cpu_estimate + 15.0)
        job = Job(plan.cid, plan.files, ids, limits, plan.check_modules, budget, probes=plan.probes or None)
        by_key[plan.cid] = (plan, job)
        work.append(job)
    preload(set(t.split("::", 1)[0] for job in work for t in job.tests) | {m for job in work for m in job.check_modules}, log)
    loaded = preload_sqlglot(root, log)
    for job in work:
        touched = {module_name(p) for p in job.files if p.endswith(".py")}
        job.warm = bool(loaded) and not (touched & loaded)
    # Cheapest first: most value per second if time runs short.
    work.sort(key=lambda j: by_key[j.key][0].verify_cpu_estimate)
    run_jobs(work, root, tmp, PARALLEL, deadline, log=log)
    for plan, job in by_key.values():
        if job.result is None or job.result.get("error"):
            plan.failed = None
            plan.note += " | verification unavailable"
            continue
        plan.failed = {number_of[i] for i in job.result["failed"] if i in number_of}
        plan.import_errors = job.result.get("import_errors", {})
        if plan.probes:
            plan.probe_result = job.result.get("probes", {})
            plan.note += f" | probes {plan.probe_result} -> {'inert' if plan.inert() else 'live'}"
        plan.note += f" | ran {len(job.tests)} in {job.cpu:.2f}s cpu: {len(plan.failed)} failed"

    # Second round for light probes that did not prove the edit inert: run the
    # remaining cheap candidates too (exact answers for them instead of
    # including them blindly; more evaluations if the probe was inconclusive).
    second = []
    for plan, job in by_key.values():
        stats = plan.probe_result
        if not plan.lite_rest or plan.failed is None or plan.import_errors or stats is None:
            continue
        if plan.inert() or plan.lite_rest_cpu > MAX_VERIFY_CPU:
            continue
        rest = sorted(plan.lite_rest)
        ids = [id_of[n] for n in rest]
        limits = {id_of[n]: max(10.0, 6.0 * seconds_of[n] + 5.0) for n in rest}
        job2 = Job(plan.cid + "-2", plan.files, ids, limits, [], max(30.0, 4.0 * plan.lite_rest_cpu + 15.0),
                   probes=plan.probes)
        touched = {module_name(p) for p in job2.files if p.endswith(".py")}
        job2.warm = bool(loaded) and not (touched & loaded)
        second.append((plan, job2))
    if second:
        run_jobs([j for _, j in second], root, tmp, PARALLEL, deadline, log=log)
        for plan, job2 in second:
            if job2.result is None or job2.result.get("error"):
                plan.note += " | second round unavailable"
                continue  # conditional tests stay included
            failed2 = {number_of[i] for i in job2.result["failed"] if i in number_of}
            plan.failed |= failed2
            plan.conditional -= plan.lite_rest
            merged = {k: dict(v) for k, v in plan.probe_result.items()}
            for k, v in job2.result.get("probes", {}).items():
                m = merged.setdefault(k, {"count": 0, "infected": 0, "tests": 0})
                m["count"] += v["count"]
                m["infected"] += v["infected"]
                m["tests"] += v["tests"]
            plan.probe_result = merged
            plan.note += (f" | round 2 ran {len(job2.tests)} in {job2.cpu:.2f}s cpu: {len(failed2)} failed,"
                          f" probes {merged} -> {'inert' if plan.inert() else 'live'}")


CORE_MODULES = (
    "sqlglot", "sqlglot.optimizer", "sqlglot.executor", "sqlglot.dialects.bigquery",
    "sqlglot.dialects.hive", "sqlglot.dialects.snowflake", "sqlglot.dialects.spark2",
)


def preload_sqlglot(root: str, log) -> set[str]:
    """Import the pinned sqlglot core once in the parent. A child whose patch
    touches none of these modules reuses them (re-pointed at its overlay);
    any other child drops them and imports everything from its overlay."""
    import importlib

    sys_path = list(sys.path)
    sys.path.insert(0, root)
    try:
        for name in CORE_MODULES:
            importlib.import_module(name)
    except Exception as error:  # noqa: BLE001
        log(f"core preload failed: {error!r}")
        for name in list(sys.modules):
            if name == "sqlglot" or name.startswith("sqlglot."):
                del sys.modules[name]
        return set()
    finally:
        sys.path[:] = sys_path
    return {name for name in sys.modules if name == "sqlglot" or name.startswith("sqlglot.")}


def preload(files: set[str], log) -> None:
    """Load third-party libraries once in the parent so children inherit them."""
    import importlib

    from . import shim

    names = ["unittest", "unittest.mock", "json", "datetime", "decimal", "re", "typing", "itertools",
             "collections", "functools", "logging", "copy", "math", "enum", "dataclasses", "string"]
    heavy = any(("test_optimizer" in f or "test_executor" in f) for f in files)
    if heavy:
        names += ["numpy", "pandas", "pandas.testing", "duckdb", "concurrent.futures", "multiprocessing", "csv", "gzip"]
    if any("test_sqlglotc" in f for f in files):
        names += ["setuptools", "setuptools.command.build_ext", "runpy", "tempfile"]
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as error:  # noqa: BLE001
            log(f"preload {name}: {error!r}")
    shim.install()
