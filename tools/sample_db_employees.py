"""Employees (datacharmer/test_db) for the sample databases eval: the adapter, and a run-time download of its data.

The Employees database (Fusheng Wang and Carlo Zaniolo's temporal data set, schema by Giuseppe Maxia, data conversion
by Patrick Crews; 6 tables, 3.9 million rows, 167 MB of INSERT scripts) is licensed Creative Commons Attribution-Share
Alike 3.0. It is therefore **never committed**: the scripts are downloaded from the pinned commit of
https://github.com/datacharmer/test_db into ``$KUMOSQL_BENCH_DATA/sample-db-employees`` (default
``~/.cache/kumosql-bench/sample-db-employees``), every file checked against its SHA-256 (a mismatch is an error, an
unreachable GitHub is :class:`DataUnavailable`, which the tests turn into a skip). Only the authored workload and pairs
and the adapted BigQuery DDL (shared under the same licence, see ``tests/fixtures/sample_databases/employees/NOTICE.md``)
are in the repository.

Loading. DuckDB runs the upstream ``INSERT`` statements as they are (backticks removed) into a database file in the
cache, built once (about 90 seconds) and opened read-only afterwards, so every worker and test connects in a moment.
Nothing is hand-copied. The load is checked against upstream in three independent ways: a row count from a
tokenizing scan of the scripts (not DuckDB), the row counts upstream publishes, and the checksums that upstream's own
test scripts (``test_employees_md5.sql``, ``test_employees_sha2.sql``) publish for every table, recomputed here from
the loaded rows in the order and with the value formatting of MySQL's ``CONCAT_WS('#', @crc, ...)`` chain.

``make_adapter(bench)`` builds the adapter from ``tools/sample_db_bench.py`` (given as an argument because this module
is imported by it).
"""

from __future__ import annotations

import fcntl
import hashlib
import http.client
import os
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.request

REPO = "datacharmer/test_db"
COMMIT = "e324b56193ca506ab7cc1ab143a9153d8c4535d7"
BASE = f"https://raw.githubusercontent.com/{REPO}/{COMMIT}/"
LICENCE = "Creative Commons Attribution-Share Alike 3.0 Unported (the header of employees.sql)"

#: published path -> SHA-256 (every file is also its own name in the cache folder)
FILES = {
    "employees.sql": "cfe3f89f7b21326c516ba65d253e35e795877e9bb60c388520d915f348403a9a",
    "objects.sql": "80c4a5915c27a41d9dff05951d88e152137f15d62dc5852f54176a5b085d7ef0",
    "load_departments.dump": "2271cfef20852e395ec72ce269a119b2c799a973a9277c971409ea53d5a17cfa",
    "load_employees.dump": "ba004ebc5fcdad59544fd8ced262d1793ca02c7936c5d7668a355c6a683d6fa8",
    "load_dept_emp.dump": "52cc6dbc1b139254533264bd5d44a6012377f34ecf1eef693ddfb349aeb40ed6",
    "load_dept_manager.dump": "d9cff691f09f2399f5490e435deb8c932946246aaf483b8f1cbef0bc556aa1dc",
    "load_titles.dump": "dcd382989c46719e1e216ffef4919483f0f71d52da517c37c22d39cdaf9bc044",
    "load_salaries1.dump": "aa485ea7b1553f1660d6db5a93e9ede0a0c182cb923f9471a39594f7ca967c5b",
    "load_salaries2.dump": "cad589bff736cb575358d7806e4e4a13e28a2e9c714c2fb51fbe4db74a5706fa",
    "load_salaries3.dump": "75fc473d2472341fbfd635d6f4853c63645051829a9c7a1a18ee819fe5816f45",
    "test_employees_md5.sql": "0f3ba5bf7abdb009357b60676626ea782bb5c6787ee0859b56c9464a70d40dfe",
    "test_employees_sha2.sql": "a78e6b89ffbdb569239b070d5489ac8db83e7c027f9df7ea849982ab28459c66",
    "README.md": "8165403df9a726c4b6fe0b9172c60a85370a12004c0591387e38134f038d39ce",
}
#: the order employees.sql sources them in (parents first)
LOAD_ORDER = (
    "load_departments.dump",
    "load_employees.dump",
    "load_dept_emp.dump",
    "load_dept_manager.dump",
    "load_titles.dump",
    "load_salaries1.dump",
    "load_salaries2.dump",
    "load_salaries3.dump",
)
#: the order of the checksum chains in the upstream test scripts: (ORDER BY, CONCAT_WS columns)
CHAIN = {
    "employees": ("emp_no", ("emp_no", "birth_date", "first_name", "last_name", "gender", "hire_date")),
    "departments": ("dept_no", ("dept_no", "dept_name")),
    "dept_manager": ("dept_no, emp_no", ("dept_no", "emp_no", "from_date", "to_date")),
    "dept_emp": ("dept_no, emp_no", ("dept_no", "emp_no", "from_date", "to_date")),
    "titles": ("emp_no, title, from_date", ("emp_no", "title", "from_date", "to_date")),
    "salaries": ("emp_no, from_date, to_date", ("emp_no", "salary", "from_date", "to_date")),
}
TABLES = tuple(CHAIN)


