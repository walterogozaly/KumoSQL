"""Execution-based regression eval: run other engines' test suites through KumoSQL.

Every query in a borrowed test suite is executed in DuckDB, sent through
KumoSQL's rewrite pipeline, and executed again. A rewrite that changes the
result is a behaviour change, and the correctness score must stay 0.

Suites are downloaded on demand (sparse git clones into the cache directory,
``KUMOSQL_SUITES_DIR`` or ``~/.cache/kumosql-suites``); nothing is vendored.

    python tools/engine_suites.py --suite duckdb-slt --jobs 4
    python tools/engine_suites.py --suite duckdb-slt --limit 500 --json out.json

Method, for every ``query`` record of a SQLLogicTest file:

1. The setup statements run natively in a fresh in-memory DuckDB.
2. The query is parsed as DuckDB SQL. Statements that are not one plain
   read-only query (or that mention non-deterministic or environment-reading
   functions) are *unsupported*.
3. **control**: the query is transpiled DuckDB -> BigQuery -> DuckDB without
   any KumoSQL rule and executed. KumoSQL reads BigQuery, so this round trip
   isolates dialect-translation gaps from rewrite behaviour. A control that
   fails, is not repeatable, or returns different columns is *unsupported*.
4. **treated**: the BigQuery text goes through KumoSQL's canonical rule order
   (cleanup, lifting, inlining, distinct removal, formatting), is transpiled
   back and executed. An unchanged tree is *declined*, a different tree is
   *transformed*, and an exception inside the pipeline is an *error*.
5. A transformed query whose result multiset differs from the control (or
   that no longer runs) is *wrong*. Wrong must be 0. Wrong cases that the
   prover labelled ``proven`` are reported separately, as prover failures.

Cases are keyed by (normalised query, hash of the file's setup), so identical
cases shared between suites are counted once and keep every provenance.
Files whose path hashes to 0 mod 4 are held out: rules are only developed
against the other files, and both splits are reported.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import hashlib
import json
import logging
import multiprocessing
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import threading
import time

import sqlglot
from sqlglot import exp

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kumosql import apply_rules
from kumosql.rewrite import canonical_rule_order

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

CACHE = Path(os.environ.get("KUMOSQL_SUITES_DIR", Path.home() / ".cache" / "kumosql-suites"))
import tempfile

WORKDIR = tempfile.mkdtemp(prefix="kumosql-suites-")
QUERY_TIMEOUT_S = 5.0
FILE_BUDGET_S = float(os.environ.get("KUMOSQL_SUITES_FILE_BUDGET", "900"))
HANG_LIMIT_S = 900.0
HOLDOUT_MODULUS = 4

SUITES = {
    "duckdb-slt": {
        "repo": "https://github.com/duckdb/duckdb.git",
        "paths": ["test/sql"],
        "licence": "MIT, Copyright Stichting DuckDB Foundation",
        "dialect": "duckdb",
    },
    "sqlite-slt": {
        # SQuaLity's source for the SQLite SQLLogicTest corpus (a mirror of sqlite.org/sqllogictest)
        "repo": "https://github.com/gregrahn/sqllogictest.git",
        "paths": ["test"],
        "licence": "SQLite sqllogictest, public domain (as collected by SQuaLity, MIT)",
        "dialect": "sqlite",
    },
    "sqlglot-fixtures": {
        # SQLGlot's optimizer, qualification, simplification and identity fixtures; TPC-H/TPC-DS ship data
        "repo": "https://github.com/tobymao/sqlglot.git",
        "paths": ["tests/fixtures"],
        "licence": "MIT, Copyright Toby Mao",
        "dialect": "mixed",
        "kind": "sqlglot",
    },
}
MAX_QUERIES_PER_FILE = int(os.environ.get("KUMOSQL_SUITES_MAX_QUERIES", "0")) or None

NONDETERMINISTIC = re.compile(
    r"\b(random|uuid|gen_random_uuid|now|current_(timestamp|date|time|user|schema|catalog|database)|"
    r"nextval|currval|setseed|sleep|getenv|version|pragma_|duckdb_[a-z_]+|read_[a-z_]+|glob|"
    r"epoch_ms|today|get_current_[a-z_]+|txid_current|current_setting|query_start_time|"
    r"checkpoint|pg_[a-z_]+|information_schema|system_[a-z_]+)\b",
    re.IGNORECASE,
)
READ_ONLY_ROOTS = (exp.Select, exp.Union, exp.Subquery, exp.Intersect, exp.Except)


# ---------------------------------------------------------------- suite access


def fetch_suite(name: str) -> Path:
    """Sparse-clone a suite into the cache (once) and return its checkout."""

    spec = SUITES[name]
    target = CACHE / name
    if not (target / ".git").exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse", spec["repo"], str(target)],
            check=True,
        )
        subprocess.run(["git", "-C", str(target), "sparse-checkout", "set", *spec["paths"]], check=True)
    return target


def checkout_revision(path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


# ---------------------------------------------------------------- SLT parsing


@dataclass
class Record:
    kind: str  # statement | query
    line: int
    sql: str
    expect_error: bool = False
    sort_mode: str = "nosort"
    ncols: int = 0
    expected: list[str] | None = None  # flattened expected values (None when hashed / absent)
    skip: bool = False


def _expand_loops(lines: list[str]) -> list[str] | None:
    """Expand ``loop``/``foreach`` blocks textually; ``None`` when the file nests them awkwardly."""

    out: list[str] = []
    i = 0
    while i < len(lines):
        parts = lines[i].split()
        if parts and parts[0] in ("loop", "foreach", "concurrentloop", "concurrentforeach"):
            depth = 1
            j = i + 1
            while j < len(lines) and depth:
                head = lines[j].split()[:1]
                if head and head[0] in ("loop", "foreach", "concurrentloop", "concurrentforeach"):
                    depth += 1
                elif head and head[0] in ("endloop", "endforeach"):
                    depth -= 1
                j += 1
            if depth:
                return None
            body = lines[i + 1 : j - 1]
            if parts[0].endswith("loop"):
                if len(parts) < 4:
                    return None
                try:
                    values = [str(v) for v in range(int(parts[2]), int(parts[3]))]
                except ValueError:
                    return None
            else:
                values = parts[2:]
            if len(values) > 12:
                values = values[:12]
            expanded_body = _expand_loops(body)
            if expanded_body is None:
                return None
            for value in values:
                out.extend(line.replace("${" + parts[1] + "}", value) for line in expanded_body)
            i = j
            continue
        out.append(lines[i])
        i += 1
    return out


def parse_slt(text: str) -> tuple[list[Record], str | None]:
    """Parse a SQLLogicTest file into records, or return why the file is skipped."""

    lines = text.splitlines()
    expanded = _expand_loops(lines)
    if expanded is None:
        return [], "loops"
    lines = expanded
    records: list[Record] = []
    i = 0
    pending_skip = False
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        head = stripped.split()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        word = head[0]
        if word == "require":
            if len(head) > 1 and head[1] in ("skip_reload", "notwindows", "no_extension_autoloading", "noforcestorage"):
                i += 1
                continue
            return [], "require " + (head[1] if len(head) > 1 else "")
        if word in ("skipif", "onlyif"):
            engine = head[1] if len(head) > 1 else ""
            if (word == "skipif" and engine == "duckdb") or (word == "onlyif" and engine != "duckdb"):
                pending_skip = True
            i += 1
            continue
        if word in ("mode", "hash-threshold", "test-env", "set", "tags", "group", "sleep", "unzip", "load", "restart", "reconnect"):
            if word in ("load", "restart", "reconnect", "unzip", "test-env"):
                return [], word
            i += 1
            continue
        if word == "halt":
            break
        if word in ("statement", "query"):
            start = i
            i += 1
            sql_lines = []
            while i < len(lines) and lines[i].strip() != "----" and lines[i].strip() != "":
                sql_lines.append(lines[i])
                i += 1
            sql = "\n".join(sql_lines).strip()
            expected: list[str] | None = None
            expect_error = word == "statement" and len(head) > 1 and head[1] == "error"
            if i < len(lines) and lines[i].strip() == "----":
                i += 1
                body = []
                while i < len(lines) and lines[i].strip() != "":
                    body.append(lines[i])
                    i += 1
                if word == "statement":
                    pass
                elif len(body) == 1 and re.match(r"^\d+ values hashing to", body[0].strip()):
                    expected = None
                else:
                    expected = [cell for line in body for cell in line.split("\t")]
            record = Record(word, start + 1, sql, expect_error=expect_error, expected=expected, skip=pending_skip)
            if word == "query":
                record.ncols = len(head[1]) if len(head) > 1 else 0
                record.sort_mode = head[2] if len(head) > 2 and head[2] in ("rowsort", "valuesort", "nosort") else "nosort"
            pending_skip = False
            records.append(record)
            continue
        if word in ("endloop", "endforeach"):
            i += 1
            continue
        return [], "unknown directive " + word
    return records, None


# ---------------------------------------------------------------- execution


def _connect():
    import duckdb

    connection = duckdb.connect(":memory:")
    for pragma in ("SET threads=1", "SET autoinstall_known_extensions=false", "SET autoload_known_extensions=false"):
        try:
            connection.execute(pragma)
        except Exception:
            pass
    return connection


def _run(connection, sql: str):
    timer = threading.Timer(QUERY_TIMEOUT_S, connection.interrupt)
    timer.start()
    try:
        cursor = connection.execute(sql)
        if cursor.description is None:
            return None, None
        columns = tuple(column[0] for column in cursor.description)
        rows = [tuple(row) for row in cursor.fetchall()]
        return columns, rows
    finally:
        timer.cancel()


def _norm(value):
    if isinstance(value, float):
        if value != value:
            return ("nan",)
        return ("f", float(f"{value:.9g}"))
    if isinstance(value, (list, tuple)):
        return ("l", tuple(_norm(v) for v in value))
    if isinstance(value, dict):
        return ("d", tuple(sorted((str(k), _norm(v)) for k, v in value.items())))
    if value is None:
        return ("null",)
    return (type(value).__name__, str(value))


def multiset(rows) -> Counter:
    return Counter(tuple(_norm(v) for v in row) for row in rows)


def _slt_token(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value if value != "" else "(empty)"
    if isinstance(value, float):
        return f"{value:.3f}" if value == value else "NaN"
    return str(value)


def matches_expected(record: Record, rows) -> bool | None:
    if record.expected is None:
        return None
    actual = [_slt_token(v) for row in rows for v in row]
    expected = list(record.expected)
    if record.sort_mode != "nosort":
        actual.sort()
        expected.sort()

    def same(a: str, b: str) -> bool:
        if a == b:
            return True
        try:
            x, y = float(a), float(b)
        except ValueError:
            return False
        return abs(x - y) <= 1e-3 * max(1.0, abs(x), abs(y))

    return len(actual) == len(expected) and all(same(a, b) for a, b in zip(actual, expected))


# ---------------------------------------------------------------- case evaluation


@dataclass
class CaseResult:
    case_id: str
    suite: str
    file: str
    line: int
    key: str
    held_out: bool
    status: str  # transformed | declined | unsupported | error | wrong
    detail: str = ""
    verification: str = ""
    expected_match: bool | None = None
    transform_ms: float = 0.0
    rules_changed: list[str] = field(default_factory=list)
    sql: str = ""
    treated_sql: str = ""
    variant: str = "plain"


def _held_out(path: str) -> bool:
    return int(hashlib.sha256(path.encode()).hexdigest(), 16) % HOLDOUT_MODULUS == 0


def _case_key(normalised: str, setup_hash: str) -> str:
    return hashlib.sha256((setup_hash + "\0" + normalised).encode()).hexdigest()[:16]


def _single_query(sql: str, dialect: str = "duckdb") -> exp.Expression | None:
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except Exception:
        return None
    statements = [s for s in statements if s is not None]
    if len(statements) != 1 or not isinstance(statements[0], READ_ONLY_ROOTS + (exp.With,)):
        return None
    tree = statements[0]
    if not (tree.find(exp.Select) or isinstance(tree, exp.Union)):
        return None
    return tree


def evaluate_query(
    connection,
    record: Record,
    suite: str,
    file: str,
    setup_hash: str,
    dialect: str = "duckdb",
    expected_sql: str | None = None,
) -> list[CaseResult]:
    held = _held_out(file)
    case = CaseResult(f"{suite}:{file}:{record.line}", suite, file, record.line, "", held, "unsupported")
    case.sql = record.sql
    tree = _single_query(record.sql, dialect)
    if tree is None:
        case.detail = "not a single read-only query"
        return [case]
    if NONDETERMINISTIC.search(record.sql):
        case.detail = "non-deterministic or environment-reading"
        return [case]
    case.key = _case_key(tree.sql(dialect="duckdb"), setup_hash)
    try:
        bigquery_sql = tree.sql(dialect="bigquery")
        control_sql = sqlglot.transpile(bigquery_sql, read="bigquery", write="duckdb")[0]
    except Exception as exc:
        case.detail = f"dialect round trip: {type(exc).__name__}"
        return [case]
    try:
        base_columns, base_rows = _run(connection, record.sql if dialect == "duckdb" else tree.sql(dialect="duckdb"))
    except Exception as exc:
        case.detail = f"original failed: {str(exc)[:60]}"
        return [case]
    if base_columns is None:
        case.detail = "no result set"
        return [case]
    case.expected_match = matches_expected(record, base_rows)
    if expected_sql is not None:
        try:
            _, fixture_rows = _run(connection, sqlglot.transpile(expected_sql, read=dialect, write="duckdb")[0])
            case.expected_match = multiset(fixture_rows) == multiset(base_rows)
        except Exception:
            case.expected_match = None
    try:
        control_columns, control_rows = _run(connection, control_sql)
        again_columns, again_rows = _run(connection, control_sql)
    except Exception as exc:
        case.detail = f"control failed: {str(exc)[:60]}"
        return [case]
    if control_columns is None or len(control_columns) != len(base_columns):
        case.detail = "control changed the column count"
        return [case]
    control_set = multiset(control_rows)
    if control_set != multiset(again_rows):
        case.detail = "not repeatable"
        return [case]
    if control_set != multiset(base_rows):
        case.detail = "dialect round trip changed the result"
        return [case]

    results = [_treat(connection, case, "plain", bigquery_sql, control_set, len(control_columns))]
    for label, variant_sql in _variants(tree):
        variant = CaseResult(f"{case.case_id}#{label}", suite, file, record.line, _case_key(label + tree.sql(dialect="duckdb"), setup_hash), held, "unsupported")
        variant.variant = label
        variant.sql = variant_sql
        try:
            variant_control = sqlglot.transpile(variant_sql, read="bigquery", write="duckdb")[0]
            columns, rows = _run(connection, variant_control)
        except Exception as exc:
            variant.detail = f"variant failed: {str(exc)[:50]}"
            results.append(variant)
            continue
        if columns is None or len(columns) != len(control_columns) or multiset(rows) != control_set:
            variant.detail = "variant is not equivalent to the query"
            results.append(variant)
            continue
        results.append(_treat(connection, variant, label, variant_sql, control_set, len(control_columns)))
    return results


def _variants(tree: exp.Expression) -> list[tuple[str, str]]:
    """Equivalent rewrites of a query that give KumoSQL's rules something to do."""

    body = tree.sql(dialect="bigquery")
    variants = [
        ("wrap-subquery", f"SELECT * FROM ({body}) AS _s"),
        ("wrap-cte", f"WITH _q AS ({body}) SELECT * FROM _q"),
        ("unused-cte", f"WITH _u AS (SELECT 1 AS z), _q AS ({body}) SELECT * FROM _q"),
    ]
    if isinstance(tree, exp.Select) and not tree.args.get("with_") and not tree.args.get("with"):
        padded = tree.copy().where("1 = 1 AND (TRUE)", append=True)
        variants.append(("trivial-predicate", padded.sql(dialect="bigquery")))
    return variants


