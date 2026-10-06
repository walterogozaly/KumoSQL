"""GoogleSQL type-inference eval: does KumoSQL give each output column of a query its exact GoogleSQL type?

Labels come from the GoogleSQL compliance tests (``googlesql/compliance/testdata/*.test`` in google/googlesql,
Apache-2.0, pinned below). Each test prints its expected result as a typed value, ``ARRAY<STRUCT<a INT64, b
STRING>>[...]``, so the header names every output column and its type. ARRAY columns print as ``ARRAY<>`` in the
header and carry their element type on each value (``ARRAY<INT64>[1, 2]``), so those are read from the first value
that has one; a column with no such value is left unlabelled.

Tables come from each file's ``[prepare_database]`` blocks, typed by those blocks' own printed results (a table's
schema, as a BigQuery catalog would give it). Value tables (``SELECT AS VALUE``/``AS STRUCT``) and tables loaded from
protos are left out of the catalog, so queries over them have unknown tables. Functions a file creates are passed as
user-defined; the typer reads the return type from each ``CREATE FUNCTION`` statement the fixture keeps
(``function_sql``; a fixture harvested before that was recorded has only the names, and those functions are unknown).

The split is fixed by file: a file whose ``sha256(stem) % 4 == 0`` is held out (63 of 285 files). Held-out cases are
written to their own fixture and are scored only with ``--heldout``; nothing is developed against them.

Each labelled output column is scored:

* **exact**: KumoSQL's type text equals the label;
* **unknown**: KumoSQL gave no type (or no schema for the query);
* **WRONG**: KumoSQL gave a different type, or a schema of a different width. This must stay at 0.

``bigquery`` columns are the ones whose label uses only BigQuery types (no INT32, UINT32, UINT64, FLOAT32, ENUM,
PROTO, UUID, graph or map types); the headline score is over them.

    python tools/googlesql_types_eval.py --harvest <googlesql>/googlesql/compliance/testdata
    python tools/googlesql_types_eval.py                    # dev, KumoSQL's typer
    python tools/googlesql_types_eval.py --typer sqlglot    # dev, sqlglot's annotate_types (the baseline)
    python tools/googlesql_types_eval.py --failures 20      # list unknown and wrong columns
"""

from __future__ import annotations

import argparse
import logging
import gzip
import hashlib
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)
ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "googlesql_types"
DEV = FIXTURES / "dev.json.gz"
HELDOUT = FIXTURES / "heldout.json.gz"
RESULTS = ROOT / "benchmarks" / "results" / "googlesql-types.json"
SOURCE = {
    "repo": "google/googlesql",
    "commit": "d82db99",
    "path": "googlesql/compliance/testdata",
    "license": "Apache-2.0",
}
SPLIT_RULE = "a file is held out when int(sha256(file stem), 16) % 4 == 0"

BIGQUERY_KINDS = {
    "INT64", "FLOAT64", "NUMERIC", "BIGNUMERIC", "BOOL", "STRING", "BYTES", "DATE", "DATETIME", "TIME",
    "TIMESTAMP", "INTERVAL", "JSON", "GEOGRAPHY", "ARRAY", "STRUCT", "RANGE",
}
_RENAME = {"DOUBLE": "FLOAT64", "FLOAT": "FLOAT32"}
# Every kind a compliance result prints; anything else means the header was misread (field names are printed
# unquoted, so `GROUPING SETS INT64` cannot be read back).
_KNOWN_KINDS = BIGQUERY_KINDS | {
    "INT32", "UINT32", "UINT64", "FLOAT32", "UUID", "TOKENLIST", "PROTO", "ENUM", "MAP", "GRAPH_ELEMENT",
    "GRAPH_PATH", "MEASURE", "TIMESTAMP_PICOS", "VECTOR",
}


def held_out(stem: str) -> bool:
    return int(hashlib.sha256(stem.encode()).hexdigest(), 16) % 4 == 0


# --- labels: the printed type header and values ------------------------------------------------------------------

_TYPE_TOKEN = re.compile(r"\s*(`(?:[^`\\]|\\.)*`|[A-Za-z_$][\w$.]*|<|>|,|\(|\))")


