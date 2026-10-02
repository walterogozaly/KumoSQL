"""Score KumoSQL's lineage against DataHub's and OpenLineage's own lineage tests.

DataHub (Apache-2.0) ships SQL with golden table and column lineage; the goldens are sqlglot's own output after review,
so agreement with them is *not* independent evidence. OpenLineage (Apache-2.0) tests a Rust parser (sqlparser-rs) that
shares no code with sqlglot, so it is the independent oracle. The two corpora are scored separately and never added.

Both are harvested unmodified by ``tools/lineage_goldens_harvest.py`` into ``tests/fixtures/lineage_goldens``. Each case
becomes a one-model KumoSQL pipeline (``tools/sqllineage_bench.py``'s ``build``) and ends in one of:

``exact``    every expected table and edge, no extra ones
``coarse``   no extra edge; an expected struct sub-field edge (``a.b``) is met by an edge to its root column ``a``
``unknown``  KumoSQL said it could not trace something and claimed nothing wrong
``missed``   confident, but an expected table or edge is missing
``wrong``    confident, and a table or edge is claimed that is not expected

Scope: ``in`` is a BigQuery case (DataHub ``dialect=bigquery``; OpenLineage ``postgres``/``generic``/``bigquery``, which
are ANSI SQL here) that parses as BigQuery and is not one the upstream project itself skips. Other dialects are tried
as BigQuery and reported apart; they never move the headline. Cases whose expectation is about state across statements
(``USE``) are out of scope.

    python tools/lineage_goldens_bench.py [--details] [--write-results]
"""

from __future__ import annotations

from collections import Counter
import importlib.util
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "lineage_goldens"
sys.path.insert(0, str(ROOT / "src"))

_SPEC = importlib.util.spec_from_file_location("sqllineage_bench", ROOT / "tools" / "sqllineage_bench.py")
base = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("sqllineage_bench", base)
_SPEC.loader.exec_module(base)

IN_SCOPE_DIALECTS = {"datahub": {"bigquery"}, "openlineage": {"postgres", "generic", "bigquery"}}
FLAGS = {"skipped_statements", "parse_error", "no_query", "qualify_error", "unparsed_operation", "unexpanded_star", "lineage_error"}
# Cases whose expectation is not BigQuery lineage even though the dialect label allows it.
EXCLUDED = (
    (r"use_|/test_use", "state across statements (USE) is not tracked"),
    (r"tests_copy|create_stage|copy_into|alter_snowflake", "Snowflake/Hive load and stage statements"),
    (r"test_compound_names", "not valid BigQuery: an alias hides the table name (``db.t.x`` after ``db.t AS t1``)"),
    (r"tests_select::select_into", "``SELECT ... INTO`` is T-SQL, not BigQuery"),
    (r"merge_identifier_function|select_identifier_function|update_identifier_function|delete_identifier_function", "Snowflake IDENTIFIER() function"),
)


# Oracle expectations that define the answer differently from KumoSQL, one reason each (never used to hide a wrong edge).
DISPUTED = {
    "openlineage/table_lineage/test_alias_resolving::table_reference_with_simple_ctes": "unused CTE: OpenLineage reads data flow; KumoSQL lists every table the query names, because dropping it still breaks the query",
    "openlineage/table_lineage/test_alias_resolving::table_reference_with_simple_q_ctes": "unused CTE (see above)",
    "openlineage/table_lineage/test_alias_resolving::table_reference_with_passersby_ctes": "unused CTE (see above)",
    "openlineage/table_lineage/test_alias_resolving::table_references_connected_ctes": "unused CTE (see above)",
}


def load(name: str) -> dict:
    data = json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))
    for case in data["cases"]:
        case["corpus"] = name
        case.setdefault("kind", "column" if case.get("edges") else "table")
    return data


def _norm(name: str | None) -> str:
    name = re.sub(r"[`\"]", "", (name or "")).lower().replace("<default>.", "")
    # DataHub names a date shard or wildcard ``table_yyyymmdd`` and drops a partition decorator (``$...``);
    # applied to both sides, so a name KumoSQL reports as written still matches.
    name = re.sub(r"\$[^.]*$", "", name)
    return re.sub(r"_(\d{8}|\d+\*|\*|yyyymmdd)$", "_yyyymmdd", name)