def _unparenthesised_original_fails(connection, original: str) -> bool:
    """Does the original, with only its parentheses dropped, already fail to parse in DuckDB?"""

    try:
        tree = sqlglot.parse_one(original, read="bigquery")
        for paren in list(tree.find_all(exp.Paren)):
            paren.replace(paren.this)
        _run(connection, tree.sql(dialect="duckdb"))
    except Exception as exc:
        return "Parser Error" in str(exc)
    return False


def _treat(
    connection, case: CaseResult, label: str, bigquery_sql: str, control_set: Counter, ncols: int, rules: tuple[str, ...] | None = None
) -> CaseResult:
    """Run ``rules`` (KumoSQL's canonical order by default) on the query and compare the result with the control."""

    started = time.perf_counter()
    try:
        result = apply_rules(rules or canonical_rule_order(), bigquery_sql)
    except Exception as exc:
        case.status = "error"
        case.detail = f"{type(exc).__name__}: {str(exc)[:80]}"
        return case
    case.transform_ms = (time.perf_counter() - started) * 1000
    case.verification = result.verification.status.value
    case.rules_changed = [step.rule for step in result.steps if step.changes]
    failures = [d for step in result.steps if not step.rule_success for d in step.diagnostics]
    if failures:
        unparsed = all(d.code == "parse_error" for d in failures)
        case.status = "unsupported" if unparsed else "error"
        case.detail = ("declined to parse: " if unparsed else "rule failed: ") + failures[0].code
        return case
    try:
        same_tree = sqlglot.parse_one(result.sql, read="bigquery") == sqlglot.parse_one(bigquery_sql, read="bigquery")
    except Exception:
        same_tree = False
    if same_tree:
        case.status = "declined"
        return case
    case.status = "transformed"
    case.treated_sql = result.sql
    try:
        treated_sql = sqlglot.transpile(result.sql, read="bigquery", write="duckdb")[0]
        treated_columns, treated_rows = _run(connection, treated_sql)
    except Exception as exc:
        if "Parser Error" in str(exc) and _unparenthesised_original_fails(connection, bigquery_sql):
            # the rewrite only dropped parentheses that BigQuery does not need; sqlglot's
            # DuckDB writer then emitted text DuckDB cannot read, which is not KumoSQL's doing
            case.status = "unsupported"
            case.detail = "translation artifact: DuckDB writer needs the parentheses"
            return case
        case.status = "wrong"
        case.detail = f"treated query no longer runs: {str(exc)[:80]}"
        return case
    if treated_columns is None or len(treated_columns) != ncols or multiset(treated_rows) != control_set:
        case.status = "wrong"
        case.detail = "result changed"
    return case


