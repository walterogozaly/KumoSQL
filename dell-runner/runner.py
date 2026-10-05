#!/usr/bin/env python3
"""KumoSQL Dell runner: runs the test suite on spare hardware and reports through a git branch.

Standard library only, so the install needs nothing but Python and git. Works on Windows (native Python), WSL and Linux.

    python runner.py init            # one-time: create the results branch and push a heartbeat (checks git sign-in)
    python runner.py run             # loop forever (what the scheduled task runs)
    python runner.py once            # do at most one job, then exit
    python runner.py status          # what it would do next and what it has finished
    python runner.py once --no-push --dry-run

Two kinds of job, in this order:

1. candidate: a branch named like ``merge-train/candidate-N`` on origin (the merge train's pre-merge branch). The runner runs
   the eval floors (``candidate_mode``: ``evals``, ``no-evals`` or ``full``) and writes a result file. It only runs when such a
   branch exists, so until the train pushes candidates this kind of job never happens.
2. master leg: current ``master`` against each leg in config.json (compiled sqlglot, pure Python sqlglot, the sqlglot 30.20
   and 26.0 versions), one leg at a time, the leg that has gone longest without a run first. A master leg stops and is
   retried later when a candidate branch appears.

Results are files on the ``results_branch`` of the repo (never master): ``results/<time>-<job>-<leg>.json`` plus a
rolling ``latest.md``. The runner never pushes anywhere else. It runs pytest with a scrubbed environment (no tokens),
at below-normal priority, so the machine stays usable over remote desktop.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

HOME = Path(__file__).resolve().parent
WORK = HOME / "work"
REPO = WORK / "KumoSQL"
RESULTS = WORK / "results-repo"
VENVS = WORK / "venvs"
LOGS = WORK / "logs"
STATE = WORK / "state.json"
CONFIG = HOME / "config.json"
IS_WINDOWS = os.name == "nt"
KEEP_ENV = {"PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE",
            "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "NUMBER_OF_PROCESSORS", "LANG", "LC_ALL"}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    line = f"{now()} {message}"
    print(line, flush=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    with (LOGS / "runner.log").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def load_config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"done": {}, "last_master_sha": {}, "last_run": {}}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def git(cwd: Path, *args: str, check: bool = True, timeout: int = 600) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="1")
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check, timeout=timeout, env=env)


def scrub(text: str) -> str:
    """Remove machine-specific paths from anything that goes to the (public) results branch."""

    for secret in {str(HOME), str(Path.home()), os.environ.get("USERNAME", ""), os.environ.get("USER", "")}:
        if secret and len(secret) > 2:
            text = text.replace(secret, "<runner>")
    return text


# --- repo and refs -----------------------------------------------------------------------------------------------------

def ensure_clone(config: dict) -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    if not (REPO / ".git").exists():
        log("cloning the repo (once)")
        subprocess.run(["git", "clone", "--no-tags", config["repo_url"], str(REPO)], check=True)


def remote_refs(config: dict) -> dict[str, str]:
    out = git(REPO, "ls-remote", "origin", "refs/heads/master", f"refs/heads/{config['candidate_ref_pattern']}").stdout
    refs = {}
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        refs[ref.removeprefix("refs/heads/")] = sha
    return refs


def candidates(config: dict, refs: dict[str, str]) -> list[tuple[int, str, str]]:
    found = []
    for ref, sha in refs.items():
        if ref != "master" and fnmatch.fnmatch(ref, config["candidate_ref_pattern"]):
            match = re.search(r"(\d+)$", ref)
            found.append((int(match.group(1)) if match else 0, ref, sha))
    return sorted(found, reverse=True)


def next_job(config: dict, refs: dict[str, str], state: dict) -> dict | None:
    for number, ref, sha in candidates(config, refs)[:3]:
        key = f"candidate:{sha}:{config['candidate_mode']}"
        if key not in state["done"]:
            return {"kind": "candidate", "ref": ref, "sha": sha, "leg": "candidate-" + config["candidate_mode"], "key": key,
                    "mode": config["candidate_mode"], "leg_config": config["legs"][0], "interruptible": False, "name": f"candidate-{number}"}
    master = refs.get("master")
    if config.get("watch_master") and master:
        legs = sorted(config["legs"], key=lambda leg: state["last_run"].get(leg["name"], ""))
        for leg in legs:
            if state["last_master_sha"].get(leg["name"]) != master:
                return {"kind": "master", "ref": "master", "sha": master, "leg": leg["name"], "key": f"master:{master}:{leg['name']}",
                        "mode": "full", "leg_config": leg, "interruptible": leg.get("interruptible", True), "name": "master"}
    return None


def checkout(job: dict) -> None:
    git(REPO, "fetch", "--no-tags", "origin", job["ref"] if job["kind"] == "candidate" else "master")
    git(REPO, "checkout", "--force", "--detach", job["sha"])
    git(REPO, "clean", "-fdq")


# --- environment per leg -----------------------------------------------------------------------------------------------

def venv_python(name: str) -> Path:
    return VENVS / name / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def ensure_venv(leg: dict) -> tuple[Path, list[str]]:
    """A venv per leg, rebuilt when pyproject.toml or the leg's sqlglot spec changes. Returns (python, notes)."""

    notes: list[str] = []
    python = venv_python(leg["name"])
    stamp_file = VENVS / leg["name"] / ".stamp"
    stamp = hashlib.sha256((REPO / "pyproject.toml").read_bytes() + json.dumps(leg, sort_keys=True).encode()).hexdigest()
    if python.exists() and stamp_file.exists() and stamp_file.read_text() == stamp:
        return python, notes
    log(f"building environment for leg {leg['name']}")
    shutil.rmtree(VENVS / leg["name"], ignore_errors=True)
    subprocess.run([sys.executable, "-m", "venv", str(VENVS / leg["name"])], check=True)
    pip = [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-q"]
    subprocess.run([*pip, "--upgrade", "pip"], check=False)
    subprocess.run([*pip, "-e", f"{REPO}[dev,smt]", f"sqlglot=={leg['sqlglot']}"], check=True, cwd=REPO)
    if leg.get("install_compiled"):
        if subprocess.run([*pip, f"sqlglotc=={leg['sqlglot']}"], check=False).returncode != 0:
            notes.append("sqlglotc wheel unavailable; ran with pure-Python sqlglot")
    stamp_file.write_text(stamp)
    return python, notes


def clean_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key.upper() in KEEP_ENV}
    env.update(KUMOSQL_TEST_HISTORY="off", PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    return env


# --- running pytest ----------------------------------------------------------------------------------------------------

def kill_tree(proc: subprocess.Popen) -> None:
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def run_pytest(config: dict, job: dict, python: Path, notes: list[str]) -> dict:
    cpus = os.cpu_count() or 1
    workers = config.get("jobs") or max(1, cpus - 1)
    mode_flag = {"evals": ["--evals"], "no-evals": ["--no-evals"], "full": []}[job["mode"]]
    junit = LOGS / "last.junit.xml"
    log_path = LOGS / f"{job['name']}-{job['leg']}.log"
    LOGS.mkdir(parents=True, exist_ok=True)
    junit.unlink(missing_ok=True)
    command = [str(python), "tools/run_tests.py", *mode_flag, *job["leg_config"].get("flags", []), "-j", str(workers),
               "--label", f"dell runner {job['name']} {job['leg']}", f"--junitxml={junit}", *config.get("pytest_args", [])]
    kwargs: dict = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
        kwargs["preexec_fn"] = lambda: os.nice(10)
    started = time.time()
    status = "finished"
    log(f"running {job['name']} {job['leg']} at {job['sha'][:10]}: {' '.join(command[1:])}")
    with log_path.open("w", encoding="utf-8", errors="replace") as handle:
        proc = subprocess.Popen(command, cwd=REPO, env=clean_env(), stdout=handle, stderr=subprocess.STDOUT, **kwargs)
        last_check = 0.0
        while proc.poll() is None:
            time.sleep(5)
            elapsed = time.time() - started
            if elapsed > config["job_timeout_minutes"] * 60:
                kill_tree(proc)
                status = "timeout"
                break
            if job["interruptible"] and time.time() - last_check > 30:
                last_check = time.time()
                try:
                    upcoming = next_job(config, remote_refs(config), load_state())
                    if upcoming and upcoming["kind"] == "candidate":
                        kill_tree(proc)
                        status = "preempted"
                        break
                except Exception as error:  # a network blip must not kill a running leg
                    log(f"preempt check failed: {error}")
        proc.wait()
    seconds = round(time.time() - started, 1)
    tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
    record = {"job": job["name"], "kind": job["kind"], "ref": job["ref"], "sha": job["sha"], "leg": job["leg"], "mode": job["mode"],
              "status": status, "exit": proc.returncode, "seconds": seconds, "workers": workers, "notes": notes, "finished": now(),
              "machine": {"system": platform.system(), "release": platform.release(), "cpus": cpus, "python": platform.python_version()},
              "passed": None, "failed": [], "errors": [], "skipped": None, "tests": None}
    if junit.exists():
        try:
            record.update(parse_junit(junit))
            known = tuple(config.get("known_failures", []))  # test-id prefixes that always fail on this machine (e.g. Windows-only)
            record["known_failed"] = [name for name in record["failed"] + record["errors"] if known and name.startswith(known)]
            record["failed"] = [name for name in record["failed"] if name not in record["known_failed"]]
            record["errors"] = [name for name in record["errors"] if name not in record["known_failed"]]
        except ET.ParseError:
            notes.append("junit file unreadable (run was cut short)")
    if status == "finished" and record["exit"] != 0 and not record["failed"] and not record["errors"] and not record.get("known_failed"):
        status = record["status"] = "crashed"
    if status != "finished" or record["failed"] or record["errors"] or (record["exit"] != 0 and not record.get("known_failed")):
        record["tail"] = scrub("\n".join(tail))
    return record


def parse_junit(path: Path) -> dict:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root)
    failed, errors, tests, skipped = [], [], 0, 0
    for suite in suites:
        tests += int(suite.get("tests", 0))
        skipped += int(suite.get("skipped", 0))
        for case in suite.iter("testcase"):
            name = f"{case.get('classname', '').replace('.', '/')}::{case.get('name')}"
            if case.find("failure") is not None:
                failed.append(name)
            elif case.find("error") is not None:
                errors.append(name)
    return {"tests": tests, "skipped": skipped, "passed": tests - skipped - len(failed) - len(errors),
            "failed": sorted(failed)[:200], "errors": sorted(errors)[:200]}


