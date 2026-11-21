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