DEFAULT_SCHEMA = {
    "x": {"a": "INT", "b": "INT"},
    "y": {"b": "INT", "c": "INT"},
    "z": {"b": "INT", "c": "INT"},
    "w": {"d": "TEXT", "e": "TEXT"},
}


def parse_fixture(text: str, identity: bool) -> list[tuple[dict, str, str | None, int]]:
    """Read a SQLGlot fixture into (metadata, sql, expected sql or None, line) items."""

    items: list[tuple[dict, str, int]] = []
    meta: dict = {}
    buffer: list[str] = []
    start = 0
    for number, line in enumerate(text.splitlines(), 1):
        if not buffer:
            if line.startswith("--") or not line.strip():
                if not line.strip():
                    meta = {}
                continue
            match = re.match(r"# (\w+): ?(.*)$", line)
            if match:
                meta[match.group(1)] = match.group(2).strip()
                continue
            if line.startswith("#"):
                continue
            start = number
        if identity:
            if line.strip():
                items.append((dict(meta), line.strip(), number))
            continue
        buffer.append(line)
        if line.rstrip().endswith(";"):
            items.append((dict(meta), "\n".join(buffer).strip().rstrip(";"), start))
            buffer = []
            if not buffer:
                meta = items[-1][0] if len(items) % 2 else {}
    if identity:
        return [(m, q, None, n) for m, q, n in items]
    paired = []
    for index in range(0, len(items) - 1, 2):
        paired.append((items[index][0], items[index][1], items[index + 1][1], items[index][2]))
    return paired