class _TypeText:
    """A tiny parser for printed GoogleSQL types; ``ARRAY<>`` (element not printed) parses to ``("ARRAY", None)``.

    A type is a tuple: ``(kind,)``, ``("ARRAY", element)``, ``("RANGE", element)``, ``("STRUCT", ((name, type), ..))``
    or ``("OTHER", text)`` for kinds that take a parameter this eval never computes (PROTO<x>, ENUM<x>, MAP<..>).
    """

    def __init__(self, text: str):
        self.tokens = []
        pos = 0
        while pos < len(text):
            m = _TYPE_TOKEN.match(text, pos)
            if not m:
                if text[pos:].strip():
                    raise ValueError(f"cannot read type at {text[pos:pos + 20]!r}")
                break
            self.tokens.append(m.group(1))
            pos = m.end()
        self.i = 0

    def peek(self, offset: int = 0):
        return self.tokens[self.i + offset] if self.i + offset < len(self.tokens) else None

    def take(self, expected: str | None = None) -> str:
        token = self.peek()
        if token is None or (expected is not None and token != expected):
            raise ValueError(f"expected {expected!r}, got {token!r}")
        self.i += 1
        return token

    def type(self):
        kind = self.take()
        upper = _RENAME.get(kind.upper(), kind.upper())
        if upper not in _KNOWN_KINDS:
            raise ValueError(f"unknown type {kind!r}")
        if self.peek() != "<":
            return (upper,)
        self.take("<")
        if upper == "ARRAY" or upper == "RANGE":
            if self.peek() == ">":
                self.take(">")
                return (upper, None)
            element = self.type()
            self.take(">")
            return (upper, element)
        if upper == "STRUCT":
            fields = []
            while self.peek() != ">":
                name = None
                if self.peek(1) not in ("<", ",", ">", None):
                    name = self.take().strip("`")
                fields.append((name, self.type()))
                if self.peek() == ",":
                    self.take(",")
            self.take(">")
            return ("STRUCT", tuple(fields))
        depth, parts = 1, []
        while depth:
            token = self.take()
            depth += {"<": 1, ">": -1}.get(token, 0)
            if depth:
                parts.append(token)
        return ("OTHER", f"{upper}<{' '.join(parts)}>")


def parse_type_text(text: str):
    reader = _TypeText(text)
    result = reader.type()
    if reader.peek() is not None:
        raise ValueError(f"trailing text after type: {reader.peek()!r}")
    return result


def type_sql(t) -> str | None:
    """Print a parsed type the way BigQuery does; None if some part is not known."""

    if t is None:
        return None
    kind = t[0]
    if kind in ("ARRAY", "RANGE"):
        inner = type_sql(t[1])
        return None if inner is None else f"{kind}<{inner}>"
    if kind == "STRUCT":
        parts = []
        for name, field in t[1]:
            inner = type_sql(field)
            if inner is None:
                return None
            parts.append(f"{_quote(name)} {inner}" if name else inner)
        return f"STRUCT<{', '.join(parts)}>"
    if kind == "OTHER":
        return t[1]
    return kind


def _quote(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name) else f"`{name}`"


def kinds(t) -> set[str]:
    if t is None:
        return set()
    if t[0] in ("ARRAY", "RANGE"):
        return {t[0]} | kinds(t[1])
    if t[0] == "STRUCT":
        out = {"STRUCT"}
        for _, field in t[1]:
            out |= kinds(field)
        return out
    if t[0] == "OTHER":
        return {t[1].split("<", 1)[0]}
    return {t[0]}


def _scan(text: str):
    """Yield (index, char, depth-before) for characters outside quoted strings; brackets of every kind count."""

    depth, i, n = 0, 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            triple = text[i:i + 3] == c * 3
            quote = c * 3 if triple else c
            j = i + len(quote)
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text.startswith(quote, j):
                    break
                j += 1
            i = j + len(quote)
            continue
        yield i, c, depth
        if c in "{[(<":
            depth += 1
        elif c in "}])>":
            depth -= 1
        i += 1


def split_top(text: str) -> list[str]:
    """Split ``text`` (the inside of a ``{..}`` or ``[..]``) on commas outside any bracket or string."""

    parts, start = [], 0
    for i, c, depth in _scan(text):
        if c == "," and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    tail = text[start:].strip()
    if tail or parts:
        parts.append(tail)
    return parts


