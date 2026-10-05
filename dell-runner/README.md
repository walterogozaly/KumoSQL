# Dell test runner

A background script on the Dell that runs KumoSQL's full test suite on spare hardware and reports through a git branch.
No LLM is involved, so it costs no tokens and nobody has to babysit it. Written 2026-10-05; the Windows install script
is untested (no Windows machine here), everything else was exercised on Linux against a local test origin.

## What it does

Standard-library Python (`runner.py`), config in `config.json`. It loops, once a minute asking GitHub what changed:

1. **Candidate jobs (off until the train opts in).** If a branch `merge-train/candidate-N` exists on origin, it runs the
   eval floors on it (`candidate_mode: evals`) and writes a result. Today the train builds candidates only in its own
   container, so this never fires until the train pushes them (see "Phase 2").
2. **Master legs (works today).** It runs the full suite on current `master` against four legs, the leg that has gone
   longest without a run first, each leg in its own virtualenv:
   - `compiled`: sqlglot 30.21.0 + sqlglotc (what the train runs)
   - `pure`: the same, with `--pure` (pure-Python sqlglot)
   - `sqlglot-30.20.0` and `sqlglot-26.0.0`: the other versions in `.github/workflows/tests.yml`

   GitHub Actions last ran on 2026-09-30, so nothing tests these today. The train runs only the compiled leg.
   A master leg is stopped (and retried later) when a candidate branch shows up.