def _synthetic_connection(schema: dict):
    connection = _connect()
    for table, columns in schema.items():
        definitions = []
        for name, kind in columns.items():
            kind = str(kind).upper()
            definitions.append(f'"{name}" {"VARCHAR" if any(t in kind for t in ("CHAR", "TEXT", "STRING")) else "BIGINT"}')
        try:
            connection.execute(f'CREATE TABLE "{table}" ({", ".join(definitions)})')
        except Exception:
            continue
        for row in range(6):
            values = []
            for index, (name, kind) in enumerate(columns.items()):
                pool = [1, 2, 3, None, 0, 2]
                value = pool[(row + index) % 6]
                if any(t in str(kind).upper() for t in ("CHAR", "TEXT", "STRING")):
                    value = None if value is None else "abc"[value % 3]
                values.append("NULL" if value is None else (f"'{value}'" if isinstance(value, str) else str(value)))
            try:
                connection.execute(f'INSERT INTO "{table}" VALUES ({", ".join(values)})')
            except Exception:
                break
    return connection


def _tpc_connection(directory: Path):
    connection = _connect()
    for data in sorted(directory.glob("*.csv.gz")):
        table = data.name.split(".")[0]
        connection.execute(f"CREATE TABLE {table} AS SELECT * FROM read_csv('{data}', delim='|', header=true)")
    return connection


