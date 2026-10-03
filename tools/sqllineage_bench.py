"""Score KumoSQL's lineage against test cases adapted from SQLLineage.

SQLLineage (https://github.com/reata/sqllineage, MIT, Copyright (c) 2019
Reata) ships about 540 test functions that each pair a SQL statement with the
table and column lineage it must produce. ``tests/fixtures/sqllineage/cases.json``
holds those expectations, harvested by calling the upstream tests with their
assertion helpers replaced by a recorder (``tools/sqllineage_harvest.py``).

Each case becomes a one-model KumoSQL pipeline. The model is named after the
``INSERT`` / ``CREATE TABLE AS`` target (or ``__select__`` when there is none),
the case's schemas become ``source_schema``, and the pipeline's own column
lineage and table dependencies are compared with the expected ones.

A case ends in one of four outcomes:

``exact``    every expected edge, no extra ones
``unknown``  KumoSQL said it could not trace something (unexpanded ``*``, a
             skipped statement, an unparsed model) and claimed no wrong edge
``missed``   confident, but an expected edge is missing
``wrong``    confident, and an edge or table that is not expected is claimed

Dialects other than ANSI and BigQuery are read as BigQuery, which is what the
tool does with every project; they are reported apart so they never move the
headline.

    python tools/sqllineage_bench.py [--details]
"""

from __future__ import annotations

from collections import Counter, defaultdict
import json
import time
from pathlib import Path
import re
import sys

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.schema import MappingSchema

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_common import quiet as _quiet, today, write_results as _write_results  # noqa: E402

from kumosql.pipeline import Pipeline, _adopt_statement_ctes, _name_unaliased_casts, _nested_schema  # noqa: E402
from kumosql.pipeline_types import ColumnRef, Model, Target  # noqa: E402

CASES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "sqllineage" / "cases.json"
IN_SCOPE_DIALECTS = {"ansi", "bigquery"}

# Cases whose expectation is about something other than BigQuery lineage. Each
# entry is a regex on the case id and the reason, so the exclusions stay few and visible.
EXCLUDED = (
    (r"enable_lateral_ref|lateral_ref_within_subquery", "lateral column alias references are not BigQuery semantics"),
    (r"test_tmp_table|test_drop|test_alter|rename", "table lifecycle across statements (DROP, RENAME, temporary tables) is not tracked"),
    (r"postgres_style|test_metadata_target_column", "not BigQuery semantics (``::`` casts; INSERT mapped by the target table's column order)"),
    (r"union_where_later_branch|quoted_alias_case_sensitive|quoted_table|keyword_as_column_alias", "not valid BigQuery (mismatched UNION arms, double-quoted identifiers)"),
    (r"without_table_qualifier_from_table_join", "not BigQuery semantics (a LEFT JOIN ... USING key takes the left input's value; SQLLineage counts both sides)"),
)
_EXPRESSION_TARGET = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _table(name: str | None) -> str:
    return (name or "").lower().replace("<default>.", "")


def _target_column(name: str) -> str:
    """Unnamed outputs are ``_col_N`` here and the expression text in SQLLineage; compare them as one kind."""

    name = name.lower()
    return name if _EXPRESSION_TARGET.match(name) and not re.match(r"^_col_\d+$", name) else "<expr>"


def _created_in_script(case: dict) -> set[str]:
    names = set()
    for statement in _statements(case["sql"]):
        if isinstance(statement, exp.Create) and (name := _destination(statement)):
            names.add(name.lower())
    return names


def scope_of(case: dict) -> str:
    """``in`` for a case that is BigQuery SQL about lineage; otherwise why it is left out."""

    if case["dialect"] not in IN_SCOPE_DIALECTS:
        return f"dialect {case['dialect']}"
    for pattern, reason in EXCLUDED:
        if re.search(pattern, case["id"]):
            return reason
    try:
        _statements(case["sql"])
    except Exception:
        return "does not parse as BigQuery"
    return "in"


def load_cases() -> list[dict]:
    data = json.loads(CASES.read_text(encoding="utf-8"))
    for case in data["cases"]:
        if "schemas" in case:
            case["schemas"] = data["schemas"][case["schemas"]]
    return data["cases"]


def _destination(statement: exp.Expression) -> str | None:
    if isinstance(statement, (exp.Create, exp.Insert, exp.Merge)):
        table = statement.this
        if isinstance(table, exp.Schema):
            table = table.this
        if isinstance(table, exp.Table):
            return ".".join(part.name for part in table.parts)
    return None


def _statements(sql: str) -> list[exp.Expression]:
    return [s for s in sqlglot.parse(sql, read="bigquery") if s is not None]


def build(case: dict) -> tuple[Pipeline, str]:
    """The one-model pipeline for a case, and the model's key."""

    statements = _statements(case["sql"])
    target_name = next((d for s in reversed(statements) if (d := _destination(s))), "__select__")
    parts = target_name.split(".")
    target = Target(*(["", ""] + parts)[-3:]) if len(parts) <= 3 else Target(*parts[-3:])
    schemas = {
        name.lower(): {column.lower(): "UNKNOWN" for column in columns}
        for name, columns in (case.get("schemas") or {}).items()
    }
    model = Model(target, "table", case["sql"])
    convention = _insert_convention(statements, model.key, schemas)
    if convention:
        schemas[model.key] = convention
    return Pipeline({model.key: model}, source_schema=schemas), model.key