def _column(name: str) -> str:
    """OpenLineage calls an unnamed output ``_0``, ``_1``; KumoSQL ``_col_N``; both compare as one kind."""

    return "<expr>" if re.fullmatch(r"_\d+", name.lower()) else base._target_column(name)


def _same_table(want: str, have: str, lenient: bool) -> bool:
    want, have = _norm(want), _norm(have)
    if want == have:
        return True
    if not lenient:
        return False
    a, b = want.split("."), have.split(".")
    shorter = min(len(a), len(b))
    return a[-shorter:] == b[-shorter:]


def scope_of(case: dict) -> str:
    if case["dialect"] not in IN_SCOPE_DIALECTS[case["corpus"]]:
        return f"dialect {case['dialect']}"
    if case.get("skipped_upstream"):
        return "skipped by the upstream project itself"
    for pattern, reason in EXCLUDED:
        if re.search(pattern, case["id"]):
            return reason
    if re.search(r"(?im)^\s*use\s", case["sql"]):
        return "state across statements (USE) is not tracked"
    try:
        base._statements(case["sql"])
    except Exception:  # noqa: BLE001
        return "does not parse as BigQuery"
    return "in"


def _spellings(sql: str) -> list[str]:
    names = []
    try:
        for statement in base._statements(sql):
            for table in statement.find_all(base.exp.Table):
                names.append(".".join(part.name for part in table.parts).lower())
    except Exception:  # noqa: BLE001
        pass
    return names


def _adapt(case: dict) -> dict:
    """The case in the shape ``sqllineage_bench.build`` and ``predict`` take.

    A schema is keyed by the full table name, while a query may spell it with fewer parts when the project or dataset
    is the default (DataHub's ``default_db``/``default_schema``). The schema is handed over under the spelling the query
    uses, so one table is never known under two names.
    """

    schemas = {}
    spelled = _spellings(case["sql"])
    for name, columns in (case.get("schemas") or {}).items():
        full = re.sub(r"[`\"]", "", name).lower()
        parts = full.split(".")
        key = next(
            (
                spelling
                for cut in range(len(parts))
                for spelling in spelled
                if _norm(spelling) == _norm(".".join(parts[cut:]))
            ),
            full,
        )
        schemas[key] = columns
    return {**case, "schemas": schemas or None}


def _lenient(case: dict) -> bool:
    return bool(case.get("default_db") or case.get("default_schema"))


def _destination(statement):
    """The table a statement writes: INSERT and CREATE (as ``sqllineage_bench``), plus the statements the goldens also cover."""

    name = base_destination(statement)
    if name:
        return name
    exp = base.exp
    table = None
    if isinstance(statement, exp.Alter):
        # a rename reads the old name and writes the new one; one model name cannot say both, so it is not named
        table = None if any(isinstance(a, exp.AlterRename) for a in statement.args.get("actions") or []) else statement.this
    elif isinstance(statement, (exp.Delete, exp.Update, exp.Merge)):
        table = statement.this
    elif isinstance(statement, exp.TruncateTable):
        table = statement.expressions[0] if statement.expressions else None
    elif isinstance(statement, exp.Drop) and str(statement.args.get("kind") or "").upper() in {"TABLE", "VIEW", "MATERIALIZED VIEW"}:
        table = statement.this or next(iter(statement.args.get("tables") or []), None)
    if isinstance(table, exp.Schema):
        table = table.this
    if isinstance(table, exp.Table):
        return ".".join(part.name for part in table.parts)
    return None


base_destination = base._destination


def classify(case: dict) -> dict:
    base._destination = _destination  # only while this case runs: the other bench shares the module
    try:
        return _classify(case)
    finally:
        base._destination = base_destination