def run_sqlglot_fixture(args: tuple[str, str, str]) -> list[CaseResult]:
    suite, root, relative = args
    path = Path(root) / relative
    text = path.read_text(encoding="utf-8")
    identity = path.name == "identity.sql"
    has_data = any(path.parent.glob("*.csv.gz"))
    results: list[CaseResult] = []
    shared = _tpc_connection(path.parent) if has_data else None
    default = None if has_data else _synthetic_connection(DEFAULT_SCHEMA)
    queries = 0
    for meta, sql, expected, line in parse_fixture(text, identity):
        if MAX_QUERIES_PER_FILE and queries >= MAX_QUERIES_PER_FILE:
            break
        queries += 1
        dialect = meta.get("dialect") or ("duckdb" if has_data else "")
        connection = shared or default
        own = None
        if meta.get("schema") and not has_data:
            try:
                own = connection = _synthetic_connection(json.loads(meta["schema"]))
            except Exception:
                connection = default
        record = Record("query", line, sql)
        try:
            results.extend(
                evaluate_query(connection, record, suite, relative, "tpc" if has_data else "synthetic" + meta.get("schema", ""), dialect or None, expected)
            )
        except Exception as exc:
            results.append(CaseResult(f"{suite}:{relative}:{line}", suite, relative, line, "", _held_out(relative), "unsupported", f"harness: {type(exc).__name__}", sql=sql))
        if own is not None:
            own.close()
    for connection in (shared, default):
        if connection is not None:
            connection.close()
    return results


