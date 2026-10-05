"""Extract Apache Doris's outer-join materialized-view rewriting tests into a JSON corpus.

    python tools/extract_doris_mv.py            # downloads the pinned sources, writes the fixture
    python tools/extract_doris_mv.py DIR        # reads the .groovy files from DIR (same relative paths)

Each case gives a materialized view, a query, and Doris's verdict: ``success`` (Doris rewrites the query
over the view) or ``fail`` (Doris finds no rewrite; that is not a proof that none exists). The tables of
each file come from its own ``CREATE TABLE`` statements (column types and NOT NULL; Doris's DUPLICATE KEY
is a sort key, not a unique key, so no keys are declared).

Files read (Doris 3.0.6, Apache-2.0, ``regression-test/suites/nereids_rules_p0/mv/``):

* ``join/left_outer/outer_join.groovy``: left outer joins with filters inside and outside the join,
  each ``async_mv_rewrite_success`` / ``async_mv_rewrite_fail`` call is one case;
* ``dimension/dimension_2_{left,right,full}_join.groovy``: twelve statements over one two-table outer
  join with a filter in a different place each; every statement is tried as the view for every
  statement as the query (144 pairs per file), with the verdict the test asserts.

The SQL text is copied unchanged apart from Groovy string joining.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

TAG = "3.0.6"
TAG_OBJECT = "df9fa9ce49f71599d239f293d4c9d92585254305"  # git ls-remote refs/tags/3.0.6
BASE = f"https://raw.githubusercontent.com/apache/doris/{TAG}/regression-test/suites/nereids_rules_p0/mv/"
FILES = {
    "join/left_outer/outer_join.groovy": "outer_join",
    "dimension/dimension_2_left_join.groovy": "dim_left",
    "dimension/dimension_2_right_join.groovy": "dim_right",
    "dimension/dimension_2_full_join.groovy": "dim_full",
}
OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "mv_reuse" / "doris_mv_cases.json"

_TYPES = (
    (re.compile(r"^(TINYINT|SMALLINT|INT|INTEGER|BIGINT|LARGEINT)\b", re.I), "int"),
    (re.compile(r"^(DECIMAL|DECIMALV3|DOUBLE|FLOAT)\b", re.I), "float"),
    (re.compile(r"^(CHAR|VARCHAR|STRING|TEXT)\b", re.I), "text"),
    (re.compile(r"^(DATE|DATEV2|DATETIME|DATETIMEV2)\b", re.I), "date"),
    (re.compile(r"^(BOOLEAN)\b", re.I), "bool"),
)


def _type(text: str) -> str | None:
    for pattern, name in _TYPES:
        if pattern.match(text.strip()):
            return name
    return None


def tables_of(source: str) -> dict[str, list[list]]:
    """``CREATE TABLE`` statements of a file: table -> [[column, type, not_null], ...]."""

    tables: dict[str, list[list]] = {}
    for match in re.finditer(r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?`?(\w+)`?\s*\((.*?)\)\s*(?:ENGINE|DUPLICATE|UNIQUE|AGGREGATE|COMMENT)", source, re.S | re.I):
        columns = []
        for line in match.group(2).splitlines():
            column = re.match(r"\s*`?(\w+)`?\s+([A-Za-z][A-Za-z0-9]*(?:\s*\([\d,\s]*\))?)(.*)", line)
            if not column:
                continue
            kind = _type(column.group(2))
            if kind is None:
                continue
            rest = column.group(3).upper()
            columns.append([column.group(1).lower(), kind, "NOT NULL" in rest])
        tables[match.group(1).lower()] = columns
    return tables


def _groovy_string(text: str, index: int) -> tuple[str, int] | None:
    """The string literal starting at ``index`` (triple- or double-quoted) and the index after it."""

    if text.startswith('"""', index):
        end = text.index('"""', index + 3)
        return text[index + 3 : end], end + 3
    if text.startswith('"', index):
        out, i = [], index + 1
        while text[i] != '"':
            if text[i] == "\\":
                out.append(text[i + 1])
                i += 2
                continue
            out.append(text[i])
            i += 1
        return "".join(out), i + 1
    return None


