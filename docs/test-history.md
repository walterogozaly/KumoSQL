# Test history

Every test run in this repository is recorded, so a report can show which tests break most often and which of them break changes that otherwise work.

## What is recorded

`tests/conftest.py` installs the recorder from `tools/test_history.py`. After any `python tools/run_tests.py` or `python -m pytest` run it writes one JSON line to its own file under `runs/` in the history folder, so runs on different threads never collide. The folder is `$KUMOSQL_TEST_HISTORY`, or `/mnt/project-files/test-history` when `/mnt/project-files` exists; `KUMOSQL_TEST_HISTORY=off` turns recording off. Without a shared folder (a laptop checkout) nothing is written.

A record holds:

- the commit, branch, whether the checkout had uncommitted changes, and a task label (`--label`, `$KUMOSQL_TASK`, else the branch name);
- the **targets**: the tests the run was aiming at. They come from `--target PATH` (repeatable; `$KUMOSQL_TEST_TARGETS` for plain pytest), else the test files given on the command line, else the test files the branch added or changed against the master it branched from. A run with none of these has no targets and cannot be attributed;
- the tests that failed, each marked as inside or outside the targets, with the first line of the message;
- the run mode (`full`, `evals`, `no-evals` or `partial`), counts, wall time, library versions (sqlglot, sqlfluff, z3, DuckDB), how many tests ran per file, and the duration of every test that took 3 seconds or more.

## Reading it

```shell
python tools/test_history.py report            # rankings
python tools/test_history.py report --since 7  # the last week
```

- **Tests that break changes that otherwise work.** Runs aimed at known tests where every target passed, on a branch other than master, and the test that failed anyway. Ranked by runs, then by how many different tasks it hit. A test high on this list guards behaviour that many fixes disturb, so it is worth running first and worth reading before changing the code it covers.
- **Tests that fail most, by where.** Failures inside the targets, outside them, on master and in runs with no known targets, over the number of runs that ran the test's file.
- **Flaky candidates.** Tests that failed in one run and passed in another at the same clean commit.
- **Slowest tests.** Median seconds.

Runs with 20 or more failures at once (a missing z3 or a wrong library version) are left out of the rankings.

When a run has targets and something outside them fails, the end of the pytest output says so and, where history exists, how many earlier changes that test broke:

```
targets (changed-tests): tests/test_quantified_rules.py
0 failed inside the targets, 1 outside them
  outside the targets: tests/test_x.py::test_q12 (broke 4 earlier changes that otherwise worked)
```

## Running failures first

`python tools/test_history.py order --write` writes `tests/order.json` from the history: the tests that took 3 seconds or more with their median durations, and the tests that failed most. Under pytest-xdist `tests/conftest.py` then runs, in this order:

1. tests that failed before;
2. the fast tests, so a broken one shows up in minutes;
3. the slow tests, longest first, so one long benchmark never runs alone at the end.

Without `tests/order.json` data it falls back to starting the files in `HEAVY_FILES` first. `python tools/run_tests.py --quick` skips the slow tier (about 6,300 of 6,440 tests, a few minutes). `python tools/run_tests.py -x` stops at the first failure.

Regenerate `tests/order.json` now and then, in a small PR of its own, once the history has more runs; a test missing from it counts as fast.

## Seeding and reuse

`python tools/test_history.py import-junit junit.xml --commit SHA --branch master --label NOTE` adds an existing JUnit file to the history. Records are plain JSON lines, so other tools can read `runs/*.jsonl` directly.