def run_file_safe(args: tuple[str, str, str]) -> tuple[str, list[CaseResult]]:
    return args[2], run_file(args)


def run_file(args: tuple[str, str, str]) -> list[CaseResult]:
    # suites write scratch files relative to the working directory
    os.chdir(WORKDIR)
    if SUITES[args[0]].get("kind") == "sqlglot":
        return run_sqlglot_fixture(args)
    suite, root, relative = args
    path = Path(root) / relative
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []
    records, skipped = parse_slt(text)
    if skipped:
        return [CaseResult(f"{suite}:{relative}", suite, relative, 0, "", _held_out(relative), "file-skipped", skipped)]
    connection = _connect()
    results: list[CaseResult] = []
    setup_hash = hashlib.sha256()
    queries = 0
    file_started = time.time()
    try:
        for record in records:
            if record.skip:
                continue
            if record.kind == "statement":
                setup_hash.update(record.sql.encode())
                try:
                    _run(connection, record.sql)
                except Exception:
                    pass
                continue
            if not record.sql:
                continue
            if MAX_QUERIES_PER_FILE and queries >= MAX_QUERIES_PER_FILE:
                continue
            queries += 1
            if time.time() - file_started > FILE_BUDGET_S:
                results.append(CaseResult(f"{suite}:{relative}:{record.line}", suite, relative, record.line, "", _held_out(relative), "timeout", "file time budget", sql=record.sql))
                continue
            try:
                results.extend(evaluate_query(connection, record, suite, relative, setup_hash.hexdigest()))
            except Exception as exc:  # the harness must never take the whole run down
                results.append(
                    CaseResult(f"{suite}:{relative}:{record.line}", suite, relative, record.line, "", _held_out(relative), "unsupported", f"harness: {type(exc).__name__}", sql=record.sql)
                )
            # a query may have left an aborted transaction behind
            try:
                connection.execute("ROLLBACK")
            except Exception:
                pass
    finally:
        try:
            connection.close()
        except Exception:
            pass
    return results


# ---------------------------------------------------------------- reporting


