"""Sample databases eval: complete public databases loaded into DuckDB, their workloads through KumoSQL.

Each database is an *adapter* (``ADAPTERS``): the pinned upstream script, committed unchanged with
its licence under ``tests/fixtures/sample_databases/<name>/upstream/``, and the adapted BigQuery DDL
(``adapted/schema.sql``, labelled ADAPTED) that says how the upstream tables become BigQuery tables.
Chinook (lerocha/chinook-database) and Northwind (microsoft/sql-server-samples) are the first two;
Pagila, Sakila, AdventureWorks and Employees plug in as further adapters (subclass ``Adapter``, give
the pins, the type conversions and the expected row counts).

For every database the harness

1. checks the upstream files against their pinned SHA-256;
2. loads the full schema and data into an in-memory DuckDB: the adapted DDL makes the tables, the
   rows are read from the upstream script's INSERT statements (no hand-copied data);
3. checks the load against upstream: the same tables and columns (in order), the same NOT NULL
   columns, primary keys and foreign keys as the upstream DDL, the row count of every table equal to
   the rows the upstream script inserts and to the counts upstream publishes (Chinook's own test
   fixture), and every declared key unique and every foreign key satisfied by the real rows.

Then it scores two things, separately:

(a) **rewrites** (``sample-databases-rewrites``): every query of the workload goes through every
    KumoSQL rewrite and is run again on the real database. The engine-suites machinery does it
    (``tools/engine_suites.py``): the canonical rule pipeline on the query and on wrapped/padded
    variants of it, then ``lift_subqueries`` (the one rule outside the canonical order) and the
    proof-gated ``query_optimizer`` with the declared keys. A rewrite whose result multiset differs
    from the unrewritten control (confirmed with DuckDB's optimizer off) or that stops running is
    *wrong*. Workload origins are kept apart: ``upstream-view`` (Northwind's 16 views),
    ``upstream-procedure`` (Northwind's stored procedures with their parameters bound),
    ``upstream-test`` (queries from Chinook's test fixture), all adapted to BigQuery SQL with the
    adaptation recorded, and ``authored`` (written for this eval).

(b) **pairs** (``sample-databases-pairs``): authored query pairs labelled equivalent or different
    under the declared keys (some with a key or NOT NULL removed: the sibling without the
    guarantee). Each pair goes through the provers: the structural prover, the algebraic/SMT prover
    with the declared constraints and its executed counterexample search, then the bounded checker
    for a counterexample. Every proof is checked on the real data; every refutation is replayed in
    DuckDB on the counterexample database, which must satisfy the declared constraints. ``wrong`` is
    a proof of a pair labelled different or that differs on the real data, a refutation of a pair
    labelled equivalent, or a counterexample that does not replay.

One case in five (by SHA-1 of its id) is held out and reported apart.

    python tools/sample_db_bench.py --check            # load and check every database
    python tools/sample_db_bench.py --part rewrites    # (a)
    python tools/sample_db_bench.py --part pairs       # (b)
    python tools/sample_db_bench.py --write-results    # both parts, write benchmarks/results/*.json
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
import datetime as _dt
from decimal import Decimal
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import time

import sqlglot

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

FIXTURES = ROOT / "tests" / "fixtures" / "sample_databases"
HOLDOUT_MODULUS = 5


def held_out(case_id: str) -> bool:
    """One case in five, by SHA-1 of its id; held-out cases are never used to tune anything."""

    return int(hashlib.sha1(case_id.encode()).hexdigest(), 16) % HOLDOUT_MODULUS == 0


# ---------------------------------------------------------------- upstream pins


@dataclass(frozen=True)
class Upstream:
    """One pinned upstream file, committed unchanged next to its licence."""

    local: str  # path under the adapter's fixture folder
    repo: str  # owner/name on GitHub
    commit: str
    path: str  # path inside the repository
    sha256: str
    licence: str

    @property
    def url(self) -> str:
        return (
            f"https://raw.githubusercontent.com/{self.repo}/{self.commit}/{self.path}"
        )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- reading upstream SQL text

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<comment>--[^\n]*|/\*.*?\*/)
  | (?P<string>[Nn]?'(?:[^']|'')*')
  | (?P<hex>0[xX][0-9A-Fa-f]*)
  | (?P<number>\d+(?:\.\d*)?(?:[eE][-+]?\d+)?|\.\d+)
  | (?P<ident>\[[^\]]*\]|"[^"]*"|`[^`]*`)
  | (?P<word>[A-Za-z_@#][\w@#$]*)
  | (?P<punct>.)
    """,
    re.VERBOSE | re.DOTALL,
)


@dataclass(frozen=True)
class Token:
    kind: str
    text: str

    @property
    def name(self) -> str:
        """An identifier or word without its quoting."""

        if self.kind == "ident":
            return self.text[1:-1]
        return self.text

    def is_word(self, *words: str) -> bool:
        return self.kind == "word" and self.text.upper() in words


def tokenize(text: str) -> list[Token]:
    return [
        Token(m.lastgroup, m.group())
        for m in _TOKEN.finditer(text)
        if m.lastgroup not in ("ws", "comment")
    ]


@dataclass(frozen=True)
class Raw:
    """A literal as the upstream script wrote it: kind ``string``, ``number``, ``hex`` or ``null``."""

    kind: str
    text: str | None


def _literal(tokens: list[Token], i: int) -> tuple[Raw, int]:
    token = tokens[i]
    if token.kind == "string":
        body = token.text[2:-1] if token.text[0] in "Nn" else token.text[1:-1]
        return Raw("string", body.replace("''", "'")), i + 1
    if token.kind == "punct" and token.text in "-+" and tokens[i + 1].kind == "number":
        return Raw(
            "number", (token.text if token.text == "-" else "") + tokens[i + 1].text
        ), i + 2
    if token.kind == "number":
        return Raw("number", token.text), i + 1
    if token.kind == "hex":
        return Raw("hex", token.text[2:]), i + 1
    if token.is_word("NULL"):
        return Raw("null", None), i + 1
    if token.is_word("TRUE", "FALSE"):
        return Raw("number", "1" if token.text.upper() == "TRUE" else "0"), i + 1
    raise ValueError(f"unexpected literal {token.text!r}")


def _qualified_name(tokens: list[Token], i: int) -> tuple[str, int]:
    """``a``, ``[dbo].[a]`` or ``"dbo"."a"``: the last part."""

    name = tokens[i].name
    i += 1
    while (
        i + 1 < len(tokens)
        and tokens[i].text == "."
        and tokens[i + 1].kind in ("ident", "word")
    ):
        name = tokens[i + 1].name
        i += 2
    return name, i


def read_inserts(
    text: str,
) -> dict[str, list[tuple[tuple[str, ...] | None, tuple[Raw, ...]]]]:
    """Every row of every ``INSERT [INTO] t [(cols)] VALUES (...), (...)`` in an upstream script.

    Rows keep the column list they were inserted with (``None`` when the statement has none) and the
    literals as written; converting them is the adapter's job.
    """

    tokens = tokenize(text)
    rows: dict[str, list] = {}
    i = 0
    while i < len(tokens):
        if not tokens[i].is_word("INSERT"):
            i += 1
            continue
        i += 1
        if tokens[i].is_word("INTO"):
            i += 1
        table, i = _qualified_name(tokens, i)
        columns = None
        if tokens[i].text == "(":
            names = []
            i += 1
            while tokens[i].text != ")":
                if tokens[i].text != ",":
                    names.append(tokens[i].name)
                i += 1
            columns = tuple(names)
            i += 1
        if not tokens[i].is_word("VALUES"):
            continue  # INSERT ... SELECT: not a data row
        i += 1
        while True:
            if tokens[i].text != "(":
                raise ValueError(f"{table}: expected a row, found {tokens[i].text!r}")
            i += 1
            values = []
            while True:
                value, i = _literal(tokens, i)
                values.append(value)
                if tokens[i].text == ",":
                    i += 1
                    continue
                if tokens[i].text == ")":
                    i += 1
                    break
                raise ValueError(f"{table}: unexpected {tokens[i].text!r} in a row")
            rows.setdefault(table, []).append((columns, tuple(values)))
            if i < len(tokens) and tokens[i].text == ",":
                i += 1
                continue
            break
    return rows


