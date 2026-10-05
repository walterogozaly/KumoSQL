"""Spider's data for the Spider evals: pinned downloads and the schemas of ``tables.json``.

Spider 1.0 (https://github.com/taoyds/spider, Apache-2.0 repository, CC BY-SA 4.0 data; Yu et al., EMNLP 2018)
and TestSuiteEval (https://github.com/ruiqi-zhong/TestSuiteEval, no licence; Zhong, Yu and Klein, EMNLP 2020)
are downloaded at run time from pinned commits, checked against SHA-256 digests and kept in
``$KUMOSQL_BENCH_DATA/spider`` (default ``~/.cache/kumosql-bench/spider``). Nothing is copied into this
repository. Spider's SQLite databases are on Google Drive and the Yale site, both blocked here, so every
database an eval uses is one KumoSQL builds from the declared tables, types, keys and foreign keys of
``tables.json``.

Keys. ``tables.json`` lists only the first column of a composite primary key (the real ``singer_in_concert``
key is ``(concert_ID, Singer_ID)``; the file lists ``concert_ID``). A listed key is part of the true key, so a
database where it is unique is a valid Spider database: the database builder uses every listed key. The
prover is given no key at all, because a listed key may not be unique in Spider's own data.

    python tools/spider_data.py --check-sources      # download and verify every pinned file
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SPIDER_COMMIT = "b7b5b8c890cd30e35427348bb9eb8c6d1350ca7c"  # taoyds/spider, master
TESTSUITE_COMMIT = "7bf637883cf092867d33f58ee6f365f5a05e0ee2"  # ruiqi-zhong/TestSuiteEval, master
SPIDER_BASE = f"https://raw.githubusercontent.com/taoyds/spider/{SPIDER_COMMIT}/"
TESTSUITE_BASE = f"https://raw.githubusercontent.com/ruiqi-zhong/TestSuiteEval/{TESTSUITE_COMMIT}/"
FILES = {
    "tables.json": (SPIDER_BASE + "evaluation_examples/examples/tables.json", "61bb20aa401f03164e2d7f3b16509b7b5f79cc9c943ca7bd159046df1159e2ed"),
    "ESMFalseNegatives.tsv": (TESTSUITE_BASE + "ESMFalseNegatives.tsv", "6367141ab7d4c7290dba3ba45361c558e5ef986d2f78af647378974ffa4c7f80"),
}
CACHE = Path(os.environ.get("KUMOSQL_BENCH_DATA", Path.home() / ".cache" / "kumosql-bench")) / "spider"
# Spider's column types; "number" columns are filled with integers, which every number column accepts
DECLARED = {"number": "INTEGER", "text": "TEXT", "time": "TEXT", "boolean": "INTEGER", "others": "TEXT"}


def fetch(name: str, cache: Path | None = None) -> Path:
    """One pinned source file, downloaded once (written whole, then renamed, so parallel runs never read half a file)."""

    url, digest = FILES[name]
    folder = (cache or CACHE) / SPIDER_COMMIT[:12]
    path = folder / name
    if not path.exists():
        folder.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
        handle, temporary = tempfile.mkstemp(dir=folder, prefix=name + ".")
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(temporary, path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise OSError(f"{path} does not match the pinned version; delete it to download again")
    return path


@dataclass
class Schema:
    database: str
    tables: dict[str, dict[str, str]]  # lower-case table -> lower-case column -> declared SQLite type
    keys: dict[str, tuple[str, ...]]  # the listed primary key (part of the true key): used to build databases only
    foreign: tuple  # ((child table, child column, parent table, parent column), ...)

    @property
    def unique(self) -> dict[str, tuple[str, ...]]:
        """Columns a foreign key points at, other than the listed key: SQL requires a referenced column to be unique."""

        out: dict[str, tuple[str, ...]] = {}
        for _, _, parent, column in self.foreign:
            if (column,) != self.keys.get(parent) and column not in out.get(parent, ()):
                out[parent] = out.get(parent, ()) + (column,)
        return out


def parse_schemas(databases: list[dict]) -> dict[str, Schema]:
    schemas = {}
    for db in databases:
        names = [t.lower() for t in db["table_names_original"]]
        # sqlite_sequence is SQLite's own bookkeeping table (world_1 lists it): never queried, and not creatable
        tables: dict[str, dict[str, str]] = {t: {} for t in names if not t.startswith("sqlite_")}
        columns: list = []  # column index -> (table, column) or None
        for (table_index, column), kind in zip(db["column_names_original"], db["column_types"]):
            if table_index < 0 or names[table_index] not in tables:
                columns.append(None)
                continue
            table = names[table_index]
            tables[table][column.lower()] = DECLARED.get(kind, "TEXT")
            columns.append((table, column.lower()))
        keys: dict[str, tuple[str, ...]] = {}
        for index in db["primary_keys"]:
            for member in (index if isinstance(index, list) else [index]):
                if columns[member]:
                    keys[columns[member][0]] = keys.get(columns[member][0], ()) + (columns[member][1],)
        foreign = tuple(
            (columns[c][0], columns[c][1], columns[p][0], columns[p][1])
            for c, p in db["foreign_keys"]
            if columns[c] and columns[p] and columns[c] != columns[p]
        )
        schemas[db["db_id"]] = Schema(db["db_id"], tables, keys, foreign)
    return schemas


def load_schemas(path: Path | None = None) -> dict[str, Schema]:
    return parse_schemas(json.loads((path or fetch("tables.json")).read_text(encoding="utf-8")))


def check_sources() -> list[str]:
    """Download every pinned file and return the problems found (none when all match their digests)."""

    problems = []
    for name in FILES:
        try:
            fetch(name)
        except (OSError, ValueError) as error:
            problems.append(f"{name}: {error}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check-sources", action="store_true", help="download the pinned files and check their SHA-256")
    args = parser.parse_args(argv)
    if not args.check_sources:
        parser.error("nothing to do: pass --check-sources")
    problems = check_sources()
    for problem in problems:
        print(problem)
    print(f"{len(FILES) - len(problems)}/{len(FILES)} pinned files match: spider {SPIDER_COMMIT[:12]}, TestSuiteEval {TESTSUITE_COMMIT[:12]}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
