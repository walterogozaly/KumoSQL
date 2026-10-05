"""Proven pairs of the evals that landed after round one: numeric traps, sample databases, paired engine tests and the fuzzer.

Each adapter proves a pair with the same entry point and options as its eval and turns it into the Case the eval's own
executed check runs (same DuckDB translation, same table types and declared constraints); ``case`` returns None where
the eval does not count the pair as proven.

* ``numeric-traps`` (``tools/numeric_traps_bench.py``): the 112 hand-written BigQuery number and error pairs. A pair counts
  when ``prove_equivalent_smt`` proves it and the proof lists none of the assumptions the case violates or discharges
  (the eval's ``proven`` outcome; an ``assumed`` proof is not a proof there). The Case is the witness replay of the eval:
  ``bigquery_on_duckdb.faithful`` SQL with its settings and macros, results read as BigQuery returns them, over one
  table ``t`` with BIGINT, DOUBLE, DECIMAL(38,9), VARCHAR and BOOLEAN columns. Every pair runs twice: ``<id>`` draws
  finite values only (INT64 and NUMERIC limits and neighbours, floats near 2**53 and 1e308); ``<id>@special`` adds NaN,
  infinities and -0.0 to the FLOAT64 columns and reads results as they are (BigQuery returns NaN). A difference counts
  only where BigQuery's rules agree: DuckDB treats ``NaN = NaN`` as TRUE and sorts NaN last, BigQuery does not, so a
  ``@special`` difference is triaged by hand. A one-side error is not a difference (``notes.one-side-error``): the
  eval's ``refines`` pairs make the original fail where the rewrite does not.
* ``sample-databases-pairs``, ``sample-databases-sakila-pairs``, ``-pagila-pairs``, ``-oracle_co-pairs``,
  ``-oracle_hr-pairs`` (``tools/sample_db_bench.py``): the authored pairs the eval proves, structurally or by the
  algebraic prover with the declared keys, NOT NULL columns and foreign keys (minus the constraint a ``drop`` sibling
  removes), run as the eval runs them (``to_duckdb``: sqlglot's BigQuery to DuckDB, no settings) on random databases
  instead of the one real database. ``sample-databases-pairs`` holds Chinook and Northwind.
* ``engine-paired-tests`` (``tools/engine_pairs_bench.py``): each scored pair the eval proves, translated as the eval's
  ``Engine.text`` does (``prepare_statements`` over the fixture's tables, BigQuery guards) or, for the DuckDB collation
  pair, as written over the fixture's own DDL types.
* ``soundness-fuzz`` (``tools/soundness_fuzz.py --seed 2 --count 2000``): each generated pair whose queries the fuzzer's
  oracle can run (``convert_sql``) and the prover proves with 2 s of solver time. The eval already runs six databases per
  pair; this runs thousands.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import logging
from pathlib import Path
import sys

import sqlglot

TOOLS = Path(__file__).resolve().parent.parent
ROOT = TOOLS.parent
for _path in (str(TOOLS), str(ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from recheck import dialect_rewrites as dr  # noqa: E402
from recheck import engine  # noqa: E402
from recheck.engine import Case, Column, Table  # noqa: E402

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

Adapter = dr.Adapter

_KIND = {"INT64": "int", "FLOAT64": "float", "NUMERIC": "decimal", "BIGNUMERIC": "decimal", "STRING": "text", "BYTES": "text",
         "BOOL": "bool", "DATE": "date", "DATETIME": "timestamp", "TIMESTAMP": "timestamp"}
_DUCK = {"INT64": "BIGINT", "FLOAT64": "DOUBLE", "NUMERIC": "DECIMAL(38,9)", "STRING": "VARCHAR", "BYTES": "BLOB",
         "BOOL": "BOOLEAN", "DATE": "DATE", "DATETIME": "TIMESTAMP", "TIMESTAMP": "TIMESTAMP"}


def _bq_column(name: str, declared: str, not_null: bool = False) -> Column:
    """An engine column for a BigQuery type (``NUMERIC(10, 2)`` keeps its precision and scale)."""

    declared = declared.strip().upper()
    base = declared.split("(")[0].strip()
    sql_type = _DUCK.get(base, "VARCHAR")
    if base in ("NUMERIC", "BIGNUMERIC") and "(" in declared:
        precision, scale = (int(x) for x in declared[declared.index("(") + 1:declared.index(")")].split(","))
        sql_type = f"DECIMAL({precision},{scale})"
    values = (b"", b"a", b"b", b"ab", b"\x00") if base == "BYTES" else ()  # BLOB holds bytes, and a string with accents is no BLOB
    return Column(name, _KIND.get(base, "text"), not_null=not_null, sql_type=sql_type, values=values)


# --- value domains the number evals need ---------------------------------------------------------------------------

_INT64 = [2**63 - 1, -(2**63), 2**63 - 2, -(2**63) + 1, 2**62, 2**53, 2**53 + 1, -(2**53) - 1, 0, 1, -1]
_FLOAT_FINITE = [1e308, -1e308, 1e-308, 2.0**53, 2.0**53 + 2, 0.1, 0.2, 0.3, 1e15, -0.0]
_FLOAT_SPECIAL = [float("nan"), float("inf"), float("-inf"), -0.0, 0.0, 1.0, 1e308]
_NUMERIC_MAX = Decimal("99999999999999999999999999999.999999999")
_NUMERIC = [_NUMERIC_MAX, -_NUMERIC_MAX, Decimal("0.000000001"), Decimal("-0.000000001"), Decimal("0.000000005"), Decimal("0.5"),
            Decimal("1.5"), Decimal("2.5"), Decimal("-2.5"), Decimal("1000000000000000"), Decimal("3.333333333")]

_WRAPPED = False


def _install() -> None:
    """The engine extensions of ``dialect_rewrites`` plus, for a case whose ``meta["numeric"]`` is set, the number domains
    above as extra value flavours (idempotent)."""

    global _WRAPPED
    dr.install()
    if _WRAPPED:
        return
    base = engine.Generator

    class NumericGenerator(base):
        def __init__(self, case, rng):
            super().__init__(case, rng)
            mode = case.meta.get("numeric")
            if mode:
                for kind, values in (("int", _INT64), ("decimal", _NUMERIC), ("float", _FLOAT_FINITE + ([] if mode == "finite" else _FLOAT_SPECIAL))):
                    if kind in self.flavours:
                        self.flavours[kind]["limits"] = list(values)
                if mode == "special" and "float" in self.flavours:
                    self.flavours["float"]["special"] = list(_FLOAT_SPECIAL)

    engine.Generator = NumericGenerator
    _WRAPPED = True


def _held(text: str, modulus: int = 5) -> bool:
    return int(hashlib.sha1(text.encode()).hexdigest(), 16) % modulus == 0


# --- numeric traps -------------------------------------------------------------------------------------------------


def float_literals(tree):
    """``tree`` with each non-integer number literal written ``CAST(x AS FLOAT64)``: BigQuery reads ``0.1`` as FLOAT64 (a
    NUMERIC needs ``NUMERIC '0.1'``), DuckDB as DECIMAL(1,1), so ``0.1 + 0.2 = 0.3`` is TRUE there and FALSE in BigQuery."""

    from sqlglot import exp

    tree = tree.copy()
    for literal in list(tree.find_all(exp.Literal)):
        text = literal.this
        if literal.is_string or not ("." in text or "e" in text.lower()):
            continue
        if isinstance(literal.parent, exp.Cast) and literal.parent.args.get("to") is not None and literal.parent.args["to"].is_type("double", "float"):
            continue
        cast = exp.Cast(this=literal.copy(), to=exp.DataType.build("FLOAT64", dialect="bigquery"))
        if literal is tree:
            return cast
        literal.replace(cast)
    return tree



class NumericTraps(Adapter):
    name = "numeric-traps"

    def items(self) -> list[dict]:
        import numeric_traps_bench as nt

        out = []
        for index, case in enumerate(nt.load_cases("all")):
            for suffix in ("", "@special"):
                out.append({"pair": case.id + suffix, "id": case.id, "special": bool(suffix), "held_out": index % 4 == 3})
        return out

    def case(self, item: dict) -> Case | None:
        import numeric_traps_bench as nt
        from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt

        _install()
        case = next(c for c in nt.load_cases("all") if c.id == item["id"])
        try:
            result = prove_equivalent_smt(case.left, case.right, schema=nt.SCHEMA, types=nt.TYPES, timeout_ms=nt.TIMEOUT_MS)
        except Exception:  # a crash is never a proof
            return None
        if result.status not in (SmtStatus.PROVEN_EQUIVALENT, SmtStatus.PROVEN_CONDITIONALLY):
            return None
        if any(a.startswith(v) for a in result.assumptions for v in case.violates + case.discharged):
            return None  # the eval's "assumed": a proof that lists an assumption the case breaks is not counted
        report = getattr(result, "errors", None)
        sides, faithful = [], True
        for sql in (case.left, case.right):
            text, ok = dr.bigquery_to_duckdb(float_literals(sqlglot.parse_one(sql, read="bigquery")))
            sides.append(text)
            faithful = faithful and ok
        columns = [_bq_column(c, nt.TYPES["t"][c]) for c in nt.SCHEMA["t"]]
        meta = {"label": case.label, "verdict": report.verdict if report is not None else None, "assumptions": list(result.assumptions),
                "faithful": faithful, "numeric": "special" if item["special"] else "finite"}
        if not item["special"]:
            meta["results"] = "bigquery"
        return Case(self.name, item["pair"], sides[0], sides[1], {"t": Table("t", columns)}, setup=dr.bigquery_setup(),
                    held_out=item["held_out"], source=(case.left, case.right), dialect="bigquery", meta=meta)


# --- sample databases ----------------------------------------------------------------------------------------------


class SampleDatabasePairs(Adapter):
    """The authored pairs of one or more sample databases (``pairs.json``)."""

    PROVER_TIMEOUT_MS = None

    def __init__(self, name: str, databases: tuple[str, ...]):
        self.name = name
        self.databases = databases

    def items(self) -> list[dict]:
        import sample_db_bench as sb

        out = []
        for database in self.databases:
            for pair in sb.ADAPTERS[database].pairs():
                out.append({"pair": f"{database}:{pair['id']}", "database": database, "id": pair["id"], "label": pair["label"]})
        return out

    @staticmethod
    def tables(adapter, drop: tuple[str, ...]) -> dict[str, Table]:
        """The adapted DDL's tables with the keys, NOT NULL columns and foreign keys the prover is given (``constraints``)."""

        out = {}
        for table in adapter.schema().values():
            not_null = {c for c in table.not_null if f"not_null:{table.name}.{c}" not in drop}
            keys = [tuple(table.primary_key)] if table.primary_key and f"pk:{table.name}" not in drop else []
            fks = [(tuple(cols), parent, tuple(pcols)) for cols, parent, pcols in table.foreign_keys
                   if not any(f"fk:{table.name}.{c}" in drop for c in cols)]
            columns = [_bq_column(c, kind, not_null=c in not_null) for c, kind in table.columns.items()]
            out[table.name] = Table(table.name, columns, keys, fks)
        return out

    def case(self, item: dict) -> Case | None:
        import sample_db_bench as sb
        from kumosql.algebraic_equivalence import prove_equivalent_algebraic
        from kumosql.equivalence import prove_equivalent
        from kumosql.smt_equivalence import SmtStatus

        _install()
        adapter = sb.ADAPTERS[item["database"]]
        pair = next(p for p in adapter.pairs() if p["id"] == item["id"])
        drop = tuple(pair.get("drop", ()))
        left, right = pair["left"], pair["right"]
        columns, types = adapter.prover_schema()
        lower = {t.lower(): [c.lower() for c in cols] for t, cols in columns.items()}
        proof = None
        try:
            if prove_equivalent(left, right).proven:
                proof = "structural"
        except Exception:  # noqa: BLE001
            pass
        if proof is None:
            try:
                result = prove_equivalent_algebraic(left, right, schema=lower, types=types, constraints=adapter.constraints(drop),
                                                    dialect="bigquery", search_counterexample=True, timeout_ms=sb.PROVER_TIMEOUT_MS)
            except Exception:  # noqa: BLE001
                return None
            if result.status is not SmtStatus.PROVEN_EQUIVALENT:
                return None
            proof = "algebraic"
        tables = self.tables(adapter, drop)
        return Case(self.name, item["pair"], sb.to_duckdb(left), sb.to_duckdb(right), tables, held_out=sb.held_out(f"{item['database']}:{pair['id']}"),
                    source=(left, right), dialect="bigquery",
                    meta={"label": pair["label"], "proof": proof, "drop": list(drop), "category": pair.get("category", "")})