def _matching(text: str, open_index: int) -> int:
    """Index of the bracket closing the one at ``open_index``."""

    for i, c, depth in _scan(text[open_index:]):
        if i and depth == 1 and c in "}])>":
            return open_index + i
    raise ValueError("unbalanced brackets in result")


def _strip_order(text: str) -> str:
    return re.sub(r"^\s*(known|unknown) order:", "", text)


def _array_elements(value: str) -> tuple[str | None, list[str]]:
    """For ``ARRAY<T>[a, b]`` / ``ARRAY<T>(NULL)``: (``T`` text, element texts)."""

    value = value.strip()
    if not value.startswith("ARRAY<"):
        return None, []
    close = _matching(value, len("ARRAY"))
    element_text = value[len("ARRAY<"):close]
    rest = value[close + 1:].strip()
    if rest.startswith("["):
        end = _matching(rest, 0)
        return element_text, split_top(_strip_order(rest[1:end]))
    return element_text, []


def fill(t, value: str):
    """``t`` with ``ARRAY<>`` placeholders filled from one printed value of that type, where the value shows them."""

    if t is None or value is None:
        return t
    value = value.strip()
    if value == "NULL":
        return t
    if t[0] == "ARRAY":
        element = t[1]
        element_text, items = _array_elements(value)
        if element is None and element_text is not None:
            element = parse_type_text(element_text) if element_text else None
        for item in items:
            if complete(element):
                break
            element = fill(element, item)
        return ("ARRAY", element)
    if t[0] == "STRUCT" and value.startswith("{"):
        end = _matching(value, 0)
        values = split_top(value[1:end])
        if len(values) != len(t[1]):
            return t
        return ("STRUCT", tuple((name, fill(field, v)) for (name, field), v in zip(t[1], values)))
    return t


def complete(t) -> bool:
    return type_sql(t) is not None


def parse_result(text: str):
    """Columns ``[(name, type_sql or None)]`` of a printed table result, or None when it is not a table of structs."""

    text = text.strip()
    if not text.startswith("ARRAY<STRUCT<"):
        return None
    close = _matching(text, len("ARRAY"))
    header = parse_type_text(text[len("ARRAY<"):close])
    rest = text[close + 1:].strip()
    if not rest.startswith("["):
        return None
    rows = split_top(_strip_order(rest[1:_matching(rest, 0)]))
    columns = []
    for position, (name, column) in enumerate(header[1]):
        for row in rows:
            if complete(column):
                break
            if not row.startswith("{"):
                break
            values = split_top(row[1:_matching(row, 0)])
            if len(values) == len(header[1]):
                column = fill(column, values[position])
        columns.append((name, type_sql(column), sorted(kinds(column))))
    return columns


# --- reading .test files -----------------------------------------------------------------------------------------

def blocks(text: str):
    """Yield (options, sql, results) per test block; ``results`` is the list of ``--``-separated sections."""

    for block in re.split(r"\n==\n", text):
        lines = block.split("\n")
        options, i = [], 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                i += 1
                continue
            if stripped.startswith("["):
                option = stripped
                while not option.endswith("]") and i + 1 < len(lines):  # an option may span lines
                    i += 1
                    option += "\n" + lines[i].strip()
                options.append(option[1:-1])
                i += 1
                continue
            break
        body = "\n".join(lines[i:])
        sections = re.split(r"\n--\n|^--\n", body)
        sql = _strip_comments(sections[0]).strip().rstrip(";").strip()
        yield options, sql, [s.strip() for s in sections[1:]]


def _strip_comments(sql: str) -> str:
    """Drop whole-line ``#`` comments (the files use them between options and SQL)."""

    return "\n".join(line for line in sql.split("\n") if not line.lstrip().startswith("#"))


_QUERY = re.compile(r"(?is)^\s*(select|with|\(|from\b)")
_CREATE_TABLE = re.compile(r"(?is)^\s*create\s+(?:temp\s+|temporary\s+)?table\s+([`\w.]+)(.*)$")
_CREATE_FUNCTION = re.compile(
    r"(?is)^\s*create\s+(?:or\s+replace\s+)?(?:temp\s+|temporary\s+|public\s+|private\s+)?"
    r"(?:aggregate\s+|table\s+)?function\s+([`\w.]+)"
)


