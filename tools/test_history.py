"""Record every test run, and rank tests by how often they break.

Every pytest run in this repository (``python tools/run_tests.py`` or ``python -m pytest``) appends one record to a
shared history directory when it can find one: ``$KUMOSQL_TEST_HISTORY``, else ``/mnt/project-files/test-history``
when ``/mnt/project-files`` exists (``KUMOSQL_TEST_HISTORY=off`` turns recording off). A record is one JSON line in its
own file under ``runs/``, so threads running at the same time never collide. It holds the commit, branch, a task
label, the tests the run was aiming at (the *targets*), the tests that failed and which of those were outside the
targets, per-file test counts and the slow tests' durations.

``tests/conftest.py`` installs the recorder, so nothing else needs to change in a test file.

Commands (``python tools/test_history.py <command>``):

* ``report``: rank tests by how often they broke a change that otherwise worked (failures outside the targets, in a
  run whose targets all passed), then by failures overall; list flaky candidates and slow tests.
* ``order``: write ``tests/order.json`` (slow tests with their durations, tests that fail often) from the history.
  ``tests/conftest.py`` uses it to run likely failures first, then the fast tests, then the slow tests longest-first.
* ``import-junit``: add an existing JUnit file to the history (used to seed it).
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import statistics
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHARED_DIR = Path("/mnt/project-files/test-history")
ORDER_FILE = ROOT / "tests" / "order.json"
SCHEMA = 1
SLOW_SECONDS = 3.0  # a test at least this slow is recorded with its duration and runs in the slow tier
MASS_FAILURE = 20  # a run with this many failures is an environment problem, not a test that broke
MESSAGE_CHARS = 200


# ---------------------------------------------------------------------------------------------------------------
# where the history lives


def history_dir() -> Path | None:
    """The directory records are written to, or None when recording is off or there is no shared folder."""

    setting = os.environ.get("KUMOSQL_TEST_HISTORY")
    if setting is not None:
        return None if setting.strip().lower() in ("", "0", "off", "false", "no") else Path(setting)
    return SHARED_DIR if SHARED_DIR.parent.is_dir() else None


def load_records(directory: Path | None = None, since_days: float | None = None) -> list[dict]:
    directory = directory or history_dir()
    if directory is None or not directory.is_dir():
        return []
    cutoff = None if since_days is None else (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)).isoformat()
    records = []
    for path in sorted(directory.rglob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and (cutoff is None or record.get("ts", "") >= cutoff):
                records.append(record)
    records.sort(key=lambda r: r.get("ts", ""))
    return records


def write_record(record: dict, directory: Path) -> Path:
    runs = directory / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    stamp = record["ts"].replace(":", "").replace("-", "")[:15]
    name = f"{stamp}-{(record.get('commit') or 'nocommit')[:8]}-{uuid.uuid4().hex[:6]}.jsonl"
    temporary = runs / (name + ".tmp")
    temporary.write_text(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
    final = runs / name
    os.replace(temporary, final)
    return final


# ---------------------------------------------------------------------------------------------------------------
# what a run was aiming at


def _git(*args: str) -> str:
    try:
        done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def git_state() -> dict:
    head = _git("rev-parse", "HEAD")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    base = _git("merge-base", "HEAD", "origin/master") or _git("merge-base", "HEAD", "master")
    return {
        "commit": head,
        "branch": branch if branch and branch != "HEAD" else os.environ.get("GITHUB_REF_NAME", "") or "(detached)",
        "base": base,
        "dirty": bool(_git("status", "--porcelain", "-uno")),
    }


def changed_test_files(base: str) -> list[str]:
    """Test files this checkout adds or changes against the master it branched from."""

    if not base:
        return []
    names = set(_git("diff", "--name-only", base).splitlines()) | set(_git("ls-files", "-o", "--exclude-standard", "tests").splitlines())
    return sorted(n for n in names if n.startswith("tests/") and n.endswith(".py") and Path(n).name.startswith("test_"))


def is_target(nodeid: str, targets: list[str]) -> bool:
    """A target is a test file, a directory or a full test id; a test id matches itself and its parametrisations."""

    for target in targets:
        target = target[2:] if target.startswith("./") else target
        if nodeid == target or nodeid.startswith(target + "::") or nodeid.startswith(target + "[") or (target.endswith("/") and nodeid.startswith(target)):
            return True
    return False


def resolve_targets(explicit_args: list[str], partial: bool, base: str) -> tuple[list[str], str]:
    """The tests this run is aiming at, and where that came from: the environment, the command line, or the test
    files the branch changed."""

    env = [t.strip() for t in os.environ.get("KUMOSQL_TEST_TARGETS", "").split(",") if t.strip()]
    if env:
        return env, "declared"
    if partial and explicit_args:
        return explicit_args, "args"
    changed = changed_test_files(base)
    return (changed, "changed-tests") if changed else ([], "none")


# ---------------------------------------------------------------------------------------------------------------
# the pytest plugin


def _versions() -> dict:
    from importlib import metadata

    found = {"python": ".".join(map(str, sys.version_info[:3]))}
    for name in ("sqlglot", "sqlglotc", "sqlfluff", "z3-solver", "duckdb", "pytest", "pytest-xdist"):
        try:
            found[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    return found


def _first_line(text: object) -> str:
    for line in str(text).splitlines():
        if line.strip():
            return line.strip()[:MESSAGE_CHARS]
    return ""


class Recorder:
    """Collects outcomes on the controller process (pytest-xdist forwards every worker's reports to it)."""

    def __init__(self, config, directory: Path):
        self.config = config
        self.directory = directory
        self.started = time.time()
        self.counts: collections.Counter = collections.Counter()
        self.failed: dict[str, dict] = {}
        self.slow: dict[str, float] = {}
        self.files: collections.Counter = collections.Counter()
        self.written: Path | None = None

    # -- outcomes
    def pytest_runtest_logreport(self, report) -> None:
        outcome = None
        wasxfail = hasattr(report, "wasxfail")
        if report.when == "call":
            if report.passed:
                outcome = "xpassed" if wasxfail else "passed"
            elif report.failed:
                outcome = "failed"
            else:
                outcome = "xfailed" if wasxfail else "skipped"
        elif report.when in ("setup", "teardown") and report.failed:
            outcome = "error"
        elif report.when == "setup" and report.skipped:
            outcome = "xfailed" if wasxfail else "skipped"
        if outcome is None:
            return
        self.counts[outcome] += 1
        nodeid = report.nodeid
        if report.when != "teardown" or outcome != "error":
            self.files[nodeid.split("::", 1)[0]] += 1
        if outcome in ("failed", "error"):
            crash = getattr(getattr(report, "longrepr", None), "reprcrash", None)
            message = _first_line(getattr(crash, "message", "") or report.longrepr)
            self.failed.setdefault(nodeid, {"id": nodeid, "kind": outcome, "seconds": round(report.duration, 2), "msg": message})
        if report.when == "call" and report.duration >= SLOW_SECONDS:
            self.slow[nodeid] = round(report.duration, 1)

    # -- the record
    def mode(self) -> tuple[str, list[str]]:
        """full, evals, no-evals or partial, and the positional arguments that made a run partial."""

        option = self.config.option
        positional = [a for a in getattr(self.config, "args", []) if a]
        defaults = [str(p) for p in self.config.getini("testpaths")]
        partial = bool(getattr(option, "keyword", "")) or bool(getattr(option, "deselect", None)) or bool(getattr(option, "quick", False))
        if positional and positional != defaults:
            return "partial", positional
        if partial:
            return "partial", []
        expression = getattr(option, "markexpr", "") or ""
        if "not eval" in expression:
            return "no-evals", []
        if "eval" in expression:
            return "evals", []
        return "full", []

    def build(self, exitstatus: int) -> dict:
        state = git_state()
        mode, explicit = self.mode()
        targets, source = resolve_targets(explicit, mode == "partial", state["base"])
        failed = []
        for entry in self.failed.values():
            failed.append({**entry, "target": is_target(entry["id"], targets)})
        failed.sort(key=lambda e: e["id"])
        label = os.environ.get("KUMOSQL_TASK", "").strip() or state["branch"]
        workers = getattr(self.config.option, "numprocesses", None)
        return {
            "v": SCHEMA,
            "ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **state,
            "label": label,
            "targets": targets,
            "targets_source": source,
            "mode": mode,
            "workers": workers if isinstance(workers, int) else 1,
            "versions": _versions(),
            "seconds": round(time.time() - self.started, 1),
            "exit": int(exitstatus),
            "counts": dict(self.counts),
            "failed": failed,
            "slow": dict(sorted(self.slow.items())),
            "files": dict(sorted(self.files.items())),
        }

    def pytest_terminal_summary(self, terminalreporter, exitstatus) -> None:
        if not sum(self.counts.values()) or getattr(self.config.option, "collectonly", False):
            return
        try:
            record = self.build(exitstatus)
            before = load_records(self.directory) if record["failed"] else []  # earlier runs only, not this one
            self.written = write_record(record, self.directory)
        except Exception as error:  # recording never fails a test run
            terminalreporter.write_line(f"test history: not recorded ({type(error).__name__}: {error})")
            return
        outside = [f["id"] for f in record["failed"] if not f["target"]] if record["targets"] else []
        if record["failed"] and record["targets"]:
            earlier = collateral_counts(before) if outside else {}
            terminalreporter.section("test history")
            hit = [f for f in record["failed"] if f["target"]]
            terminalreporter.write_line(f"targets ({record['targets_source']}): {', '.join(record['targets'][:6])}{' ...' if len(record['targets']) > 6 else ''}")
            terminalreporter.write_line(f"{len(hit)} failed inside the targets, {len(outside)} outside them")
            for nodeid in outside[:12]:
                times = earlier.get(nodeid, {}).get("runs", 0)
                terminalreporter.write_line(f"  outside the targets: {nodeid}" + (f" (broke {times} earlier change{'s' if times != 1 else ''} that otherwise worked)" if times else ""))
        terminalreporter.write_line(f"test history: recorded to {self.written}")


def install(config) -> None:
    """Called from tests/conftest.py; only the controller records, and only when a history directory exists."""

    if hasattr(config, "workerinput"):
        return
    directory = history_dir()
    if directory is None:
        return
    config.pluginmanager.register(Recorder(config, directory), "kumosql-test-history")


# ---------------------------------------------------------------------------------------------------------------
# analysis


def working_change_run(record: dict) -> bool:
    """A run aimed at known tests where all of them passed, on a branch other than master."""

    if record.get("targets_source") in (None, "none") or not record.get("targets"):
        return False
    if record.get("branch") in ("master", "main"):
        return False
    return not any(f.get("target") for f in record.get("failed", []))


def mass_failure(record: dict) -> bool:
    return len(record.get("failed", [])) >= MASS_FAILURE


def collateral_counts(records: list[dict]) -> dict[str, dict]:
    """Per test: runs where it failed outside the targets while every target passed."""

    found: dict[str, dict] = {}
    for record in records:
        if not working_change_run(record) or mass_failure(record):
            continue
        for failure in record["failed"]:
            entry = found.setdefault(failure["id"], {"runs": 0, "labels": set(), "branches": set(), "last": "", "msg": ""})
            entry["runs"] += 1
            entry["labels"].add(record.get("label") or record.get("branch") or "?")
            entry["branches"].add(record.get("branch", "?"))
            entry["last"] = max(entry["last"], record.get("ts", ""))
            entry["msg"] = failure.get("msg", "") or entry["msg"]
    return found


def failure_counts(records: list[dict]) -> dict[str, dict]:
    """Per test: failures overall, split by where they happened, and how many comparable runs ran it."""

    found: dict[str, dict] = {}
    comparable = [r for r in records if not mass_failure(r)]
    ran_by_file: collections.Counter = collections.Counter()
    for record in comparable:
        for file in record.get("files", {}):
            ran_by_file[file] += 1
    for record in comparable:
        for failure in record.get("failed", []):
            entry = found.setdefault(failure["id"], {"failed": 0, "targeted": 0, "outside": 0, "master": 0, "unattributed": 0, "last": "", "msg": ""})
            entry["failed"] += 1
            entry["last"] = max(entry["last"], record.get("ts", ""))
            entry["msg"] = failure.get("msg", "") or entry["msg"]
            if record.get("branch") in ("master", "main"):
                entry["master"] += 1
            elif record.get("targets_source") in (None, "none") or not record.get("targets"):
                entry["unattributed"] += 1
            elif failure.get("target"):
                entry["targeted"] += 1
            else:
                entry["outside"] += 1
    for nodeid, entry in found.items():
        entry["runs"] = ran_by_file.get(nodeid.split("::", 1)[0], 0)
    return found


def flaky_candidates(records: list[dict]) -> list[tuple[str, int, int, str]]:
    """Tests that failed in one clean run and passed in another at the same commit."""

    by_commit: dict[str, list[dict]] = collections.defaultdict(list)
    for record in records:
        if record.get("commit") and not record.get("dirty") and not mass_failure(record):
            by_commit[record["commit"]].append(record)
    found: dict[str, list] = {}
    for commit, runs in by_commit.items():
        if len(runs) < 2:
            continue
        failed_ids = {f["id"] for r in runs for f in r.get("failed", [])}
        for nodeid in failed_ids:
            file = nodeid.split("::", 1)[0]
            failing = sum(1 for r in runs if any(f["id"] == nodeid for f in r["failed"]))
            passing = sum(1 for r in runs if file in r.get("files", {}) and not any(f["id"] == nodeid for f in r["failed"]))
            if failing and passing:
                entry = found.setdefault(nodeid, [0, 0, ""])
                entry[0] += failing
                entry[1] += passing
                entry[2] = commit[:8]
    return sorted(((n, e[0], e[1], e[2]) for n, e in found.items()), key=lambda x: -x[1])


def slow_tests(records: list[dict]) -> dict[str, float]:
    seen: dict[str, list[float]] = collections.defaultdict(list)
    for record in records:
        if mass_failure(record):
            continue
        for nodeid, seconds in record.get("slow", {}).items():
            seen[nodeid].append(seconds)
    return {nodeid: round(statistics.median(values), 1) for nodeid, values in seen.items()}


def build_order(records: list[dict], risky_limit: int = 40) -> dict:
    counts = failure_counts(records)
    collateral = collateral_counts(records)
    risky = sorted(
        (n for n, e in counts.items() if e["failed"] >= 1),
        key=lambda n: (-collateral.get(n, {}).get("runs", 0), -counts[n]["failed"], n),
    )[:risky_limit]
    return {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "runs": len(records),
        "slow_seconds": SLOW_SECONDS,
        "slow": dict(sorted(slow_tests(records).items(), key=lambda kv: -kv[1])),
        "risky": risky,
    }


# ---------------------------------------------------------------------------------------------------------------
# commands


def _table(rows: list[list[str]], header: list[str]) -> str:
    rows = [header] + rows
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(header))]
    lines = ["  ".join(str(c).ljust(widths[i]) if i == 0 or not str(c).replace(".", "").isdigit() else str(c).rjust(widths[i]) for i, c in enumerate(r)).rstrip() for r in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def command_report(args) -> int:
    records = load_records(Path(args.dir) if args.dir else None, args.since)
    if not records:
        print("No test history yet. Run tests with a shared folder at /mnt/project-files or set KUMOSQL_TEST_HISTORY.")
        return 0
    working = [r for r in records if working_change_run(r) and not mass_failure(r)]
    mass = [r for r in records if mass_failure(r)]
    print(f"{len(records)} runs from {records[0]['ts'][:10]} to {records[-1]['ts'][:10]}; {len(working)} aimed at known tests that all passed; {len(mass)} environment-sized failures ({MASS_FAILURE}+ at once) left out of the rankings.\n")
    collateral = collateral_counts(records)
    print("Tests that break changes that otherwise work (failed outside the targets while every target passed)")
    rows = [[n, e["runs"], len(e["labels"]), e["last"][:10], e["msg"][:60]] for n, e in sorted(collateral.items(), key=lambda kv: (-kv[1]["runs"], -len(kv[1]["labels"]), kv[0]))[: args.top]]
    print(_table(rows, ["test", "runs", "tasks", "last", "message"]) if rows else "  none recorded yet")
    counts = failure_counts(records)
    print("\nTests that fail most, by where")
    rows = [
        [n, e["failed"], f"{e['failed']}/{e['runs']}" if e["runs"] else "-", e["targeted"], e["outside"], e["master"], e["unattributed"]]
        for n, e in sorted(counts.items(), key=lambda kv: (-kv[1]["failed"], kv[0]))[: args.top]
    ]
    print(_table(rows, ["test", "failed", "of runs", "targeted", "outside targets", "on master", "unknown"]) if rows else "  none")
    flaky = flaky_candidates(records)
    print("\nFlaky candidates (failed and passed at the same commit)")
    print(_table([[n, f, p, c] for n, f, p, c in flaky[: args.top]], ["test", "failed", "passed", "commit"]) if flaky else "  none")
    slow = sorted(slow_tests(records).items(), key=lambda kv: -kv[1])[: args.top]
    print(f"\nSlowest tests (median seconds, runs with at least {SLOW_SECONDS:g} s)")
    print(_table([[n, s] for n, s in slow], ["test", "seconds"]) if slow else "  none")
    return 0


def command_order(args) -> int:
    records = load_records(Path(args.dir) if args.dir else None, args.since)
    if not records:
        print("No test history to build an order from.", file=sys.stderr)
        return 1
    order = build_order(records)
    target = Path(args.output)
    if args.write:
        target.write_text(json.dumps(order, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {target}: {len(order['slow'])} slow tests, {len(order['risky'])} often-failing tests, from {order['runs']} runs")
    else:
        print(json.dumps(order, indent=1))
    return 0


def _node_id(classname: str, name: str, root: Path) -> str:
    parts = classname.split(".")
    for k in range(len(parts), 0, -1):
        candidate = "/".join(parts[:k]) + ".py"
        if (root / candidate).is_file():
            return "::".join([candidate, *parts[k:], name])
    return "::".join([classname.replace(".", "/") + ".py", name])


def record_from_junit(path: Path, *, commit: str, branch: str, label: str, base: str = "", root: Path = ROOT) -> dict:
    tree = ET.parse(path).getroot()
    counts: collections.Counter = collections.Counter()
    failed, slow, files = [], {}, collections.Counter()
    total = 0.0
    for case in tree.iter("testcase"):
        nodeid = _node_id(case.get("classname", ""), case.get("name", ""), root)
        seconds = float(case.get("time", 0))
        total += seconds
        files[nodeid.split("::", 1)[0]] += 1
        problem = case.find("failure")
        problem = problem if problem is not None else case.find("error")
        if problem is not None:
            counts["error" if problem.tag == "error" else "failed"] += 1
            failed.append({"id": nodeid, "kind": problem.tag if problem.tag == "error" else "failed", "seconds": round(seconds, 2), "msg": _first_line(problem.get("message", "")), "target": False})
        elif case.find("skipped") is not None:
            counts["xfailed" if "xfail" in (case.find("skipped").get("type", "") + case.find("skipped").get("message", "")) else "skipped"] += 1
        else:
            counts["passed"] += 1
        if seconds >= SLOW_SECONDS:
            slow[nodeid] = round(seconds, 1)
    stamp = tree.find("testsuite").get("timestamp") if tree.tag == "testsuites" and tree.find("testsuite") is not None else tree.get("timestamp")
    ts = (stamp or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")).split(".")[0].rstrip("Z") + "Z"
    return {
        "v": SCHEMA, "ts": ts, "commit": commit, "branch": branch, "base": base, "dirty": False, "label": label,
        "targets": [], "targets_source": "none", "mode": "full", "workers": 0, "versions": _versions(),
        "seconds": round(total, 1), "exit": 1 if failed else 0, "counts": dict(counts),
        "failed": sorted(failed, key=lambda e: e["id"]), "slow": dict(sorted(slow.items())), "files": dict(sorted(files.items())),
    }


def command_import(args) -> int:
    directory = Path(args.dir) if args.dir else history_dir()
    if directory is None:
        print("No history directory (set KUMOSQL_TEST_HISTORY or pass --dir).", file=sys.stderr)
        return 1
    record = record_from_junit(Path(args.junit), commit=args.commit, branch=args.branch, label=args.label)
    print(f"wrote {write_record(record, directory)} ({record['counts']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="rank tests by how often they break")
    order = sub.add_parser("order", help="write tests/order.json from the history")
    imp = sub.add_parser("import-junit", help="add a JUnit file to the history")
    for p in (report, order, imp):
        p.add_argument("--dir", help="history directory (default: the shared one)")
    for p in (report, order):
        p.add_argument("--since", type=float, help="only runs from the last N days")
    report.add_argument("--top", type=int, default=15, help="rows per table")
    order.add_argument("--write", action="store_true", help="write the file instead of printing it")
    order.add_argument("--output", default=str(ORDER_FILE))
    imp.add_argument("junit")
    imp.add_argument("--commit", default="")
    imp.add_argument("--branch", default="master")
    imp.add_argument("--label", default="imported")
    args = parser.parse_args(argv)
    return {"report": command_report, "order": command_order, "import-junit": command_import}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
