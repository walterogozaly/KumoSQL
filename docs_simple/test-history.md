# Running tests and reading their history

[All simple guides](README.md) · [Full reference](../docs/test-history.md)

Test history records what ran, what failed, and how long it took. It helps distinguish a failure in the feature being changed from a failure elsewhere.

From a development checkout, install test dependencies:

```sh
python -m pip install -e ".[dev]"
```

For a documentation change:

```sh
python tools/run_tests.py --label "simple documentation" --target tests/test_docs.py tests/test_docs.py
```

`--target` records the intended test file in history. The final positional path chooses which tests actually run. Use `-j 2` to choose two workers or `-j 1` for a serial run.

## Other useful runs

| Command option | What runs |
| --- | --- |
| No selection | The default suite, excluding tests marked slow |
| `--evals` | Benchmark floor tests |
| `--no-evals` | Tests other than benchmark floors |
| `--quick` | Skips the slow tier (tests that usually take 3 seconds or more, or 10 seconds or more in any run); about 9,500 tests in a few minutes |

The recorder writes one JSON-lines file per run in the shared history directory when configured. Set `KUMOSQL_TEST_HISTORY` to choose the folder, or `off` to disable recording. An ordinary laptop checkout without a shared folder does not record it automatically.

## What the history can tell you

It can highlight tests that failed outside a task's targets even when the targets passed. Those tests may protect behavior that many changes accidentally disturb.

It can also compare durations and place likely failures earlier in a parallel test run. `python tools/test_history.py order --write` regenerates `tests/order.json` from recorded runs.

Example of using it: 29 changes in the recorded history broke a test outside their own targets. Most of the broken tests were cheap (28 of 43 took under a second) and different each time: only 6 of the 43 had failed in an earlier change, and the one real repeat offender, the scoreboard tests (the check that every eval test file is listed in `tests/conftest.py` and the check that the README scoreboard is current), accounts for 6 of the 29 changes. So a short fixed list of "tests that always fail" would have caught only a minority; running every test that is not slow caught 27 of the 29. The two it missed were broken only in slow evals. Treat `--quick` as the run to make before queueing a pull request, and the merge train's full run as the check for the slow evals. This is a small sample from one project's first days of history, so the percentages are a guide, not a promise.

History explains past runs; it does not establish that your current change passes. Run the relevant tests for the current checkout. The full reference covers reports, importing JUnit results, and sharing history across runs.
