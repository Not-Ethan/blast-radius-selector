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

## M4 — re-run only the TPC-DS cases whose optimizer output changed (submitted: 165.0)

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

## M5 — import checks (soundness), re-export-aware reads, dialect-typed coverage, stage abort (superseded by M6)

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

## M6 — function-scoped computed-name reads, typed records for the slowest tests (submitted: 157.2)

Commit: **d1cc829** (branch br-opt). Builds on M5 (so it carries M5's import-check
soundness fix). Docker re-run on exactly this tree (valid, 0 misses).

What changed on top of M5:
* A computed-name read (`getattr`/`vars`/`__dict__`) inside a function or lambda
  only counts for tests that ran that function; the module body executing the same
  line just defines it. The executor's `EXTRACT` lambda (`getattr(e, this)`) shares its
  line with a module-level dict, so before, every module- or class-level edit
  reachable from the executor also selected TPC-DS execution.
* Typed coverage now also has records for test_execute_tpcds and test_max_nodes
  (they were skipped before, which kept them as candidates for dialect-setting edits).
* One cached AST index per file for name/import/attribute scans: candidate sets
  identical to M5 on all 420 local commits, ~30% less analysis CPU.

| set | M5 sim | M6 sim | misses | M6 local CPU (M5) |
| --- | ---: | ---: | ---: | ---: |
| kit sample (30) | 106.3 | 106.3 | 0 | 20.5 (23.8) |
| val1, M5's 141 | 690.4 | **667.3** | 0 | |
| val1 now 199 (+58 pytest labels) | n/a | 1120.8 (oracle 717.9) | 0 | 175.9 |
| val2 (68) | 226.7 | 226.7 | 0 | 70.9 (88.5) |
| val4 (36) | 87.0 | 87.0 | 0 | 78.8 (87.9) |
| val3 (40 class-level) | 350.0 | **292.0** | 0 | 45.0 (42.8) |
| val5 (64 new kinds) | 209.7 | 209.7 | 0 | 10.2 (13.8) |
| val6, M5's 41 official-style | 145.3 | **96.4** | 0 | |
| val6 now 57 | n/a | 186.9 (oracle 159.6) | 0 | 36.8 |

(Local CPU varies with machine load; compare within a row only roughly.)

Expected on practice: simulated down for module-level data and dialect/class-setting
edits that TPC-DS used to be dragged into (val6 7bef and 9q8d: 25.5 s each before),
CPU slightly down. Risk: the function-scope rule relies on line coverage separating
function code from module code (the tracer records them separately) and on locating
the getattr/vars/__dict__ call on that line (a line where none is found keeps the old
rule).

## M7 — fallbacks for unreadable patches, fast planner packing (superseded by M8)

Commit: **32e3c98** (branch br-opt). Builds on M6. Docker re-run on exactly this tree
(`git archive` of the commit): valid, 0 misses (emulated amd64; 3 verifications killed
by emulation slowness, as for every candidate).

What changed:
* Robustness: text that parses to no diff section, or a patch with two sections for one
  file, now runs everything (before: "nothing reachable", 0 tests); a section with no
  hunks and no mode change counts as an unseen (binary) change; analysis stops reasoning
  after half the time budget. Checked on 17 malformed/edge patches natively and in Docker.
* Planner option estimates pack big selections without the quadratic move/swap pass
  (memoised). Selections identical on every set; planning CPU on val1 28.2 s -> 1.0 s,
  val3 15.9 -> 0.3 s (only commits with ~1600-test blind plans were affected; sample
  0.08 s either way).
* val2 +52 and val6 +3 commits labelled with pytest; val4's 36 and 25 of val2's 68 older
  in-process labels re-checked with pytest: identical.

## M8 — per-case runs for the other 23 fixture-loop tests of test_optimizer (superseded by M9)

Commit: **8b8c280** (branch br-opt). Builds on M7. Docker re-run on exactly this tree
(`git archive` of the commit): valid, 0 misses, done at 126 s of the emulated run (2
verifications killed by emulation slowness, as before). Map 668 KB -> 893 KB.

What changed: per-case coverage and CPU for test_optimize, test_qualify_columns,
test_annotate_funcs, test_simplify, test_merge_subqueries and the 18 other
`check_file` loops of tests/test_optimizer.py (4,064 cases). Each case was traced in a
child of a zygote that had imported the tests, and its record is unioned with the
test's cold zero-case record (imports + the code outside the loops), so it covers at
least what a cold run of that case covers. Verification of such a test runs only the
cases that reach the change, under the same rule as M1 (every reason that reaches the
test must reach a kept run, else the whole test runs).

