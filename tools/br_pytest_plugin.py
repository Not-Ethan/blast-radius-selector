"""pytest plugin for tools/label_pytest.py: record which node ids failed (dev tool).

A test fails when any of its reports fails: setup/call/teardown errors, a
failing subtest (pytest 9 reports SUBFAILED next to PASSED), a timeout
(pytest-timeout raises inside the test), an unexpected success. A file that
cannot be collected fails every test of that file.
"""

import json
import os

# Keep sqlglot's spawn process pools (sized by the CPU count) small on a shared
# machine; this changes how many workers run the cases, not what they compute.
_CPUS = int(os.environ.get("BR_LABEL_CPUS", "2"))
os.cpu_count = lambda: _CPUS
if hasattr(os, "process_cpu_count"):
    os.process_cpu_count = lambda: _CPUS

FAILED = set()
COLLECT_FAILED = set()
RAN = set()


def pytest_runtest_logreport(report):
    RAN.add(report.nodeid)
    if report.failed:
        FAILED.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        COLLECT_FAILED.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    out = os.environ.get("BR_LABEL_OUT")
    if out:
        with open(out, "w") as handle:
            json.dump({"failed": sorted(FAILED), "collect_failed": sorted(COLLECT_FAILED),
                       "ran": len(RAN), "exitstatus": int(exitstatus)}, handle)