def _option(options: list[str], name: str) -> str | None:
    for option in options:
        if option == name or option.startswith(name + "="):
            return option.partition("=")[2]
    return None


def read_file(path: Path) -> tuple[dict, list[dict]]:
    """(the file's catalog, its labelled query cases)."""

    text = path.read_text(encoding="utf-8", errors="replace")
    tables: dict[str, list] = {}
    functions: set[str] = set()
    function_sql: list[str] = []
    skipped_tables: list[str] = []
    cases = []
    for options, sql, results in blocks(text):
        if "prepare_database" in options or any(o.startswith("prepare_database") for o in options):
            m = _CREATE_TABLE.match(sql)
            f = _CREATE_FUNCTION.match(sql)
            if f:
                functions.add(f.group(1).strip("`").lower())
                function_sql.append(sql)
            if m and results:
                name, rest = m.group(1).strip("`"), m.group(2)
                columns = None
                try:
                    columns = parse_result(results[0])
                except ValueError:
                    pass
                value_table = re.search(r"(?is)\bselect\s+as\s+(value|struct)\b", rest) is not None
                if columns is None or value_table or any(t is None for _, t, _ in columns):
                    skipped_tables.append(name)
                else:
                    tables[name] = [[n, t] for n, t, _ in columns]
                    query = re.sub(r"(?is)^\s*as\s+", "", rest.strip())
                    query = re.sub(r"(?is)^\s*(\([^)]*\)\s*)?(options\s*\(.*?\)\s*)?as\s+", "", query)
                    if _QUERY.match(query) and len(results) == 1:
                        cases.append({"id": f"{path.stem}/prepare:{name}", "sql": query, "options": options,
                                      "columns": columns, "before": name})
            continue
        name = _option(options, "name")
        if not name or not sql or not _QUERY.match(sql) or len(results) != 1:
            continue
        if _option(options, "parameters") is not None or re.search(r"(?<![@\w])@{1,2}[A-Za-z_]", _no_strings(sql)):
            continue
        if results[0].startswith("ERROR"):
            continue
        try:
            columns = parse_result(results[0])
        except ValueError:
            continue
        if columns is None:
            continue
        cases.append({"id": f"{path.stem}/{name}", "sql": sql, "options": options, "columns": columns})
    catalog = {"tables": tables, "functions": sorted(functions), "function_sql": function_sql,
               "skipped_tables": sorted(set(skipped_tables))}
    return catalog, cases


