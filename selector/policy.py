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
MAX_VERIFY_CPU = float(os.environ.get("BR_MAX_VERIFY_CPU", "12.0"))
PARALLEL = int(os.environ.get("BR_PARALLEL", "2"))
# Expensive candidates are left out when at least this many cheap candidates
# (all of which executed the changed code) were run and none failed.
SKIP_EXPENSIVE_MIN_CHEAP = int(os.environ.get("BR_SKIP_EXPENSIVE_MIN_CHEAP", "1000000"))

# Rough probability that a candidate set contains a failure, by reason.
P_FAIL = {
    "certain": 1.0,
    "data": 0.9,
    "func": 0.5,
    "imp": 0.3,
    "other": 0.5,
}


@dataclass
class Plan:
    cid: str
    include: set[int] = field(default_factory=set)
    verify: list[int] = field(default_factory=list)
    expensive: set[int] = field(default_factory=set)  # decided after verification
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
                chosen |= set(self.verify) | self.expensive  # verification did not happen: include all
            else:
                chosen |= self.failed
                clean = not self.failed and not self.include and not self.import_errors
                if self.expensive and not (clean and len(self.verify) >= SKIP_EXPENSIVE_MIN_CHEAP):
                    chosen |= self.expensive
        else:
            chosen |= self.expensive
        for module in self.import_errors:
            chosen |= set(self.import_fallback.get(module, ()))
        return chosen


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
    by_reason = {"data": 0, "func": 0, "imp": 0, "other": 0}
    for f in found.files:
        by_reason["data"] |= f.data
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
    for reason in ("data", "func", "imp", "other"):
        for n in numbers(by_reason[reason]):
            reason_of.setdefault(n, reason)
    index_of = {n: i for i, n in enumerate(map_to_number) if n is not None}
    id_of_number = {n: maps.tests[i] for n, i in index_of.items()}
    est = {n: cpu_estimate(maps, index_of[n], seconds_of[n]) if n in index_of else seconds_of[n] * 3 + 1
           for n in possible}
    ms_of = {n: int(seconds_of[n] * 1000 + 0.5) for n in seconds_of}

    expensive = {n for n in possible if est[n] > EXPENSIVE_CPU}
    cheap = possible - expensive
    p_any = 1.0
    for n in cheap:
        p_any = min(p_any, 1.0)
    reasons = {reason_of.get(n, "other") for n in cheap}
    p_any = max((P_FAIL[r] for r in reasons), default=0.0)

    _, blind_cost = pack(sorted(certain | possible), ms_of)
    classes = {"::".join(id_of_number[n].split("::")[:2]) for n in cheap if n in id_of_number}
    verify_cpu = VERIFY_OVERHEAD + sum(est[n] for n in cheap) + sum(maps.setup_cost.get(c, 0.0) for c in classes)
    base = certain | expensive
    _, base_cost = pack(sorted(base), ms_of)
    _, certain_cost = pack(sorted(certain), ms_of)
    may_skip = bool(expensive) and len(cheap) >= SKIP_EXPENSIVE_MIN_CHEAP
    if may_skip:
        # Expensive tests are only added when a cheap one fails (or certain).
        expected_after = p_any * (base_cost / 1000.0 + 0.5) + (1 - p_any) * certain_cost / 1000.0
    elif base:
        expected_after = base_cost / 1000.0 + p_any * 0.5
    else:
        expected_after = p_any * (2.0 + max((seconds_of[n] for n in cheap), default=0))
    verify_cost = verify_cpu + expected_after
    plan.verify_cpu_estimate = verify_cpu
    if cheap and verify_cpu <= MAX_VERIFY_CPU and verify_cost < blind_cost / 1000.0:
        plan.expensive = set(expensive)
        plan.verify = sorted(cheap)
        plan.note = f"verify {len(cheap)} (est {verify_cpu:.2f}s cpu, blind {blind_cost / 1000:.2f}s)"
    else:
        plan.include |= possible
        plan.note = f"blind {len(possible)} (blind {blind_cost / 1000:.2f}s, verify would be {verify_cost:.2f}s)"
        return plan

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
        budget = max(20.0, 3.0 * plan.verify_cpu_estimate + 10.0)
        job = Job(plan.cid, plan.files, ids, limits, plan.check_modules, budget)
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
        plan.note += f" | ran {len(job.tests)} in {job.cpu:.2f}s cpu: {len(plan.failed)} failed"


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