@dataclass
class TableDef:
    """A table as a DDL declares it."""

    name: str
    columns: dict[str, str] = field(
        default_factory=dict
    )  # name -> declared type (upper case)
    not_null: set[str] = field(default_factory=set)
    primary_key: tuple[str, ...] = ()
    foreign_keys: list[tuple[tuple[str, ...], str, tuple[str, ...]]] = field(
        default_factory=list
    )


def _paren_items(tokens: list[Token], i: int) -> tuple[list[list[Token]], int]:
    """The comma-separated items of the parenthesised list starting at ``tokens[i] == '('``."""

    depth, items, current = 0, [], []
    while True:
        token = tokens[i]
        if token.text == "(":
            depth += 1
            if depth == 1:
                i += 1
                continue
        elif token.text == ")":
            depth -= 1
            if depth == 0:
                items.append(current)
                return items, i + 1
        elif token.text == "," and depth == 1:
            items.append(current)
            current = []
            i += 1
            continue
        current.append(token)
        i += 1


def _names(tokens: list[Token]) -> tuple[str, ...]:
    return tuple(
        t.name
        for t in tokens
        if t.kind in ("ident", "word") and not t.is_word("ASC", "DESC")
    )


def _constraint(table: TableDef, item: list[Token]) -> bool:
    """Record a PRIMARY KEY or FOREIGN KEY clause; ``False`` when the item is no key constraint."""

    words = [t.text.upper() for t in item]
    if item[0].kind != "word" or words[0] not in (
        "CONSTRAINT",
        "PRIMARY",
        "FOREIGN",
        "CHECK",
        "UNIQUE",
        "INDEX",
        "KEY",
        "ADD",
    ):
        return False  # a column definition (an inline PRIMARY KEY or REFERENCES is read with the column)
    if "PRIMARY" in words and "KEY" in words:
        start = next(k for k, t in enumerate(item) if t.text == "(")
        end = next(k for k, t in enumerate(item) if t.text == ")")
        table.primary_key = _names(item[start + 1 : end])
        return True
    if "FOREIGN" in words and "REFERENCES" in words:
        ref = words.index("REFERENCES")
        start = next(k for k, t in enumerate(item) if t.text == "(")
        end = next(k for k, t in enumerate(item) if t.text == ")")
        parent, k = _qualified_name(item, ref + 1)
        pstart = next(j for j in range(k, len(item)) if item[j].text == "(")
        pend = next(j for j in range(pstart, len(item)) if item[j].text == ")")
        table.foreign_keys.append(
            (_names(item[start + 1 : end]), parent, _names(item[pstart + 1 : pend]))
        )
        return True
    return words[:1] in (["CONSTRAINT"], ["CHECK"], ["UNIQUE"], ["INDEX"], ["KEY"])


def read_ddl(text: str) -> dict[str, TableDef]:
    """Tables, columns, NOT NULL columns and keys from CREATE TABLE and ALTER TABLE ... ADD statements.

    Written for the dialects of public sample scripts (SQLite, T-SQL, PostgreSQL, MySQL, BigQuery):
    quoting by ``[]``, ``""`` or backticks; constraints inline or added later by ALTER TABLE.
    """

    tokens = tokenize(text)
    tables: dict[str, TableDef] = {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if (
            token.is_word("CREATE")
            and i + 1 < len(tokens)
            and tokens[i + 1].is_word("TABLE")
        ):
            i += 2
            if tokens[i].is_word("IF"):
                i += 3  # IF NOT EXISTS
            name, i = _qualified_name(tokens, i)
            table = tables.setdefault(name, TableDef(name))
            items, i = _paren_items(tokens, i)
            for item in items:
                if not item or _constraint(table, item):
                    continue
                column = item[0].name
                rest = [t.text.upper() for t in item[1:]]
                kind = []
                for k, t in enumerate(item[1:], 1):
                    if t.kind in ("word", "ident") and t.text.upper() not in (
                        "NOT",
                        "NULL",
                        "PRIMARY",
                        "CONSTRAINT",
                        "DEFAULT",
                        "IDENTITY",
                        "REFERENCES",
                        "CHECK",
                        "COLLATE",
                    ):
                        kind.append(t.name.upper())
                        continue
                    if (
                        t.text == "(" and kind
                    ):  # type parameters: NUMERIC(10, 2), nvarchar (20)
                        end = next(
                            j for j in range(k, len(item)) if item[j].text == ")"
                        )
                        kind[-1] += (
                            "("
                            + ", ".join(
                                p.text for p in item[k + 1 : end] if p.text != ","
                            )
                            + ")"
                        )
                    break
                table.columns[column] = " ".join(kind)
                if (
                    any(
                        rest[k] == "NOT" and k + 1 < len(rest) and rest[k + 1] == "NULL"
                        for k in range(len(rest))
                    )
                    or "PRIMARY" in rest
                ):
                    table.not_null.add(column)
                if "PRIMARY" in rest:
                    table.primary_key = (column,)
                if "REFERENCES" in rest:
                    ref = rest.index("REFERENCES") + 1
                    parent, k = _qualified_name(item, ref + 1)
                    pcols = (
                        _paren_items(item, k)[0]
                        if k < len(item) and item[k].text == "("
                        else []
                    )
                    table.foreign_keys.append(
                        ((column,), parent, tuple(c[0].name for c in pcols))
                    )
            continue
        if (
            token.is_word("ALTER")
            and i + 1 < len(tokens)
            and tokens[i + 1].is_word("TABLE")
        ):
            i += 2
            if tokens[i].is_word("ONLY"):
                i += 1
            name, i = _qualified_name(tokens, i)
            if i < len(tokens) and tokens[i].is_word("ADD") and name in tables:
                j = i + 1
                depth, item = 0, []
                while j < len(tokens) and not (
                    depth == 0
                    and (
                        tokens[j].text == ";"
                        or tokens[j].is_word("GO", "ON", "ALTER", "CREATE", "INSERT")
                    )
                ):
                    depth += tokens[j].text == "("
                    depth -= tokens[j].text == ")"
                    item.append(tokens[j])
                    j += 1
                _constraint(tables[name], item)
                if tables[name].primary_key:
                    tables[name].not_null.update(tables[name].primary_key)
                i = j
            continue
        i += 1
    for table in tables.values():
        table.not_null.update(table.primary_key)
    return tables


def read_views(text: str) -> dict[str, str]:
    """``CREATE VIEW name AS body`` statements of an upstream script, by name (body as written)."""

    views = {}
    for match in re.finditer(
        r'(?ims)^create\s+view\s+("([^"]+)"|\[([^\]]+)\]|(\w+))\s+as\s*\n(.*?)(?=^\s*go\s*$|\Z)',
        text,
    ):
        name = match.group(2) or match.group(3) or match.group(4)
        views[name] = match.group(5).strip()
    return views


# ---------------------------------------------------------------- adapters

DUCK_TYPES = {
    "INT64": "BIGINT",
    "STRING": "VARCHAR",
    "FLOAT64": "DOUBLE",
    "DATETIME": "TIMESTAMP",
    "BYTES": "BLOB",
    "BOOL": "BOOLEAN",
    "DATE": "DATE",
}


def duck_type(bigquery_type: str) -> str:
    match = re.fullmatch(
        r"(NUMERIC|BIGNUMERIC)\s*(?:\((\d+),\s*(\d+)\))?", bigquery_type
    )
    if match:
        return f"DECIMAL({match.group(2) or 38}, {match.group(3) or 9})"
    return DUCK_TYPES[bigquery_type]


def prover_type(bigquery_type: str) -> str:
    return bigquery_type.split("(")[0].strip()


class Adapter:
    """One sample database. Subclasses set the class attributes and, where needed, ``convert``."""

    name: str = ""
    title: str = ""
    upstream: tuple[Upstream, ...] = ()
    data_file: str = ""  # the upstream file holding the DDL and the INSERT statements
    #: row counts upstream publishes, and where (checked in addition to the rows the script inserts)
    published_counts: dict[str, int] = {}
    published_counts_source: str = ""
    #: upstream table name -> adapted name, when they differ
    renames: dict[str, str] = {}
    #: (BigQuery SQL, expected rows) that upstream asserts about its data
    assertions: tuple[tuple[str, list[tuple]], ...] = ()

    @property
    def folder(self) -> Path:
        return FIXTURES / self.name

    def upstream_text(self) -> str:
        return (self.folder / self.data_file).read_text(encoding="utf-8")

    def schema(self) -> dict[str, TableDef]:
        """The adapted BigQuery DDL."""

        return read_ddl(
            (self.folder / "adapted" / "schema.sql").read_text(encoding="utf-8")
        )

    def workload(self) -> list[dict]:
        path = self.folder / "workload.json"
        return (
            json.loads(path.read_text(encoding="utf-8"))["queries"]
            if path.exists()
            else []
        )

    def pairs(self) -> list[dict]:
        path = self.folder / "pairs.json"
        return (
            json.loads(path.read_text(encoding="utf-8"))["pairs"]
            if path.exists()
            else []
        )

    # -- converting upstream literals

    def parse_datetime(self, text: str) -> _dt.datetime:
        return _dt.datetime.fromisoformat(text)

    def convert(self, table: str, column: str, kind: str, raw: Raw):
        if raw.kind == "null":
            return None
        base = prover_type(kind)
        if base == "INT64":
            return int(Decimal(raw.text))
        if base in ("NUMERIC", "BIGNUMERIC"):
            return Decimal(raw.text)
        if base == "FLOAT64":
            return float(raw.text)
        if base == "DATETIME":
            return self.parse_datetime(raw.text)
        if base == "DATE":
            return self.parse_datetime(raw.text).date()
        if base == "BYTES":
            return bytes.fromhex(raw.text) if raw.kind == "hex" else raw.text.encode()
        if base == "BOOL":
            return raw.text not in ("0", "f", "false")
        return raw.text

    def rows(self) -> dict[str, list[tuple]]:
        """Every upstream row, converted to the adapted column order and types."""

        schema = self.schema()
        out: dict[str, list[tuple]] = {name: [] for name in schema}
        for upstream_table, inserted in read_inserts(self.upstream_text()).items():
            table = self.renames.get(upstream_table, upstream_table)
            spec = schema[table]
            order = list(spec.columns)
            for columns, values in inserted:
                names = columns or tuple(order)
                if len(names) != len(values):
                    raise ValueError(
                        f"{table}: {len(values)} values for {len(names)} columns"
                    )
                given = dict(zip(names, values))
                row = []
                for column in order:
                    raw = given.get(column, Raw("null", None))
                    row.append(self.convert(table, column, spec.columns[column], raw))
                out[table].append(tuple(row))
        return out

    # -- DuckDB

    def connect(self, rows: dict[str, list[tuple]] | None = None):
        """An in-memory DuckDB holding the full database (and the adapter's views)."""

        import duckdb

        schema = self.schema()
        rows = self.rows() if rows is None else rows
        con = duckdb.connect(":memory:")
        con.execute("SET threads=1")
        for table in schema.values():
            con.execute(create_table_sql(table))
            insert_all(con, table, rows.get(table.name, []))
        for name, sql in self.views():
            con.execute(f'CREATE VIEW "{name}" AS {to_duckdb(sql)}')
        return con

    def views(self) -> list[tuple[str, str]]:
        """Views to create after loading, as (name, BigQuery SQL): workload queries other queries read."""

        return [(q["name"], q["sql"]) for q in self.workload() if q.get("create_view")]

    # -- prover inputs

    def prover_schema(self) -> tuple[dict, dict]:
        schema = self.schema()
        columns = {t.name: list(t.columns) for t in schema.values()}
        types = {
            t.name.lower(): {c.lower(): prover_type(k) for c, k in t.columns.items()}
            for t in schema.values()
        }
        return columns, types

    def constraints(self, drop: tuple[str, ...] = ()) -> dict:
        """The declared keys as ``TableConstraints`` (lower-case names, as the prover expects), minus ``drop``.

        ``drop`` items: ``not_null:T.c``, ``pk:T``, ``fk:T.c`` (the foreign key on that column).
        """

        from kumosql.smt_equivalence import TableConstraints

        out = {}
        for table in self.schema().values():
            not_null = {
                c for c in table.not_null if f"not_null:{table.name}.{c}" not in drop
            }
            keys = (
                (table.primary_key,)
                if table.primary_key and f"pk:{table.name}" not in drop
                else ()
            )
            fks = tuple(
                (cols, parent, pcols)
                for cols, parent, pcols in table.foreign_keys
                if not any(f"fk:{table.name}.{c}" in drop for c in cols)
            )
            out[table.name.lower()] = TableConstraints(
                not_null=frozenset(c.lower() for c in not_null),
                keys=tuple(tuple(c.lower() for c in k) for k in keys),
                foreign_keys=tuple(
                    (
                        tuple(c.lower() for c in cols),
                        parent.lower(),
                        tuple(c.lower() for c in pcols),
                    )
                    for cols, parent, pcols in fks
                ),
            )
        return out


def create_table_sql(table: TableDef) -> str:
    columns = ", ".join(
        f'"{c}" {duck_type(k)}' + (" NOT NULL" if c in table.not_null else "")
        for c, k in table.columns.items()
    )
    return f'CREATE TABLE "{table.name}" ({columns})'


def sql_literal(value) -> str:
    """A DuckDB literal (binding Python parameters row by row is far slower for whole tables)."""

    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, float):
        return repr(value) if value == value else "'nan'::DOUBLE"
    if isinstance(value, _dt.datetime):
        return f"TIMESTAMP '{value.isoformat(sep=' ')}'"
    if isinstance(value, _dt.date):
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, bytes):
        return f"from_hex('{value.hex()}')"
    return "'" + str(value).replace("'", "''") + "'"