def _classify(case: dict) -> dict:
    row = {"id": case["id"], "kind": case["kind"], "dialect": case["dialect"], "corpus": case["corpus"]}
    try:
        got = base.predict(_adapt(case))
    except Exception as exc:  # noqa: BLE001 - a crash is a miss, never a pass
        return {**row, "extra": [], "missing": ["crash"], "outcome": "missed", "why": f"crash {type(exc).__name__}: {str(exc)[:80]}"}
    flagged = bool(got["unknown"]) or bool(got["diagnostics"] & FLAGS)
    lenient = _lenient(case)
    coarse = False
    extra: list = []
    missing: list = []
    # Tables read and written
    want_sources = case.get("sources")
    want_targets = case.get("targets")
    have_sources = {_norm(t) for t in got["reads"]}
    have_targets = {_norm(got["key"])} if got["key"] != "__select__" else set()
    if want_sources is not None:
        extra += [t for t in have_sources if not any(_same_table(w, t, lenient) for w in want_sources)]
        missing += [w for w in want_sources if not any(_same_table(w, t, lenient) for t in have_sources)]
    if want_targets is not None:
        extra += [t for t in have_targets if not any(_same_table(w, t, lenient) for w in want_targets)]
        missing += [w for w in want_targets if not any(_same_table(w, t, lenient) for t in have_targets)]
    edges_expected = edges_found = 0
    if case["kind"] == "column":
        want = {(_norm(s[0]), s[1].lower(), _norm(t[0]) if t[0] != "__select__" else "__select__", _column(t[1])) for s, t in case["edges"]}
        have = {(e[0], e[1], e[2], e[3]) for e in got["edges"]}
        star_expected = {e for e in want if e[1] == "*"}
        for e in sorted(want - star_expected):
            if e in have or any(h[2:] == e[2:] and _same_table(e[0], h[0], lenient) and h[1] == e[1] for h in have):
                continue
            # a struct sub-field edge a.b is met by an edge to its root column a
            root = e[1].split(".")[0]
            if "." in e[1] and any(h[2:] == e[2:] and _same_table(e[0], h[0], lenient) and h[1] == root for h in have):
                coarse = True
                continue
            missing.append(list(e))
        for h in sorted(have):
            if any(w[2:] == h[2:] and _same_table(w[0], h[0], lenient) and (w[1] == h[1] or w[1].split(".")[0] == h[1]) for w in want):
                continue
            if h[1] == "*" or h[0] in base._created_in_script(case):
                continue
            extra.append(list(h))
        edges_expected = len(want - star_expected)
        edges_found = edges_expected - len([m for m in missing if isinstance(m, list)])
        if star_expected and got["unknown"] and not extra:
            missing = [m for m in missing if not (isinstance(m, list) and m[1] == "*")]
    row.update(extra=extra, missing=missing, edges_expected=edges_expected, edges_found=edges_found)
    if not extra and not missing:
        row["outcome"] = "coarse" if coarse else "exact"
    elif flagged and not extra:
        row["outcome"] = "unknown"
    elif extra and case["id"] in DISPUTED:
        row["outcome"] = "disputed"
        row["why"] = DISPUTED[case["id"]]
    elif extra:
        row["outcome"] = "wrong"
    else:
        row["outcome"] = "missed"
    row["diagnostics"] = sorted(got["diagnostics"])
    return row


def run() -> dict:
    corpora = {name: load(name) for name in ("datahub", "openlineage")}
    rows = []
    for name, data in corpora.items():
        for case in data["cases"]:
            scope = scope_of(case)
            row = classify(case)
            row["scope"] = scope
            rows.append(row)

    def tally(selection: list[dict]) -> dict:
        counts = Counter(r["outcome"] for r in selection)
        found = sum(r.get("edges_found", 0) for r in selection)
        wanted = sum(r.get("edges_expected", 0) for r in selection)
        return {
            "total": len(selection), "exact": counts["exact"], "coarse": counts["coarse"], "disputed": counts["disputed"], "unknown": counts["unknown"],
            "missed": counts["missed"], "wrong": counts["wrong"], "edges_expected": wanted, "edges_found": found,
        }

    out = {"rows": rows, "unharvested": {n: len(d["unharvested"]) for n, d in corpora.items()}, "commits": {n: d["commit"] for n, d in corpora.items()}}
    for name in corpora:
        mine = [r for r in rows if r["corpus"] == name]
        out[name] = {
            "in": tally([r for r in mine if r["scope"] == "in"]),
            "other": tally([r for r in mine if r["scope"] != "in"]),
            "dialects": tally([r for r in mine if r["scope"].startswith("dialect")]),
            "left_out": dict(Counter(r["scope"].split(" ")[0] if r["scope"].startswith("dialect") else r["scope"] for r in mine if r["scope"] != "in")),
        }
    return out


def _quiet() -> None:
    os.environ.setdefault("KUMOSQL_TIMING", "0")
    logging.getLogger("sqlglot").setLevel(logging.CRITICAL)


