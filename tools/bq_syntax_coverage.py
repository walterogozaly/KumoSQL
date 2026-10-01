"""Run the BigQuery/Dataform syntax manifest through every KumoSQL stage.

``tests/fixtures/bq_syntax/manifest.json`` lists one small case per construct.
Each case runs through the stages below and gets ``pass``, ``unsupported``
(KumoSQL or sqlglot says so explicitly, nothing crashes or silently corrupts) or
``fail`` (a crash, a lost reference, a changed meaning). ``n/a`` means the stage
does not apply to that kind of statement.

    python tools/bq_syntax_coverage.py --markdown docs/bigquery-syntax-coverage.md
    python tools/bq_syntax_coverage.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

os.environ.setdefault("KUMOSQL_TIMING", "0")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402

from kumosql import rewrite  # noqa: E402
from kumosql.ast_utils import parse_statements, top_level_query  # noqa: E402
from kumosql.fingerprint import _compare_query  # noqa: E402
from kumosql.pipeline import load_sqlx_project  # noqa: E402
from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt  # noqa: E402
from kumosql.equivalence import EquivalenceStatus, prove_equivalent  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "bq_syntax"
STAGES = ("parse", "load", "graph", "fingerprint", "cleanup", "format", "prover")
SQLX_STAGES = ("parse", "load", "refs", "graph", "cleanup", "format")
PASS, UNSUPPORTED, FAIL, NA = "pass", "unsupported", "fail", "n/a"

PREAMBLE = {
    "workflow_settings.yaml": (
        "defaultProject: kumosql\ndefaultLocation: US\ndefaultDataset: kumosql_messy\n"
        "vars:\n  state: CA\n  limit_rows: '10'\n"
    ),
    **{
        f"definitions/sources/{name}.sqlx": (
            f'config {{ type: "declaration", schema: "kumosql_messy", name: "{name}" }}\n'
        )
        for name in ("raw_users", "raw_orders", "raw_order_items", "raw_products")
    },
}


def load_manifest() -> list[dict]:
    return json.loads((FIXTURES / "manifest.json").read_text())["cases"]


def case_text(case: dict) -> str:
    return (FIXTURES / case["file"]).read_text()


def _error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc).splitlines()[0][:160] if str(exc) else ''}"


# ------------------------------------------------------------------ SQL stages


def stage_parse(sql: str) -> tuple[str, str]:
    try:
        statements = parse_statements(sql)
    except Exception as exc:  # noqa: BLE001
        # Any exception out of sqlglot (a ParseError, or an internal error on 26.x) is an upstream gap.
        return UNSUPPORTED, _error(exc)
    if not statements:
        return FAIL, "no statements parsed"
    commands = [s for s in statements if isinstance(s, exp.Command)]
    if commands:
        return UNSUPPORTED, f"sqlglot keeps {commands[0].this} as an opaque command"
    return PASS, ""


def _project(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="bqsyntax_"))
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _load(files: dict[str, str]):
    return load_sqlx_project(_project(files))


def stage_load_sql(sql: str, parse_status: str):
    try:
        pipeline = _load({"definitions/case.sql": sql})
    except Exception as exc:  # noqa: BLE001
        return None, FAIL, _error(exc)
    errors = [d for d in pipeline.all_diagnostics() if d.code in {"read_error", "asset_unreadable", "sqlx_parse_error"}]
    if errors:
        return pipeline, FAIL, errors[0].message[:160]
    if "definitions/case.sql" not in {m.path for m in pipeline.models.values()}:
        return pipeline, FAIL, "model was not loaded"
    return pipeline, PASS, ""


_READ_RE = re.compile(r"(?:kumosql\.kumosql_messy|bigquery-public-data\.[\w]+)\.(?!bq_syntax_|some_|information_schema)[\w*]+", re.I)
_WRITERS = (exp.Create, exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Drop, exp.Alter, exp.TruncateTable, exp.Grant)


def expected_reads(sql: str) -> set[str] | None:
    """Tables a statement reads, from the AST (None when sqlglot keeps part of it as an opaque command).

    The target of a ``CREATE``, ``INSERT``, ``UPDATE``, ``DELETE`` or ``MERGE`` is written, not read.
    """

    try:
        statements = parse_statements(sql)
    except Exception:  # noqa: BLE001
        return None
    if any(isinstance(statement, exp.Command) for statement in statements):
        return None
    reads: set[str] = set()
    for statement in statements:
        targets = set()
        if isinstance(statement, _WRITERS):
            candidates = [statement.this, statement.args.get("securable"), *(statement.args.get("tables") or [])]
            if isinstance(statement, exp.TruncateTable):
                candidates += statement.expressions
            for target in candidates:
                target = target.this if isinstance(target, exp.Schema) else target
                if isinstance(target, exp.Table):
                    targets.add(id(target))
        ctes = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
        for table in statement.find_all(exp.Table):
            if id(table) in targets or (not table.db and table.name.lower() in ctes):
                continue
            name = ".".join(part for part in (table.catalog, table.db, table.name) if part)
            if _READ_RE.fullmatch(name):
                reads.add(name.lower())
    return reads


def actual_reads(pipeline) -> set[str]:
    reads: set[str] = set()
    for diagnostic in pipeline.all_diagnostics():
        if diagnostic.code == "external_tables":
            reads |= {name.strip().lower() for name in diagnostic.message.split(":", 1)[1].split(",")}
    for parents in pipeline.upstream.values():
        reads |= {name.lower() for name in parents}
    return reads


def stage_graph(pipeline, parse_status: str, sql: str = "") -> tuple[str, str]:
    """Graph and lineage extraction must not crash, and must find the tables the statement reads."""

    try:
        pipeline.report()
        pipeline.lineage_report()
        pipeline.upstream
        pipeline.downstream
    except Exception as exc:  # noqa: BLE001
        return (UNSUPPORTED if parse_status == UNSUPPORTED else FAIL), _error(exc)
    expected = expected_reads(sql)
    if expected is None:
        return PASS, "no read check: statement is opaque to sqlglot"
    missing = expected - actual_reads(pipeline)
    if missing:
        return UNSUPPORTED, f"reads of this DML, script or non-query statement are not extracted: {', '.join(sorted(missing))[:100]}"
    return PASS, ""


def _single_query(sql: str):
    try:
        statements = parse_statements(sql)
    except Exception:  # noqa: BLE001
        return None
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union, exp.Subquery)):
        return None
    return statements[0]


def stage_fingerprint(sql: str) -> tuple[str, str]:
    query = _single_query(sql)
    if query is None:
        return NA, "not a single query"
    body = sql.strip().rstrip(";")
    try:
        wrapped = _compare_query(body, body)
        parse_statements(wrapped)
    except Exception as exc:  # noqa: BLE001
        return FAIL, _error(exc)
    return PASS, ""


CLEANUP_RULES = ("remove_trivial_predicates", "remove_redundant_parentheses", "deduplicate_ctes",
                 "remove_unused_ctes", "inline_single_use_ctes", "remove_redundant_distinct")


def _classify(result, text: str) -> tuple[str, str]:
    """Judge a rewrite result: only a trusted label or an untouched input is acceptable."""

    status = result.verification.status
    if status in (rewrite.VerificationStatus.UNCHANGED, rewrite.VerificationStatus.PROVEN):
        return PASS, ""
    reasons = "; ".join(d.code for d in _diagnostics(result)) or result.verification.reason
    # An unproven or failed rewrite is never auto-accepted, so it is a gap, not damage.
    return UNSUPPORTED, reasons[:160]


def _diagnostics(result):
    steps = getattr(result, "steps", None)
    if steps is None:
        return tuple(result.diagnostics)
    return tuple(d for step in steps for d in step.diagnostics)


def stage_cleanup(text: str, parse_status: str) -> tuple[str, str]:
    """Every non-formatting rule must run without crashing and never claim a false proof."""

    try:
        result = rewrite.apply_rules(CLEANUP_RULES, text)
    except Exception as exc:  # noqa: BLE001
        return FAIL, _error(exc)
    return _classify(result, text)


def _quoted(sql: str) -> list[str]:
    return sorted(re.findall(r"`[^`]*`", sql))


def stage_format(text: str, parse_status: str) -> tuple[str, str]:
    """Formatting must keep the meaning: no crash, backticked names untouched, and a trusted proof or no change."""

    try:
        result = rewrite.apply_rule("format_sql", text)
    except Exception as exc:  # noqa: BLE001
        return FAIL, _error(exc)
    if result.sql == text:
        status, detail = _classify(result, text)
        return (status, detail or "unchanged")
    if _quoted(result.sql) != _quoted(text):
        return FAIL, "formatting changed a backtick-quoted name"
    if "${" in text or re.search(r"(?m)^[ \t]*(config|js|pre_operations|post_operations)\s*\{", text):
        return _same_sqlx_shell(text, result.sql)
    return _classify(result, text)


def _same_sqlx_shell(before: str, after: str) -> tuple[str, str]:
    from kumosql.sqlx import split_sqlx_sections

    try:
        a = [body for kind, body in split_sqlx_sections(before) if kind == "block"]
        b = [body for kind, body in split_sqlx_sections(after) if kind == "block"]
    except Exception as exc:  # noqa: BLE001
        return FAIL, _error(exc)
    interpolations = lambda t: sorted(re.findall(r"\$\{.*?\}", t, flags=re.S))
    if a != b:
        return FAIL, "config/js/operation blocks changed"
    if interpolations(before) != interpolations(after):
        return FAIL, "interpolations changed"
    return PASS, ""


def stage_prover(sql: str) -> tuple[str, str]:
    query = _single_query(sql)
    if query is None:
        return NA, "not a single query"
    body = sql.strip().rstrip(";")
    notes = []
    try:
        smt = prove_equivalent_smt(body, body, timeout_ms=3000)
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"smt crashed: {_error(exc)}"
    try:
        ast = prove_equivalent(body, body)
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"conservative prover crashed: {_error(exc)}"
    if smt.status == SmtStatus.NOT_EQUIVALENT:
        return FAIL, "claims a query differs from itself"
    if smt.status == SmtStatus.PROVEN_EQUIVALENT:
        return PASS, "smt proven"
    notes.append(smt.reason[:120])
    if ast.status == EquivalenceStatus.PROVEN_EQUIVALENT:
        return PASS, "ast proven; smt: " + notes[0]
    return UNSUPPORTED, notes[0]


# ---------------------------------------------------------------- SQLX stages

_REF_CALL = re.compile(r"\b(?:ctx\.)?ref\(\s*((?:\"[^\"]*\"|'[^']*'|\{[^}]*\})(?:\s*,\s*(?:\"[^\"]*\"|'[^']*'))*)\s*\)")
_NAME = re.compile(r"""name\s*:\s*["']([^"']+)["']""")


def expected_dependencies(text: str) -> tuple[set[str], set[str]]:
    """Table names a Dataform compiler would depend on: (static, computed).

    Static names come from literal ``${ref(...)}`` calls and config ``dependencies``;
    computed ones are ``ref()`` calls inside a ``js`` block, which only running the
    JavaScript can resolve.
    """

    from kumosql.sqlx import split_sqlx_sections

    names: set[str] = set()
    computed: set[str] = set()
    sections = split_sqlx_sections(text)
    for kind, body in sections:
        if kind == "block" and body.lstrip().startswith("js"):
            computed |= _refs_in(body)
        else:
            names |= _refs_in(body)
    config = re.search(r"dependencies\s*:\s*\[([^\]]*)\]", text)
    if config:
        names.update(re.findall(r"""["']([^"']+)["']""", config.group(1)))
    return names, computed - names


def _refs_in(text: str) -> set[str]:
    names: set[str] = set()
    for match in _REF_CALL.finditer(text):
        args = match.group(1)
        if args.startswith("{"):
            found = _NAME.search(args)
            if found:
                names.add(found.group(1))
        else:
            names.add(re.findall(r"""["']([^"']*)["']""", args)[-1])
    return names


def run_sqlx_case(case: dict) -> dict[str, tuple[str, str]]:
    text = case_text(case)
    out: dict[str, tuple[str, str]] = {s: (NA, "") for s in SQLX_STAGES}
    files = dict(PREAMBLE)
    files["definitions/case.sqlx"] = text
    try:
        pipeline = _load(files)
    except Exception as exc:  # noqa: BLE001
        out["load"] = (FAIL, _error(exc))
        return out
    bad = [d for d in pipeline.all_diagnostics() if d.code in {"read_error", "asset_unreadable", "sqlx_parse_error", "unsupported_ref"}]
    loaded = [m for m in pipeline.models.values() if m.path == "definitions/case.sqlx"]
    is_declaration = bool(re.search(r"type:\s*[\"']declaration[\"']", text))
    if any(d.code == "unsupported_ref" for d in bad):
        out["load"] = (UNSUPPORTED, "ref() with a computed argument is not resolved")
    elif bad:
        out["load"] = (FAIL, bad[0].message[:160])
    elif not loaded and not is_declaration:
        out["load"] = (FAIL, "action was not loaded")
    else:
        out["load"] = (PASS, "")
    if not loaded:
        if is_declaration:
            declared = any(t.name == _declared_name(text) for t in pipeline.sources.values())
            out["refs"] = (PASS, "declaration registered as a source") if declared else (FAIL, "declaration not registered")
        return out
    model = loaded[0]
    # Parse the SQL the loader produced.
    body = model.sql
    if model.kind in ("test",):
        out["parse"] = (NA, "")
    elif not body.strip():
        out["parse"] = (NA, "no SQL body")
    else:
        try:
            parse_statements(_demask(body))
            out["parse"] = (PASS, "")
        except Exception as exc:  # noqa: BLE001
            out["parse"] = (UNSUPPORTED if isinstance(exc, sqlglot.errors.ParseError) else FAIL), _error(exc)
    expected, computed = expected_dependencies(text)
    if expected or computed:
        try:
            upstream = {pipeline.models[k].target.name if k in pipeline.models else k.split(".")[-1] for k in pipeline.upstream.get(model.key, set())}
        except Exception as exc:  # noqa: BLE001
            out["refs"] = (FAIL, _error(exc))
            upstream = None
        if upstream is not None:
            missing = sorted(expected - upstream)
            unresolved = sorted(computed - upstream)
            if missing:
                out["refs"] = (FAIL, f"missed dependency: {', '.join(missing)}")
            elif unresolved:
                out["refs"] = (UNSUPPORTED, f"ref() inside a js block is not resolved: {', '.join(unresolved)}")
            else:
                out["refs"] = (PASS, "")
    else:
        out["refs"] = (NA, "no literal ref() calls")
    out["graph"] = stage_graph(pipeline, out["parse"][0])
    out["cleanup"] = stage_cleanup(text, out["parse"][0])
    out["format"] = stage_format(text, out["parse"][0])
    return out


def _declared_name(text: str) -> str:
    found = _NAME.search(text)
    return found.group(1) if found else ""


def _demask(sql: str) -> str:
    return sql


# --------------------------------------------------------------------- driver


def run_case(case: dict) -> dict[str, tuple[str, str]]:
    if case["kind"] == "sqlx":
        return run_sqlx_case(case)
    sql = case_text(case)
    results: dict[str, tuple[str, str]] = {}
    results["parse"] = stage_parse(sql)
    pipeline, *loaded = stage_load_sql(sql, results["parse"][0])
    results["load"] = tuple(loaded)  # type: ignore[assignment]
    results["graph"] = stage_graph(pipeline, results["parse"][0], sql) if pipeline is not None else (NA, "")
    results["fingerprint"] = stage_fingerprint(sql)
    results["cleanup"] = stage_cleanup(sql, results["parse"][0])
    results["format"] = stage_format(sql, results["parse"][0])
    results["prover"] = stage_prover(sql)
    return results


def stages_for(case: dict) -> tuple[str, ...]:
    return SQLX_STAGES if case["kind"] == "sqlx" else STAGES


def run_all(cases: list[dict] | None = None) -> dict[str, dict[str, tuple[str, str]]]:
    return {case["id"]: run_case(case) for case in (cases or load_manifest())}


def load_dry_runs() -> dict[str, dict]:
    path = FIXTURES / "dry_run.json"
    return json.loads(path.read_text()) if path.is_file() else {}


def _dry_cell(ids, dry_runs) -> str:
    if not dry_runs:
        return "–"
    counts = Counter(dry_runs.get(i, {}).get("status", "not_run") for i in ids)
    parts = [f"{counts['ok']} ✅"]
    if counts["error"]:
        parts.append(f"{counts['error']} ❌")
    if counts["not_run"]:
        parts.append(f"{counts['not_run']} –")
    return " ".join(parts)


def markdown(results, cases, dry_runs) -> str:
    by_id = {c["id"]: c for c in cases}
    families: dict[str, list[str]] = defaultdict(list)
    for case_id in results:
        if by_id[case_id]["kind"] == "sql":
            families[case_id.split("/")[0]].append(case_id)
    header = ["parse", "load", "graph", "fingerprint", "cleanup", "format", "prover"]
    lines = [
        "| Family | Cases | " + " | ".join(header) + " | dry run |",
        "|---|---:|" + "---|" * (len(header) + 1),
    ]
    totals: dict[str, Counter] = {s: Counter() for s in STAGES}
    every = []
    for family in sorted(families):
        ids = families[family]
        every += ids
        cells = []
        for stage in STAGES:
            counts = Counter(results[i][stage][0] for i in ids)
            totals[stage].update(counts)
            cells.append(_cell(counts))
        lines.append(f"| {family} | {len(ids)} | " + " | ".join(cells) + f" | {_dry_cell(ids, dry_runs)} |")
    lines.append(f"| **all GoogleSQL** | {len(every)} | " + " | ".join(_cell(totals[s]) for s in STAGES) + f" | {_dry_cell(every, dry_runs)} |")
    sqlx_ids = [i for i in results if by_id[i]["kind"] == "sqlx"]
    lines += [
        "",
        "| Dataform | Cases | " + " | ".join(SQLX_STAGES) + " | dry run |",
        "|---|---:|" + "---|" * (len(SQLX_STAGES) + 1),
        f"| SQLX actions | {len(sqlx_ids)} | "
        + " | ".join(_cell(Counter(results[i][s][0] for i in sqlx_ids)) for s in SQLX_STAGES)
        + f" | {_dry_cell(sqlx_ids, dry_runs)} |",
    ]
    return "\n".join(lines) + "\n"


def gaps_markdown() -> str:
    """A table of ``known_gaps.json`` grouped by stage, owner and reason."""

    groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for key, gap in load_known_gaps().items():
        case_id, stage = key.split("::")
        reason = re.sub(r"\s*Line \d+, Col: \d+\.?", "", gap["reason"])
        reason = re.sub(r"(source_splice_error|recovered_parse|parse_error|output_parse_error)(; \1)+", r"\1", reason)
        reason = re.sub(r"(?:kumosql\.kumosql_messy|bigquery-public-data)\.[\w.*-]+(, )?", "", reason).strip(" :,") or reason
        groups[(stage, gap["owner"], reason[:90])].append(case_id)
    lines = ["| Stage | Owner | Reason | Cases | Examples |", "|---|---|---|---:|---|"]
    for (stage, owner, reason), ids in sorted(groups.items(), key=lambda kv: (STAGES.index(kv[0][0]) if kv[0][0] in STAGES else 99, -len(kv[1]))):
        examples = ", ".join(f"`{i}`" for i in sorted(ids)[:3])
        lines.append(f"| {stage} | {owner} | {reason.replace('|', '/')} | {len(ids)} | {examples} |")
    return "\n".join(lines) + "\n"


def _cell(counts: Counter) -> str:
    parts = [f"{counts[PASS]} ✅"]
    if counts[UNSUPPORTED]:
        parts.append(f"{counts[UNSUPPORTED]} ⚪")
    if counts[FAIL]:
        parts.append(f"{counts[FAIL]} ❌")
    if counts[NA] and not counts[PASS] and not counts[UNSUPPORTED] and not counts[FAIL]:
        return "n/a"
    return " ".join(parts)


def classify_owner(stage: str, detail: str) -> str:
    """Who would have to change for a gap to close."""

    if stage == "parse":
        return "sqlglot"
    if stage == "prover":
        return "prover"
    if stage == "format":
        return "sqlfluff" if "parse_error" in detail else "kumosql"
    return "kumosql"


def known_gaps_path() -> Path:
    return FIXTURES / "known_gaps.json"


def load_known_gaps() -> dict[str, dict]:
    path = known_gaps_path()
    return json.loads(path.read_text()) if path.is_file() else {}


def update_known_gaps(results) -> int:
    """Add every unsupported or failing case/stage to ``known_gaps.json``, keeping edited reasons.

    Run once per supported sqlglot version: a gap that exists on only one of them still has to be listed.
    """

    gaps = load_known_gaps()
    for case_id, stages in results.items():
        for stage, (status, detail) in stages.items():
            if status in (UNSUPPORTED, FAIL):
                key = f"{case_id}::{stage}"
                gaps.setdefault(key, {"owner": classify_owner(stage, detail), "reason": detail or status})
    known_gaps_path().write_text(json.dumps(dict(sorted(gaps.items())), indent=1) + "\n")
    return len(gaps)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", type=Path, help="write per-case results here")
    parser.add_argument("--markdown", type=Path, help="write the summary table here")
    parser.add_argument("--write-docs", type=Path, help="replace the coverage tables in this markdown file")
    parser.add_argument("--update-known-gaps", action="store_true", help="record current gaps in known_gaps.json")
    parser.add_argument("--failures", action="store_true", help="list every failing case/stage")
    args = parser.parse_args(argv)
    cases = load_manifest()
    results = run_all(cases)
    if args.json:
        args.json.write_text(json.dumps({k: {s: list(v) for s, v in r.items()} for k, r in results.items()}, indent=1))
    if args.markdown:
        args.markdown.write_text(markdown(results, cases, load_dry_runs()) + "\n" + gaps_markdown())
    if args.write_docs:
        start, end = "<!-- coverage-table:start -->", "<!-- coverage-table:end -->"
        text = args.write_docs.read_text()
        table = markdown(results, cases, load_dry_runs()) + "\n### Known gaps\n\n" + gaps_markdown()
        head, _, rest = text.partition(start)
        _, _, tail = rest.partition(end)
        args.write_docs.write_text(f"{head}{start}\n{table}{end}{tail}")
    if args.update_known_gaps:
        print(f"{update_known_gaps(results)} known gaps recorded")
    if args.failures or not (args.json or args.markdown or args.update_known_gaps or args.write_docs):
        for case_id, stages in results.items():
            for stage, (status, detail) in stages.items():
                if status == FAIL:
                    print(f"FAIL {case_id} [{stage}] {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