| set | M6/M7 sim | M8 sim | misses | M8 extra verify CPU (planner estimate, local) |
| --- | ---: | ---: | ---: | ---: |
| kit sample (30) | 106.3 | **104.8** | 0 | +1.3 |
| val1 (199) | 1120.8 | **1105.5** | 0 | +17.3 |
| val2 (120) | 609.3 | **581.4** | 0 | +19.4 |
| val4 (36) | 87.0 | **79.3** | 0 | +1.2 |
| val3 (40 class-level) | 292.0 | **276.9** | 0 | +8.5 |
| val5 (64 new kinds) | 209.7 | **203.5** | 0 | +2.5 |
| val6 (60 official-style) | 197.6 | **193.2** | 0 | +0.1 |

Measured local CPU is not comparable this hour (other jobs on the machine pushed the
load average to ~40; commits with unchanged plans took ~2x the CPU of the earlier run),
so the CPU column is the planner's own estimate. At the observed scorer/local CPU
ratio (~0.5) every set gains net. Expected on practice: ~1.5 s simulated less on a
sample-like batch for ~0.7 s more CPU; more where optimizer code is edited.
Risk (not verified): the per-case risk of M1 now applies to 23 more tests: a case is
skipped when its own run (alone) does not reach the change, which assumes no earlier case
of the same test leaves state behind that makes it reach the change in a full run. Each
case parses its SQL afresh; whether shared objects such as a test's schema are mutated
across cases has not been checked. Consistency check done: for all 28 per-case tests,
every line the full run covers (at the pinned commit) is covered by the zero-case run or
by some case run alone (0 lines only in the full run).

## M9 — tokenizer settings narrowed through the tokenizer core (recommended next; includes M7, M8)

Commit: **f3572a4** (branch br-opt). Builds on M8. Docker re-run on exactly this tree
(`git archive` of the commit): valid, 0 misses, done at 126 s of the emulated run (1
verification killed at the emulated deadline).

What changed:
* `Tokenizer._init_core` copies the tokenizer settings into a `TokenizerCore`
  (`numeric_escapes=self.NUMERIC_ESCAPES`, ...). That line runs whenever a tokenizer
  is built, so an edit of any setting in a dialect's Tokenizer selected every test
  that tokenized that dialect (sample 4d63: 358 tests for a Snowflake
  `NUMERIC_ESCAPES` entry). New build-time record (`tools/trace_tokcore.py`, each test
  alone in a fresh interpreter, 1620/1620 recorded): which lines of
  `tokenizer_core.py` that observe a setting field ran for cores built by which
  Tokenizer class. The `_init_core` line now counts only for tests that ran an
  observing line of that field for the changed class or a subclass
  (`selector/tokcore.py`). Off if the patch touches the core or `_init_core`, or
  mentions `TokenizerCore`/`_init_core` elsewhere.
* A line that uses a changed class attribute only for its truth value (following a
  local copied from it) is dropped when both values have the same truth; for a dict
  display whose keys stay the same, `key in X` lines are dropped too (the Dialect
  metaclass only tests `"x" in NUMERIC_ESCAPES`). `NAME: annotation` statements
  without a value are not reads.
* Candidates: 4d63 358 -> 7, val2 muzi 316 -> 20, vfxf -> 4, w47z -> 12 (3 failing,
  all kept), icb8 -> 1, val3 7upf -> 7.

| set | M8 sim | M9 sim | misses | verify CPU estimate M8 -> M9 (local) |
| --- | ---: | ---: | ---: | ---: |
| kit sample (30) | 104.8 | **96.8** (oracle 94.4) | 0 | 23.8 -> 22.6 |
| val1 (199) | 1105.5 | 1105.5 | 0 | 136.0 -> 136.0 |
| val2 (120) | 581.4 | **552.8** | 0 | 157.7 -> 155.3 |
| val4 (36) | 79.3 | 79.3 | 0 | 42.8 -> 42.8 |
| val3 (40 class-level) | 276.9 | **270.6** | 0 | 31.4 -> 30.7 |
| val5 (64 new kinds) | 203.5 | 203.5 | 0 | 9.4 -> 9.4 |
| val6 (60 official-style) | 193.2 | 193.2 | 0 | 49.8 -> 49.8 |
| val7 = 44 generated tokenizer-setting edits (pytest labels, 15 with failures) | 562.8 | **70.8** (oracle 32.8) | 0 | |

Expected on practice: one sample commit in 30 was such an edit (4d63: 9.5 s in M7,
7.9 s in M8, 0 s in M9: its 7 remaining candidates are verified in 1.1 s local CPU);
M9's sample total is 2.5 s above the oracle.
Risk: the record is per test at the pinned commit; it assumes a test's path through
the core up to the first read of the changed field does not depend on that field
(true by construction: identical inputs and code until the field is read) and that
runs are deterministic. A core built outside `_init_core` matches every class.