def insert_all(con, table: TableDef, rows: list[tuple], chunk: int = 2000) -> None:
    for start in range(0, len(rows), chunk):
        values = ", ".join(
            "(" + ", ".join(sql_literal(v) for v in row) + ")"
            for row in rows[start : start + chunk]
        )
        con.execute(f'INSERT INTO "{table.name}" VALUES {values}')


def to_duckdb(sql: str) -> str:
    return sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]


class Chinook(Adapter):
    name = "chinook"
    title = "Chinook"
    data_file = "upstream/Chinook_Sqlite.sql"
    upstream = (
        Upstream(
            "upstream/Chinook_Sqlite.sql",
            "lerocha/chinook-database",
            "7f67772503d71ba90f19283c38e93923addb43fa",
            "ChinookDatabase/DataSources/Chinook_Sqlite.sql",
            "caf31d698a4a79c628215b552dfe6575e71be052ae02b8f18e763498f55f5d44",
            "MIT-style, Copyright (c) 2008-2024 Luis Rocha (upstream/LICENSE.md)",
        ),
        Upstream(
            "upstream/LICENSE.md",
            "lerocha/chinook-database",
            "7f67772503d71ba90f19283c38e93923addb43fa",
            "LICENSE.md",
            "5064d720db431474b0a0b0cef9d2b5e362b6c6f80602b35c6443b07cbc979f77",
            "the licence itself",
        ),
    )
    # Asserted by upstream's own test fixture (ChinookDatabase.Test/DatabaseTests/ChinookSqliteFixture.cs
    # at the pinned commit): "SELECT * FROM [Genre]" returns 25 rows, and so on.
    published_counts = {
        "Genre": 25,
        "MediaType": 5,
        "Artist": 275,
        "Album": 347,
        "Track": 3503,
        "Employee": 8,
        "Customer": 59,
        "Invoice": 412,
        "InvoiceLine": 2240,
        "Playlist": 18,
        "PlaylistTrack": 8715,
    }
    published_counts_source = "ChinookDatabase.Test/DatabaseTests/ChinookSqliteFixture.cs (upstream test fixture)"
    # More assertions of the same fixture: the last row of each table, and every invoice total equal
    # to the sum of its lines.
    assertions = (
        (
            "SELECT GenreId, Name FROM Genre ORDER BY GenreId DESC LIMIT 1",
            [(25, "Opera")],
        ),
        (
            "SELECT MediaTypeId, Name FROM MediaType ORDER BY MediaTypeId DESC LIMIT 1",
            [(5, "AAC audio file")],
        ),
        (
            "SELECT ArtistId, Name FROM Artist ORDER BY ArtistId DESC LIMIT 1",
            [(275, "Philip Glass Ensemble")],
        ),
        (
            "SELECT AlbumId, Title, ArtistId FROM Album ORDER BY AlbumId DESC LIMIT 1",
            [(347, "Koyaanisqatsi (Soundtrack from the Motion Picture)", 275)],
        ),
        (
            "SELECT TrackId, Name, AlbumId, MediaTypeId, GenreId, Composer, Milliseconds, Bytes, CAST(UnitPrice AS STRING) FROM Track ORDER BY TrackId DESC LIMIT 1",
            [
                (
                    3503,
                    "Koyaanisqatsi",
                    347,
                    2,
                    10,
                    "Philip Glass",
                    206005,
                    3305164,
                    "0.99",
                )
            ],
        ),
        (
            "SELECT EmployeeId, LastName, FirstName, Title, ReportsTo, City FROM Employee ORDER BY EmployeeId DESC LIMIT 1",
            [(8, "Callahan", "Laura", "IT Staff", 6, "Lethbridge")],
        ),
        (
            "SELECT COUNT(*) FROM (SELECT Invoice.InvoiceId, ROUND(SUM(InvoiceLine.UnitPrice * InvoiceLine.Quantity), 2) AS CalculatedTotal, Invoice.Total AS Total "
            "FROM InvoiceLine INNER JOIN Invoice ON InvoiceLine.InvoiceId = Invoice.InvoiceId GROUP BY Invoice.InvoiceId, Invoice.Total) WHERE CalculatedTotal <> Total",
            [(0,)],
        ),
    )