def main(argv: list[str]) -> int:
    _quiet()
    start = time.perf_counter()
    result = run()
    seconds = time.perf_counter() - start
    for name in ("datahub", "openlineage"):
        for part in ("in", "other"):
            t = result[name][part]
            print(f"{name:12} {part:5} {t['exact']}/{t['total']} exact, {t['coarse']} coarse, {t['disputed']} disputed, {t['unknown']} unknown, {t['missed']} missed, {t['wrong']} wrong")
        print("   left out:", result[name]["left_out"])
    if "--details" in argv:
        for r in result["rows"]:
            if r["scope"] == "in" and r["outcome"] in {"wrong", "missed", "unknown", "coarse"}:
                print(r["outcome"], r["id"], "extra", r["extra"][:3], "missing", r["missing"][:3], r.get("why", ""), r.get("diagnostics", ""))
    if "--write-results" in argv:
        write_results(result, seconds)
    return 0


def write_results(result: dict, seconds: float) -> None:
    """One scoreboard row per corpus: they are never added, because only OpenLineage is an independent oracle."""

    def edges(name: str) -> str:
        t = result[name]["in"]
        return f"{t['edges_found']}/{t['edges_expected']} expected column edges found"

    other = {name: result[name]["dialects"] for name in ("datahub", "openlineage")}
    held = "; ".join(
        f"{name}: {t['exact']}/{t['total']} exact, {t['unknown']} unknown, {t['missed']} missed, {t['wrong']} wrong" for name, t in other.items()
    )
    common = {
        "evidence": "executed",
        "date": "2026-10-02",
        "held_out": (
            "Held out: the other-dialect cases (Snowflake, MySQL, T-SQL and so on, read as BigQuery) were never adjudicated or used to "
            f"shape the adapter, so they are an unseen generalisation check; first run: {held}. Their wrong/missed counts are dialect differences "
            "and are not in the headline. The in-scope cases are not held out: each mismatch was read while building the adapter."
        ),
        "performance": f"{result['datahub']['in']['total'] + result['openlineage']['in']['total']} in-scope cases in {seconds:.1f} s",
    }
    oracle = {
        "openlineage": (
            "OpenLineage's own lineage tests (Apache-2.0, commit 7a84fd4d48) for a Rust parser (sqlparser-rs) that shares no code with sqlglot: the independent oracle.",
            "lineage-goldens-openlineage", 250,
        ),
        "datahub": (
            "DataHub's lineage goldens (Apache-2.0, commit 8a117ba1f5). The goldens are sqlglot's own output after review, so agreement is not independent evidence; read this row as regression coverage of BigQuery shapes, not as an accuracy score.",
            "lineage-goldens-datahub", 251,
        ),
    }
    for name, (description, slug, order) in oracle.items():
        t = result[name]["in"]
        score = f"{t['exact'] + t['coarse']}/{t['total']} matched" + (f" ({t['coarse']} only to a struct's root column)" if t["coarse"] else "")
        if t["disputed"]:
            score += f", {t['disputed']} disputed"
        score += f", {t['unknown']} unknown, {t['wrong']} wrong"
        row = {
            "suite": "Lineage goldens: " + ("OpenLineage (independent)" if name == "openlineage" else "DataHub (sqlglot output)"),
            "order": order,
            "size": t["total"],
            "score": score,
            "metric": description + " Each case is one SQL statement with its expected table and column lineage; KumoSQL must produce exactly it or say unknown.",
            "correctness": f"{t['wrong']} cases claim a table or edge the oracle does not have; {t['missed']} confident misses",
            "coverage": {"proven": t["exact"] + t["coarse"], "unknown": t["unknown"], **({"error": t["missed"]} if t["missed"] else {})},
            "docs": "docs/lineage-goldens-bench.md",
            "command": "python tools/lineage_goldens_bench.py --write-results",
            "caveats": (
                f"Cases left out: {sum(result[name]['left_out'].values())} (other dialects, upstream-skipped tests, USE state, not BigQuery); "
                f"{result['unharvested'][name]} upstream tests could not be harvested. Statements other than queries and CREATE ... AS "
                "(DELETE, UPDATE, MERGE, ALTER, DROP, TRUNCATE) are not traced and are reported unknown."
            ),
            "analysis": f"{edges(name)} (column cases only; recall counts edges reported unknown)",
            **common,
        }
        (ROOT / "benchmarks" / "results" / f"{slug}.json").write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