Results go to the branch **`dell-runner/results`** (never master): `results/<time>-<job>-<leg>.json` (counts, failing test
ids, a scrubbed output tail when something failed) and a rolling `latest.md`
(<https://github.com/walterogozaly/KumoSQL/blob/dell-runner/results/latest.md>). Threads and the train read it with
`python /mnt/project-files/dell-runner/read_results.py [--failed] [--sha X] [--job candidate-7]`, which needs no credentials.

It runs pytest with a scrubbed environment (no tokens), below-normal priority and one core left free, so Chrome Remote
Desktop stays responsive. It only ever fetches branches of the origin repo (master and `merge-train/candidate-*`),
never forks. Tests are your own code, but they do run on the Dell with your Windows user's rights.

## Which setup wins

| Option | Verdict |
|---|---|
| **Scripted runner (this)** | **Wins.** No tokens, survives reboots and Remote Desktop disconnects, same flow whether the Dell is Windows or WSL. Fills a real gap now (the sqlglot/pure/Windows matrix nobody runs) and can take half of each train run later. |
| Claude Code Remote Control session on the Dell | Good for one-off work on the Dell (debugging a Windows-only failure, the first install). Poor as the runner: every run spends model turns to do what a 300-line script does, and it needs the Claude app kept open. Keep it as the escape hatch. |
| Walter runs `python tools/run_tests.py` by hand | No: it does nothing for the train, and a full run makes the remote desktop crawl. |
| Dell as a second merge train | No. Two trains testing overlapping candidates means conflicting merges; the project is test-limited, not machine-limited. |

**Honest size of the win.** Full train runs lately take 1,400 to 2,000 s on 4 cores (5,100 to 7,400 CPU-s). About two
thirds of that CPU is the eval floors (`--evals` ~4,800 CPU-s, `--no-evals` ~2,000 to 2,300). If the train runs `--no-evals`
(~600 to 1,000 s) while the Dell runs `--evals` on the same candidate, wall time per candidate is bounded by the Dell's
eval run: about 25 to 35% faster with a 4-core Dell, about 2x with 8 cores. That is not a game changer, and it needs the train
to change, so Phase 1 delivers value without touching it. The bigger lever remains running less: `--quick` plus targets
catches 27 of 29 collateral breakages in about 4 minutes (see `test-speed/sentinel-analysis.md`).

## What Walter does on the Dell (about 15 minutes, once)

On the Dell (over Chrome Remote Desktop):

1. Get the files there, either way:
   - the install script downloads the rest itself: open PowerShell and run
     `iwr https://raw.githubusercontent.com/walterogozaly/KumoSQL/claude/project-thread-lm6mcq/dell-runner/install.ps1 -OutFile $env:TEMP\install.ps1; powershell -ExecutionPolicy Bypass -File $env:TEMP\install.ps1`
   - or download `install.ps1`, `runner.py`, `config.json` from this thread's attachments, put them in one folder
     (Chrome Remote Desktop's file transfer works) and run `powershell -ExecutionPolicy Bypass -File install.ps1`.
2. The script installs Python 3.12 and Git with winget if missing (accept the admin prompt for Git), stops the Dell
   sleeping while plugged in, and runs `runner.py init`.
3. **GitHub sign-in:** `init` pushes a heartbeat to `dell-runner/results`. Git Credential Manager opens a browser window;
   sign in as the account that owns KumoSQL and approve. (If you prefer a token: fine-grained, this repo only, Contents
   read/write.) That sign-in has write access to the repo, so use a Dell account you trust and keep the Dell's login locked.
4. The script registers the scheduled task "KumoSQL Dell Runner" (at log-on, restarts on failure, no time limit) and starts it.
   Leave the Dell logged in; closing Chrome Remote Desktop does not log it out.
5. Optional, faster tests: rerun from an admin PowerShell with `-DefenderExclusion` to exclude the runner's work folder
   from Defender's real-time scan.

The first full master run takes a while (installing z3, duckdb and sqlfluff, then the suite). Check progress with
`cd %USERPROFILE%\kumo-runner; python runner.py status` or `Get-Content work\logs\runner.log -Tail 20 -Wait`.

**Expect a Windows baseline.** The suite has POSIX-only spots (`signal.SIGALRM` in `query_optimizer.py`, `import resource` in
`tests/test_test_history.py`), so the first native-Windows run will likely show some failures that are Windows-only, not
regressions. Those are findings too (the test user's laptop is Windows). Once the first run is in, list the known ones in
`config.json` as `"known_failures": ["tests/test_x.py::test_name", ...]` (prefixes); they then show as "green except N known"
and only new failures read as failures.

## WSL instead of native Windows

Native Windows matches the test user's platform; the cloud already covers Linux. If you want the Dell's results to be
trustworthy for gating merges, or the Windows baseline is too noisy, run the Linux build inside WSL2 (needs admin once:
`wsl --install -d Ubuntu`, reboot): in Ubuntu run `bash install.sh` (same files; it prompts for a GitHub token or reuses the
Windows credential manager), then start with `nohup python3 runner.py run &`, or register a Windows scheduled task that runs
`wsl -e bash -lc "cd ~/kumo-runner && python3 runner.py run"`. Run one or the other, not both, unless you give them
different `results_branch` values.

## Phase 2: handing the train's evals to the Dell (needs the Merge train thread)

Not done; nothing here changes the train. Proposed protocol, for the train thread to accept or adjust through the coordinator:

1. When `train.py build` makes candidate N, the train also pushes it: `git push origin merge-train/candidate-N`
   (a throwaway branch, deleted after the finish step).
2. The train starts `run_tests.py --no-evals` in its container as today. The Dell, within a minute, starts `--evals` on the
   same sha (a `candidate-N` job) and pushes the result.
3. When `--no-evals` finishes, the train runs `python /mnt/project-files/dell-runner/read_results.py --job candidate-N`. A
   result for the right sha: green means the floors passed; red is handled like any other failure. No result yet: wait until
   the train's own `--evals` would have finished, then run `--evals` itself (the Dell is a bonus, never a dependency).
4. Rebuilds on a moved master are new candidates with new shas, so stale Dell results can never count (the record carries `sha`).

Cores decide how good this is, so measure before switching it on: run one candidate both ways and compare wall times. The eval
files could also be split between the two machines (`pytest_args` accepts a file list) to balance them.

## Files

| File | What |
|---|---|
| `runner.py` | the runner (`init`, `run`, `once`, `status`; `--no-push`, `--dry-run`) |
| `config.json` | repo, legs, polling, `known_failures`, `jobs` (workers; default cores minus one) |
| `install.ps1` | Windows one-time setup and scheduled task |
| `install.sh` | Linux/WSL one-time setup |
| `read_results.py` | read the results branch from anywhere, no credentials |

Copies of the scripts also live on the `claude/project-thread-lm6mcq` branch of KumoSQL under `dell-runner/` so the install
command can download them; that branch is not meant to merge.

## Limits

- Result files are plain data on a branch of a public repo: test ids and a scrubbed output tail, no paths or credentials.
- A crashed job (no tests ran, timeout, killed) is recorded as `crashed` or `timeout`, never as green.
- The Dell being off, asleep or logged out means no results, and nothing else breaks.