class DataUnavailable(OSError):
    """GitHub cannot be reached (or the file is not served): callers skip instead of failing."""


class PinMismatch(RuntimeError):
    """A downloaded or cached file is not the pinned one: always an error."""


def cache_dir() -> Path:
    root = Path(os.environ.get("KUMOSQL_BENCH_DATA", Path.home() / ".cache" / "kumosql-bench"))
    return root / "sample-db-employees" / COMMIT[:12]


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _attempt(name: str, folder: Path) -> tuple[str, str]:
    """One download into a temporary file: ``(temporary path, "")`` when it is whole, else ``("", why)``."""

    handle, temporary = tempfile.mkstemp(dir=folder, prefix=name + ".")
    try:
        with os.fdopen(handle, "wb") as out, urllib.request.urlopen(BASE + name, timeout=120) as response:
            expected = response.headers.get("Content-Length")
            for block in iter(lambda: response.read(1 << 20), b""):
                out.write(block)
        size = os.path.getsize(temporary)
        if expected is not None and size != int(expected):
            raise OSError(f"cut off after {size} of {expected} bytes")
        return temporary, ""
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, http.client.HTTPException) as error:
        os.unlink(temporary)
        return "", f"{BASE + name} cannot be fetched: {error}"


def download(name: str, folder: Path | None = None, attempts: int = 3) -> Path:
    """One pinned file in the cache: downloaded once (checked whole against its SHA-256 before it is renamed into place,
    so parallel runs never read half a file and a transfer the network cut short is retried, never cached)."""

    folder = folder or cache_dir()
    path = folder / name
    if not path.exists():
        folder.mkdir(parents=True, exist_ok=True)
        why = ""
        for _ in range(attempts):
            temporary, why = _attempt(name, folder)
            if temporary and _digest(Path(temporary)) == FILES[name]:
                os.replace(temporary, path)
                break
            if temporary:
                os.unlink(temporary)
                why = f"{BASE + name} arrived but is not the pinned file (SHA-256 {FILES[name]})"
        else:
            raise DataUnavailable(why)
    if _digest(path) != FILES[name]:
        raise PinMismatch(f"{path} does not match the pinned SHA-256 {FILES[name]}; delete it to download again")
    return path


def fetch(names: tuple[str, ...] | None = None) -> Path:
    """The folder holding the pinned files (all of them, or ``names``), downloading what is missing."""

    for name in names or tuple(FILES):
        download(name)
    return cache_dir()


# ---------------------------------------------------------------- reading the INSERT scripts

_ROW = r"\((?:[^'()]+|'(?:[^']|'')*')*\)"
_STATEMENT = re.compile(r"INSERT INTO `(\w+)` VALUES\s*((?:" + _ROW + r"\s*,?\s*)+);", re.DOTALL)
_ROW_RE = re.compile(_ROW)