def definitions(source: str) -> dict[str, str]:
    """``def name = "..." + "..."`` and ``def name = \"\"\"...\"\"\"`` string definitions, in file order."""

    found: dict[str, str] = {}
    for match in re.finditer(r"\bdef\s+(\w+)\s*=\s*", source):
        index, parts = match.end(), []
        while True:
            while index < len(source) and source[index] in " \t\r\n":
                index += 1
            literal = _groovy_string(source, index)
            if literal is None:
                break
            parts.append(literal[0])
            index = literal[1]
            while index < len(source) and source[index] in " \t\r\n":
                index += 1
            if index < len(source) and source[index] == "+":
                index += 1
                continue
            break
        if parts:
            found[match.group(1)] = "".join(parts)
    return found


def _clean(sql: str) -> str:
    return sql.strip().rstrip(";").strip()


def pair_cases(source: str, origin: str) -> list[dict]:
    """One case per ``async_mv_rewrite_success`` / ``async_mv_rewrite_fail`` call."""

    env = definitions(source)
    cases = []
    for match in re.finditer(r"\basync_mv_rewrite_(success|fail)\(\s*db\s*,\s*(\w+)\s*,\s*(\w+)\s*,\s*\"(\w+)\"", source):
        verdict, mv, query, name = match.groups()
        if mv not in env or query not in env:
            continue
        cases.append({"id": f"doris.{origin}.{name}", "name": name, "origin": origin, "materialization": _clean(env[mv]), "query": _clean(env[query]), "doris": verdict})
    return cases


def matrix_cases(source: str, origin: str) -> list[dict]:
    """The statement-by-statement matrix: view ``i``, query ``j``, success when ``j`` is listed for ``i``."""

    env = definitions(source)
    listing = re.search(r"def\s+mv_list_1\s*=\s*\[(.*?)\]", source, re.S)
    if not listing:
        return []
    names = [n.strip() for n in listing.group(1).split(",") if n.strip()]
    statements = [_clean(env[n]) for n in names]
    successes: dict[int, set[int]] = {}
    for block in re.finditer(r"i\s*==\s*(\d+)\s*\)\s*\{.*?j\s+in\s+\[([\d,\s]*)\]", source, re.S):
        successes[int(block.group(1))] = {int(x) for x in block.group(2).split(",") if x.strip()}
    cases = []
    for i, view in enumerate(statements):
        if i not in successes:
            continue
        for j, query in enumerate(statements):
            name = f"v{i}_q{j}"
            verdict = "success" if j in successes[i] else "fail"
            cases.append({"id": f"doris.{origin}.{name}", "name": name, "origin": origin, "materialization": view, "query": query, "doris": verdict})
    return cases


def fetch(path: str, directory: Path | None) -> str:
    if directory:
        return (directory / path).read_text(encoding="utf-8")
    with urllib.request.urlopen(BASE + path) as response:  # noqa: S310 - fixed pinned URL
        return response.read().decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    directory = Path(argv[0]) if argv else None
    cases, schemas = [], {}
    for path, origin in FILES.items():
        source = fetch(path, directory)
        schemas[origin] = tables_of(source)
        found = pair_cases(source, origin) if origin == "outer_join" else matrix_cases(source, origin)
        for case in found:
            case["schema"] = f"doris_{origin}"
        cases += found
        print(f"{origin}: {len(found)} cases, tables {sorted(schemas[origin])}")
    OUT.write_text(
        json.dumps(
            {
                "source": "Apache Doris regression tests (regression-test/suites/nereids_rules_p0/mv)",
                "tag": TAG,
                "tag_object": TAG_OBJECT,
                "license": "Apache-2.0",
                "files": sorted(FILES),
                "schemas": {f"doris_{origin}": tables for origin, tables in schemas.items()},
                "cases": cases,
            },
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"{len(cases)} cases written to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
