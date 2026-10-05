# Test history

[Plain-language version](../docs_simple/test-history.md)

Every test run in this repository is recorded, so a report can show which tests break most often and which of them break changes that otherwise work.

## Merge testing procedure

Walter requested recording this procedure on `master`; SQLGlot remains pinned to 30.21.0 with one matching compiled configuration.

1. **Before queueing a code PR:** run the change's targeted regressions and affected evals, including an eval a PR is intended to improve. Run `python tools/run_tests.py --quick --label "<task>"` as the broad, cheaper screen. Documentation-only changes can use `tests/test_docs.py` instead. Passing quick checks alone does not cover all benchmark floors.
2. **Choose eval coverage from the change:** shared prover, parsing, normalization, translation or execution changes need broader comparisons against the base. Compare case outcomes, not just a net score: gains in one suite do not justify losses in another. For a change intended to preserve all scores, use `tools/eval_diff.py` as described in `CLAUDE.md`. Maintain each suite's floors and zero wrong proofs; a soundness correction that removes invalid proofs must explicitly document corrected scores and update the affected results and guards rather than hide the loss.
3. **Before merging a code batch:** run `python tools/run_tests.py --label "merge candidate <sha>"` on the exact combined candidate. This full default run includes eval floors and excludes tests marked `slow`; explicitly select affected slow checks when needed. One full candidate run can validate several PRs together. A changed candidate needs new validation. If non-eval tests and evals run separately, both passing reports must refer to the same candidate SHA. A failure or worker crash must be investigated before claiming the batch passes.
4. **Use fresh evidence appropriately:** the optional SQLSolver [sample-execution cache](evals/sqlsolver.md#reusing-sample-execution-checks) skips unchanged finite execution checks, never the current prover or targeted counterexample search. Changed inputs invalidate it and entries expire after seven days. Use `--eval-cache off` for an explicitly fresh run; CI remains uncached by default. Cache timing on a small sample is not a measurement of the entire suite.

**Current enforcement:** the Dell presently watches `master` and publishes post-merge results on `dell-runner/results`. That provides monitoring, not the pre-merge candidate gate above. Its candidate handoff is not connected, and `master` currently has no GitHub branch-protection requirement enforcing these tests. Until connected, the merge operator must obtain and check the exact candidate's validation through the existing train or a manual candidate run. Do not describe a post-merge Dell result as pre-merge validation or auto-merge code PRs solely because GitHub permits it.

## What is recorded

`tests/conftest.py` installs the recorder from `tools/test_history.py`. After any `python tools/run_tests.py` or `python -m pytest` run it writes one JSON line to its own file under `runs/` in the history folder, so runs on different threads never collide. The folder is `$KUMOSQL_TEST_HISTORY`, or `/mnt/project-files/test-history` when `/mnt/project-files` exists; `KUMOSQL_TEST_HISTORY=off` turns recording off. Without a shared folder (a laptop checkout) nothing is written.

A record holds:

- the commit, branch, whether the checkout had uncommitted changes, and a task label (`--label`, `$KUMOSQL_TASK`, else the branch name);
- the **targets**: the tests the run was aiming at. They come from `--target PATH` (repeatable; `$KUMOSQL_TEST_TARGETS` for plain pytest), else the test files given on the command line, else the test files the branch added or changed against the master it branched from. A run with none of these has no targets and cannot be attributed;
- the tests that failed, each marked as inside or outside the targets, with the first line of the message;
- the run mode (`full`, `evals`, `no-evals` or `partial`), counts, library versions (sqlglot, sqlfluff, z3, DuckDB) and how many tests ran per file;
- the times: the run's wall time (`seconds`) and CPU time across every worker (`cpu_seconds`, start-up and collection included), each file's wall and CPU seconds (`file_seconds`, setup to teardown, summed over its tests), their totals (`test_seconds`, `test_cpu_seconds`), and, for every test that took 3 seconds or more from setup to teardown, that duration (`slow`) and its CPU seconds (`slow_cpu`), and every setup of 3 seconds or more (`slow_setup`: a shared fixture built for that test). Records before version 3 timed the slow tests' call only;
- the machine: cores, processor, memory, operating system and whether sqlglot ran compiled or pure (no host or user names).

CPU time counts every thread of a worker (DuckDB's too) and the child processes a test waited for, such as a benchmark's process pool. On Windows only the worker's own time is counted. Wall time depends on how busy the machine was; CPU time is the steadier measure of the work a test does, and the one to compare when a change is meant to make a test cheaper.

## Reading it

```shell
python tools/test_history.py report            # rankings
python tools/test_history.py report --since 7  # the last week
```

- **Tests that break changes that otherwise work.** Runs aimed at known tests where every target passed, on a branch other than master, and the test that failed anyway. Ranked by runs, then by how many different tasks it hit. A test high on this list guards behaviour that many fixes disturb, so it is worth running first and worth reading before changing the code it covers.
- **Tests that fail most, by where.** Failures inside the targets, outside them, on master and in runs with no known targets, over the number of runs that ran the test's file.
- **Flaky candidates.** Tests that failed in one run and passed in another at the same clean commit.
- **Slowest tests.** Median seconds.

```shell
python tools/test_history.py trend                                       # test times over time
python tools/test_history.py trend --test tests/test_qed_benchmarks.py   # one file's (or one test's) history
```

`trend` lists the whole runs (`full`, `evals`, `no-evals`), newest last, with commit, workers, cores, the sqlglot build, wall and CPU time; then the files whose wall time moved most between the earliest and the latest whole runs of the same mode on the same core count; then where the CPU goes in the newest whole run, file by file. `--test` prints every recorded time of the files, or the slow tests (an id with `::` or `[`), whose name contains the text. Runs recorded before the times were kept show the slow tests' summed seconds and no CPU.

Runs with 20 or more failures at once (a missing z3 or a wrong library version) are left out of the rankings.

When a run has targets and something outside them fails, the end of the pytest output says so and, where history exists, how many earlier changes that test broke:

```
targets (changed-tests): tests/test_quantified_rules.py
0 failed inside the targets, 1 outside them
  outside the targets: tests/test_x.py::test_q12 (broke 4 earlier changes that otherwise worked)
```

## Running failures first

`python tools/test_history.py order --write` writes `tests/order.json` from the history: the slow tier (tests that took 3 seconds or more, usually or by a wide margin, with their median durations), the files with a slow shared fixture (`together`, with the whole file's median seconds), the tests that failed most, and every test file the history has run (`files`). Under pytest-xdist `tests/conftest.py` then runs, in this order:

1. any test that alone takes more than half of one worker's share of the run (started after the fast tests, it would end the run late);
2. tests that failed before;
3. the fast tests, so a broken one shows up in minutes;
4. the slow tests, longest first, so one long benchmark never runs alone at the end.

The workers take tests in exactly that order. `tools/xdist_scheduler.py` replaces pytest-xdist's `loadgroup` scheduler, which kept up to three tests queued on each worker and moved test groups to the front: a worker now takes more work only while everything it holds is fast. A pytest-xdist worker starts a test only once it holds the one it runs next as well, so a worker starting a slow test gets the first fast test left (or the quickest one) as its next test, never the next slow one, and two long benchmarks never wait in one worker's queue while another worker is idle. Groups (`xdist_group`) still run on one worker, and so do the tests of a `together` file: the scheduler hands the file out as one unit, so its fixture is built once instead of once per worker, and its test ids stay as they are (an `xdist_group` mark would change them). The first full run that timed fixtures found five such files (the duplicate, lineage-goldens, LLM-SQL-Solver, analysis-scale and optimizer-bugs benchmarks), whose module fixtures had been built on two or three workers each; run on 4 workers, those five files took 29.0 s wall and 101.5 s CPU split across workers and 24.6 s wall and 52.0 s CPU with one worker per file. In a full run on 4 workers the workers were busy 98% of the run's wall time with this scheduler, against 76% with `loadgroup`, whose last three workers sat idle for the last 13 minutes while one worker finished the MV workload eval and the tests queued behind it.

Without `tests/order.json` data it falls back to starting the files in `HEAVY_FILES` first. `python tools/run_tests.py --quick` skips the slow tier and runs the rest (about 9,500 tests; 4 to 7 minutes on a 4-core container, about a tenth of the CPU time of a full run). A run records a test only when it took 3 seconds or more, so a test that was slow on a loaded machine in two runs out of a hundred looked slow to the median of those two runs and was dropped from `--quick`. The slow tier therefore holds a test only if it was slow in at least half of the runs that ran its file or its median slow run took 10 seconds or more; a cheap test that spiked now and then stays in `--quick`. Across the recorded runs, 27 of the 29 changes whose branch broke a test outside its own targets broke at least one test this tier keeps in `--quick`; the other two were caught only by slow tests (the quantified-rules cases in one, the QED Calcite and Cosette SPES floors in the other). A test that is cheap alone but sits in an eval file the history has never run still counts as slow until `order --write` has seen it, so refresh `tests/order.json` after adding eval tests. `python tools/run_tests.py -x` stops at the first failure.

Regenerate `tests/order.json` now and then, in a small PR of its own, once the history has more runs (the test speed thread does it daily). A test missing from it counts as fast, except in an eval file (`EVAL_FILES`) that the history has never run: those count as slow (60 seconds) until a recorded run times them, so a new benchmark neither lands in `--quick` nor starts late.

## DuckDB on one thread

Tests open thousands of tiny in-memory DuckDB databases (a few rows per table). `tests/conftest.py` wraps `duckdb.connect` for the whole test process, and the pool workers its tests fork, so a connection gets `threads=1` unless the caller passes `threads` itself; every other argument and config key goes through unchanged. By default DuckDB starts a thread per core for every database, and under pytest-xdist those threads only compete with the other workers for the same cores. When pandas is not installed the same hook also puts `None` under `pandas` in `sys.modules`: DuckDB otherwise tries `import pandas` for every bound parameter of `execute` and `executemany`, and each try searches all of `sys.path` before it fails. `import pandas` still fails and `importlib.util.find_spec("pandas")` still returns None; an installed pandas is left alone. `tests/test_duckdb_defaults.py` checks both. On nine DuckDB-heavy test files (the VeriEQL, Singh and Bedathur, targeted-data, behaviour, incremental and fuzzing floors, the quantified-rules and result-equivalence tests), run serially before and after on a shared 4-CPU machine, the two together cut CPU time by 22% (2,379 to 1,853 seconds; 3% to 35% per file) and every eval verdict stayed the same. A caller that passes `threads` keeps its own setting: a test or benchmark that needs more threads passes `threads` (or runs `SET threads`), and a helper that already opens its databases with `threads=1` works the same with or without this default.

## Seeding and reuse

`python tools/test_history.py import-junit junit.xml --commit SHA --branch master --label NOTE` adds an existing JUnit file to the history. Records are plain JSON lines, so other tools can read `runs/*.jsonl` directly.
