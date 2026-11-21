# Blast Radius selector for sqlglot

Entry for the Blast Radius track (Builders Buildathon, sponsor RWX). For every patch
against sqlglot `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, it prints which tests to
run and how to split them over workers. Lower cost = recorded seconds of the busiest
worker + 2 s per worker, plus 300 s for every commit where a failing test was left
out, plus the CPU time the container uses.

The idea in one line: **compute a sound superset of the tests a patch can reach from
per-test coverage of the pinned commit, then, when it pays, actually run the cheap
ones against the patched tree and keep only the ones that fail.**

## How it decides

1. **Parse and apply the patch in memory** (`selector/patch.py`). New, deleted,
   renamed and binary files are handled; nothing touches the read-only checkout.

2. **Find what changed, semantically** (`selector/analyze.py`). Old and new Python
   files are parsed and compared statement by statement. Comments and formatting never
   reach the AST, so comment-only edits disappear (unless a test reads that file as
   data, see below). Each real difference becomes a *site* in the old file:
   * inside a function or lambda body: the statement (or compound-statement header)
     that changed, an inserted statement's neighbours, or a removed statement;
   * at import time (class bodies, module level, decorators, default values):
     the statement and the names it binds, and for dict literals the entry keys.
   Edits that no longer compile are detected (syntax and compile-time errors).

3. **Turn sites into candidate tests** (`selector/candidates.py`) using maps built
   from the pinned commit (`maps/`, see below):
   * function sites: tests that executed one of the site's lines, or entered the
     function (needed for lambdas and one-line defs, whose body shares the `def`
     line and never produces a line event);
   * import-time sites: tests that executed the statement (i.e. imported the module),
     narrowed to tests that **used the class** (instantiated it or a subclass, or
     entered one of their methods; a dialect and its tokenizer/parser/generator count
     as one family), then to tests that ran a line **mentioning a bound name** (or a
     line that reads attributes by computed name and could match it). For dicts keyed
     by sqlglot classes (`TRANSFORMS = {exp.Foo: ...}`) only tests that built an
     `exp.Foo` remain; for dicts keyed by SQL text (`FUNCTIONS`, `KEYWORDS`,
     `*_PARSERS`, `*_MAPPING`) only tests whose source or fixtures contain the key;
   * data files (fixtures, `.csv.gz`, `sqlglotc/setup.py`, ...): tests that opened the
     file. `.sql` fixture edits that only touch empty or `--` lines are no-ops (the
     test helpers drop those lines);
   * a module that no longer compiles fails every test that imports it; deleted files
     fail their importers and readers; new files reach tests that list the directory or
     mention the module's name; pytest configuration changes reach everything.

4. **Verify by running, when it is cheaper** (`selector/policy.py`, `selector/verify.py`).
   For each commit the planner compares the expected cost of including the candidates
   blindly (packing cost) with running the cheap candidates first (their CPU cost plus
   the expected packing cost of what fails). Verification runs in a forked child that
   builds a per-commit overlay in `/tmp` (symlinks to the pinned tree, real files only
   for patched paths, `__pycache__` shared so unchanged modules load from bytecode),
   imports sqlglot and the tests from it and runs the tests with unittest semantics
   (subtests, class fixtures, import errors fail the file, per-test time limits). The
   optimizer and executor tests use spawn-based process pools; those are replaced by a
   synchronous executor that still pickles arguments and results across the "boundary".
   The parent preloads third-party libraries and the sqlglot core once; a child whose
   patch does not touch the core reuses them. Expensive candidates (TPC-DS execution,
   `test_max_nodes`, ...) are never run locally.

   **Infection probes.** When every change of a commit is a side-effect-free
   expression inside a function body (a swapped operator, a changed constant, ...),
   the verifier compiles the patched module so that each changed expression evaluates
   both the original and the patched version and records whether they ever differ
   (value, truth value in conditions, or exception type). If thousands of evaluations
   across several tests never differ, the edit is inert for this test suite, and the
   expensive candidates are left out instead of included blindly. For `is`/`==` style
   swaps, which are nearly always inert, only the cheapest few candidates are run to
   observe this; anything else falls back to running or including every candidate.

5. **Pack** (`selector/pack.py`): longest-processing-time packing for every worker
   count 1..8 plus a move/swap improvement pass, choosing the cheapest.

Anything unexpected (unparsable patch, a file we do not know, a crash) falls back to
including every candidate (or every test) for that commit only; misses cost far more
than over-selection.

## Maps (built from the pinned commit only)

`maps/` holds plain files generated from the pinned commit, no patch-specific data:

* `coverage.json.gz`: for each test, run alone in a fresh interpreter (so import-time
  code and lazily built caches are attributed to every test that needs them):
  lines executed by function code and by module/class-body code, functions entered,
  repository files opened, directories listed, sqlglot classes instantiated, and the
  CPU each test costs our verifier.
* `classinfo.json.gz`: sqlglot class hierarchy, dialect families, class spans, the
  lines that read attributes by computed name, and test-method spans.

Regenerate with (Linux, the labeler's Python and packages):

```sh
docker build --platform linux/arm64 -f tools/trace_image/Dockerfile -t br-trace .
docker run --rm -v $PWD/out:/out -v /path/to/tests.json:/data/tests.json br-trace \
    python tools/trace_tests.py /opt/sqlglot /data/tests.json /out/trace --jobs 3