def count_rows(text: str) -> dict[str, int]:
    """Rows per table that the INSERT statements of a dump script give, counted by tokenizing the VALUES lists.

    Independent of DuckDB: it must account for every character of the file (statements, tuples and the commas
    between them) or it raises, so a statement it does not understand is never silently skipped.
    """

    if "\\" in text:
        raise ValueError("a backslash escape in a dump: the reader does not handle MySQL escapes")
    counts: dict[str, int] = {}
    pos = 0
    for match in _STATEMENT.finditer(text):
        if text[pos : match.start()].strip():
            raise ValueError(f"unread text before offset {match.start()}: {text[pos : match.start()][:60]!r}")
        pos = match.end()
        counts[match.group(1)] = counts.get(match.group(1), 0) + len(_ROW_RE.findall(match.group(2)))
    if text[pos:].strip():
        raise ValueError(f"unread text after offset {pos}: {text[pos:][:60]!r}")
    return counts


def upstream_counts() -> dict[str, int]:
    """Rows per table of every INSERT script, the sum over the three salaries files included."""

    total: dict[str, int] = {}
    for name in LOAD_ORDER:
        text = download(name).read_text(encoding="utf-8")
        for table, n in count_rows(text).items():
            total[table] = total.get(table, 0) + n
    return total


def published_checksums() -> dict[str, tuple[int, str, str]]:
    """``{table: (records, md5 chain, sha-256 chain)}`` that upstream's test scripts publish."""

    def read(name: str, digest_length: int) -> dict[str, tuple[int, str]]:
        text = download(name).read_text(encoding="utf-8")
        found = {}
        for table, recs, *hashes in re.findall(
            r"\('(\w+)',\s*(\d+),\s*'([0-9a-f]+)'(?:,\s*'([0-9a-f]+)')?\)", text
        ):
            wanted = [h for h in hashes if h]
            found[table] = (int(recs), wanted[-1])
            assert len(wanted[-1]) == digest_length, name
        return found

    md5 = read("test_employees_md5.sql", 32)
    sha2 = read("test_employees_sha2.sql", 64)
    assert set(md5) == set(sha2) == set(TABLES), (set(md5), set(sha2))
    for table in TABLES:
        assert md5[table][0] == sha2[table][0]
    return {t: (md5[t][0], md5[t][1], sha2[t][1]) for t in TABLES}


def chain_checksums(con, table: str) -> tuple[int, str, str]:
    """The record count and the MD5 and SHA-256 chains of ``table``: ``crc = H(CONCAT_WS('#', crc, col, ...))`` over the
    rows in the upstream script's ORDER BY, from ``crc = ''`` (NULLs are skipped, dates and numbers print as MySQL does)."""

    order, columns = CHAIN[table]
    cols = ", ".join(f'"{c}"' for c in columns)
    cursor = con.execute(f'SELECT {cols} FROM "{table}" ORDER BY {order}')
    md5 = sha = ""
    n = 0
    while True:
        rows = cursor.fetchmany(50_000)
        if not rows:
            break
        for row in rows:
            tail = "#".join(str(v) for v in row if v is not None)
            md5 = hashlib.md5(f"{md5}#{tail}".encode()).hexdigest()
            sha = hashlib.sha256(f"{sha}#{tail}".encode()).hexdigest()
            n += 1
    return n, md5, sha


# ---------------------------------------------------------------- the DuckDB file

def build_database(path: Path, create_statements: list[str]) -> None:
    """Load the dump scripts into a DuckDB file at ``path`` (atomically; a concurrent build waits and reuses the result)."""

    import duckdb

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            return
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.unlink(missing_ok=True)
        con = duckdb.connect(str(temporary))
        try:
            for statement in create_statements:
                con.execute(statement)
            for name in LOAD_ORDER:
                text = download(name).read_text(encoding="utf-8")
                if "\\" in text:
                    raise ValueError(f"{name}: a backslash escape")
                # MySQL quotes identifiers with backticks; the statements are otherwise standard INSERT ... VALUES
                con.execute(text.replace("`", ""))
            con.execute("CHECKPOINT")
        finally:
            con.close()
        os.replace(temporary, path)
        Path(str(temporary) + ".wal").unlink(missing_ok=True)


# ---------------------------------------------------------------- the adapter