def summarise(cases: list[CaseResult]) -> dict:
    seen: dict[str, CaseResult] = {}
    provenance: dict[str, list[str]] = defaultdict(list)
    skipped_files: Counter = Counter()
    for case in cases:
        if case.status == "file-skipped":
            skipped_files[case.detail] += 1
            continue
        if case.key:
            provenance[case.key].append(case.case_id)
            seen.setdefault(case.key, case)
        else:
            seen[case.case_id] = case
    unique = list(seen.values())

    def block(selection: list[CaseResult]) -> dict:
        counts = Counter(c.status for c in selection)
        executed = [c for c in selection if c.status in ("transformed", "declined", "wrong")]
        times = sorted(c.transform_ms for c in selection if c.transform_ms)
        prover_failures = [c.case_id for c in selection if c.status == "wrong" and c.verification == "proven"]
        return {
            "cases": len(selection),
            "executed": len(executed),
            "transformed": counts["transformed"] + counts["wrong"],
            "declined": counts["declined"],
            "unsupported": counts["unsupported"],
            "timeout": counts["timeout"],
            "error": counts["error"],
            "wrong": counts["wrong"],
            "wrong_but_proven": len(prover_failures),
            "median_ms": round(statistics.median(times), 1) if times else None,
            "p95_ms": round(times[int(len(times) * 0.95) - 1], 1) if len(times) > 20 else None,
        }

    return {
        "all": block(unique),
        "plain": block([c for c in unique if c.variant == "plain"]),
        "amplified": block([c for c in unique if c.variant != "plain"]),
        "dev": block([c for c in unique if not c.held_out]),
        "held_out": block([c for c in unique if c.held_out]),
        "duplicates_collapsed": len(cases) - len(unique) - sum(skipped_files.values()),
        "files_skipped": dict(skipped_files),
        "files_hung": sum(1 for c in cases if c.status == "timeout" and c.detail == "file hung"),
        "expected_match": dict(Counter(str(c.expected_match) for c in unique if c.status in ("transformed", "declined", "wrong"))),
        "by_rule": dict(Counter(rule for c in unique for rule in c.rules_changed)),
        "wrong_cases": [
            {"id": c.case_id, "detail": c.detail, "verification": c.verification, "sql": c.sql, "treated": c.treated_sql}
            for c in unique
            if c.status == "wrong"
        ],
        "error_causes": dict(Counter(c.detail[:60] for c in unique if c.status == "error").most_common(10)),
    }


SUITE_TITLES = {
    "duckdb-slt": "DuckDB SQLLogicTest",
    "sqlglot-fixtures": "SQLGlot fixtures",
    "sqlite-slt": "SQLite SQLLogicTest (SQuaLity)",
}


def scoreboard_rows(summary: dict, results_dir: Path) -> list[Path]:
    """Write benchmarks/results rows: original queries and adapted (amplified) variants apart."""

    suite = summary["suite"]
    written = []
    for part, label, order_offset in (("plain", "original queries", 0), ("amplified", "adapted variants", 1)):
        block = summary[part]
        if not block["cases"]:
            continue
        # transformed = the rewrite changed the query and the result was compared, so a verified
        # transformation is a transformed case that was not scored wrong
        verified = block["transformed"] - block["wrong"]
        row = {
            "suite": f"{SUITE_TITLES[suite]}, {label}",
            "order": {"duckdb-slt": 300, "sqlglot-fixtures": 310, "sqlite-slt": 320}[suite] + order_offset,
            "size": block["cases"],
            "score": f"{block['wrong']} wrong in {block['executed']} executed; {verified} rewrites verified",
            "metric": (
                "Each query runs in DuckDB, goes through every KumoSQL rewrite, and runs again; "
                "a rewrite that changes the result multiset (or stops running) is wrong."
                + (" Variants wrap or pad each query so the rules have work to do." if part == "amplified" else "")
            ),
            "evidence": "executed",
            "correctness": (
                f"{block['wrong']} behaviour-changing rewrites ({block['wrong_but_proven']} of them labelled proven); "
                "results compared against an unrewritten control run in the same database"
            ),
            "coverage": {
                "proven": verified,
                "unknown": block["declined"],
                "unsupported": block["unsupported"],
                "timeout": block["timeout"],
                "error": block["error"],
            },
            "usefulness": f"{block['transformed']} of {block['executed']} executed queries changed by a rewrite; each changed result was compared with the control",
            "performance": f"median {block['median_ms']} ms, p95 {block['p95_ms']} ms per query through all rules",
            "held_out": "dev {} wrong / {} transformed; held out (files hashing to 0 mod {}) {} wrong / {} transformed".format(
                summary["dev"]["wrong"], summary["dev"]["transformed"], HOLDOUT_MODULUS, summary["held_out"]["wrong"], summary["held_out"]["transformed"]
            ),
            "docs": "docs/evals/engine-suites.md",
            "command": f"python tools/engine_suites.py --suite {suite}" + SUITE_ARGS.get(suite, ""),
            "date": time.strftime("%Y-%m-%d"),
            "caveats": (
                f"Pinned to {summary['revision'][:10]} ({summary['licence']}). "
                "In coverage, 'proven' counts rewrites whose executed result matched the control, and 'unknown' counts queries no rule changed. "
                f"{summary['duplicates_collapsed']} duplicate cases collapsed; {sum(summary['files_skipped'].values())} files skipped for unsupported directives."
            ),
        }
        path = results_dir / f"engine-{suite}-{part}.json"
        path.write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written