class Northwind(Adapter):
    name = "northwind"
    title = "Northwind"
    data_file = "upstream/instnwnd.sql"
    upstream = (
        Upstream(
            "upstream/instnwnd.sql",
            "microsoft/sql-server-samples",
            "beaab06ef72831089ca80e5355d65e661fd19b26",
            "samples/databases/northwind-pubs/instnwnd.sql",
            "3cc62b3fca6d244a47dbde698b809331e4f85988a0685b2b370717d431e94871",
            "MIT, Copyright (c) Microsoft Corporation (upstream/license.txt)",
        ),
        Upstream(
            "upstream/license.txt",
            "microsoft/sql-server-samples",
            "beaab06ef72831089ca80e5355d65e661fd19b26",
            "license.txt",
            "201f3705979e932c92cd3c6ddbddf4dc3c71dcc5a82089a1e4018227cfe1cf09",
            "the licence itself",
        ),
    )
    # Northwind publishes no row counts; these are the counts every Northwind distribution documents,
    # and they equal the INSERT statements of the pinned script (checked separately).
    published_counts = {
        "Categories": 8,
        "Customers": 91,
        "Employees": 9,
        "Order Details": 2155,
        "Orders": 830,
        "Products": 77,
        "Shippers": 3,
        "Suppliers": 29,
        "Region": 4,
        "Territories": 53,
        "EmployeeTerritories": 49,
        "CustomerCustomerDemo": 0,
        "CustomerDemographics": 0,
    }
    published_counts_source = (
        "the documented Northwind counts (830 orders, 2,155 order lines, ...)"
    )

    def parse_datetime(self, text: str) -> _dt.datetime:
        # the script runs under SET DATEFORMAT mdy: '7/4/1996'
        return _dt.datetime.strptime(text.strip(), "%m/%d/%Y")

    def convert(self, table, column, kind, raw):
        value = super().convert(table, column, kind, raw)
        if isinstance(value, str) and table in (
            "Region",
            "Territories",
            "CustomerCustomerDemo",
            "CustomerDemographics",
        ):
            return value.rstrip()  # nchar padding (see the adapted DDL)
        return value


ADAPTERS: dict[str, Adapter] = {a.name: a for a in (Chinook(), Northwind())}


# ---------------------------------------------------------------- load checks


def check_database(adapter: Adapter, con=None) -> dict:
    """Pins, tables, columns, keys and row counts against upstream; keys against the real rows."""

    problems: list[str] = []
    for pin in adapter.upstream:
        digest = sha256(adapter.folder / pin.local)
        if digest != pin.sha256:
            problems.append(
                f"{pin.local}: SHA-256 {digest} is not the pinned {pin.sha256}"
            )
    upstream_text = adapter.upstream_text()
    upstream = {
        adapter.renames.get(n, n): t for n, t in read_ddl(upstream_text).items()
    }
    adapted = adapter.schema()
    if set(upstream) != set(adapted):
        problems.append(
            f"tables differ: upstream only {sorted(set(upstream) - set(adapted))}, adapted only {sorted(set(adapted) - set(upstream))}"
        )
    declared = {"primary_keys": 0, "foreign_keys": 0, "not_null": 0}
    for name, table in adapted.items():
        source = upstream.get(name)
        if source is None:
            continue
        if list(source.columns) != list(table.columns):
            problems.append(
                f"{name}: columns {list(table.columns)} are not upstream's {list(source.columns)}"
            )
        if source.not_null != table.not_null:
            problems.append(
                f"{name}: NOT NULL {sorted(table.not_null)} is not upstream's {sorted(source.not_null)}"
            )
        if source.primary_key != table.primary_key:
            problems.append(
                f"{name}: primary key {table.primary_key} is not upstream's {source.primary_key}"
            )
        ups = sorted(
            (c, adapter.renames.get(p, p), pc) for c, p, pc in source.foreign_keys
        )
        if ups != sorted(table.foreign_keys):
            problems.append(
                f"{name}: foreign keys {sorted(table.foreign_keys)} are not upstream's {ups}"
            )
        declared["primary_keys"] += bool(table.primary_key)
        declared["foreign_keys"] += len(table.foreign_keys)
        declared["not_null"] += len(table.not_null)
    inserted = Counter()
    for upstream_table, rows in read_inserts(upstream_text).items():
        inserted[adapter.renames.get(upstream_table, upstream_table)] += len(rows)
    upstream_views = read_views(upstream_text)
    for query in adapter.workload():
        if query["origin"] == "upstream-view" and query["name"] not in upstream_views:
            problems.append(
                f"{query['id']}: upstream has no view named {query['name']!r}"
            )
    own = con is None
    con = adapter.connect() if own else con
    counts = {}
    try:
        for name, table in adapted.items():
            loaded = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            counts[name] = loaded
            if loaded != inserted.get(name, 0):
                problems.append(
                    f"{name}: {loaded} rows loaded, the upstream script inserts {inserted.get(name, 0)}"
                )
            if (
                name in adapter.published_counts
                and loaded != adapter.published_counts[name]
            ):
                problems.append(
                    f"{name}: {loaded} rows loaded, upstream publishes {adapter.published_counts[name]}"
                )
            if table.primary_key:
                cols = ", ".join(f'"{c}"' for c in table.primary_key)
                dupes = con.execute(
                    f'SELECT COUNT(*) FROM (SELECT {cols} FROM "{name}" GROUP BY {cols} HAVING COUNT(*) > 1)'
                ).fetchone()[0]
                if dupes:
                    problems.append(
                        f"{name}: primary key {table.primary_key} has {dupes} duplicated values"
                    )
            for column in table.not_null:
                nulls = con.execute(
                    f'SELECT COUNT(*) FROM "{name}" WHERE "{column}" IS NULL'
                ).fetchone()[0]
                if nulls:
                    problems.append(
                        f"{name}.{column}: {nulls} NULLs in a NOT NULL column"
                    )
            for cols, parent, pcols in table.foreign_keys:
                on = " AND ".join(f'c."{a}" = p."{b}"' for a, b in zip(cols, pcols))
                present = " AND ".join(f'c."{a}" IS NOT NULL' for a in cols)
                orphans = con.execute(
                    f'SELECT COUNT(*) FROM "{name}" AS c WHERE {present} AND NOT EXISTS (SELECT 1 FROM "{parent}" AS p WHERE {on})'
                ).fetchone()[0]
                if orphans:
                    problems.append(
                        f"{name}{cols} -> {parent}{pcols}: {orphans} rows without a parent"
                    )
        checks = list(adapter.assertions) + [
            (q["sql"], [tuple(r) for r in q["expect"]])
            for q in adapter.workload()
            if "expect" in q
        ]
        for sql, expected in checks:
            got = con.execute(to_duckdb(sql)).fetchall()
            if got != expected:
                problems.append(
                    f"upstream asserts {expected} for {sql[:70]!r}, got {got[:3]}"
                )
    finally:
        if own:
            con.close()
    missing = set(adapter.published_counts) - set(adapted)
    if missing:
        problems.append(f"published counts name unknown tables {sorted(missing)}")
    return {
        "database": adapter.name,
        "tables": len(adapted),
        "rows": sum(counts.values()),
        "counts": counts,
        "declared": declared,
        "upstream_views": len(upstream_views),
        "assertions": len(checks),
        "problems": problems,
    }