def make_adapter(b):
    """The Employees adapter, built on the classes of ``tools/sample_db_bench.py`` (module ``b``)."""

    class Employees(b.Adapter):
        name = "employees"
        title = "Employees"
        results_order = 360
        docs_page = "docs/evals/sample-databases-employees.md"
        downloaded = True
        upstream = tuple(
            b.Upstream(name, REPO, COMMIT, name, digest, LICENCE) for name, digest in FILES.items()
        )
        pins_summary = (
            f"Employees {REPO}@{COMMIT[:10]}, 13 files (employees.sql, objects.sql, the 8 load_*.dump scripts, the two checksum "
            "test scripts and README.md; the SHA-256 of each is in tools/sample_db_employees.py), Creative Commons Attribution-Share Alike 3.0, "
            "downloaded at run time and never committed"
        )
        published_counts = {
            "employees": 300024,
            "departments": 9,
            "dept_manager": 24,
            "dept_emp": 331603,
            "titles": 443308,
            "salaries": 2844047,
        }
        published_counts_source = "upstream's test_employees_md5.sql and test_employees_sha2.sql (expected_values)"
        workload_note = (
            "Employees' 4 views (dept_emp_latest_date, current_dept_emp, v_full_employees, v_full_departments), the SELECTs of its stored "
            "functions and of the show_departments procedure (parameters bound, the procedure's temporary tables as CTEs) and the record-count "
            "comparison of its test script are adapted from MySQL to BigQuery (each adaptation recorded in the workload file). "
            "The data is CC BY-SA 3.0 and is downloaded at run time from the pinned commit, never committed"
        )
        baseline_note = ""

        @property
        def folder(self):
            """The cache folder of the pinned upstream files (downloaded on first use)."""

            return fetch()

        @property
        def fixtures(self):
            return b.FIXTURES / self.name

        def upstream_text(self) -> str:
            return (self.folder / "employees.sql").read_text(encoding="utf-8")

        def schema(self):
            return b.read_ddl((self.fixtures / "adapted" / "schema.sql").read_text(encoding="utf-8"))

        def workload(self):
            return b.json.loads((self.fixtures / "workload.json").read_text(encoding="utf-8"))["queries"]

        def pairs(self):
            return b.json.loads((self.fixtures / "pairs.json").read_text(encoding="utf-8"))["pairs"]

        def upstream_views(self):
            """The CREATE VIEW statements of employees.sql and objects.sql, by name (MySQL ``#`` comments removed)."""

            text = (self.folder / "employees.sql").read_text(encoding="utf-8") + "\n" + (
                self.folder / "objects.sql"
            ).read_text(encoding="utf-8")
            text = re.sub(r"(?m)^\s*#.*$", "", text)
            return {
                m.group(1): m.group(2).strip()
                for m in re.finditer(
                    r"(?ims)^create\s+or\s+replace\s+view\s+(\w+)\s+as\s*(.*?);\s*$", text
                )
            }

        def upstream_row_counts(self):
            return b.Counter(upstream_counts())

        def upstream_rows(self):
            raise NotImplementedError("3.9 million rows are counted, not materialised: see upstream_row_counts")

        def connect(self, rows=None):
            """The loaded database, read-only (built into the cache from the dump scripts on first use)."""

            import duckdb

            if rows is not None:
                raise ValueError("Employees loads from the dump scripts only")
            schema = self.schema()
            fingerprint = hashlib.sha256(
                ("|".join(sorted(FILES.values())) + "|".join(b.create_table_sql(t) for t in schema.values())).encode()
            ).hexdigest()[:12]
            path = cache_dir() / f"employees-{fingerprint}.duckdb"
            fetch(LOAD_ORDER)
            if not path.exists():
                build_database(path, [b.create_table_sql(t) for t in schema.values()])
            con = duckdb.connect(str(path), read_only=True)
            con.execute("SET threads=1")
            return con

        def views(self):
            return []

        def extra_checks(self, con) -> list[str]:
            """The record counts and checksums upstream's test scripts publish, recomputed from the loaded rows."""

            problems = []
            expected = published_checksums()
            for table in TABLES:
                records, md5, sha = expected[table]
                got = chain_checksums(con, table)
                if got[0] != records:
                    problems.append(f"{table}: {got[0]} records, upstream's test script expects {records}")
                if got[1] != md5:
                    problems.append(f"{table}: MD5 chain {got[1]} is not the published {md5}")
                if got[2] != sha:
                    problems.append(f"{table}: SHA-256 chain {got[2]} is not the published {sha}")
            return problems

    return Employees()