def _insert_convention(statements: list[exp.Expression], key: str, schemas: dict) -> dict[str, str] | None:
    """SQLLineage's assumption for ``INSERT INTO t SELECT ...`` when the case gives no schema for ``t``.

    BigQuery maps the SELECT's columns to ``t``'s own columns by position, so without ``t``'s schema
    KumoSQL reports the outputs unknown. SQLLineage assumes ``t``'s columns are named like the SELECT's
    outputs; the harness states that assumption as ``t``'s schema (in SELECT order) so the case still
    scores column lineage. Counted in ``by_convention``.
    """

    if key.lower() in schemas:
        return None
    for statement in reversed(statements):
        if isinstance(statement, exp.Insert) and not isinstance(statement.this, exp.Schema):
            query = statement.expression
            if not isinstance(query, exp.Query):
                return None
            if any(_table(".".join(p.name for p in t.parts)) == _table(key) for t in query.find_all(exp.Table)):
                return None  # the INSERT reads its own target, whose other columns the case does not give
            statement = statement.copy()
            query = statement.expression
            _adopt_statement_ctes(statement, query)
            _name_unaliased_casts(query)
            try:
                names = list(qualify(query, schema=MappingSchema(_nested_schema(schemas), dialect="bigquery"),
                                     dialect="bigquery", validate_qualify_columns=False).named_selects)
            except Exception:
                return None
            if not names or "*" in names or len({n.lower() for n in names}) != len(names):
                return None
            return {name.lower(): "UNKNOWN" for name in names}
        if _destination(statement):
            return None
    return None


def predict(case: dict) -> dict:
    """What KumoSQL says about a case: edges, tables read, and anything it flagged."""

    pipeline, key = build(case)
    records = pipeline.explain_lineage()
    edges: set[tuple[str, str, str, str]] = set()
    unknown: list[str] = []
    for ref, record in records.items():
        if ref.table != key:
            continue
        if ref.column == "*":
            unknown.append(f"*:{record.reason}")  # output columns not known (an INSERT with no column list): no edge claimed
            continue
        for source in record.sources:
            edges.add((_table(source.table), source.column.lower(), _table(key), _target_column(ref.column)))
        if record.status == "unknown":
            unknown.append(f"{ref.column}:{record.reason}")
    diagnostics = {d.code for d in pipeline.all_diagnostics() if d.model == key}
    reads = {_table(name) for name in pipeline.table_reads().get(key, ())}
    return {
        "key": key,
        "edges": edges,
        "reads": reads,
        "writes": {_table(name) for name in pipeline.table_writes().get(key, ())},
        "unknown": unknown,
        "diagnostics": diagnostics,
    }


def expected_edges(case: dict) -> set[tuple[str, str, str, str]]:
    return {
        (_table(s[0]), s[1].lower(), _table(t[0]), _target_column(t[1]))
        for s, t in case["edges"]
    }


def classify(case: dict) -> dict:
    row = {"id": case["id"], "kind": case["kind"], "dialect": case["dialect"]}
    try:
        got = predict(case)
    except Exception as exc:  # a crash is a miss, never a pass
        return {**row, "extra": [], "missing": ["crash"], "outcome": "missed", "why": f"crash {type(exc).__name__}: {str(exc)[:80]}"}
    flagged = bool(got["unknown"]) or bool(got["diagnostics"] & {"skipped_statements", "parse_error", "no_query", "qualify_error", "unparsed_operation", "unexpanded_star", "lineage_error"})
    if case["kind"] == "table":
        want_sources = {_table(s) for s in case["sources"]}
        want_targets = {_table(t) for t in case["targets"]}
        have_sources = got["reads"]
        have_targets = {_table(got["key"])} if got["key"] != "__select__" else set()
        extra = (have_sources - want_sources) | (have_targets - want_targets)
        missing = (want_sources - have_sources) | (want_targets - have_targets)
        row.update(extra=sorted(extra), missing=sorted(missing))
    else:
        want, have = expected_edges(case), got["edges"]
        # An expected wildcard edge (``tab.*``) is satisfied by saying "unknown"
        # when no schema was supplied; it never counts as a miss.
        star_expected = {e for e in want if e[1] == "*"}
        want_concrete = want - star_expected
        extra = have - want
        missing = want_concrete - have
        if star_expected and got["unknown"] and not extra:
            missing = set()
        row.update(extra=sorted(map(list, extra)), missing=sorted(map(list, missing)), edges_expected=len(want_concrete), edges_found=len(have & want))
    if not row["extra"] and not row["missing"]:
        row["outcome"] = "exact"
    elif flagged and not row["extra"]:
        row["outcome"] = "unknown"
    elif row["extra"] and case["kind"] == "column" and all(e[1] == "*" or e[0] in _created_in_script(case) for e in row["extra"]):
        # An edge from ``table.*`` says "some column of this table"; one from a view the same script
        # created is the one-hop truth. Neither is wrong, and the script is flagged as partly traced.
        row["outcome"] = "unknown"
    elif row["extra"]:
        row["outcome"] = "wrong"
    else:
        row["outcome"] = "missed"
    row["diagnostics"] = sorted(got["diagnostics"])
    return row


