"""AdventureWorks OLTP in the sample-databases eval: the adapter (``tools/sample_db_bench.py`` registers it).

Microsoft's AdventureWorks sample (MIT, ``microsoft/sql-server-samples``) is a T-SQL script, ``instawdb.sql``, that
creates about 70 tables and loads them from 69 CSV files (``BULK INSERT``), all shipped in one release asset,
``AdventureWorks-oltp-install-script.zip``. The data is 91 MB (760,000 rows): too large to commit, so the zip is
downloaded at run time from the pinned release, checked against its SHA-256 (and every file in it against
``members.sha256``) and read in place. The script itself (330 KB, MIT) is committed unchanged with the licence, so
the schema is checked against upstream without the network.

The adapter reads the script as it is: the DDL (``CREATE TABLE``, then the keys that ``ALTER TABLE ... ADD`` declares
after the load, split into one constraint per statement), the ``BULK INSERT`` statements (which file feeds which table, and
with which field and row terminators: tab or ``+|``, line feed or ``&|`` and a line feed), and the ``CREATE VIEW``
bodies. No row is copied by hand and no row is held in memory beyond the table being loaded.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal
import hashlib
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.request
import zipfile
from collections.abc import Mapping

from sample_db_bench import (  # the harness module, importable under this name while it runs as a script
    Adapter,
    TableDef,
    Upstream,
    prover_type,
    read_ddl,
    tokenize,
)

REPO = "microsoft/sql-server-samples"
TAG = "adventureworks"
ASSET = "AdventureWorks-oltp-install-script.zip"
ASSET_URL = f"https://github.com/{REPO}/releases/download/{TAG}/{ASSET}"
ASSET_SHA256 = "58962e94ea386ef7cd3d8a08211bfd42a79d9b81bdd68fd4b6b0051de6c5bd42"
SCRIPT = "instawdb.sql"
SCRIPT_SHA256 = "fd1be672069cc4f909427c90b5a969ff65efa9d0a2bb6a0eb9e3619ddeb4fb64"
#: the commit of ``license.txt`` (the same file as at the release's own commit): the licence of the whole repository
LICENCE_COMMIT = "beaab06ef72831089ca80e5355d65e661fd19b26"
LICENCE_SHA256 = "201f3705979e932c92cd3c6ddbddf4dc3c71dcc5a82089a1e4018227cfe1cf09"
CACHE = (
    Path(os.environ.get("KUMOSQL_BENCH_DATA", Path.home() / ".cache" / "kumosql-bench"))
    / "sample-databases"
    / "adventureworks"
)


class DataUnavailable(OSError):
    """The release asset could not be downloaded (no network, or GitHub unreachable)."""


def fetch_asset(path: Path | None = None) -> Path:
    """The pinned release zip in the cache: downloaded once (written whole, then renamed, so parallel runs never read half a file)."""

    path = path or CACHE / ASSET_SHA256[:12] / ASSET
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(ASSET_URL, timeout=120) as response:  # noqa: S310 - pinned https URL
                data = response.read()
        except OSError as error:  # URLError, timeouts, connection errors
            raise DataUnavailable(f"cannot download {ASSET_URL}: {error}") from error
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(temporary, path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != ASSET_SHA256:
        raise OSError(f"{path} has SHA-256 {digest}, not the pinned {ASSET_SHA256}; delete it to download again")
    return path


# ---------------------------------------------------------------- the script's BULK INSERT statements

_BULK = re.compile(
    r"BULK\s+INSERT\s+\[(\w+)\]\.\[(\w+)\]\s+FROM\s+'\$\(SqlSamplesSourceDataPath\)([\w.]+)'\s*WITH\s*\((.*?)\)\s*;",
    re.IGNORECASE | re.DOTALL,
)


def read_bulk_inserts(text: str) -> dict[str, tuple[str, str, str]]:
    """``{table: (file, field terminator, row terminator)}`` of every ``BULK INSERT`` in the script.

    ``'\\t'`` is a tab; ``'0x0a'`` and ``'\\n'`` a line feed; ``'+|'`` and ``'&|\\n'`` are literal. Where a script option is
    given twice (``FIELDTERMINATOR`` of one table) the last one counts, as in T-SQL.
    """

    def terminator(options: str, name: str) -> str:
        found = re.findall(name + r"\s*=\s*'([^']*)'", options, re.IGNORECASE)
        if not found:
            raise ValueError(f"no {name} in {options[:60]!r}")
        value = found[-1]
        return value.replace("0x0a", "\n").replace("\\t", "\t").replace("\\n", "\n")

    return {
        table: (
            file,
            terminator(options, "FIELDTERMINATOR"),
            terminator(options, "ROWTERMINATOR"),
        )
        for _, table, file, options in _BULK.findall(text)
    }


def split_records(data: str, row_terminator: str) -> list[str]:
    """The records of a data file. A line feed in a row terminator also matches a carriage return and line feed
    (``Document.csv`` is written that way); inside a field a carriage return and line feed is data."""

    pattern = "".join("\r?\n" if char == "\n" else re.escape(char) for char in row_terminator)
    records = re.split(pattern, data)
    if records and records[-1] == "":
        records.pop()
    return records


def join_continuations(lines: list[str], field: str, columns: int) -> list[str]:
    """Records from lines of a file whose text columns hold line feeds: a record is complete once it has ``columns``
    fields, and the line feeds inside it are kept."""

    records, pending = [], None
    for line in lines:
        pending = line if pending is None else pending + "\n" + line
        if pending.count(field) >= columns - 1:
            records.append(pending)
            pending = None
    if pending is not None:
        raise ValueError(f"the last record has too few fields: {pending[:60]!r}")
    return records


def _datetime(text: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(text)  # 2013-05-30 00:00:00.000


def column_reader(kind: str):
    """A function turning one field of a data file into the value of a column of BigQuery type ``kind``."""

    base = prover_type(kind)
    if base == "INT64":
        return int
    if base in ("NUMERIC", "BIGNUMERIC"):
        return Decimal
    if base == "FLOAT64":
        return float
    if base == "BOOL":
        return lambda text: text not in ("0", "f", "false")
    if base == "DATETIME":
        return _datetime
    if base == "DATE":
        return lambda text: _dt.date.fromisoformat(text)
    if base == "BYTES":
        return bytes.fromhex
    return lambda text: text


class _RowCount:
    """The row count of a table's data file (``check_database`` only asks how many rows the script inserts)."""

    def __init__(self, count: int):
        self.count = count

    def __len__(self) -> int:
        return self.count


