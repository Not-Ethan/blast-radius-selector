# Candidate submissions (branch br-opt)

Newest last. Numbers are local: simulated seconds from the kit's `score.py` (exact for
that output), oracle = packing exactly the failing tests, misses must be 0. "CPU" is
user+sys of the whole local run on a loaded Apple M1 (native, not emulated); the
scorer's CPU for the same work is unknown (the first submission used 6.45 s on the
~40 practice commits), so read it as relative.

Reference (first submission, 01c632d on the public repo): practice 195.8 s
(189.352 simulated + 6.45-6.49 CPU, 0 missed).

| set | commits | oracle |
| --- | ---: | ---: |
| kit sample | 30 | 94.4 |
| val1 | 126 | 208.6 |
| val2 | 68 | 169.7 |
| val4 | 36 | 12.4 |

## M1 — per-case restricted runs of fixture-loop tests + planner cost model

Commit: **7c3bd18** (branch br-opt). Docker re-run on exactly this tree: valid, 0 misses.

What changed:
* TPC-DS/TPC-H execution and optimizer tests (and every other test that loops over
  fixture cases) can be verified on only the cases a patch can affect: cases whose
  fixture text changed, or whose own per-case coverage (traced at build time as
  pseudo-tests in the coverage map) touches the changed code.
* Planner: per-test failure probabilities (measured), a test counts as too expensive to
  run only above max(3 s, 60% of its recorded seconds), and "verify the best
  seconds-per-CPU tests, include the cheap tail" options.

| set | before sim | after sim | misses | CPU before -> after |
| --- | ---: | ---: | ---: | ---: |
| kit sample | 109.5 | 106.3 | 0 | 16.7 -> 18.5 |
| val1 | 344.5 | 330.0 | 0 | 56.4 -> 56.4 |
| val2 | 352.2 | 277.8 | 0 | 30.7 -> 62.2 |
| val4 | 205.9 | 106.6 | 0 | 62.4 -> 113.7 |

Docker, scorer flags, linux/amd64 emulated on the M1, kit sample: valid output, 0
misses, finished in ~133 s wall; 3 verifications were killed by their CPU budget under
emulation (as before: emulation runs 5-10x slower), so the emulated simulated score is
higher than native (157.6).

Expected on practice: simulated down where a TPC-DS/TPC-H test is a candidate but does
not fail (each such commit was 25.45 s; now ~5-12 s of CPU to clear it); CPU up by a
few seconds. Risk: restricted runs rely on fixture cases being independent (checked by
reading every listed loop) and on per-case coverage; any reason a test is a candidate
that does not reach one of its traced cases forces a full run.

Real result of M1 (from SUBMISSIONS.md): 185.3 s = 171.045 simulated + 14.1-14.5 CPU,
0 missed (first entry 189.352 + 6.5). Simulated -18.3 s, CPU +7.8 s.

## M2 — typed coverage for class attributes + no-op statements

Commit: **4df374d** (branch br-opt). Builds on M1. Docker re-run on exactly this tree.

What changed:
* A changed class attribute read only as `self.NAME` / `cls.NAME` (dialect settings
  such as `JOIN_HINTS`, `LIMIT_ONLY_LITERALS`) reaches only tests that ran that line
  with a receiver of the changed class or a subclass (typed coverage: runtime class
  and MRO of `self`/`cls` per test, from a fresh interpreter; no record = kept).
* Inside function bodies, adding/removing/swapping `pass`, bare constants, or stores
  of constants to names the function never mentions is a no-op.
* Same planner and CPU scale as M1.

| set | M1 sim | M2 sim | misses | M2 local CPU |
| --- | ---: | ---: | ---: | ---: |
| kit sample (30) | 106.3 | 106.3 | 0 | 17.0 |
| val1, original 126 | 330.0 | 321.9 | 0 | (in the 141 run) |
| val1 now 141 (+15 expensive, pytest labels) | n/a | 690.4 (oracle 364.5) | 0 | 115.9 |
| val2 (68) | 277.8 | 276.6 | 0 | 59.8 |
| val4 (36) | 106.6 | 106.6 | 0 | 101.9 |
| val5 new kinds (15 labelled so far) | 109.7 | 49.6 (oracle 40.5) | 0 | 3.1 |

