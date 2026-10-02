"""Rebuild ``tests/fixtures/lineage_goldens/*.json`` from DataHub's and OpenLineage's own lineage tests.

DataHub (https://github.com/datahub-project/datahub, Apache-2.0), ``metadata-ingestion/tests/unit/sql_parsing``:
each ``assert_sql_result(sql, dialect=..., schemas=..., expected_file=...)`` call in ``test_sqlglot_lineage.py``
pairs a SQL string with a golden JSON file of table and column lineage. The goldens are sqlglot's own output that
maintainers reviewed, so they are not an independent oracle.

OpenLineage (https://github.com/OpenLineage/OpenLineage, Apache-2.0), ``integration/sql/impl/tests``: Rust tests of a
parser (sqlparser-rs) that shares no code with sqlglot, so it is the independent oracle. Each test pairs SQL with the
expected ``TableLineage`` or ``ColumnLineage``.

Nothing about an expectation is changed. A test whose SQL or expectation is not a plain literal (a variable, an error
assertion, a loop) is not harvested and is counted as ``unharvested``.

    git clone --filter=blob:none --no-checkout https://github.com/datahub-project/datahub /tmp/gl/datahub
    git -C /tmp/gl/datahub sparse-checkout set --no-cone metadata-ingestion/tests/unit/sql_parsing
    git -C /tmp/gl/datahub checkout 8a117ba1f5
    git clone --filter=blob:none --no-checkout https://github.com/OpenLineage/OpenLineage /tmp/gl/OpenLineage
    git -C /tmp/gl/OpenLineage sparse-checkout set --no-cone integration/sql
    git -C /tmp/gl/OpenLineage checkout 7a84fd4d48
    python tools/lineage_goldens_harvest.py /tmp/gl
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "lineage_goldens"
URN = re.compile(r"urn:li:dataset:\(urn:li:dataPlatform:[^,]+,([^,]+),[^)]+\)")


def _name(urn: str | None) -> str | None:
    if urn is None:
        return None
    match = URN.fullmatch(urn)
    return match.group(1) if match else urn


# ------------------------------------------------------------------ DataHub

def harvest_datahub(root: Path) -> dict:
    base = root / "datahub" / "metadata-ingestion" / "tests" / "unit" / "sql_parsing"
    tree = ast.parse((base / "test_sqlglot_lineage.py").read_text(encoding="utf-8"))
    cases, unharvested = [], []
    for func in tree.body:
        if not isinstance(func, ast.FunctionDef):
            continue
        skipped = any("skip" in ast.unparse(d) for d in func.decorator_list)
        calls = [c for c in ast.walk(func) if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "assert_sql_result"]
        for index, call in enumerate(calls, start=1):
            try:
                sql = ast.literal_eval(call.args[0])
                options = {k.arg: ast.literal_eval(k.value) for k in call.keywords if k.arg not in {"expected_file"}}
                expected = next(k.value for k in call.keywords if k.arg == "expected_file")
                golden = json.loads((base / "goldens" / Path(ast.literal_eval(expected.right)).name).read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001 - a non-literal call is reported, not guessed at
                unharvested.append({"test": func.name, "why": f"{type(exc).__name__}"})
                continue
            schemas = {_name(urn): list(cols) for urn, cols in (options.get("schemas") or {}).items()}
            edges = []
            for entry in golden.get("column_lineage") or []:
                target = entry["downstream"]
                for up in entry.get("upstreams") or []:
                    edges.append([[_name(up["table"]), up["column"]], [_name(target.get("table")) or "__select__", target["column"]]])
            cases.append(
                {
                    "id": f"datahub/{func.name}" + (f"#{index}" if len(calls) > 1 else ""),
                    "test": func.name,
                    "dialect": options.get("dialect", "generic"),
                    "sql": sql.strip("\n"),
                    "schemas": schemas or None,
                    "default_db": options.get("default_db"),
                    "default_schema": options.get("default_schema"),
                    "skipped_upstream": skipped,
                    "sources": sorted(_name(u) for u in golden.get("in_tables") or []),
                    "targets": sorted(_name(u) for u in golden.get("out_tables") or []),
                    "edges": sorted(edges, key=json.dumps),
                    "query_type": golden.get("query_type"),
                }
            )
    return {"cases": cases, "unharvested": unharvested}


# ------------------------------------------------------------------ OpenLineage

_TOKEN = re.compile(
    r"""
    (?P<raw>r(?P<h>\#*)"(?:.|\n)*?"(?P=h))
  | (?P<str>"(?:\\(?:.|\n)|[^"\\])*")
  | (?P<char>'(?:\\.|[^'\\])')
  | (?P<comment>//[^\n]*|/\*(?:.|\n)*?\*/)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*!?)
  | (?P<num>\d+)
  | (?P<punct>[{}()\[\],:.;<>=&|!#\-+*/?])
  | (?P<space>\s+)
    """,
    re.X,
)


def _unescape(token: str) -> str:
    if token.startswith("r"):
        hashes = len(token) - len(token.lstrip("r").lstrip("#")) - 1
        inner = token[1 + hashes + 1 : len(token) - 1 - hashes]
        return inner
    inner = token[1:-1]
    inner = re.sub(r"\\\n\s*", "", inner)  # a trailing backslash joins lines
    return bytes(inner, "utf-8").decode("unicode_escape")


def _lex(text: str) -> list[tuple[str, str]]:
    tokens = []
    pos = 0
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if not match:
            pos += 1
            continue
        kind = match.lastgroup if match.lastgroup != "h" else "raw"
        if match.group("raw"):
            kind = "str"
            tokens.append(("str", _unescape(match.group("raw"))))
        elif match.group("str"):
            tokens.append(("str", _unescape(match.group("str"))))
        elif match.group("comment") or match.group("space") or match.group("char"):
            pass
        else:
            tokens.append((kind, match.group(0)))
        pos = match.end()
    return tokens


class _Stop(Exception):
    pass


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]):
        self.t = tokens
        self.i = 0

    def peek(self, offset: int = 0):
        return self.t[self.i + offset] if self.i + offset < len(self.t) else ("end", "")

    def take(self, value: str | None = None, kind: str | None = None):
        token = self.peek()
        if (value is not None and token[1] != value) or (kind is not None and token[0] != kind):
            raise _Stop(f"expected {value or kind} got {token}")
        self.i += 1
        return token

    def accept(self, value: str) -> bool:
        if self.peek()[1] == value:
            self.i += 1
            return True
        return False

    def string(self) -> str:
        value = self.take(kind="str")[1]
        if self.accept("."):  # "x".to_string() / .to_owned()
            self.take(kind="ident")
            self.take("(")
            self.take(")")
        return value

    def option_string(self):
        if self.accept("None"):
            return None
        self.take("Some")
        self.take("(")
        value = self.string()
        self.take(")")
        return value

    def table_expr(self) -> str:
        """``table("x")`` or ``_tbl(Some("db"), None, "x")``, as a dotted name."""

        name = self.take(kind="ident")[1]
        self.take("(")
        if name in {"table"}:
            value = self.string()
        elif name == "_tbl":
            db, schema, tbl = self.option_string(), self.take(",") and self.option_string(), None
            self.take(",")
            tbl = self.string()
            value = ".".join(p for p in (db, schema, tbl) if p)
        else:
            raise _Stop(f"table constructor {name}")
        self.take(")")
        return value

    def table_list(self) -> list[str]:
        name = self.take(kind="ident")[1]
        if name == "tables":
            self.take("(")
            self.take("vec!")
            items = self._bracketed(self.string)
            self.take(")")
            return items
        if name == "vec!":
            return self._bracketed(self.table_expr)
        raise _Stop(f"list {name}")

    def _bracketed(self, item):
        self.take("[")
        out = []
        while not self.accept("]"):
            out.append(item())
            self.accept(",")
        return out

    def field(self, name: str) -> None:
        self.take(name)
        self.take(":")

    def table_lineage(self) -> tuple[list[str], list[str]]:
        self.take("TableLineage")
        self.take("{")
        self.field("in_tables")
        sources = self.table_list()
        self.take(",")
        self.field("out_tables")
        targets = self.table_list()
        self.accept(",")
        self.take("}")
        return sources, targets

    def column_meta(self) -> tuple[str | None, str]:
        self.take("ColumnMeta")
        self.take("{")
        self.field("origin")
        origin = None
        if self.accept("None"):
            pass
        else:
            self.take("Some")
            self.take("(")
            origin = self.table_expr()
            self.take(")")
        self.take(",")
        self.field("name")
        name = self.string()
        self.accept(",")
        self.take("}")
        return origin, name

    def column_lineages(self) -> list[tuple[tuple, list[tuple]]]:
        self.take("vec!")
        self.take("[")
        out = []
        while not self.accept("]"):
            self.take("ColumnLineage")
            self.take("{")
            self.field("descendant")
            down = self.column_meta()
            self.take(",")
            self.field("lineage")
            self.take("vec!")
            ups = self._bracketed(self.column_meta)
            self.accept(",")
            self.take("}")
            out.append((down, ups))
            self.accept(",")
        return out


def _functions(tokens: list[tuple[str, str]]):
    """(name, body tokens) of every ``#[test] fn``."""

    i = 0
    while i < len(tokens):
        if tokens[i][1] == "#" and tokens[i + 1][1] == "[" and tokens[i + 2][1] == "test":
            j = i
            while not (tokens[j][1] == "fn"):
                j += 1
            name = tokens[j + 1][1]
            while tokens[j][1] != "{":
                j += 1
            depth, k = 0, j
            while True:
                if tokens[k][1] == "{" and tokens[k][0] == "punct":
                    depth += 1
                elif tokens[k][1] == "}" and tokens[k][0] == "punct":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            attrs = [v for _, v in tokens[max(0, i - 12) : j]]
            yield name, tokens[j + 1 : k], "ignore" in attrs[attrs.index("#") if "#" in attrs else 0 :] or "ignore" in [v for _, v in tokens[i:j]]
            i = k
        i += 1


def _bindings(body: list[tuple[str, str]]) -> dict[str, str]:
    """``let name = "sql";`` bindings whose value is a plain string."""

    found = {}
    for i in range(len(body) - 4):
        if body[i][1] == "let" and body[i + 1][0] == "ident" and body[i + 2][1] == "=" and body[i + 3][0] == "str" and body[i + 4][1] == ";":
            found[body[i + 1][1]] = body[i + 3][1]
    return found


def _call_args(body: list[tuple[str, str]]):
    """The SQL statements and dialect of the test's one parse call, or None."""

    bound = _bindings(body)
    body = [("str", bound[v]) if k == "ident" and v in bound and i > 0 and body[i - 1][1] in {"(", "&"} else (k, v) for i, (k, v) in enumerate(body)]

    for index, (kind, value) in enumerate(body):
        if value in {"test_sql", "test_sql_dialect", "test_multiple_sql", "test_multiple_sql_dialect"} and body[index + 1][1] == "(":
            parser = _Parser(body)
            parser.i = index + 2
            multiple = "multiple" in value
            with_dialect = value.endswith("dialect")
            try:
                if multiple:
                    parser.take("vec!")
                    statements = parser._bracketed(parser.string)
                else:
                    statements = [parser.string()]
                dialect = "postgres"
                if with_dialect:
                    parser.accept(",")
                    dialect = parser.string()
            except _Stop:
                return None
            return statements, dialect
    return None


def _partial_tables(body: list[tuple[str, str]], values: list[str]) -> tuple[list[str] | None, list[str] | None]:
    """Expectations that name only ``in_tables`` or only ``out_tables`` (None for the side not asserted)."""

    parser = _Parser(body)
    sources = targets = None
    if "in_tables" in values:
        start = values.index("in_tables")
        index = next(i for i in range(start, len(body)) if body[i][1] == "vec!")
        parser.i = index + 1
        sources = parser._bracketed(lambda: parser.table_expr() if parser.peek()[0] == "ident" else parser.string())
    if "out_tables" in values:
        start = values.index("out_tables")
        if body[start + 1][1] == ":":
            raise _Stop("out_tables inside a literal")
        index = next(i for i in range(start, len(body)) if body[i][0] == "str" and body[i - 1][1] == ",")
        targets = [body[index][1]]
    return sources, targets


def harvest_openlineage(root: Path) -> dict:
    base = root / "OpenLineage" / "integration" / "sql" / "impl" / "tests"
    cases, unharvested = [], []
    for path in sorted(base.rglob("*.rs")):
        if path.name in {"mod.rs", "tests.rs"} and "test_utils" in path.parts:
            continue
        tokens = _lex(path.read_text(encoding="utf-8"))
        rel = path.relative_to(base).as_posix().removesuffix(".rs")
        for name, body, ignored in _functions(tokens):
            call = _call_args(body)
            if call is None:
                unharvested.append({"test": f"{rel}::{name}", "why": "sql is not a plain literal"})
                continue
            statements, dialect = call
            values = [v for _, v in body]
            row = {"id": f"openlineage/{rel}::{name}", "dialect": dialect, "sql": ";\n".join(s.strip().rstrip(";") for s in statements)}
            try:
                if "TableLineage" in values and body[values.index("TableLineage") + 1][1] == "{":
                    parser = _Parser(body)
                    parser.i = values.index("TableLineage")
                    sources, targets = parser.table_lineage()
                    row.update(kind="table", sources=sorted(sources), targets=sorted(targets))
                elif "column_lineage" in values and "ColumnLineage" in values:
                    parser = _Parser(body)
                    start = next(i for i, v in enumerate(values) if v == "vec!" and body[i + 2][1] == "ColumnLineage")
                    parser.i = start
                    edges = []
                    for (_, down), ups in parser.column_lineages():
                        for origin, up in ups:
                            edges.append([[origin, up], ["__select__", down]])
                    row.update(kind="column", edges=sorted(edges, key=json.dumps))
                elif "in_tables" in values or "out_tables" in values:
                    sources, targets = _partial_tables(body, values)
                    row.update(kind="table", sources=sources, targets=targets)
                else:
                    raise _Stop("expectation is not a table or column lineage literal")
            except (_Stop, StopIteration, IndexError) as exc:
                unharvested.append({"test": f"{rel}::{name}", "why": str(exc)[:80]})
                continue
            row["multi_statement"] = len(statements) > 1
            row["skipped_upstream"] = ignored
            cases.append(row)
    return {"cases": cases, "unharvested": unharvested}


def main(argv: list[str]) -> int:
    root = Path(argv[0] if argv else "/tmp/gl")
    OUT.mkdir(parents=True, exist_ok=True)

    def head(repo: str) -> str:
        return subprocess.check_output(["git", "-C", str(root / repo), "rev-parse", "HEAD"], text=True).strip()

    for name, repo, source, harvest in (
        ("datahub", "datahub", "https://github.com/datahub-project/datahub (Apache-2.0), metadata-ingestion/tests/unit/sql_parsing/test_sqlglot_lineage.py and goldens", harvest_datahub),
        ("openlineage", "OpenLineage", "https://github.com/OpenLineage/OpenLineage (Apache-2.0), integration/sql/impl/tests", harvest_openlineage),
    ):
        data = harvest(root)
        data.update(source=source, commit=head(repo))
        (OUT / f"{name}.json").write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        print(f"{name}: {len(data['cases'])} harvested, {len(data['unharvested'])} not, commit {data['commit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