# --- paired engine tests -------------------------------------------------------------------------------------------


class EnginePairs(Adapter):
    name = "engine-paired-tests"

    def items(self) -> list[dict]:
        import engine_pairs_bench as ep

        return [{"pair": c.id, "label": c.label, "source": c.source, "held_out": c.held_out} for c in ep.load_cases() if c.scored]

    def case(self, item: dict) -> Case | None:
        import engine_pairs_bench as ep
        from kumosql import prove_equivalent
        from kumosql.algebraic_equivalence import prove_equivalent_algebraic
        from kumosql.result_equivalence import _local_name, prepare_statements
        from kumosql.smt_equivalence import SmtStatus

        _install()
        case = next(c for c in ep.load_cases() if c.id == item["pair"])
        fixtures = ep.load_fixtures()
        fixture = fixtures[case.fixture]
        types = ep.types_of(fixture)
        schema = {t: list(cols) for t, cols in types.items()}
        proof = None
        if case.dialect == "bigquery":
            try:
                if prove_equivalent(case.left, case.right).proven:
                    proof = "structural"
            except Exception:  # noqa: BLE001
                pass
        if proof is None:
            try:
                result = prove_equivalent_algebraic(
                    case.left, case.right, schema=schema, types=types if case.dialect == "bigquery" else None,
                    constraints=ep.constraints_of(fixture) or None, compare_names=False, dialect=case.dialect,
                    timeout_ms=ep.PROVER_TIMEOUT_MS, search_counterexample=True,
                )
            except Exception:  # noqa: BLE001
                return None
            if result.status is not SmtStatus.PROVEN_EQUIVALENT:
                return None
            proof = "algebraic"
        rules = fixture.get("constraints") or {}
        tables = {}
        for table, spec in fixture["tables"].items():
            facts = rules.get(table, {})
            not_null = {c.lower() for c in facts.get("not_null", ())}
            columns = [_bq_column(c, kind, not_null=c.lower() in not_null) for c, kind in spec["columns"]]
            tables[table] = Table(table, columns, [tuple(k) for k in facts.get("keys", ())])
        if case.dialect == "bigquery":
            sides = []
            for sql in (case.left, case.right):
                statements, target = prepare_statements(ep.inline_struct_rows(sql), types, run_tag="pairs")
                if len(statements) != 1 or target is not None:
                    return None
                sides.append(statements[0])
            tables = {_local_name(name): Table(_local_name(name), t.columns, t.keys, t.foreign_keys) for name, t in tables.items()}
            return Case(self.name, item["pair"], sides[0], sides[1], tables, setup=dr.bigquery_setup(), held_out=case.held_out,
                        source=(case.left, case.right), dialect="bigquery",
                        meta={"label": case.label, "proof": proof, "fixture": case.fixture, "results": "bigquery"})
        # the DuckDB collation pair stays in DuckDB SQL over the fixture's own column types (collations included)
        import re as _re

        ddl = fixture.get("setup", "")
        for statement in _re.split(r";\s*", ddl):
            m = _re.match(r"\s*CREATE TABLE (\w+) \((.*)\)\s*$", statement, _re.S)
            if not m or m.group(1) not in tables:
                continue
            for part in _split_columns(m.group(2)):
                words = part.split(None, 1)
                if len(words) == 2 and words[0].upper() not in ("PRIMARY", "UNIQUE", "FOREIGN", "CONSTRAINT"):
                    kind = _re.sub(r"\s+(NOT NULL|UNIQUE|PRIMARY KEY).*$", "", words[1], flags=_re.I)
                    column = next(c for c in tables[m.group(1)].columns if c.name == words[0])
                    column.sql_type = kind
                    column.kind = "bool" if kind.upper().startswith("BOOL") else "text"
        return Case(self.name, item["pair"], case.left, case.right, tables, held_out=case.held_out, source=(case.left, case.right),
                    dialect="duckdb", meta={"label": case.label, "proof": proof, "fixture": case.fixture})