# ---------------------------------------------------------------- (a) rewrites on the workload

OPTIMIZER_BUDGET_S = 30.0


def optimizer_catalog(adapter: Adapter):
    from kumosql import query_optimizer as qo

    schema = adapter.schema()
    return qo.Catalog(
        columns={
            t.name.lower(): [c.lower() for c in t.columns] for t in schema.values()
        },
        types={
            t.name.lower(): {c.lower(): prover_type(k) for c, k in t.columns.items()}
            for t in schema.values()
        },
        not_null={
            t.name.lower(): {c.lower() for c in t.not_null} for t in schema.values()
        },
        keys={
            t.name.lower(): [tuple(c.lower() for c in t.primary_key)]
            if t.primary_key
            else []
            for t in schema.values()
        },
    )


def _confirm_difference(con, control_sql: str, treated_sql: str) -> bool:
    """Rerun a difference with DuckDB's optimizer off (``run_unoptimized``); it counts only if it persists."""

    import engine_suites as es
    from kumosql.duckdb_load import run_unoptimized

    try:
        control, treated = run_unoptimized(
            con, to_duckdb(control_sql), to_duckdb(treated_sql)
        )
    except Exception:  # noqa: BLE001 - the treated query does not run at all: a real failure
        return True
    return es.multiset(control) != es.multiset(treated)


def _case_row(adapter: Adapter, query: dict, case, stage: str) -> dict:
    return {
        "id": f"{query['id']}#{case.variant}#{stage}",
        "query": query["id"],
        "database": adapter.name,
        "origin": query["origin"],
        "variant": case.variant,
        "stage": stage,
        "status": case.status,
        "detail": case.detail,
        "rules": case.rules_changed,
        "verification": case.verification,
        "held_out": held_out(f"{adapter.name}:{query['id']}"),
        "ms": round(case.transform_ms, 1),
        "sql": case.sql,
        "treated": case.treated_sql,
    }


def rewrite_cases(job: tuple[str, list[str], bool]) -> list[dict]:
    """Every rewrite stage on the listed workload queries of one database (loads the database once)."""

    import engine_suites as es
    from kumosql import query_optimizer as qo

    name, query_ids, optimizer = job
    adapter = ADAPTERS[name]
    wanted = set(query_ids)
    queries = [q for q in adapter.workload() if q["id"] in wanted]
    con = adapter.connect()
    catalog = optimizer_catalog(adapter) if optimizer else None
    out: list[dict] = []
    try:
        for index, query in enumerate(queries):
            record = es.Record("query", index + 1, query["sql"])
            cases = es.evaluate_query(
                con, record, adapter.name, query["id"], adapter.name, dialect="bigquery"
            )
            plain = cases[0]
            for case in cases:
                if (
                    case.status == "wrong"
                    and case.detail == "result changed"
                    and not _confirm_difference(con, case.sql, case.treated_sql)
                ):
                    case.status, case.detail = (
                        "unsupported",
                        "DuckDB optimizer artefact: no difference with the optimizer off",
                    )
                out.append(_case_row(adapter, query, case, "pipeline"))
            if (
                plain.status == "unsupported"
                and not plain.treated_sql
                and plain.detail != "declined to parse: parse_error"
            ):
                continue  # the control itself did not run: no later stage can be checked
            control_sql = to_duckdb(query["sql"])
            try:
                columns, rows = es._run(con, control_sql)
            except Exception:  # noqa: BLE001
                continue
            control = es.multiset(rows)
            # the one rule outside the canonical order
            lifted = es.CaseResult(
                plain.case_id,
                plain.suite,
                plain.file,
                plain.line,
                plain.key,
                plain.held_out,
                "unsupported",
                sql=query["sql"],
            )
            lifted = es._treat(
                con,
                lifted,
                "plain",
                query["sql"],
                control,
                len(columns),
                rules=("lift_subqueries",),
            )
            if (
                lifted.status == "wrong"
                and lifted.detail == "result changed"
                and not _confirm_difference(con, lifted.sql, lifted.treated_sql)
            ):
                lifted.status, lifted.detail = (
                    "unsupported",
                    "DuckDB optimizer artefact: no difference with the optimizer off",
                )
            out.append(_case_row(adapter, query, lifted, "lift_subqueries"))
            if not optimizer:
                continue
            # the proof-gated optimizer, with the declared keys and NOT NULL columns
            case = es.CaseResult(
                plain.case_id,
                plain.suite,
                plain.file,
                plain.line,
                plain.key,
                plain.held_out,
                "declined",
                sql=query["sql"],
            )
            started = time.perf_counter()
            try:
                outcome = qo.optimize(
                    query["sql"],
                    catalog,
                    dialect="bigquery",
                    timeout_ms=5000,
                    deletion_budget_s=10.0,
                    budget_s=OPTIMIZER_BUDGET_S,
                )
            except Exception as error:  # noqa: BLE001
                case.status, case.detail = (
                    "error",
                    f"{type(error).__name__}: {str(error)[:80]}",
                )
                out.append(_case_row(adapter, query, case, "optimizer"))
                continue
            case.transform_ms = (time.perf_counter() - started) * 1000
            if outcome.sql:
                case.status, case.treated_sql, case.rules_changed, case.verification = (
                    "transformed",
                    outcome.sql,
                    list(outcome.steps),
                    "proven",
                )
                try:
                    treated_columns, treated_rows = es._run(con, to_duckdb(outcome.sql))
                    if (
                        treated_columns is None
                        or len(treated_columns) != len(columns)
                        or es.multiset(treated_rows) != control
                    ):
                        if _confirm_difference(con, query["sql"], outcome.sql):
                            case.status, case.detail = "wrong", "result changed"
                        else:
                            case.status, case.detail = (
                                "unsupported",
                                "DuckDB optimizer artefact: no difference with the optimizer off",
                            )
                except Exception as error:  # noqa: BLE001
                    case.status, case.detail = (
                        "wrong",
                        f"treated query no longer runs: {str(error)[:80]}",
                    )
            else:
                case.detail = outcome.reason[:120]
            out.append(_case_row(adapter, query, case, "optimizer"))
    finally:
        con.close()
    return out