# --- results branch ----------------------------------------------------------------------------------------------------

def ensure_results_repo(config: dict) -> None:
    if not (RESULTS / ".git").exists():
        RESULTS.mkdir(parents=True, exist_ok=True)
        git(RESULTS, "init", "-q")
        git(RESULTS, "remote", "add", "origin", config["repo_url"])
        git(RESULTS, "config", "user.name", "KumoSQL Dell runner")
        git(RESULTS, "config", "user.email", "dell-runner@users.noreply.github.com")
    branch = config["results_branch"]
    fetched = git(RESULTS, "fetch", "-q", "origin", branch, check=False)
    if fetched.returncode == 0:
        git(RESULTS, "checkout", "-q", "-B", "results", "FETCH_HEAD")
    elif git(RESULTS, "rev-parse", "--verify", "-q", "results", check=False).returncode != 0:
        git(RESULTS, "checkout", "-q", "--orphan", "results")


def summary_line(record: dict) -> str:
    if record["status"] != "finished":
        verdict = record["status"]
    elif record["failed"] or record["errors"]:
        verdict = f"{len(record['failed'])} failed, {len(record['errors'])} errors"
    elif record.get("known_failed"):
        verdict = f"green except {len(record['known_failed'])} known"
    else:
        verdict = "green"
    return (f"| {record['finished']} | {record['job']} | {record['leg']} | `{record['sha'][:10]}` | {verdict} | "
            f"{record['passed']}/{record['tests']} | {round(record['seconds'] / 60)} min |")