class LazyRows(Mapping):
    """``{table: rows}`` converted when a table is asked for and not kept: loading the database holds one table at a time."""

    def __init__(self, adapter: "AdventureWorks"):
        self.adapter = adapter

    def __getitem__(self, table: str) -> list[tuple]:
        if table not in self.adapter.schema():
            raise KeyError(table)
        return self.adapter.table_rows(table)

    def __iter__(self):
        return iter(self.adapter.schema())

    def __len__(self) -> int:
        return len(self.adapter.schema())


class AdventureWorks(Adapter):
    name = "adventureworks"
    title = "AdventureWorks"
    results_order = 360
    docs_page = "docs/evals/sample-databases-adventureworks.md"
    downloaded = True
    ddl_file = "upstream/instawdb.sql"
    data_file = "upstream/instawdb.sql"
    upstream = (
        Upstream(
            "upstream/instawdb.sql",
            REPO,
            LICENCE_COMMIT,
            f"{ASSET}!{SCRIPT}",
            SCRIPT_SHA256,
            "MIT, Copyright (c) Microsoft Corporation (upstream/license.txt)",
        ),
        Upstream(
            "upstream/license.txt",
            REPO,
            LICENCE_COMMIT,
            "license.txt",
            LICENCE_SHA256,
            "the licence itself",
        ),
        Upstream(
            f"release/{ASSET}",
            REPO,
            LICENCE_COMMIT,
            f"releases/download/{TAG}/{ASSET}",
            ASSET_SHA256,
            "MIT, Copyright (c) Microsoft Corporation (upstream/license.txt)",
        ),
    )
    workload_note = (
        "AdventureWorks' 12 portable views (8 more read XML columns with XQuery methods and have no BigQuery form; PIVOT is rewritten as conditional "
        "sums, T-SQL string concatenation and DATEADD as their BigQuery forms) and the SELECT statements of its functions and recursive stored procedures "
        "(parameters bound) are adapted from T-SQL to BigQuery, each adaptation recorded in the workload file. Computed columns are loaded as the "
        "data files give them, hierarchyid, geography and uniqueidentifier values as the hex or text the files write, and XML as text; nchar values keep their padding"
    )
    baseline_note = "The first run is the baseline (nothing tuned)"

    # -- the release asset

    def asset(self) -> Path:
        return fetch_asset()

    def available(self) -> bool:
        try:
            self.asset()
        except OSError:
            return False
        return True

    def digest(self, pin: Upstream) -> str:
        if pin.local.startswith("release/"):
            return hashlib.sha256(self.asset().read_bytes()).hexdigest()
        return super().digest(pin)

    def pins_text(self) -> str:
        return (
            f"{self.title} {REPO} release {TAG}, asset {ASSET} (SHA-256 {ASSET_SHA256[:12]}; its {SCRIPT} SHA-256 {SCRIPT_SHA256[:12]}, MIT Copyright (c) Microsoft Corporation, "
            f"licence text at {LICENCE_COMMIT[:10]}, committed unchanged; the 69 data files are checked against members.sha256 and downloaded at run time, not committed)"
        )

    def member_digests(self) -> dict[str, str]:
        """``{file: SHA-256}`` of everything in the release zip, as pinned in ``members.sha256``."""

        pinned = {}
        for line in (self.folder / "members.sha256").read_text(encoding="utf-8").splitlines():
            digest, _, name = line.partition("  ")
            pinned[name] = digest
        return pinned

    def check_members(self) -> list[str]:
        """What differs between the zip and ``members.sha256`` (empty: identical files, and the committed script is the zip's)."""

        problems = []
        pinned = self.member_digests()
        with zipfile.ZipFile(self.asset()) as archive:
            names = set(archive.namelist())
            for name in sorted(names | set(pinned)):
                if name not in names:
                    problems.append(f"{name}: pinned in members.sha256, not in the release zip")
                elif name not in pinned:
                    problems.append(f"{name}: in the release zip, not pinned")
                elif hashlib.sha256(archive.read(name)).hexdigest() != pinned[name]:
                    problems.append(f"{name}: SHA-256 differs from members.sha256")
            if SCRIPT in names and archive.read(SCRIPT) != (self.folder / self.data_file).read_bytes():
                problems.append(f"the committed {SCRIPT} is not the release zip's")
        return problems

    # -- upstream text

    def _script(self) -> str:
        return (self.folder / self.ddl_file).read_text(encoding="utf-8-sig")

    def ddl_text(self) -> str:
        """The script with the key statements read one constraint at a time: ``WITH CHECK ADD`` is ``ADD``, and an
        ``ALTER TABLE t ADD c1, c2;`` is one statement per constraint."""

        text = re.sub(r"(?i)\bWITH\s+CHECK\s+ADD\b", "ADD", self._script())
        out, pos = [], 0
        for match in re.finditer(r"(?is)ALTER\s+TABLE\s+(\[\w+\]\.\[\w+\])\s+ADD\s+(.*?);[ \t]*\r?\n", text):
            if match.start() < pos:
                continue
            depth, start, items = 0, 0, []
            body = match.group(2)
            for i, char in enumerate(body):
                depth += char == "("
                depth -= char == ")"
                if char == "," and depth == 0:
                    items.append(body[start:i])
                    start = i + 1
            items.append(body[start:])
            out.append(text[pos : match.start()])
            out += [f"ALTER TABLE {match.group(1)} ADD {item.strip()};\n" for item in items]
            pos = match.end()
        return "".join(out) + text[pos:]

    #: computed columns upstream declares ``ISNULL(expression, constant)``: SQL Server infers them NOT NULL
    _ISNULL_COMPUTED = re.compile(r"(?is)\[(\w+)\]\s+AS\s+ISNULL\s*\(")

    def upstream_tables(self) -> dict[str, TableDef]:
        """Upstream's tables: ``DatabaseLog`` (a heap the DDL trigger fills while the script runs) left out, and the computed
        columns ``ISNULL(expression, constant)`` NOT NULL, as SQL Server infers them."""

        text = self.ddl_text()
        tables = read_ddl(text)
        tables.pop("DatabaseLog", None)
        for body in re.finditer(r"(?is)CREATE\s+TABLE\s+\[\w+\]\.\[(\w+)\]\s*\((.*?)\n\)\s*ON\s+\[PRIMARY\]", text):
            table = tables.get(body.group(1))
            if table is None:
                continue
            for column in self._ISNULL_COMPUTED.findall(body.group(2)):
                table.not_null.add(column)
        return tables

    def upstream_views(self) -> dict[str, str]:
        """``CREATE VIEW [schema].[name] [WITH SCHEMABINDING] AS body`` of the script, by name (body as written)."""

        return {
            m.group(1): m.group(2).strip()
            for m in re.finditer(
                r"(?ims)^create\s+view\s+\[\w+\]\.\[(\w+)\]\s*(?:with\s+schemabinding\s*)?as\b(.*?)(?=^\s*go\s*$)",
                self._script(),
            )
        }

    # -- the data files

    #: tables the script fills with something other than a ``BULK INSERT``: AWBuildVersion with one ``INSERT`` of the
    #: server's own version (the release ships the row of the instance that wrote it, ``AWBuildVersion.csv``, tab
    #: separated) and ErrorLog, which stays empty
    _OTHER_FILES = {"AWBuildVersion": ("AWBuildVersion.csv", "\t", "\n")}
    _EMPTY = ("ErrorLog",)

    def bulk_inserts(self) -> dict[str, tuple[str, str, str]]:
        return {**read_bulk_inserts(self._script()), **self._OTHER_FILES}

    def _records(self, table: str) -> list[list[str]]:
        if table in self._EMPTY:
            return []
        file, field, row = self.bulk_inserts()[table]
        with zipfile.ZipFile(self.asset()) as archive:
            data = archive.read(file).decode("utf-8")
        records = split_records(data, row)
        if row == "\n":
            # a line-terminated file whose text columns hold line feeds (ProductReview.Comments): a line with fewer
            # fields than the table has columns continues in the next one
            records = join_continuations(records, field, len(self.schema()[table].columns))
        return [record.split(field) for record in records]

    def upstream_rows(self) -> dict[str, list]:
        """Row counts only (one entry per table: ``len()`` is the number of records the script's ``BULK INSERT`` reads)."""

        return {t: _RowCount(len(self._records(t))) for t in (*self.bulk_inserts(), *self._EMPTY)}

    def table_rows(self, table: str) -> list[tuple]:
        """The rows of one table, converted to the adapted column types."""

        spec = self.schema()[table]
        readers = [column_reader(kind) for kind in spec.columns.values()]
        names = list(spec.columns)
        out = []
        for number, record in enumerate(self._records(table), 1):
            if len(record) != len(names):
                raise ValueError(f"{table} row {number}: {len(record)} fields for {len(names)} columns")
            row = []
            for name, kind, read, text in zip(names, spec.columns.values(), readers, record):
                if text == "":
                    row.append(None)  # an empty field is NULL
                elif text == "\x00":
                    # an empty string is written as one NUL byte (Document: the root hierarchyid, folders' FileExtension)
                    if prover_type(kind) not in ("STRING", "BYTES"):
                        raise ValueError(f"{table}.{name} row {number}: a NUL in a {kind} column")
                    row.append(b"" if prover_type(kind) == "BYTES" else "")
                else:
                    row.append(read(text))
            out.append(tuple(row))
        return out

    # -- DuckDB

    def database_file(self) -> Path:
        """The cache file of the loaded database. Its name holds a hash of everything that decides its content (the zip's
        SHA-256, the adapted DDL and this module), so a changed reader or schema never meets a stale file."""

        import inspect

        import sample_db_bench as bench

        readers = (
            read_bulk_inserts,
            split_records,
            join_continuations,
            column_reader,
            _datetime,
            AdventureWorks._records,
            AdventureWorks.table_rows,
            bench.create_table_sql,
            bench.insert_all,
            bench.sql_literal,
        )
        key = hashlib.sha256(
            b"\0".join(
                [
                    ASSET_SHA256.encode(),
                    (self.folder / "adapted" / "schema.sql").read_bytes(),
                    *(inspect.getsource(fn).encode() for fn in readers),
                ]
            )
        ).hexdigest()[:16]
        return CACHE / ASSET_SHA256[:12] / f"loaded-{key}.duckdb"

    def _build_database(self, path: Path) -> None:
        import duckdb

        from sample_db_bench import create_table_sql, insert_all

        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
        os.close(handle)
        os.unlink(temporary)  # DuckDB wants to create the file itself
        con = duckdb.connect(temporary)
        try:
            con.execute("SET threads=1")
            for table in self.schema().values():
                con.execute(create_table_sql(table))
                insert_all(con, table, self.table_rows(table.name))
        finally:
            con.close()
        os.replace(temporary, path)

    def connect(self, rows=None):
        """An in-memory DuckDB holding the whole database. Parsing 91 MB of data files and inserting 760,000 rows takes minutes,
        so the loaded database is built once per content (see ``database_file``) and copied from there."""

        if rows is not None:
            return super().connect(rows)
        import duckdb

        from sample_db_bench import create_table_sql, to_duckdb

        path = self.database_file()
        if not path.exists():
            self._build_database(path)
        con = duckdb.connect(":memory:")
        con.execute("SET threads=1")
        con.execute(f"ATTACH '{path}' AS loaded (READ_ONLY)")
        for table in self.schema().values():
            con.execute(create_table_sql(table))
            con.execute(f'INSERT INTO "{table.name}" SELECT * FROM loaded."{table.name}"')
        con.execute("DETACH loaded")
        for name, sql in self.views():
            con.execute(f'CREATE VIEW "{name}" AS {to_duckdb(sql)}')
        return con

    def inserted_rows(self) -> LazyRows:
        return LazyRows(self)

    def trigger_rows(self, rows) -> dict[str, list[tuple]]:
        return {}  # the script's triggers fire on UPDATE and DELETE and on later INSERTs, not on the BULK INSERTs

    # published counts: the records of the pinned files (Microsoft publishes none); a changed reader shows up as a difference
    published_counts = {}
    published_counts_source = ""