SUITE_ARGS = {"sqlite-slt": " --stride 4 --max-queries 40"}


def collect_files(suite: str, root: Path, limit: int | None, stride: int) -> list[str]:
    spec = SUITES[suite]
    suffixes = (".sql",) if spec.get("kind") == "sqlglot" else (".test", ".slt", ".test_slow")
    files = sorted(str(p.relative_to(root)) for base in spec["paths"] for p in (root / base).rglob("*") if p.suffix in suffixes)
    files = files[::stride]
    return files[:limit] if limit else files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--suite", choices=sorted(SUITES), default="duckdb-slt")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--limit", type=int, help="cap the number of files")
    parser.add_argument("--stride", type=int, default=1, help="take every Nth file")
    parser.add_argument("--max-queries", type=int, help="cap the queries taken from each file")
    parser.add_argument("--json", help="write the summary here")
    parser.add_argument("--scoreboard", action="store_true", help="also write the benchmarks/results rows")
    args = parser.parse_args(argv)

    global MAX_QUERIES_PER_FILE
    if args.max_queries:
        MAX_QUERIES_PER_FILE = args.max_queries
    root = fetch_suite(args.suite)
    files = collect_files(args.suite, root, args.limit, args.stride)
    started = time.time()
    jobs = [(args.suite, str(root), f) for f in files]
    cases: list[CaseResult] = []
    cache = Path(args.json + ".partial") if args.json else None
    finished: set[str] = set()
    if cache and cache.exists():
        for line in cache.read_text().splitlines():
            record = json.loads(line)
            finished.add(record["file"])
            cases.extend(CaseResult(**c) for c in record["cases"])
        jobs = [j for j in jobs if j[2] not in finished]
    pool = multiprocessing.Pool(args.jobs)
    iterator = pool.imap_unordered(run_file_safe, jobs, chunksize=1)
    done = 0
    while done < len(jobs):
        try:
            file, batch = iterator.next(timeout=HANG_LIMIT_S)
        except multiprocessing.TimeoutError:
            break  # the remaining files are hung; they are reported as timeouts
        done += 1
        finished.add(file)
        cases.extend(batch)
        if cache:
            with cache.open("a") as handle:
                handle.write(json.dumps({"file": file, "cases": [c.__dict__ for c in batch]}) + "\n")
        if done % 200 == 0:
            print(f"  {done}/{len(jobs)} files", file=sys.stderr)
    pool.terminate()
    for _, _, file in jobs:
        if file not in finished:
            cases.append(CaseResult(f"{args.suite}:{file}", args.suite, file, 0, "", _held_out(file), "timeout", "file hung"))
    summary = summarise(cases)
    summary["suite"] = args.suite
    summary["revision"] = checkout_revision(root)
    summary["licence"] = SUITES[args.suite]["licence"]
    summary["files"] = len(files)
    summary["seconds"] = round(time.time() - started, 1)
    text = json.dumps(summary, indent=2, default=str)
    if args.json:
        Path(args.json).write_text(text, encoding="utf-8")
    else:
        print(text)
    if args.scoreboard:
        scoreboard_rows(summary, Path(__file__).resolve().parents[1] / "benchmarks" / "results")
    allb = summary["all"]
    print(
        f"{args.suite}: {allb['transformed']} transformed, {allb['declined']} declined, "
        f"{allb['unsupported']} unsupported, {allb['error']} error, {allb['wrong']} wrong "
        f"({allb['cases']} cases, {summary['seconds']}s)",
        file=sys.stderr,
    )
    return 1 if allb["wrong"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