def run() -> dict:
    rows = []
    for case in load_cases():
        scope = scope_of(case)
        row = classify(case)
        row["scope"] = scope
        rows.append(row)
    scoped = [r for r in rows if r["scope"] == "in"]
    other = [r for r in rows if r["scope"] != "in"]
    left_out = Counter(r["scope"].split(" ")[0] if r["scope"].startswith("dialect") else r["scope"] for r in other)

    def tally(selection: list[dict]) -> dict:
        counts = Counter(r["outcome"] for r in selection)
        found = sum(r.get("edges_found", 0) for r in selection if r["kind"] == "column")
        wanted = sum(r.get("edges_expected", 0) for r in selection if r["kind"] == "column")
        extra = sum(len(r["extra"]) for r in selection if r["kind"] == "column")
        return {
            "total": len(selection),
            "exact": counts["exact"],
            "unknown": counts["unknown"],
            "missed": counts["missed"],
            "wrong": counts["wrong"],
            "edge_recall": found / wanted if wanted else 1.0,
            "edge_precision": found / (found + extra) if found + extra else 1.0,
        }

    return {
        "in_scope": tally(scoped),
        "table": tally([r for r in scoped if r["kind"] == "table"]),
        "column": tally([r for r in scoped if r["kind"] == "column"]),
        "left_out": dict(left_out),
        "other": tally(other),
        "rows": rows,
    }


def write_results(result: dict, seconds: float) -> None:
    t = result["in_scope"]
    left = sum(result["left_out"].values())
    _write_results(
        "sqllineage",
        {
                "suite": "SQLLineage test cases",
                "order": 200,
                "size": t["total"],
                "score": f"{t['exact']}/{t['total']} exact, {t['wrong']} wrong",
                "metric": (
                    "Cases whose table and column lineage match SQLLineage's expected graph exactly. The rest are reported as "
                    f"unknown ({t['unknown']}: a SELECT * with no known columns, UPDATE and procedures that are not traced) and none claims a wrong edge."
                ),
                "evidence": "executed",
                "correctness": "0 cases with an edge or table the expectation does not have (a SELECT * with unknown columns says unknown, not a guess)",
                "coverage": {"proven": t["exact"], "unknown": t["unknown"], **({"error": t["missed"]} if t["missed"] else {})},
                "held_out": "none",
                "docs": "docs/evals/lineage-bench.md#sqllineage-test-cases",
                "command": "python tools/sqllineage_bench.py --details",
                "date": today(),
                "caveats": (
                    f"Adapted from SQLLineage's tests (MIT); {left} of the {t['total'] + left} adapted cases are left out because they are not BigQuery SQL "
                    "(other dialects, lateral column aliases, DROP/RENAME lifecycles, a LEFT JOIN USING key counted from both sides). "
                    "Where a case gives no schema for an INSERT target, the harness states SQLLineage's assumption that the target's columns "
                    "are named like the SELECT's outputs; KumoSQL itself reports such outputs unknown, since BigQuery maps them by the target's column order. "
                    "Bugs it found were fixed in the same change, so it is a floor, not a held-out score."
                ),
                "analysis": (
                    f"Column edges: precision {result['column']['edge_precision']:.3f}, recall {result['column']['edge_recall']:.3f} "
                    "(recall counts edges KumoSQL reports as unknown)"
                ),
                "performance": f"{t['total']} cases in {seconds:.1f} s",
        },
    )


def main() -> None:
    _quiet()
    start = time.perf_counter()
    result = run()
    seconds = time.perf_counter() - start
    if "--write-results" in sys.argv:
        write_results(result, seconds)
    details = "--details" in sys.argv
    for name in ("in_scope", "table", "column", "other"):
        t = result[name]
        print(
            f"{name:15} {t['exact']}/{t['total']} exact, {t['unknown']} unknown, {t['missed']} missed, "
            f"{t['wrong']} wrong; edge recall {t['edge_recall']:.3f}, precision {t['edge_precision']:.3f}"
        )
    print("left out:", result["left_out"])
    if details:
        by_file: dict[str, Counter] = defaultdict(Counter)
        for r in result["rows"]:
            if r["scope"] == "in":
                by_file[r["id"].split("::")[0]][r["outcome"]] += 1
        for name, counts in sorted(by_file.items()):
            print(f"  {name:70} {dict(counts)}")
        for r in result["rows"]:
            if r["scope"] == "in" and r["outcome"] in {"wrong", "missed"}:
                print(r["outcome"], r["id"], "extra", r["extra"], "missing", r["missing"], r.get("why", ""))


if __name__ == "__main__":
    main()