def run_rewrites(
    adapters: list[Adapter],
    jobs: int = 1,
    optimizer: bool = True,
    query_ids: set[str] | None = None,
) -> list[dict]:
    work = []
    for adapter in adapters:
        ids = [
            q["id"]
            for q in adapter.workload()
            if query_ids is None or q["id"] in query_ids
        ]
        chunk = max(1, -(-len(ids) // max(1, jobs)))
        work += [
            (adapter.name, ids[k : k + chunk], optimizer)
            for k in range(0, len(ids), chunk)
        ]
    if jobs > 1 and len(work) > 1:
        with ProcessPoolExecutor(jobs) as pool:
            return [row for rows in pool.map(rewrite_cases, work) for row in rows]
    return [row for job in work for row in rewrite_cases(job)]


def summarize_rewrites(rows: list[dict]) -> dict:
    def block(selection: list[dict]) -> dict:
        counts = Counter(r["status"] for r in selection)
        return {
            "cases": len(selection),
            "queries": len({(r["database"], r["query"]) for r in selection}),
            "executed": counts["transformed"] + counts["declined"] + counts["wrong"],
            "transformed": counts["transformed"] + counts["wrong"],
            "verified": counts["transformed"],
            "declined": counts["declined"],
            "unsupported": counts["unsupported"],
            "error": counts["error"],
            "timeout": counts["timeout"],
            "wrong": counts["wrong"],
            "wrong_but_proven": sum(
                1
                for r in selection
                if r["status"] == "wrong" and r["verification"] == "proven"
            ),
        }

    upstream = [r for r in rows if r["origin"] != "authored"]
    return {
        "all": block(rows),
        "upstream": block(upstream),
        "authored": block([r for r in rows if r["origin"] == "authored"]),
        "plain": block([r for r in rows if r["variant"] == "plain"]),
        "amplified": block([r for r in rows if r["variant"] != "plain"]),
        "dev": block([r for r in rows if not r["held_out"]]),
        "held_out": block([r for r in rows if r["held_out"]]),
        "by_database": {
            d: block([r for r in rows if r["database"] == d])
            for d in sorted({r["database"] for r in rows})
        },
        "by_origin": {
            o: block([r for r in rows if r["origin"] == o])
            for o in sorted({r["origin"] for r in rows})
        },
        "by_stage": {
            s: block([r for r in rows if r["stage"] == s])
            for s in sorted({r["stage"] for r in rows})
        },
        "by_rule": dict(
            Counter(
                rule
                for r in rows
                if r["status"] == "transformed"
                for rule in r["rules"]
            )
        ),
        "wrong_cases": [
            {k: r[k] for k in ("id", "detail", "verification", "sql", "treated")}
            for r in rows
            if r["status"] == "wrong"
        ],
        "unsupported_causes": dict(
            Counter(
                r["detail"][:70] for r in rows if r["status"] == "unsupported"
            ).most_common(12)
        ),
        "error_causes": dict(
            Counter(
                r["detail"][:70] for r in rows if r["status"] == "error"
            ).most_common(12)
        ),
    }


# ---------------------------------------------------------------- (b) equivalent pairs and their siblings

PROVER_TIMEOUT_MS = 5000
BOUNDED_ROWS = 2


def _typed(value, kind: str):
    """A prover or witness value as the column's Python value."""

    if value is None:
        return None
    base = prover_type(kind)
    if base == "INT64":
        return (
            int(value)
            if not isinstance(value, str) or value.lstrip("-").isdigit()
            else 0
        )
    if base in ("NUMERIC", "BIGNUMERIC"):
        return Decimal(str(value))
    if base == "FLOAT64":
        return float(value)
    if base == "DATETIME":
        if isinstance(value, _dt.datetime):
            return value
        if isinstance(value, _dt.date):
            return _dt.datetime(value.year, value.month, value.day)
        if isinstance(value, (int, float)):
            return _dt.datetime(2000, 1, 1) + _dt.timedelta(days=int(value))
        return _dt.datetime.fromisoformat(str(value))
    if base == "BYTES":
        return value if isinstance(value, bytes) else str(value).encode()
    return value if isinstance(value, str) else str(value)


def _filler(kind: str, n: int, unique: bool):
    base = prover_type(kind)
    if base == "INT64":
        return 900_000 + n if unique else 0
    if base in ("NUMERIC", "BIGNUMERIC"):
        return Decimal(900_000 + n) if unique else Decimal(0)
    if base == "FLOAT64":
        return float(900_000 + n) if unique else 0.0
    if base == "DATETIME":
        return _dt.datetime(2000, 1, 1) + _dt.timedelta(days=n if unique else 0)
    if base == "BYTES":
        return f"k{n}".encode() if unique else b""
    return f"k{n}" if unique else "x"


def complete_database(
    adapter: Adapter, partial: dict, drop: tuple[str, ...] = ()
) -> tuple[dict[str, list[tuple]], list[str]]:
    """A full database from partial rows (``{table: [{column: value}]}``, any case).

    Columns the rows leave out get legal values: a fresh value for a key column, a type default for a
    NOT NULL column, NULL otherwise; a foreign key value with no parent gets a parent row. Returns the
    rows and the declared constraints (minus ``drop``) the result still violates (none, normally).
    """

    schema = adapter.schema()
    lookup = {t.lower(): t for t in schema}
    rows: dict[str, list[dict]] = {t: [] for t in schema}
    counter = [0]

    def fresh(kind):
        counter[0] += 1
        return _filler(kind, counter[0], True)

    def fill(table: str, given: dict) -> dict:
        spec = schema[table]
        cols = {c.lower(): c for c in spec.columns}
        values = {
            cols[k.lower()]: _typed(v, spec.columns[cols[k.lower()]])
            for k, v in given.items()
            if k.lower() in cols
        }
        key = set(spec.primary_key) if f"pk:{table}" not in drop else set()
        not_null = {c for c in spec.not_null if f"not_null:{table}.{c}" not in drop}
        for column, kind in spec.columns.items():
            if column in values:
                continue
            if column in key:
                values[column] = fresh(kind)
            elif column in not_null:
                values[column] = _filler(kind, 0, False)
            else:
                values[column] = None
        return values

    for name, items in partial.items():
        table = lookup[name.lower()]
        rows[table] += [fill(table, item) for item in items]
    for _ in range(6):  # parents for foreign keys, then the parents' own parents
        added = False
        for table, spec in schema.items():
            for cols, parent, pcols in spec.foreign_keys:
                if any(f"fk:{table}.{c}" in drop for c in cols):
                    continue
                have = {tuple(r[p] for p in pcols) for r in rows[parent]}
                for row in list(rows[table]):
                    value = tuple(row[c] for c in cols)
                    if None in value or value in have:
                        continue
                    rows[parent].append(fill(parent, dict(zip(pcols, value))))
                    have.add(value)
                    added = True
        if not added:
            break
    data = {
        t: [tuple(r[c] for c in schema[t].columns) for r in items]
        for t, items in rows.items()
    }
    return data, violations(adapter, data, drop)


def violations(
    adapter: Adapter, data: dict[str, list[tuple]], drop: tuple[str, ...] = ()
) -> list[str]:
    out = []
    schema = adapter.schema()
    for table, spec in schema.items():
        names = list(spec.columns)
        rows = data.get(table, [])
        for column in spec.not_null:
            if f"not_null:{table}.{column}" not in drop and any(
                r[names.index(column)] is None for r in rows
            ):
                out.append(f"{table}.{column} is NULL")
        if spec.primary_key and f"pk:{table}" not in drop:
            keys = [tuple(r[names.index(c)] for c in spec.primary_key) for r in rows]
            if len(keys) != len(set(keys)):
                out.append(f"{table} repeats a key")
        for cols, parent, pcols in spec.foreign_keys:
            if any(f"fk:{table}.{c}" in drop for c in cols):
                continue
            pnames = list(schema[parent].columns)
            have = {
                tuple(p[pnames.index(c)] for c in pcols) for p in data.get(parent, [])
            }
            for r in rows:
                value = tuple(r[names.index(c)] for c in cols)
                if None not in value and value not in have:
                    out.append(f"{table}{cols} = {value} has no {parent} row")
    return out


def separates(
    adapter: Adapter, data: dict[str, list[tuple]], left: str, right: str
) -> bool | None:
    """Do the two queries return different bags on this database? (``None``: a query does not run.)"""

    import duckdb
    import engine_suites as es

    con = duckdb.connect(":memory:")
    try:
        for table in adapter.schema().values():
            con.execute(create_table_sql(table).replace(" NOT NULL", ""))
            insert_all(con, table, data.get(table.name, []))
        try:
            a, b = (
                con.execute(to_duckdb(left)).fetchall(),
                con.execute(to_duckdb(right)).fetchall(),
            )
        except Exception:  # noqa: BLE001
            return None
        if es.multiset(a) == es.multiset(b):
            return False
        return _confirm_difference(con, left, right)
    finally:
        con.close()


def _counterexample_rows(result) -> dict:
    """The prover's counterexample as ``{table: [{column: value}]}``."""

    ce = result.counterexample
    return {table: [dict(r) for r in rows] for table, rows in (ce.tables or {}).items()}


def _bounded_rows(adapter: Adapter, data: dict[str, list[tuple]]) -> dict:
    schema = {t.lower(): spec for t, spec in adapter.schema().items()}
    out = {}
    for table, rows in data.items():
        spec = schema[table.lower()]
        out[spec.name] = [dict(zip(spec.columns, row)) for row in rows]
    return out


def decide_pairs(job: tuple[str, list[dict]]) -> list[dict]:
    """Decide the listed pairs of one database (loading the real database once)."""

    name, pairs = job
    con = ADAPTERS[name].connect()
    try:
        return [decide_pair(name, pair, con) for pair in pairs]
    finally:
        con.close()


def decide_pair(name: str, pair: dict, con) -> dict:
    """Run one pair through the provers and check every answer on the real data (``con``) and on its counterexample."""

    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    from kumosql.bounded_equivalence import check_bounded, schema_from_prover
    from kumosql.equivalence import prove_equivalent
    from kumosql.smt_equivalence import SmtStatus

    adapter = ADAPTERS[name]
    drop = tuple(pair.get("drop", ()))
    left, right = pair["left"], pair["right"]
    columns, types = adapter.prover_schema()
    lower = {t.lower(): [c.lower() for c in cols] for t, cols in columns.items()}
    constraints = adapter.constraints(drop)
    started = time.time()
    row = {
        "id": pair["id"],
        "database": name,
        "label": pair["label"],
        "category": pair.get("category", ""),
        "drop": list(drop),
        "held_out": held_out(f"{name}:{pair['id']}"),
        "outcome": "unknown",
        "how": "",
        "wrong": "",
        "real_differs": None,
        "witness_ok": None,
        "counterexample_ok": None,
    }
    # the label's own evidence: a witness that separates the pair on a legal database
    import engine_suites as es

    try:
        a, b = (
            con.execute(to_duckdb(left)).fetchall(),
            con.execute(to_duckdb(right)).fetchall(),
        )
        row["real_differs"] = es.multiset(a) != es.multiset(b) and _confirm_difference(
            con, left, right
        )
    except Exception as error:  # noqa: BLE001 - a pair that does not run on its own database is not scored
        row["outcome"], row["how"] = (
            "unsupported",
            f"does not run in DuckDB: {str(error)[:80]}",
        )
        return row
    witness = pair.get("witness")
    if pair["label"] == "different":
        if witness == "real":
            row["witness_ok"] = bool(row["real_differs"])
        elif isinstance(witness, dict):
            data, broken = complete_database(adapter, witness, drop)
            row["witness_ok"] = not broken and bool(
                separates(adapter, data, left, right)
            )
        else:
            row["witness_ok"] = False
    elif row["real_differs"]:
        row["witness_ok"] = (
            False  # an "equivalent" pair the real data separates: the label is wrong
        )
    # 1. the structural prover, 2. the algebraic/SMT prover with the declared constraints
    proof = None
    try:
        if prove_equivalent(left, right).proven:
            proof = "structural"
    except Exception:  # noqa: BLE001 - a crash is never a proof
        pass
    result = None
    if proof is None:
        try:
            result = prove_equivalent_algebraic(
                left,
                right,
                schema=lower,
                types=types,
                constraints=constraints,
                dialect="bigquery",
                search_counterexample=True,
                timeout_ms=PROVER_TIMEOUT_MS,
            )
        except Exception as error:  # noqa: BLE001
            row["how"] = f"algebraic prover crashed: {type(error).__name__}"
        if result is not None and result.status is SmtStatus.PROVEN_EQUIVALENT:
            proof = "algebraic"
    if proof:
        row["outcome"], row["how"] = "proven", proof
        if pair["label"] == "different":
            row["wrong"] = "proved a pair labelled different"
        if row["real_differs"]:
            row["wrong"] = "proved a pair that differs on the real data"
    else:
        counterexample, how = None, ""
        if (
            result is not None
            and result.status is SmtStatus.NOT_EQUIVALENT
            and result.counterexample is not None
        ):
            counterexample, how = (
                _counterexample_rows(result),
                "algebraic counterexample",
            )
        else:
            # 3. the bounded checker, for a counterexample only (a bounded "same" is no proof)
            try:
                bounded = check_bounded(
                    left,
                    right,
                    schema_from_prover(lower, constraints, types),
                    rows=BOUNDED_ROWS,
                    dialect="bigquery",
                    timeout_ms=PROVER_TIMEOUT_MS,
                )
                if bounded.counterexample is not None:
                    counterexample, how = (
                        _bounded_rows(adapter, bounded.counterexample),
                        "bounded counterexample",
                    )
            except Exception as error:  # noqa: BLE001
                row["how"] = f"bounded checker crashed: {type(error).__name__}"
        if counterexample is not None:
            data, broken = complete_database(adapter, counterexample, drop)
            replayed = None if broken else separates(adapter, data, left, right)
            row["counterexample_ok"] = bool(replayed)
            row["counterexample"] = {
                t: [list(map(str, r)) for r in rs] for t, rs in data.items() if rs
            }
            if replayed:
                row["outcome"], row["how"] = "refuted", how
                if pair["label"] == "equivalent":
                    row["wrong"] = "refuted a pair labelled equivalent (replayed)"
            else:
                row["outcome"], row["how"] = (
                    "unknown",
                    f"{how} did not replay" + (f": {broken[0]}" if broken else ""),
                )
                row["wrong"] = "counterexample does not replay on a legal database"
        elif not row["how"]:
            row["how"] = result.reason[:100] if result is not None else ""
    row["seconds"] = round(time.time() - started, 2)
    return row


def run_pairs(
    adapters: list[Adapter], jobs: int = 1, pair_ids: set[str] | None = None
) -> list[dict]:
    work = []
    for adapter in adapters:
        chosen = [p for p in adapter.pairs() if pair_ids is None or p["id"] in pair_ids]
        chunk = max(1, -(-len(chosen) // max(1, jobs)))
        work += [
            (adapter.name, chosen[k : k + chunk]) for k in range(0, len(chosen), chunk)
        ]
    if jobs > 1 and len(work) > 1:
        with ProcessPoolExecutor(jobs) as pool:
            return [row for rows in pool.map(decide_pairs, work) for row in rows]
    return [row for job in work for row in decide_pairs(job)]


def summarize_pairs(rows: list[dict]) -> dict:
    def block(selection: list[dict]) -> dict:
        eq = [r for r in selection if r["label"] == "equivalent"]
        ne = [r for r in selection if r["label"] == "different"]
        return {
            "pairs": len(selection),
            "equivalent": len(eq),
            "different": len(ne),
            "proven": sum(r["outcome"] == "proven" for r in eq),
            "refuted": sum(r["outcome"] == "refuted" for r in ne),
            "unknown": sum(r["outcome"] == "unknown" for r in selection),
            "unsupported": sum(r["outcome"] == "unsupported" for r in selection),
            "constraint_siblings": sum(bool(r["drop"]) for r in selection),
            "constraint_siblings_refuted": sum(
                bool(r["drop"]) and r["outcome"] == "refuted" for r in selection
            ),
            "wrong": sum(bool(r["wrong"]) for r in selection),
            "labels_unverified": sum(r["witness_ok"] is False for r in selection),
        }

    return {
        "all": block(rows),
        "dev": block([r for r in rows if not r["held_out"]]),
        "held_out": block([r for r in rows if r["held_out"]]),
        "by_database": {
            d: block([r for r in rows if r["database"] == d])
            for d in sorted({r["database"] for r in rows})
        },
        "by_category": {
            c: block([r for r in rows if r["category"] == c])
            for c in sorted({r["category"] for r in rows})
        },
        "wrong_cases": [
            {k: r.get(k) for k in ("id", "wrong", "how", "counterexample")}
            for r in rows
            if r["wrong"]
        ],
        "unverified_labels": [r["id"] for r in rows if r["witness_ok"] is False],
    }


# ---------------------------------------------------------------- results files

COMMAND = "python tools/sample_db_bench.py --write-results"


def _pins_text(adapters: list[Adapter]) -> str:
    return "; ".join(
        f"{a.title} {a.upstream[0].repo}@{a.upstream[0].commit[:10]} {a.upstream[0].path} (SHA-256 {a.upstream[0].sha256[:12]}, {a.upstream[0].licence.rsplit(' (', 1)[0]})"
        for a in adapters
    )


def results_rows(
    adapters: list[Adapter], reports: list[dict], rewrites: dict, pairs: dict
) -> dict[str, dict]:
    from bench_common import today

    loaded = ", ".join(
        f"{r['database']} {r['tables']} tables / {r['rows']:,} rows" for r in reports
    )
    keys = (
        sum(r["declared"]["primary_keys"] for r in reports),
        sum(r["declared"]["foreign_keys"] for r in reports),
    )
    a, up, au, dev, ho = (
        rewrites[k] for k in ("all", "upstream", "authored", "dev", "held_out")
    )
    plain = rewrites["plain"]
    stages = rewrites["by_stage"]
    rows = {
        "sample-databases-rewrites": {
            "suite": "Sample databases (Chinook, Northwind): workload rewrites",
            "order": 340,
            "size": a["cases"],
            "score": f"{a['wrong']} wrong in {a['executed']} executed; {a['verified']} rewrites verified on the real data",
            "metric": (
                f"{plain['queries']} workload queries ({up['queries']} upstream, adapted to BigQuery; {au['queries']} authored), each through "
                "KumoSQL's canonical rule pipeline (plain and wrapped/padded variants, as the engine-suites eval), lift_subqueries and the "
                "proof-gated query optimizer with the declared keys, then run on the full database in DuckDB; a rewrite that changes the "
                "result multiset or stops running is wrong."
            ),
            "evidence": "executed",
            "correctness": (
                f"{a['wrong']} behaviour-changing rewrites ({a['wrong_but_proven']} of them labelled proven); every rewrite is compared with an "
                "unrewritten control on the real Chinook and Northwind data, a difference confirmed with DuckDB's optimizer off"
            ),
            "coverage": {
                "proven": a["verified"],
                "unknown": a["declined"],
                "unsupported": a["unsupported"],
                "timeout": a["timeout"],
                "error": a["error"],
            },
            "usefulness": (
                f"{a['transformed']} of {a['executed']} executed cases changed by a rewrite: pipeline {stages.get('pipeline', {}).get('transformed', 0)}, "
                f"lift_subqueries {stages.get('lift_subqueries', {}).get('transformed', 0)}, optimizer {stages.get('optimizer', {}).get('transformed', 0)}; "
                f"upstream queries {up['wrong']} wrong / {up['transformed']} changed, authored {au['wrong']} wrong / {au['transformed']} changed"
            ),
            "held_out": f"dev {dev['wrong']} wrong / {dev['transformed']} changed; held out (a fifth of the queries by SHA-1 of their id) {ho['wrong']} wrong / {ho['transformed']} changed of {ho['executed']} executed",
            "docs": "docs/evals/sample-databases.md",
            "command": COMMAND,
            "date": today(),
            "caveats": (
                f"Pinned: {_pins_text(adapters)}. Loaded and checked against upstream: {loaded}, {keys[0]} primary and {keys[1]} foreign keys. "
                "Northwind's 16 views and 7 stored procedures (parameters bound) and Chinook's 2 test-fixture queries are adapted from T-SQL/SQLite "
                "to BigQuery (each adaptation recorded in the workload file); the authored queries were written for this eval. In coverage, 'proven' "
                "counts rewrites whose executed result matched the control and 'unknown' counts cases no rule changed. No rule was changed for this eval."
            ),
        },
    }
    p, pdev, pho = pairs["all"], pairs["dev"], pairs["held_out"]
    rows["sample-databases-pairs"] = {
        "suite": "Sample databases (Chinook, Northwind): equivalent pairs and siblings",
        "order": 341,
        "size": p["pairs"],
        "score": f"{p['proven']}/{p['equivalent']} equivalent proved, {p['refuted']}/{p['different']} different refuted (replayed), {p['wrong']} wrong",
        "metric": (
            "Authored pairs on the two schemas under their declared keys, labelled equivalent or different (each 'different' label has a witness "
            f"database); {p['constraint_siblings']} siblings drop one key, foreign key or NOT NULL the equivalence needs. Provers: structural, "
            "algebraic/SMT with the declared constraints and its counterexample search, then the bounded checker for a counterexample."
        ),
        "evidence": "proof",
        "correctness": (
            f"{p['wrong']} wrong: every proof also gives the same result on the real data and no proof is of a pair labelled different; every "
            "refutation's counterexample, completed with legal values, satisfies the declared constraints and separates the pair when replayed in DuckDB"
        ),
        "coverage": {
            "proven": p["proven"],
            "refuted": p["refuted"],
            "unknown": p["unknown"],
            "unsupported": p["unsupported"],
        },
        "held_out": f"{pho['proven']}/{pho['equivalent']} proved, {pho['refuted']}/{pho['different']} refuted, {pho['wrong']} wrong (a fifth of the pairs by SHA-1 of their id)",
        "docs": "docs/evals/sample-databases.md",
        "command": COMMAND,
        "date": today(),
        "caveats": (
            f"Authored pairs ({p['pairs']}; dev {pdev['pairs']}, held out {pho['pairs']}), not from upstream. "
            f"Constraint siblings refuted: {p['constraint_siblings_refuted']}/{p['constraint_siblings']}. The algebraic prover's executed counterexample search "
            "is time-limited, so on a loaded machine a refutation can fall back to unknown (ch-filter-vs-conditional-count: refuted on an idle "
            "machine, unknown in the recorded run). No prover was changed for this eval; the first run is the baseline."
        ),
    }
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database", choices=sorted(ADAPTERS), action="append")
    parser.add_argument(
        "--check",
        action="store_true",
        help="only load every database and check it against upstream",
    )
    parser.add_argument("--part", choices=("rewrites", "pairs", "both"), default="both")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument(
        "--query", action="append", help="only these workload query ids"
    )
    parser.add_argument("--pair", action="append", help="only these pair ids")
    parser.add_argument(
        "--no-optimizer",
        action="store_true",
        help="skip the proof-gated optimizer stage (slow)",
    )
    parser.add_argument("--json", help="write every case to this file")
    parser.add_argument(
        "--show", action="store_true", help="print every transformed case"
    )
    parser.add_argument(
        "--write-results",
        action="store_true",
        help="write benchmarks/results/sample-databases-*.json (every database, both parts)",
    )
    args = parser.parse_args(argv)
    if args.write_results and (
        args.database
        or args.query
        or args.pair
        or args.part != "both"
        or args.no_optimizer
    ):
        parser.error(
            "--write-results needs the full run: every database, both parts, no --query/--pair/--no-optimizer"
        )
    from bench_common import quiet

    quiet()
    adapters = [ADAPTERS[n] for n in (args.database or sorted(ADAPTERS))]
    failed = False
    reports = []
    for adapter in adapters:
        started = time.time()
        report = check_database(adapter)
        reports.append(report)
        print(
            f"{adapter.name}: {report['tables']} tables, {report['rows']} rows, declared {report['declared']}, "
            f"{report['upstream_views']} upstream views, {report['assertions']} upstream assertions ({time.time() - started:.1f}s)"
        )
        for problem in report["problems"]:
            print("  PROBLEM", problem)
        failed |= bool(report["problems"])
    if args.check:
        return 1 if failed else 0
    dump: dict = {"databases": reports}
    if args.part in ("rewrites", "both"):
        started = time.time()
        rows = run_rewrites(
            adapters,
            args.jobs,
            optimizer=not args.no_optimizer,
            query_ids=set(args.query) if args.query else None,
        )
        summary = summarize_rewrites(rows)
        summary["seconds"] = round(time.time() - started, 1)
        dump["rewrites"] = {"summary": summary, "cases": rows}
        for key in (
            "all",
            "upstream",
            "authored",
            "plain",
            "amplified",
            "dev",
            "held_out",
        ):
            print(f"rewrites {key:9} {summary[key]}")
        for stage, block in summary["by_stage"].items():
            print(f"  stage {stage:16} {block}")
        print(f"  by rule {summary['by_rule']}")
        print(f"  unsupported {summary['unsupported_causes']}")
        print(f"  errors {summary['error_causes']}")
        for row in rows:
            if row["status"] == "wrong" or (
                args.show and row["status"] == "transformed"
            ):
                print(
                    f"\n{row['id']} {row['status']} {row['detail']} [{row['verification']}]\n  {row['sql']}\n  -> {row['treated']}"
                )
        failed |= summary["all"]["wrong"] > 0
    if args.part in ("pairs", "both"):
        started = time.time()
        rows = run_pairs(
            adapters, args.jobs, pair_ids=set(args.pair) if args.pair else None
        )
        summary = summarize_pairs(rows)
        summary["seconds"] = round(time.time() - started, 1)
        dump["pairs"] = {"summary": summary, "cases": rows}
        for key in ("all", "dev", "held_out"):
            print(f"pairs {key:9} {summary[key]}")
        for row in rows:
            flag = f" WRONG: {row['wrong']}" if row["wrong"] else ""
            label = "" if row["witness_ok"] is not False else " (label unverified)"
            print(
                f"  {row['id']:52} {row['label']:10} {row['outcome']:8} {row['how'][:60]}{flag}{label}"
            )
        failed |= summary["all"]["wrong"] > 0
    if args.json:
        Path(args.json).write_text(
            json.dumps(dump, indent=1, default=str), encoding="utf-8"
        )
    if args.write_results:
        from bench_common import write_results

        written = results_rows(
            adapters, reports, dump["rewrites"]["summary"], dump["pairs"]["summary"]
        )
        for index, (name, row) in enumerate(written.items()):
            write_results(name, row, scoreboard=index == len(written) - 1)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