def publish(config: dict, record: dict | None, push: bool, heartbeat: bool = False) -> bool:
    ensure_results_repo(config)
    stamp = now().replace(":", "")
    if record is not None:
        (RESULTS / "results").mkdir(exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", f"{stamp}-{record['job']}-{record['leg']}.json")
        (RESULTS / "results" / name).write_text(json.dumps(record, indent=2), encoding="utf-8")
    if heartbeat:
        (RESULTS / "heartbeat.json").write_text(json.dumps({"at": now(), "system": platform.system(), "cpus": os.cpu_count()}), encoding="utf-8")
    rows = []
    for path in sorted((RESULTS / "results").glob("*.json"), reverse=True)[:40]:
        rows.append(summary_line(json.loads(path.read_text(encoding="utf-8"))))
    (RESULTS / "latest.md").write_text(
        "# Dell runner results\n\nNewest first. Details per run in `results/`.\n\n"
        "| finished (UTC) | job | leg | sha | verdict | passed/tests | time |\n|---|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n",
        encoding="utf-8")
    git(RESULTS, "add", "-A")
    if git(RESULTS, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return True
    git(RESULTS, "commit", "-q", "-m", f"Dell runner: {record['job'] + ' ' + record['leg'] if record else 'heartbeat'}")
    if not push:
        return True
    for attempt in range(4):
        pushed = git(RESULTS, "push", "-q", "origin", f"HEAD:refs/heads/{config['results_branch']}", check=False)
        if pushed.returncode == 0:
            return True
        log(f"push failed ({pushed.stderr.strip()[:200]}); retrying")
        time.sleep(2 ** (attempt + 1))
        if git(RESULTS, "fetch", "-q", "origin", config["results_branch"], check=False).returncode == 0:
            git(RESULTS, "rebase", "-q", "FETCH_HEAD", check=False)
    return False


# --- commands ----------------------------------------------------------------------------------------------------------

def do_one(config: dict, push: bool, dry_run: bool) -> bool:
    ensure_clone(config)
    refs = remote_refs(config)
    state = load_state()
    job = next_job(config, refs, state)
    if job is None:
        return False
    if dry_run:
        log(f"would run {job['name']} / {job['leg']} at {job['sha'][:10]}")
        return True
    checkout(job)
    python, notes = ensure_venv(job["leg_config"])
    record = run_pytest(config, job, python, notes)
    log(f"{job['name']} {job['leg']}: {record['status']}, exit {record['exit']}, failed {len(record['failed'])}, errors {len(record['errors'])}")
    if record["status"] != "preempted":
        state["done"][job["key"]] = record["finished"]
        if job["kind"] == "master" and record["status"] in ("finished",):
            state["last_master_sha"][job["leg"]] = job["sha"]
            state["last_run"][job["leg"]] = record["finished"]
        elif job["kind"] == "master":
            state["last_run"][job["leg"]] = record["finished"]  # crashed or timed out: back of the line, retry later
        save_state(state)
        if not publish(config, record, push):
            log("could not push the result; it is committed locally in the results repo and goes out with the next push")
    return True


def cmd_init(config: dict) -> int:
    ensure_clone(config)
    ok = publish(config, None, push=True, heartbeat=True)
    log("heartbeat pushed: git sign-in works" if ok else "push failed: sign in to GitHub for git on this machine and run init again")
    return 0 if ok else 1


def cmd_status(config: dict) -> int:
    ensure_clone(config)
    refs = remote_refs(config)
    state = load_state()
    job = next_job(config, refs, state)
    print("master:", refs.get("master", "?")[:10])
    print("candidates:", [(ref, sha[:10]) for _, ref, sha in candidates(config, refs)[:3]] or "none")
    print("next job:", f"{job['name']} / {job['leg']}" if job else "nothing (everything is up to date)")
    for leg in config["legs"]:
        print(f"  leg {leg['name']}: last master sha {str(state['last_master_sha'].get(leg['name'], '-'))[:10]}, last run {state['last_run'].get(leg['name'], '-')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["init", "run", "once", "status"])
    parser.add_argument("--no-push", action="store_true", help="write results locally only")
    parser.add_argument("--dry-run", action="store_true", help="say which job is next without running it")
    args = parser.parse_args(argv)
    config = load_config()
    if args.command == "init":
        return cmd_init(config)
    if args.command == "status":
        return cmd_status(config)
    if args.command == "once":
        do_one(config, push=not args.no_push, dry_run=args.dry_run)
        return 0
    log("runner started")
    while True:
        try:
            busy = do_one(config, push=not args.no_push, dry_run=args.dry_run)
        except Exception as error:  # keep the loop alive across network and install problems
            log(f"error: {type(error).__name__}: {str(error)[:300]}")
            busy = False
            time.sleep(300)
        if args.dry_run:
            return 0
        if not busy:
            time.sleep(config["poll_seconds"])


if __name__ == "__main__":
    raise SystemExit(main())
