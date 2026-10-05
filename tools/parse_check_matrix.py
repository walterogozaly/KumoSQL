"""Compare pure and compiled SQLGlot 30.21.0 readings for focused diagnostics.

    python tools/parse_check_matrix.py                       # 30.21.0 pure and compiled (optional diagnostic)
    python tools/parse_check_matrix.py --versions 30.21.0 30.21.0+compiled --limit 200

Each release gets a temporary virtual environment (as in ``tools/test_sqlglot_matrix.py``); the compiled run
installs the matching ``sqlglotc`` build too. Every query of the dev splits under ``tests/fixtures`` (held-out
files and rows are skipped, see ``tools/parse_check_sweep.py``) is read under its dialect and reduced to

* what ``kumosql.parse_check`` says (agree, disagree, unchecked), and
* a digest of the operator grouping of sqlglot's tree (``parse_check._skeleton``).

A query whose verdict differs between releases is printed, and so is one every release accepts but whose trees group
operators differently. Exit status 1 when a verdict differs: a proof that depends on one release's reading would not
hold under another.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VERSIONS = ["30.21.0", "30.21.0+compiled"]
# fixture directory -> the dialects its queries are read under
CORPORA = {
    "googlesql": ["bigquery"], "spider2": ["bigquery"], "bq_syntax": ["bigquery"], "bq_edge": ["bigquery"],
    "calcite_mined": ["mysql"], "qed": ["mysql", "postgres"], "rbot": ["mysql"], "spes": ["mysql"],
    "cosette": ["mysql"], "sqlsolver": ["mysql"], "constraint_rewrites": ["bigquery"], "join_rewrites": ["bigquery"],
    "containment": ["bigquery"], "documented_rewrites": ["bigquery"], "optimizer_bugs": ["bigquery"],
}


def _python_in(folder: Path) -> Path:
    return folder / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def corpus(limit: int) -> list[tuple[str, str, str]]:
    """``(directory, dialect, sql)`` for up to ``limit`` distinct dev queries of each directory."""

    sys.path.insert(0, str(ROOT / "tools"))
    import glob
    import re

    import parse_check_sweep as sweep

    found = []
    for name, dialects in CORPORA.items():
        files = sorted(
            p for p in glob.glob(f"{sweep.ROOT}/{name}/**/*", recursive=True)
            if os.path.isfile(p) and re.search(r"\.(json|jsonl|json\.gz|jsonl\.gz|sql)$", p) and not re.search(r"held|test_split", p, re.I)
        )
        seen: set[str] = set()
        for path in files:
            for sql in sweep.load(path):
                if sql not in seen and len(seen) < limit:
                    seen.add(sql)
        for dialect in dialects:
            found += [(name, dialect, sql) for sql in sorted(seen)]
    return found


def worker(cases_file: str, out_file: str) -> int:
    """Run inside one environment: read every case and write ``{key: [status, round trip, grouping digest]}``."""

    sys.path.insert(0, str(ROOT / "src"))
    import sqlglot

    from kumosql import parse_check
    from kumosql.ast_utils import canonical_negation, quiet_parser

    result = {"sqlglot": sqlglot.__version__, "compiled": any(Path(sqlglot.__file__).parent.rglob("*.so")), "rows": {}}
    for directory, dialect, sql in json.loads(Path(cases_file).read_text(encoding="utf-8")):
        key = hashlib.sha1(f"{dialect}\0{sql}".encode()).hexdigest()
        verdict = parse_check.check_query(sql, dialect)
        status = verdict.status
        try:
            with quiet_parser():
                trees = [canonical_negation(t) for t in sqlglot.parse(sql, read=dialect) if t is not None]
            grouping = hashlib.sha1(repr([parse_check._skeleton(t) for t in trees]).encode()).hexdigest()[:12]
        except Exception as error:
            grouping = "error:" + type(error).__name__
        result["rows"][key] = [status, parse_check.round_trip(sql, dialect) is not None, grouping, (verdict.reasons or (verdict.note,))[0][:100]]
    Path(out_file).write_text(json.dumps(result), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--versions", nargs="+", default=DEFAULT_VERSIONS, choices=DEFAULT_VERSIONS, help="sqlglot releases; X.Y.Z+compiled adds sqlglotc")
    parser.add_argument("--limit", type=int, default=250, help="queries per fixture directory (default 250)")
    parser.add_argument("--show", type=int, default=10, help="differing queries to print")
    parser.add_argument("--worker", nargs=2, metavar=("CASES", "OUT"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        return worker(*args.worker)

    cases = corpus(args.limit)
    print(f"{len(cases)} (dialect, query) readings per release", flush=True)
    results: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="kumosql-parse-check-matrix-") as folder:
        root = Path(folder)
        (root / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
        for spec in args.versions:
            version, _, compiled = spec.partition("+")
            environment = root / f"env-{spec}"
            venv.EnvBuilder(with_pip=True).create(environment)
            python = str(_python_in(environment))
            packages = [f"sqlglot=={version}"] + ([f"sqlglotc=={version}"] if compiled else [])
            install = subprocess.run([python, "-m", "pip", "install", "-q", "--disable-pip-version-check", f"{ROOT}[smt]", *packages])
            if install.returncode != 0:
                print(f"{spec}: environment setup failed", file=sys.stderr)
                return 2
            out = root / f"out-{spec}.json"
            run = subprocess.run([python, __file__, "--worker", str(root / "cases.json"), str(out)], cwd=ROOT)
            if run.returncode != 0:
                print(f"{spec}: the run failed", file=sys.stderr)
                return 2
            results[spec] = json.loads(out.read_text(encoding="utf-8"))
            print(f"{spec}: sqlglot {results[spec]['sqlglot']}{' (compiled)' if results[spec]['compiled'] else ''}", flush=True)

    specs = list(results)
    texts = {hashlib.sha1(f"{d}\0{s}".encode()).hexdigest(): (name, d, s) for name, d, s in cases}
    status_differs, grouping_differs = [], []
    for key, (name, dialect, sql) in texts.items():
        readings = {spec: tuple(results[spec]["rows"][key]) for spec in specs}
        if len({row[0] for row in readings.values()}) > 1:
            status_differs.append((name, dialect, sql, readings))
        elif len({row[2] for row in readings.values()}) > 1 and all(row[0] == "agree" for row in readings.values()):
            grouping_differs.append((name, dialect, sql, readings))
    for spec in specs:
        counts: dict[str, int] = {}
        for row in results[spec]["rows"].values():
            counts[row[0]] = counts.get(row[0], 0) + 1
        print(f"  {spec}: {counts}, round trip refused {sum(1 for r in results[spec]['rows'].values() if r[1])}")
    print(f"{len(status_differs)} of {len(texts)} readings get a different verdict between releases")
    print(f"{len(grouping_differs)} more agree everywhere but group operators differently in the releases' own trees")
    for title, rows in (("verdict differs", status_differs), ("grouping differs", grouping_differs)):
        for name, dialect, sql, readings in rows[: args.show]:
            print(f"  [{title}: {name} / {dialect}] {' '.join(sql.split())[:200]}")
            for spec, row in readings.items():
                print(f"      {spec}: {row}")
    # a different verdict between releases is the finding: a proof that holds under one release would not under another
    return 1 if status_differs else 0


if __name__ == "__main__":
    raise SystemExit(main())
