#!/usr/bin/env python3
"""Build and exercise a messy BigQuery model layer for testing KumoSQL.

Creates a dataset of sampled raw tables, views and a few small tables over
``bigquery-public-data.thelook_ecommerce``, then runs a repeatable query
workload so the project accumulates job history. Uses the ``bq`` CLI and your
gcloud login. See docs/bigquery-testbed.md for access, cost and guard setup.

    python examples/bq_testbed/build_testbed.py --project kumosql --print-only
    python examples/bq_testbed/build_testbed.py --project kumosql --estimate-only
    python examples/bq_testbed/build_testbed.py --project kumosql

Every job gets ``maximum_bytes_billed`` and a ``kumosql_testbed`` label. A
dry-run estimate runs before each billable job, and the run stops if the
running total would pass ``--max-total-gb``. It is idempotent: views and tables
are replaced, raw copies are reused unless ``--refresh-raw`` is given, and the
workload can be repeated with ``--workload-only``. Only DDL and CTAS are used
(no DML), and everything expires after 60 days, so the free sandbox works.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import models  # noqa: E402
import workload  # noqa: E402

LOCATION = "US"  # must match the public dataset
EXPIRY_DAYS = 60  # the BigQuery sandbox maximum
LABEL = "kumosql_testbed"
_ESTIMATE = re.compile(r"process (\d+) bytes")


class GuardError(RuntimeError):
    pass


class Runner:
    def __init__(self, project: str, max_job_bytes: int, max_total_bytes: int, bq: str = "bq"):
        self.project = project
        self.max_job_bytes = max_job_bytes
        self.max_total_bytes = max_total_bytes
        self.bq = bq
        self.estimated_total = 0
        self.jobs_run = 0

    def _cmd(self, *extra: str) -> list[str]:
        return [self.bq, f"--project_id={self.project}", f"--location={LOCATION}", "query",
                "--nouse_legacy_sql", "--quiet", *extra]

    def _run(self, cmd: list[str], sql: str) -> str:
        done = subprocess.run(cmd, input=sql, capture_output=True, text=True)
        if done.returncode != 0:
            raise RuntimeError(f"bq failed: {(done.stderr or done.stdout).strip()}\n--- SQL ---\n{sql}")
        return done.stdout

    def estimate(self, sql: str) -> int:
        out = self._run(self._cmd("--dry_run"), sql)
        match = _ESTIMATE.search(out)
        return int(match.group(1)) if match else 0

    def execute(self, sql: str, role: str, billable: bool = True) -> None:
        if billable:
            size = self.estimate(sql)
            if size > self.max_job_bytes:
                raise GuardError(f"one job would process {size:,} bytes (cap {self.max_job_bytes:,})")
            if self.estimated_total + size > self.max_total_bytes:
                raise GuardError(f"run would pass the total cap of {self.max_total_bytes:,} bytes")
            self.estimated_total += size
        self._run(self._cmd(f"--maximum_bytes_billed={self.max_job_bytes}", "--nouse_cache",
                            f"--label={LABEL}:1", f"--label=role:{role}", "--format=none"), sql)
        self.jobs_run += 1

    def exists(self, dataset: str, table: str) -> bool:
        done = subprocess.run([self.bq, f"--project_id={self.project}", "show", f"{dataset}.{table}"],
                              capture_output=True, text=True)
        return done.returncode == 0


def render_step(step: models.Step, project: str, dataset: str, pct: int) -> str:
    body = step.sql.format(p=project, d=dataset, src=models.SOURCE, pct=pct)
    target = f"`{project}.{dataset}.{step.name}`"
    if step.kind == "view":
        return f"CREATE OR REPLACE VIEW {target} AS\n{body}"
    expiry = f"TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL {EXPIRY_DAYS} DAY)"
    return f"CREATE OR REPLACE TABLE {target}\nOPTIONS (expiration_timestamp = {expiry}) AS\n{body}"


def render_dataset(project: str, dataset: str) -> str:
    return (f"CREATE SCHEMA IF NOT EXISTS `{project}.{dataset}`\n"
            f"OPTIONS (location = '{LOCATION}', default_table_expiration_days = {EXPIRY_DAYS})")


def plan(project: str, dataset: str, pct: int, rounds: int) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    build = [("ddl", render_dataset(project, dataset))]
    build += [(s.kind, render_step(s, project, dataset, pct)) for s in models.STEPS]
    return build, workload.build(f"{project}.{dataset}.", rounds)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True, help="GCP project to build in")
    ap.add_argument("--dataset", default="kumosql_messy")
    ap.add_argument("--sample-pct", type=int, default=10, help="percent of users to copy (default 10)")
    ap.add_argument("--rounds", type=int, default=3, help="workload repetitions (default 3)")
    ap.add_argument("--max-job-mb", type=int, default=500, help="maximum_bytes_billed per job (default 500 MB)")
    ap.add_argument("--max-total-gb", type=float, default=5.0, help="stop if estimated bytes pass this (default 5)")
    ap.add_argument("--refresh-raw", action="store_true", help="re-copy raw tables from the public dataset")
    ap.add_argument("--workload-only", action="store_true", help="skip the build, just run the workload")
    ap.add_argument("--skip-workload", action="store_true")
    ap.add_argument("--print-only", action="store_true", help="print all SQL; no gcloud/bq needed")
    ap.add_argument("--estimate-only", action="store_true", help="dry-run estimates only; creates nothing")
    args = ap.parse_args(argv)

    build, jobs = plan(args.project, args.dataset, args.sample_pct, args.rounds)
    if args.workload_only:
        build = []
    if args.skip_workload:
        jobs = []

    if args.print_only:
        for kind, sql in build:
            print(f"-- [{kind}]\n{sql};\n")
        for role, sql in jobs:
            print(f"-- [workload:{role}]\n{sql};\n")
        return 0

    runner = Runner(args.project, args.max_job_mb * 1024**2, int(args.max_total_gb * 1024**3))
    try:
        if args.estimate_only:
            for kind, sql in build:
                if kind == "raw":
                    try:
                        runner.estimated_total += runner.estimate(sql)
                    except RuntimeError:
                        print("skipped an estimate that needs a table from an earlier step")
            print(f"raw copy would process about {runner.estimated_total / 1024**2:,.1f} MB; "
                  "workload estimates need the models to exist (run a build first)")
            return 0
        names = [None] + [s.name for s in models.STEPS]
        for (kind, sql), name in zip(build, names):
            if kind == "raw" and not args.refresh_raw and runner.exists(args.dataset, name):
                print(f"reuse  {name}")
                continue
            runner.execute(sql, "build", billable=kind in ("raw", "table"))
            print(f"built  {name or args.dataset}")
        for i, (role, sql) in enumerate(jobs, 1):
            runner.execute(sql, role)
            print(f"query {i}/{len(jobs)} ({role})")
    except (GuardError, RuntimeError) as err:
        print(f"stopped: {err}", file=sys.stderr)
        return 1
    print(f"done: {runner.jobs_run} jobs, estimated {runner.estimated_total / 1024**2:,.1f} MB processed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
