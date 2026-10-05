"""Oracle Sales History (SH) for the sample-database eval: the run-time download and the CSV load.

SH is a star schema whose rows are six CSV files (91 MB, 918,843 sales rows) next to the SQL\\*Plus scripts. The
scripts are small and committed unchanged (``tests/fixtures/sample_databases/oracle_sh/upstream/``); the CSV files are
too large to commit, so they are downloaded once from the pinned commit of ``oracle-samples/db-sample-schemas`` into
a cache folder (``$KUMOSQL_BENCH_DATA/oracle-sh``, default ``~/.cache/kumosql-bench/oracle-sh``) and checked against
their pinned SHA-256 on every use. A file that is fetched but does not match its digest raises ``ValueError``; one that
cannot be fetched (no network, GitHub unreachable) raises ``OSError``, which the tests turn into a skip.

``tools/sample_db_bench.py`` holds the ``OracleSH`` adapter; this module has no dependency on it.

    python tools/sample_db_oracle_sh.py            # download and verify the six files
"""

from __future__ import annotations

import csv
import hashlib
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.request

REPO = "oracle-samples/db-sample-schemas"
COMMIT = "6660bad68c07bd143430ace58565b3f727e17263"
BASE = f"https://raw.githubusercontent.com/{REPO}/{COMMIT}/sales_history/"
CACHE = Path(os.environ.get("KUMOSQL_BENCH_DATA", Path.home() / ".cache" / "kumosql-bench")) / "oracle-sh"

#: file name -> SHA-256 of the pinned version
CSV_FILES = {
    "costs.csv": "85e8e912a04e1454a39b23d9a067564bdaa82727732d1cf0b5761045e73f3daa",
    "customers.csv": "664d9426ba42132fab4383140d2caa9be24960911448bec9f4db8ad2f6d676be",
    "promotions.csv": "db0b579e3a5b6a29f326af2d75913c3e8ec38a0cb4da3ac101180ee9be81474c",
    "sales.csv": "484699e0e239858d59dbef9d5ce3e91c93dab3010ea17f99c7e0221db5c36187",
    "supplementary_demographics.csv": "7a8d9f1a24e6187f42ca9264721c71621f55ddec4168d9a6132bf178bc2a151b",
    "times.csv": "69382c241c84780c78ca4a5b621ab6b35e70f1378a81a3f002cda040ebab0577",
}


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(name: str, digest: str | None = None, cache: Path | None = None, verify: bool = True) -> Path:
    """The pinned CSV file in the cache, downloaded on first use (written whole, then renamed, so parallel runs never
    read half a file). ``OSError`` when it cannot be fetched, ``ValueError`` when it is not the pinned file (unless
    ``verify`` is false: the caller compares the digest itself and reports a difference)."""

    digest = digest or CSV_FILES[name]
    path = (cache or CACHE) / COMMIT[:12] / name
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=name + ".")
        try:
            with os.fdopen(handle, "wb") as out, urllib.request.urlopen(BASE + name, timeout=60) as response:
                while block := response.read(1 << 20):
                    out.write(block)
            os.replace(temporary, path)
        except BaseException:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise
    if verify and _digest(path) != digest:
        raise ValueError(f"{path} is not the pinned version (SHA-256 {digest}); delete it to download again")
    return path


def csv_pins(upstream, repo: str, commit: str, licence: str) -> list:
    """The six files as ``Upstream`` pins (``upstream`` is the pin class of the harness)."""

    return [
        upstream(name, repo, commit, f"sales_history/{name}", digest, licence, remote=True)
        for name, digest in sorted(CSV_FILES.items())
    ]


def fetch_all() -> dict[str, Path]:
    return {name: download(name) for name in CSV_FILES}


def count_records(path: Path) -> int:
    """The data records of a CSV file (its header excluded), counted with Python's csv reader, not with DuckDB."""

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        next(reader)
        return sum(1 for _ in reader)


def header(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as handle:
        return next(csv.reader(handle))


_DECIMAL = re.compile(r"DECIMAL\((\d+), (\d+)\)")


def load_csv(con, table: str, columns: dict[str, str], path: Path) -> None:
    """Insert the CSV ``path`` into the existing DuckDB ``table``; ``columns`` maps each column to its DuckDB type.

    The scripts load these files with SQLcl's ``LOAD`` and Oracle reads an empty field as NULL, so an empty field
    is NULL here, and ``sales.csv`` pads its last field with blanks, which are trimmed from every number and date.
    Every number must be exactly representable in its declared type and every date must read ``YYYY-MM-DD``: the
    load raises ``ValueError`` otherwise (a cast must not round or reinterpret a value silently).
    """

    names = header(path)
    if [n.lower() for n in names] != list(columns):
        raise ValueError(f"{path.name}: columns {names} are not the table's {list(columns)}")
    source = f"read_csv('{path.as_posix()}', header=true, all_varchar=true, quote='\"', escape='\"')"
    select, checks = [], []
    for name, kind in zip(names, columns.values()):
        field = f'"{name}"'
        text = f"NULLIF(TRIM({field}), '')"
        if kind == "VARCHAR":
            select.append(f"NULLIF({field}, '')")
        elif kind == "DATE":
            select.append(f"CAST({text} AS DATE)")
            checks.append((name, f"{text} IS NOT NULL AND NOT regexp_matches({text}, '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$')"))
        elif kind == "BIGINT":
            select.append(f"CAST({text} AS BIGINT)")
            checks.append((name, f"{text} IS NOT NULL AND NOT regexp_matches({text}, '^-?[0-9]+$')"))
        elif match := _DECIMAL.fullmatch(kind):
            select.append(f"CAST({text} AS {kind})")
            scale = int(match.group(2))
            checks.append((name, f"{text} IS NOT NULL AND NOT regexp_matches({text}, '^-?[0-9]+(\\.[0-9]{{1,{scale}}})?$')"))
        else:
            raise ValueError(f"{table}.{name}: no CSV reader for {kind}")
    for name, bad in checks:
        count = con.execute(f"SELECT COUNT(*) FROM {source} WHERE {bad}").fetchone()[0]
        if count:
            raise ValueError(f"{path.name}.{name}: {count} values are not in the declared type's exact form")
    con.execute(f'INSERT INTO "{table}" SELECT {", ".join(select)} FROM {source}')


if __name__ == "__main__":
    try:
        for name, path in fetch_all().items():
            print(f"{name}: {path} ({path.stat().st_size:,} bytes, SHA-256 verified)")
    except OSError as error:
        print(f"cannot fetch the pinned files: {error}", file=sys.stderr)
        raise SystemExit(1)
