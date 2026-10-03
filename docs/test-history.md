# Test history

Every test run in this repository is recorded, so a report can show which tests break most often and which of them break changes that otherwise work.

## What is recorded

`tests/conftest.py` installs the recorder from `tools/test_history.py`. After any `python tools/run_tests.py` or `python -m pytest` run it writes one JSON line to its own file under `runs/` in the history folder, so runs on different threads never collide. The folder is `$KUMOSQL_TEST_HISTORY`, or `/mnt/project-files/test-history` when `/mnt/project-files` exists; `KUMOSQL_TEST_HISTORY=off` turns recording off. Without a shared folder (a laptop checkout) nothing is written.

A record holds:

- the commit, branch, whether the checkout had uncommitted changes, and a task label (`--label`, `$KUMOSQL_TASK`, else the branch name);
- the **targets**: the tests the run was aiming at. They come from `--target PATH` (repeatable; `$KUMOSQL_TEST_TARGETS` for plain pytest), else the test files given on the command line, else the test files the branch added or changed against the master it branched from. A run with none of these has no targets and cannot be attributed;
- the tests that failed, each marked as inside or outside the targets, with the first line of the message;
- the run mode (`full`, `evals`, `no-evals` or `partial`), counts, library versions (sqlglot, sqlfluff, z3, DuckDB) and how many tests ran per file;
- the times: the run's wall time (`seconds`) and CPU time across every worker (`cpu_seconds`, start-up and collection included), each file's wall and CPU seconds (`file_seconds`, setup to teardown, summed over its tests), their totals (`test_seconds`, `test_cpu_seconds`), and, for every test whose call took 3 seconds or more, that duration (`slow`) and its CPU seconds from setup to teardown (`slow_cpu`);
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

`python tools/test_history.py order --write` writes `tests/order.json` from the history: the tests that took 3 seconds or more with their median durations, and the tests that failed most. Under pytest-xdist `tests/conftest.py` then runs, in this order:

1. any test that alone takes more than half of one worker's share of the run (started after the fast tests, it would end the run late);
2. tests that failed before;
3. the fast tests, so a broken one shows up in minutes;
4. the slow tests, longest first, so one long benchmark never runs alone at the end.

The workers take tests in exactly that order. `tools/xdist_scheduler.py` replaces pytest-xdist's `loadgroup` scheduler, which kept up to three tests queued on each worker and moved test groups to the front: a worker now takes more work only while everything it holds is fast. A pytest-xdist worker starts a test only once it holds the one it runs next as well, so a worker starting a slow test gets the first fast test left (or the quickest one) as its next test, never the next slow one, and two long benchmarks never wait in one worker's queue while another worker is idle. Groups (`xdist_group`) still run on one worker. In a full run on 4 workers the workers were busy 98% of the run's wall time with this scheduler, against 76% with `loadgroup`, whose last three workers sat idle for the last 13 minutes while one worker finished the MV workload eval and the tests queued behind it.

Without `tests/order.json` data it falls back to starting the files in `HEAVY_FILES` first. `python tools/run_tests.py --quick` skips the slow tier (about 6,300 of 6,440 tests, a few minutes). `python tools/run_tests.py -x` stops at the first failure.

Regenerate `tests/order.json` now and then, in a small PR of its own, once the history has more runs; a test missing from it counts as fast.

## Seeding and reuse

`python tools/test_history.py import-junit junit.xml --commit SHA --branch master --label NOTE` adds an existing JUnit file to the history. Records are plain JSON lines, so other tools can read `runs/*.jsonl` directly.