def _no_strings(sql: str) -> str:
    return re.sub(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'", "''", sql)


def harvest(directory: Path) -> dict[str, int]:
    out = {"dev": {"files": {}, "cases": []}, "heldout": {"files": {}, "cases": []}}
    for path in sorted(directory.glob("*.test")):
        catalog, cases = read_file(path)
        split = out["heldout" if held_out(path.stem) else "dev"]
        split["files"][path.stem] = catalog
        for case in cases:
            features = sorted({f for o in case.pop("options") for f in re.findall(r"[A-Z][A-Z0-9_]+", o)
                               if o.startswith(("required_features", "default required_features"))})
            row = {"id": case["id"], "file": path.stem, "sql": case["sql"],
                   "columns": [[n, t] for n, t, _ in case["columns"]]}
            if features:
                row["features"] = features
            if "before" in case:
                row["prepare"] = case["before"]
            split["cases"].append(row)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    counts = {}
    for name, target in (("dev", DEV), ("heldout", HELDOUT)):
        payload = {"source": SOURCE, "split": name, "split_rule": SPLIT_RULE, **out[name]}
        with gzip.open(target, "wt", encoding="utf-8") as f:
            json.dump(payload, f, sort_keys=True, separators=(",", ":"))
        counts[name] = len(out[name]["cases"])
        counts[f"{name}_files"] = len(out[name]["files"])
    return counts


def load(path: Path = DEV) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


# --- scoring -----------------------------------------------------------------------------------------------------

def is_bigquery_type(type_text: str | None) -> bool:
    if type_text is None:
        return False
    try:
        return kinds(parse_type_text(type_text)) <= BIGQUERY_KINDS
    except ValueError:
        return False


def catalog_for(data: dict, case: dict):
    """(tables, functions, function_sql) visible to ``case``: its file's tables, minus the one a prepare query creates,
    the names of the functions the file creates, and their ``CREATE FUNCTION`` statements (empty in a fixture
    harvested before they were kept)."""

    files = data["files"][case["file"]]
    tables = {name: cols for name, cols in files["tables"].items() if name != case.get("prepare")}
    return tables, files["functions"], files.get("function_sql", [])


def typer_kumosql(sql: str, tables: dict, functions: list[str], function_sql: list[str] = ()):
    from kumosql.googlesql_types import Catalog, infer

    catalog = Catalog.from_types({name: dict(cols) if len({c for c, _ in cols}) == len(cols) else cols
                                  for name, cols in tables.items()}, functions=functions)
    for statement in function_sql:  # names are registered first, so a body that calls a later function is not guessed
        catalog.add_function_sql(statement)
    typed = infer(sql, catalog, dialect="bigquery")
    if typed.columns is None:
        return None
    return [(c.name, c.type.sql() if c.type is not None and c.type.complete else None) for c in typed.columns]


_SQLGLOT_NAMES = {
    "BIGINT": "INT64", "INT": "INT64", "DOUBLE": "FLOAT64", "VARCHAR": "STRING", "TEXT": "STRING",
    "BOOLEAN": "BOOL", "DECIMAL": "NUMERIC", "BIGDECIMAL": "BIGNUMERIC", "VARBINARY": "BYTES",
    "TIMESTAMPTZ": "TIMESTAMP", "TIMESTAMP": "DATETIME",
}


def typer_sqlglot(sql: str, tables: dict, functions: list[str], function_sql: list[str] = ()):
    """The baseline: sqlglot's qualify + annotate_types, with sqlglot's type names mapped to GoogleSQL's."""

    import sqlglot
    from sqlglot import exp
    from sqlglot.optimizer.annotate_types import annotate_types
    from sqlglot.optimizer.qualify import qualify
    from sqlglot.schema import MappingSchema

    schema = MappingSchema(
        {name: {c: t for c, t in cols} for name, cols in tables.items()}, dialect="bigquery"
    )
    tree = sqlglot.parse_one(sql, read="bigquery")
    if not isinstance(tree, exp.Query):
        return None
    try:
        tree = qualify(tree, schema=schema, dialect="bigquery", validate_qualify_columns=False, quote_identifiers=False)
    except Exception:  # noqa: BLE001 - unresolvable: no schema
        return None
    typed = annotate_types(tree, schema=schema, dialect="bigquery")
    out = []
    for select in typed.selects:
        t = getattr(select, "type", None)
        text = None
        if t is not None and not t.is_type(exp.DataType.Type.UNKNOWN) and t.this != exp.DataType.Type.NULL:
            text = _sqlglot_type_text(t)
        name = select.alias_or_name if isinstance(select, (exp.Alias, exp.Column)) else None
        out.append((name, text))
    return out


def _sqlglot_type_text(t) -> str | None:
    from sqlglot import exp

    if t.is_type(exp.DataType.Type.UNKNOWN):
        return None
    if t.this == exp.DataType.Type.ARRAY:
        inner = t.expressions[0] if t.expressions else None
        inner_text = _sqlglot_type_text(inner) if isinstance(inner, exp.DataType) else None
        return f"ARRAY<{inner_text}>" if inner_text else None
    if t.this == exp.DataType.Type.STRUCT:
        parts = []
        for field in t.expressions:
            if isinstance(field, exp.ColumnDef) and isinstance(field.args.get("kind"), exp.DataType):
                inner = _sqlglot_type_text(field.args["kind"])
                if inner is None:
                    return None
                parts.append(f"{field.name} {inner}")
            elif isinstance(field, exp.DataType):
                inner = _sqlglot_type_text(field)
                if inner is None:
                    return None
                parts.append(inner)
            else:
                return None
        return f"STRUCT<{', '.join(parts)}>"
    if t.expressions:
        return None  # parameterized (NUMERIC(10, 2)); the result type of a query never has parameters
    text = t.sql("bigquery").upper()
    return _SQLGLOT_NAMES.get(text, text)


TYPERS = {"kumosql": typer_kumosql, "sqlglot": typer_sqlglot}


def score(data: dict, typer: str = "kumosql", failures: int = 0, only: str | None = None) -> dict:
    run = TYPERS[typer]
    counts = Counter()
    rows = []
    started = time.perf_counter()
    for case in data["cases"]:
        if only and only not in case["id"]:
            continue
        tables, functions, function_sql = catalog_for(data, case)
        labels = case["columns"]
        try:
            got = run(case["sql"], tables, functions, function_sql)
        except Exception as exc:  # noqa: BLE001 - a crash is an unknown, counted apart
            counts["crashed queries"] += 1
            got = None
            if failures:
                rows.append((case["id"], "CRASH", f"{type(exc).__name__}: {exc}"[:200], case["sql"]))
        counts["queries"] += 1
        if got is not None and len(got) != len(labels):
            for name, expected in labels:
                if expected is not None:
                    counts["wrong"] += 1
                    counts["bigquery wrong"] += is_bigquery_type(expected)
            rows.append((case["id"], "WIDTH", f"{len(got)} columns, expected {len(labels)}", case["sql"]))
            counts["labelled columns"] += sum(1 for _, t in labels if t is not None)
            counts["bigquery columns"] += sum(1 for _, t in labels if is_bigquery_type(t))
            continue
        for position, (name, expected) in enumerate(labels):
            if expected is None:
                counts["unlabelled columns"] += 1
                continue
            bq = is_bigquery_type(expected)
            counts["labelled columns"] += 1
            counts["bigquery columns"] += bq
            mine = None if got is None else got[position][1]
            if mine is None:
                verdict = "unknown"
            elif mine == expected:
                verdict = "exact"
            else:
                verdict = "wrong"
                rows.append((case["id"], "WRONG", f"column {position + 1} {name}: {mine}, expected {expected}", case["sql"]))
            counts[verdict] += 1
            if bq:
                counts[f"bigquery {verdict}"] += 1
            if verdict == "unknown" and failures and got is not None:
                rows.append((case["id"], "unknown", f"column {position + 1} {name}: expected {expected}", case["sql"]))
        if got is None and failures:
            rows.append((case["id"], "unknown", "no schema", case["sql"]))
    counts["seconds"] = round(time.perf_counter() - started, 1)
    if failures:
        order = {"WRONG": 0, "WIDTH": 0, "CRASH": 1, "unknown": 2}
        for case_id, kind, detail, sql in sorted(rows, key=lambda r: order[r[1]])[:failures]:
            print(f"--- {kind} {case_id}: {detail}\n{sql}\n")
    return dict(counts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--harvest", type=Path, help="GoogleSQL compliance testdata directory: write the fixtures")
    ap.add_argument("--typer", choices=sorted(TYPERS), default="kumosql")
    ap.add_argument("--heldout", action="store_true", help="score the held-out split (final measurement only)")
    ap.add_argument("--failures", type=int, default=0, help="print up to N unknown or wrong columns")
    ap.add_argument("--only", help="score only cases whose id contains this text")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.harvest:
        print(json.dumps(harvest(args.harvest), indent=1))
        return 0
    data = load(HELDOUT if args.heldout else DEV)
    counts = score(data, args.typer, args.failures, args.only)
    if args.json:
        print(json.dumps(counts, indent=1, sort_keys=True))
    else:
        bq = counts.get("bigquery columns", 0)
        print(f"{'held-out' if args.heldout else 'dev'} / {args.typer}: {counts.get('queries', 0)} queries, "
              f"{counts.get('labelled columns', 0)} labelled columns")
        print(f"  all columns:      exact {counts.get('exact', 0)}, unknown {counts.get('unknown', 0)}, "
              f"WRONG {counts.get('wrong', 0)}")
        if bq:
            print(f"  BigQuery columns: exact {counts.get('bigquery exact', 0)}/{bq} "
                  f"({100 * counts.get('bigquery exact', 0) / bq:.1f}%), unknown {counts.get('bigquery unknown', 0)}, "
                  f"WRONG {counts.get('bigquery wrong', 0)}")
        print(f"  crashed queries {counts.get('crashed queries', 0)}, {counts['seconds']} s")
    return 1 if counts.get("wrong") else 0


if __name__ == "__main__":
    sys.exit(main())