def _split_columns(body: str) -> list[str]:
    parts, depth, current = [], 0, ""
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current.strip())
    return parts


# --- soundness fuzz ------------------------------------------------------------------------------------------------

FUZZ_SEED, FUZZ_COUNT = 2, 2000  # the results file's command: ``soundness_fuzz.py --seed 2 --count 2000``


class SoundnessFuzz(Adapter):
    name = "soundness-fuzz"

    def items(self) -> list[dict]:
        import soundness_fuzz as sf

        planned = list(sf.historical_cases()) + list(sf.generated_cases(FUZZ_SEED, FUZZ_COUNT))
        out = []
        for case in planned:
            case = {k: v for k, v in case.items() if k != "fixture_variants"}
            out.append({"pair": case["id"], "case": case})
        return out

    def case(self, item: dict) -> Case | None:
        import soundness_fuzz as sf
        from kumosql.algebraic_equivalence import prove_equivalent_algebraic

        _install()
        case = item["case"]
        options = sf.default_options()
        if sf.fixture_errors(case):
            return None
        try:
            left, right = (sf.convert_sql(case[k], case) for k in ("left", "right"))
        except sf.UnsupportedConversion:
            return None
        try:
            result = prove_equivalent_algebraic(case["left"], case["right"], **sf.prover_kwargs(case, options))
        except Exception:  # noqa: BLE001
            return None
        if result.status.value != "proven_equivalent":
            return None
        kinds = {"INT64": ("int", "BIGINT"), "STRING": ("text", "VARCHAR")}
        tables = {}
        for table, columns in case["schema"].items():
            rules = case.get("constraints", {}).get(table, {})
            required = sf._required(rules)
            cols = [Column(n, kinds[k][0], not_null=n in required, sql_type=kinds[k][1]) for n, k in columns]
            keys = [tuple(k) for k in rules.get("keys", [])]
            fks = [(tuple(a), parent, tuple(b)) for a, parent, b in rules.get("foreign_keys", [])]
            tables[table] = Table(table, cols, keys, fks)
        return Case(self.name, item["pair"], left, right, tables, source=(case["left"], case["right"]), dialect="bigquery",
                    meta={"family": case.get("family", ""), "mutation": case.get("mutation", {}).get("name", "")})


ADAPTERS = {
    a.name: a
    for a in [
        NumericTraps(),
        SampleDatabasePairs("sample-databases-pairs", ("chinook", "northwind")),
        SampleDatabasePairs("sample-databases-sakila-pairs", ("sakila",)),
        SampleDatabasePairs("sample-databases-pagila-pairs", ("pagila",)),
        SampleDatabasePairs("sample-databases-oracle_co-pairs", ("oracle_co",)),
        SampleDatabasePairs("sample-databases-oracle_hr-pairs", ("oracle_hr",)),
        EnginePairs(),
        SoundnessFuzz(),
    ]
}