docker run --rm -v $PWD/out:/out -v /path/to/tests.json:/data/tests.json br-trace \
    python tools/measure_cpu.py /opt/sqlglot /data/tests.json /out/cpu.json
python tools/build_maps.py out/trace tests.json maps/coverage.json.gz out/cpu.json <scale>
python tools/build_classinfo.py <sqlglot checkout> maps/classinfo.json.gz
```

## Validation (`tools/`, not used at decision time)

* `gen_mutants.py` generates patches of many kinds (operator/constant/boolean edits in
  function and class bodies, deleted statements, comments in code and fixtures, fixture
  edits, test edits, docstrings, syntax errors, new and deleted files, multi-file mixes).
* `ground_truth.py` labels them by running every candidate test plus a random sample of
  other tests importing a changed module on the patched tree; a failing test outside
  the candidates would expose an unsound selection.
* `breakdown.py` scores an output against answers, per commit and per kind.
* `validation/` holds the generated patch sets with their labels. They exist only for
  this harness: `.dockerignore` keeps them (and `tools/`) out of the image, and the
  selector never reads them.

## Results (local)

Measured with the kit's `score.py` (simulated seconds are exact; CPU is not part of
those numbers):

| set | commits | simulated | oracle* | misses |
| --- | ---: | ---: | ---: | ---: |
| kit sample | 30 | 109.5 s | 94.4 s | 0 |
| generated mix (`validation/val1`) | see `validation/` | | | 0 |

\* oracle = cost of packing exactly the failing tests, the floor for any selector.

CPU on a heavily loaded Apple M1 host was roughly 0.3-0.6 s per commit; the scoring
machine's number is not known locally (amd64 emulation is far too slow to measure).

## Known risks

* Verification reproduces the labeler's run in-process; tests that depend on global
  state from other tests, or on real process pools, could behave differently.
* Name-reference narrowing of import-time changes, the SQL-text narrowing of
  `FUNCTIONS`/`KEYWORDS`/`*_PARSERS` entries and the "inert edit" probes are
  heuristics, not proofs; the validation sets found no miss from them.
* Expensive tests (TPC-DS execution, `test_max_nodes`) are only left out on the probe
  evidence; otherwise they are included whenever they are reachable.

## Layout

```
Dockerfile            build: locked deps, sqlglot @ pinned commit, bytecode, selector
selector/main.py      entry point (stdout: one JSON line per commit; logs on stderr)
selector/*.py         patch, analyze, candidates, policy, verify, runner, shim, pack
maps/                 coverage and class maps of the pinned commit
tools/                map generation and validation harness
```

MIT licensed.
