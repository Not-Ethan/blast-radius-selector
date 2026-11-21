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