Docker, scorer flags, emulated amd64, kit sample: valid, 0 misses, 106 s wall.

Expected on practice: small (the sample is unchanged; gains are on class-level flag
edits and dead-code/docstring edits). Low risk: both changes only narrow where a
record proves the receiver class or the statement is provably inert.

Calibration note: the planner's per-job CPU estimate is ~0.79x the measured local CPU
(measured/estimate = 1.26 on val4+val5). Telling the planner CPU is 0.6x as expensive
changed nothing on the kit sample except one wasted verification (j6uz: both candidates
fail anyway) and on val1 cost +84 s local CPU for -21.9 s simulated, so the scale stays.
The remaining practice-like excess (val2, uniform file choice like the official
generator) is dominated by TPC-DS execution included when an optimizer edit is run by
most TPC-DS cases (9scn, g7wf, gzdj: 68 of val2's 107 s); that is the next target.

## M3 — TPC-DS execution verified by comparing optimizer outputs

Commit: **4cf2213** (branch br-opt). Builds on M2. Docker re-run on exactly this tree.

What changed: when every reason `test_execute_tpcds` is a candidate only reaches code
its cases run inside `optimize()` (coverage outside `optimize()` traced per case at
build time), the verifier runs the affected cases only up to the end of `optimize()`
and compares fingerprints of the call's input, output and input-after-call with the
ones recorded at the pinned commit (recorded under two hash seeds and once with all
cases in one process: identical for all 99 cases). Equal: the test cannot fail.
Different: run those cases in full when that is cheap, otherwise include the test.
Typical cost: ~0.2 s warm-up + ~0.1 s per case (map units), e.g. 5.6 s for all 57
executed cases, instead of ~36 s to execute them.

| set | M2 sim | M3 sim | misses | M3 local CPU (M2) |
| --- | ---: | ---: | ---: | ---: |
| kit sample (30) | 106.3 | 106.3 | 0 | 27.0 (17.0) |
| val1 (141) | 690.4 | 690.4 | 0 | 127.3 (115.9) |
| val2 (68) | 276.6 | **242.1** | 0 | 83.5 (59.8) |
| val4 (36) | 106.6 | **87.0** | 0 | 88.9 (101.9) |
| val5 (46 labelled) | 151.1 | 151.1 | 0 | 10.0 |
| TPC-DS check set (28 commits from all sets) | 509.8 | 455.7 | 0 | |

Docker, scorer flags, emulated amd64, kit sample: valid, 0 misses, finished within the
deadline (132 s wall emulated; 3 verifications killed by emulation slowness as before).

Expected on practice: helps whenever an optimizer edit reaches most TPC-DS cases and
does not break execution (val2's 9scn and g7wf: 25.5 s -> 9.1 s and 7.4 s). Cost: one
stage check (~4 s on the scorer at the observed CPU ratio) when such an edit does
break TPC-DS (the sample's 2jrq: +8.6 s local CPU, simulated unchanged).
Risk: relies on code outside optimize() behaving identically when optimize() returns
an identical tree (it is deterministic code fed identical inputs); residual gap as for
all coverage-based selection: state shared across cases inside one process.

CPU scale: also tried 0.7 with M3 on val2: -7.7 s simulated, +14.1 s local CPU, i.e.
break-even at a 0.5 scorer/local CPU ratio, so the scale stays at 1.0.

## M4 — re-run only the TPC-DS cases whose optimizer output changed (recommended next)

Commit: **0fdd4d8** (branch br-opt). Builds on M3. Docker re-run on exactly this tree
(valid, 0 misses, 131 s wall emulated).

What changed: a stage check matches `optimize()` calls to cases by the fingerprint of
their input; the cases whose output differs (or whose call is missing) are run in full
when that costs less than including the test (e.g. val2 gzdj: 4 of 32 cases differ ->
re-run 4 -> TPC-DS cleared: 25.45 s -> 10.0 s).

| set | M3 sim | M4 sim | misses | M4 local CPU |
| --- | ---: | ---: | ---: | ---: |
| kit sample (30) | 106.3 | 106.3 | 0 | 27.3 |
| val1 (141) | 690.4 | 690.4 | 0 | 122.4 |
| val2 (68) | 242.1 | **226.7** | 0 | 90.3 |
| val4 (36) | 87.0 | 87.0 | 0 | 92.0 |
| val5 (now all 64 labelled) | n/a | 209.7 (oracle 128.9) | 0 | 13.8 |
| TPC-DS check set (28) | 455.7 | 440.3 | 0 | |

Versus M1 (practice 185.3): on val2, the closest proxy for the official generator,
simulated 277.8 -> 226.7 (-51 s / 68 commits); sample unchanged; local CPU on val2
62 -> 90 s (about +14 s at the observed ~0.5 scorer/local ratio).

Real result of M4 (SUBMISSIONS.md): 165.0 s = 143.412 simulated + 21.4-21.7 CPU, 0 missed.
Scorer/local CPU ratio: practice 21.6 s / 40 commits = 0.54 s per commit vs 0.91 s per
commit locally on the kit sample (27.3 s / 30) -> about 0.6 (0.4 against val2).

## M5 — import checks (soundness), re-export-aware reads, dialect-typed coverage, stage abort (recommended next)

Commit: **7a966cc** (branch br-opt). Builds on M4. Docker re-run on exactly this tree
(valid, 0 misses).

What changed:
* Soundness: an import-time edit (class body, module level, decorators, defaults)
  whose new value is not provably safe to evaluate now always gets its module
  imported by the verifier, also when the plan is blind or nothing is reachable (a
  zero-test job); if the import fails or cannot be checked, every importer is
  included. Before, such an edit (e.g. a class-body value that raises at import) was
  narrowed by class usage and, on a blind plan, never checked. Checked with a
  synthetic tsql class-body edit that raises: 171 tests fail (pytest), 171 selected.
* A module-level value change that keeps the same names bound: `from M import NAME`
  lines are no longer reads (their later uses are; re-exports followed transitively);
  computed-name readers that only use classes are skipped for plain-data values
  (val6 7bef, a TIMEZONES entry: 1617 -> 93 candidates).
* Typed coverage also records the class of `self.dialect` on `self.dialect.NAME`
  lines (dialect settings read by the shared parser/generator).
* A TPC-DS stage check stops as soon as the test will be included anyway (sample
  2jrq: 8.6 s -> ~5 s local CPU).

| set | M4 sim | M5 sim | misses | local CPU M4 -> M5 |
| --- | ---: | ---: | ---: | ---: |
| kit sample (30) | 106.3 | 106.3 | 0 | 27.3 -> 23.8 |
| val1 (141) | 690.4 | 690.4 | 0 | 122.4 -> 125.5 |
| val2 (68) | 226.7 | 226.7 | 0 | 90.3 -> 88.5 |
| val4 (36) | 87.0 | 87.0 | 0 | 92.0 -> 87.9 |
| val3 (40 class-level edits, pytest labels) | 367.6 | **350.0** | 0 | 57.9 -> 42.8 |
| val5 (64 new kinds) | 209.7 | 209.7 | 0 | 13.8 -> 13.8 |
| val6 (41 official-style mutations, pytest labels) | 156.7 | **145.3** | 0 | 48.9 -> 36.5 |

One reported miss during this round was a false label: val6 bmwq listed
test_max_nodes as failing because the labeller's per-test limit (149 s) expired under
load; run alone on the patched tree it passes (45 s). Label corrected, limits widened.

Expected on practice: CPU slightly down; simulated down only where module-level data,
dialect settings or class-level values are edited. Main value: the import-check
soundness fix for unseen kinds of change in the final.
