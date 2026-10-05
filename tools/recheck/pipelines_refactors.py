"""Proven items of the pipeline, refactor, fix and output-property evals, as engine cases.

Each pair is proved as its eval proves it and run as the eval's own executed check runs it (same DuckDB
translation, tables, column types and declared constraints).

* ``sqlfluff-semantic-fixes`` (``tools/sqlfluff_fixtures_bench.py semantic``): each fail/fix pair the
  algebraic prover proves, read in the fixture's dialect with the schema read off the queries, run as
  ``kumosql.random_check`` translates it (``_duck``: BigQuery through ``bigquery_on_duckdb``, other dialects by
  sqlglot). Where the eval's own run cannot (schema-qualified tables, a query DuckDB does not bind) the eval
  counts the proof "not re-checkable", and so the record is ``unrunnable`` or ``search-error``. One addition:
  when the guessed column types do not bind, every column is tried as VARCHAR (a proof claims every typing).
* ``duplicate-exact`` and ``shared-refactors-proof`` (``tools/dup_bench.py --sizes 600 --seed 2``): the generated
  600-model project of seed 2, run as ``dup_bench.rows_of`` runs a model (plain sqlglot to DuckDB, qualifiers
  dropped, no settings). Every exact-duplicate pair the eval scores (each group's scored copies against the
  group's first scored copy of the same split; equality is transitive) and, for every ready shared-model
  proposal, each edited model before and after the refactor (the shared SELECT inlined as a derived table,
  which is what ``verify_proposal`` proves). The proving happens in ``items()`` (one project analysis).
* ``pipeline-equivalence`` (``tools/pipeline_bench.py``, dev and held-out families): one item per (case, output)
  the prover proves; each pipeline is one query, every model a CTE translated as ``pipeline_bench.Executor``
  translates it (``_flat`` then ``faithful``). A project with a model that cannot be read as BigQuery does is
  an error in the eval and has no items.
* ``incremental-proofs`` (``tools/incremental_bench.py``): the inductive step of each proven case. The target
  holds the full refresh of the old sources; one batch allowed by the contract (inserted rows, touched/updated
  rows, deletions, as separate tables tied to the old rows by a ``legal`` check) is applied; the left query is
  the Dataform run as the simulator runs it (append, or MERGE; a MERGE conflict, which the eval counts as a
  divergence, is one marked row no full refresh returns), the right one the full refresh of the new sources.
  Queries are translated by the simulator's own ``_pin_clock``. A first divergence after any number of batches
  is such a step, so this covers every run length. Assumptions the eval states are declared (key unique and
  non-NULL, event times after the COALESCE default, a re-aggregated group never NULL). ``pre_operations`` are
  not modelled, so such a case is not re-checkable.
* ``table-minimization`` (``tools/minimization_bench.py --jobs 4``, dev and held-out): the minimizer's output
  for each case (cached under ``$RECHECK_SCRATCH``), judged by the harness's ``check_output``; one item per
  case that the harness proved (not merely checked), covering every protected table it proved: each row
  tagged with its table and column names, packed into one text column, translated as the harness's views are.
* ``output-properties`` and ``output-properties-adapted`` (``tools/output_properties_bench.py``): every claim
  the analysis makes about a query becomes a violation query (rows breaking it, tagged by claim); the right
  side is the same query with ``WHERE FALSE``. The query is translated as the eval's ``to_duck`` and
  ``run_adapted`` translate it.
"""

from __future__ import annotations

import contextlib
import functools
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import sqlglot
from sqlglot import exp

TOOLS = Path(__file__).resolve().parent.parent
ROOT = TOOLS.parent
for _path in (str(TOOLS), str(ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from recheck.engine import Case, Column, Table  # noqa: E402

SCRATCH = Path(os.environ.get("RECHECK_SCRATCH") or Path(tempfile.gettempdir()) / "kumosql-recheck-pipelines")


class NotRechecked(Exception):
    """A pair the eval counts as proven but whose DuckDB translation or run is not available (the eval itself
    reports it "not re-checkable"); the record's verdict is ``search-error`` with this message."""


class Adapter:
    """``items()`` lists every pair (picklable); ``case(item)`` proves it as the eval does and returns a Case, or None."""

    name = ""

    def items(self) -> list[dict]:
        raise NotImplementedError

    def case(self, item: dict) -> Case | None:
        raise NotImplementedError


# ------------------------------------------------------------------ shared helpers


def bq_setup() -> tuple[str, ...]:
    from kumosql.bigquery_on_duckdb import MACROS, SETTINGS

    return tuple(SETTINGS) + tuple(MACROS)


def to_duck(sql: str, dialect: str = "bigquery") -> str:
    """The DuckDB SQL ``kumosql.random_check`` runs for ``sql`` (BigQuery through ``bigquery_on_duckdb``);
    raises ``NotRechecked`` where the eval's own translation raises (it then counts the pair "not re-checkable")."""

    from kumosql.random_check import CheckError, _duck

    try:
        return _duck(sql, dialect)
    except CheckError as error:
        raise NotRechecked(f"not re-checkable: {error}"[:300]) from error


def describe(tables: dict[str, Table], sql: str, setup: tuple[str, ...] = ()) -> tuple[list[str], list[str]]:
    """Output column names and DuckDB types of ``sql`` over empty ``tables``; raises on a bind error."""

    import duckdb

    db = duckdb.connect(":memory:")
    try:
        for statement in setup:
            db.execute(statement)
        for table in tables.values():
            columns = ", ".join(f'"{c.name}" {c.ddl_type()}' for c in table.columns)
            db.execute(f'CREATE TABLE "{table.name}" ({columns})')
        rows = db.execute(f"DESCRIBE {sql}").fetchall()
        return [r[0] for r in rows], [str(r[1]) for r in rows]
    finally:
        db.close()


def runs(tables: dict[str, Table], sqls, setup: tuple[str, ...] = ()) -> str:
    """``""`` when every query binds and runs on empty tables, else the first error."""

    import duckdb

    db = duckdb.connect(":memory:")
    try:
        for statement in setup:
            db.execute(statement)
        for table in tables.values():
            columns = ", ".join(f'"{c.name}" {c.ddl_type()}' for c in table.columns)
            db.execute(f'CREATE TABLE "{table.name}" ({columns})')
        for sql in sqls:
            try:
                db.execute(sql).fetchall()
            except duckdb.Error as error:
                return f"{type(error).__name__}: {str(error).splitlines()[0][:200]}"
        return ""
    finally:
        db.close()


_KIND = {"INT64": "int", "INTEGER": "int", "STRING": "text", "FLOAT64": "float", "NUMERIC": "decimal", "BOOL": "bool",
         "BOOLEAN": "bool", "DATE": "date", "TIMESTAMP": "timestamp", "DATETIME": "timestamp"}
_DUCK = {"INT64": "BIGINT", "INTEGER": "BIGINT", "STRING": "VARCHAR", "FLOAT64": "DOUBLE", "NUMERIC": "DECIMAL(38,9)",
         "BOOL": "BOOLEAN", "BOOLEAN": "BOOLEAN", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP", "DATETIME": "TIMESTAMP"}


def bq_column(name: str, bq_type: str, not_null: bool = False) -> Column:
    t = bq_type.upper()
    return Column(name, _KIND.get(t, "text"), not_null=not_null, sql_type=_DUCK.get(t, "VARCHAR"))


@contextlib.contextmanager
def silenced():
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        yield


# ------------------------------------------------------------------ sqlfluff semantic fixes


class SqlfluffSemantic(Adapter):
    name = "sqlfluff-semantic-fixes"

    def items(self) -> list[dict]:
        import sqlfluff_fixtures_bench as sf

        out = []
        for case in sf.semantic_cases(sf.load_cases()):
            item = {"pair": case.id, "id": case.id, "rule": case.rule, "dialect": case.dialect, "fail": case.fail,
                    "fix": case.fix, "configs": case.configs, "held_out": case.held_out}
            out.append(item)
            # the eval also compares a case it cannot read as one query in an adapted form (outside its score)
            for number, (fail, fix) in enumerate(self._adapted(case), 1):
                out.append({**item, "pair": f"{case.id}#adapted{number}", "fail": fail, "fix": fix})
        return out

    @staticmethod
    def _adapted(case) -> list[tuple[str, str]]:
        """The query pairs ``sqlfluff_fixtures_bench.decide_adapted`` proves one by one: the template-masked pair of a
        Jinja case, or each changed statement's query of a script, ``INSERT ... SELECT`` or ``CREATE TABLE ... AS``."""

        import sqlfluff_fixtures_bench as sf

        if "{{" in case.fail or "{%" in case.fail:
            return [(sf.mask_templates(case.fail), sf.mask_templates(case.fix))]
        try:
            left, right = sf.parse_all(case.fail, case.dialect), sf.parse_all(case.fix, case.dialect)
        except sf.Unsupported:
            return []
        if len(left) == 1 and len(right) == 1 and isinstance(left[0], sf.exp.Query) and isinstance(right[0], sf.exp.Query):
            return []
        pairs = sf.adapted_pairs(case)
        return [] if isinstance(pairs, str) else list(pairs)

    def case(self, item: dict) -> Case | None:
        import sqlfluff_fixtures_bench as sf
        from kumosql.algebraic_equivalence import prove_equivalent_algebraic

        fixture = sf.Case(item["id"], item["rule"], item["dialect"], item["fail"], item["fix"], item["configs"])
        try:
            left, right = sf.parse_all(fixture.fail, fixture.dialect), sf.parse_all(fixture.fix, fixture.dialect)
        except sf.Unsupported:
            return None
        label = sf.meaning_label(fixture, left, right)
        if len(left) != 1 or len(right) != 1:
            return None
        schema, kinds = sf.infer_schema(left + right)
        dialect = sf.DIALECTS[fixture.dialect]
        try:
            result = prove_equivalent_algebraic(fixture.fail, fixture.fix, schema=schema, compare_names=True, dialect=dialect, timeout_ms=4000)
        except Exception:  # a crash is a failure to prove
            return None
        if not result.proven:
            return None
        # the eval's own translation (``random_check._duck``); where it raises the eval reports "not re-checkable"
        sqls = [to_duck(sql, dialect) for sql in (fixture.fail, fixture.fix)]
        notes = []
        setup = bq_setup() if dialect == "bigquery" else ()

        def tables(all_text: bool) -> dict[str, Table]:
            out = {}
            for name, columns in schema.items():
                cols = [Column(c, "text" if all_text or kinds.get(f"{name}.{c}") == "text" else "int") for c in columns]
                out[name] = Table(name, cols)
            return out

        chosen = tables(False)
        error = runs(chosen, sqls, setup)
        if error:
            alternative = tables(True)
            if not runs(alternative, sqls, setup):
                chosen = alternative
                notes.append(f"guessed types do not bind ({error[:80]}); every column VARCHAR (the eval does not re-check this pair)")
        meta = {"label": label, "rule": fixture.rule, "dialect": fixture.dialect, "reason": result.reason[:200]}
        if notes:
            meta["notes"] = sorted(set(notes))
        return Case(self.name, item["pair"], sqls[0], sqls[1], chosen, setup=setup, held_out=item["held_out"],
                    source=(fixture.fail, fixture.fix), dialect=fixture.dialect or "ansi", meta=meta)


# ------------------------------------------------------------------ duplicate detection and shared refactors

DUP_SIZE, DUP_SEED = 600, 2


def _standalone(select: exp.Expression) -> exp.Expression | None:
    """``select`` with every CTE it can see from the queries around it, as one query; None on a name clash."""

    visible: list[list[exp.CTE]] = []
    child, node = select, select.parent
    while node is not None:
        if isinstance(node, exp.CTE):
            with_ = node.parent
            if isinstance(with_, exp.With):
                position = next(i for i, c in enumerate(with_.expressions) if c is node)
                visible.append(list(with_.expressions[:position]))
                owner = with_.parent
                if owner is None:
                    break
                child, node = owner, owner.parent
                continue
        if isinstance(node, exp.Query):
            with_ = node.args.get("with_") or node.args.get("with")
            if with_ is not None and child is not with_:
                visible.append(list(with_.expressions))
        child, node = node, node.parent
    out = select.copy()
    outer = [cte.copy() for group in reversed(visible) for cte in group]
    if not outer:
        return out
    own = out.args.get("with_") or out.args.get("with")
    ctes = outer + (list(own.expressions) if own is not None else [])
    names = [c.alias_or_name.lower() for c in ctes]
    if len(set(names)) != len(names):
        return None
    key = "with_" if "with_" in out.arg_types else "with"
    out.set(key, exp.With(expressions=ctes))
    return out


def _dup_tables(sqls) -> dict[str, Table]:
    import dup_bench as gen

    names = set()
    for sql in sqls:
        for table in sqlglot.parse_one(sql, read="duckdb").find_all(exp.Table):
            names.add(table.name.lower())
    out = {}
    for name in sorted(names):
        if re.fullmatch(r"t\d{3}", name):
            spec = gen.TABLE_COLUMNS
        elif re.fullmatch(r"dt\d{3}", name):
            spec = gen.DIM_COLUMNS
        else:
            continue  # a CTE
        out[name] = Table(name, [Column(c, "int" if k == "int" else "text", sql_type="INTEGER" if k == "int" else "VARCHAR")
                                 for c, k in spec.items()])
    return out


@functools.lru_cache(maxsize=None)
def _dup_analysis() -> dict:
    """Both dup evals' proven items for the generated project (seed 2, 600 models)."""

    import dup_bench as gen
    import dup_bench_run as run
    from kumosql import load_compiled_graph
    from kumosql.pipeline_duplicates import _fingerprint_hash
    from kumosql.shared_logic import propose_shared_logic, refactor_sql

    os.environ.setdefault("KUMOSQL_TIMING", "0")
    project = gen.generate(DUP_SIZE, DUP_SEED)
    with silenced():
        pipeline = load_compiled_graph(project.graph)
        groups = pipeline.duplicate_selects()
        proposals = propose_shared_logic(pipeline, verify=True, verify_limit=10**6)
    parsed = pipeline._analyse().parsed
    databases = [gen.make_database(project.tables, 100 + i) for i in range(run.DATABASES)]
    labels = gen.check_labels(project, databases)
    skip = set(labels["decoy_agree"])
    site = {s.model: s for s in project.sites}

    def scored(model: str, location: str) -> bool:
        s = site.get(model)
        return s is not None and location in (s.location, "query") and model not in skip

    def occurrence_sql(model: str, location: str, fingerprint: str) -> str | None:
        tree = parsed.get(model)
        if tree is None:
            return None
        for select in tree.find_all(exp.Select):
            from kumosql.pipeline_duplicates import _select_location

            if _select_location(select) == location and _fingerprint_hash(select) == fingerprint:
                whole = _standalone(select)
                return None if whole is None else whole.sql(dialect="bigquery")
        return None

    exact = []
    for group in groups:
        members = [o for o in group.occurrences if scored(o.model, o.location)]
        for holdout in (False, True):
            part = [o for o in members if site[o.model].holdout is holdout]
            # one occurrence per model (the eval scores model pairs)
            seen, chosen = set(), []
            for o in part:
                if o.model not in seen:
                    seen.add(o.model)
                    chosen.append(o)
            if len(chosen) < 2:
                continue
            first = chosen[0]
            first_sql = occurrence_sql(first.model, first.location, group.fingerprint)
            for other in chosen[1:]:
                other_sql = occurrence_sql(other.model, other.location, group.fingerprint)
                exact.append({
                    "pair": f"{first.model.split('.')[-1]}@{first.location}={other.model.split('.')[-1]}@{other.location}",
                    "left": first_sql, "right": other_sql, "held_out": holdout, "fingerprint": group.fingerprint,
                    "kinds": [site[first.model].kind, site[other.model].kind],
                })
    refactors = []
    for proposal in proposals:
        if not proposal.ready:
            continue
        by_model: dict[str, list] = {}
        for s in proposal.sites:
            by_model.setdefault(s[0], []).append(s)
        for model_key, sites in by_model.items():
            before = pipeline.models[model_key].sql
            after = before
            for s in sites:
                after = refactor_sql(proposal, after, s) if after else None
            refactors.append({
                "pair": f"{proposal.id}:{model_key.split('.')[-1]}",
                "left": before, "right": after, "held_out": all(site[m].holdout for m in by_model if m in site),
                "proposal": proposal.id, "origin": proposal.origin, "label": (proposal.verification or {}).get(model_key, ""),
                "shared_sql": proposal.shared_sql,
            })
    return {"exact": exact, "refactors": refactors, "proposals": len(proposals), "ready": sum(p.ready for p in proposals)}


class DupExact(Adapter):
    name = "duplicate-exact"
    kind = "exact"

    def items(self) -> list[dict]:
        return list(_dup_analysis()[self.kind])

    def case(self, item: dict) -> Case | None:
        import dup_bench as gen

        if not item["left"] or not item["right"]:
            return None
        # the DuckDB SQL of ``dup_bench.rows_of`` (plain sqlglot, qualifiers dropped), on its tables and no settings
        sqls = [gen.to_duckdb(sql) for sql in (item["left"], item["right"])]
        meta = {k: item[k] for k in item if k not in ("pair", "left", "right", "held_out")}
        return Case(self.name, item["pair"], sqls[0], sqls[1], _dup_tables(sqls), held_out=item["held_out"],
                    source=(item["left"], item["right"]), dialect="bigquery", meta=meta)


class DupRefactor(DupExact):
    name = "shared-refactors-proof"
    kind = "refactors"


# ------------------------------------------------------------------ pipeline equivalence


def _pipeline_cases() -> dict:
    import pipeline_bench as pb

    return {c.id: c for held in (False, True) for c in pb.all_cases(held)}


def translate_models(pipeline) -> dict[str, str] | None:
    """Every model as the eval's ``Executor`` translates it (``_flat``, then ``faithful``, then DuckDB), by key.

    The executor translates every model of the project on every database, so one that cannot be read as
    BigQuery does ends the eval's run for the case in an error, and the case is never counted: None here.
    """

    import pipeline_bench as pb
    from kumosql.bigquery_on_duckdb import faithful

    out = {}
    for key in pipeline.topological_order():
        try:
            tree = pb._flat(sqlglot.parse_one(pipeline.models[key].sql, read="bigquery"))
            out[key] = faithful(tree).sql(dialect="duckdb")
        except sqlglot.errors.SqlglotError:
            return None
    return out


def compose_models(pipeline, translated: dict[str, str], target: str) -> str:
    """``target`` with every upstream model a CTE named ``an__<model>`` (sources are the tables ``raw__<table>``):
    the eval materialises each model as the table ``an__<model>`` and reads the output from it."""

    needed, todo = set(), [target]
    while todo:
        key = todo.pop()
        if key in needed or key not in pipeline.models:
            continue
        needed.add(key)
        todo.extend(pipeline.upstream.get(key, ()))
    ctes = [f'"an__{key.split(".")[-1]}" AS ({translated[key]})' for key in pipeline.topological_order() if key in needed]
    return f'WITH {", ".join(ctes)} SELECT * FROM "an__{target.split(".")[-1]}"'


class PipelineEquivalence(Adapter):
    name = "pipeline-equivalence"

    def items(self) -> list[dict]:
        return [{"pair": f"{c.id}::{out}", "case": c.id, "output": out, "held_out": c.held_out, "label": c.label}
                for c in _pipeline_cases().values() for out in c.outputs]

    def case(self, item: dict) -> Case | None:
        import pipeline_bench as pb
        from kumosql import load_sqlx_project
        from kumosql.prover_schema import from_pipeline

        case = _pipeline_cases()[item["case"]]
        with tempfile.TemporaryDirectory() as tmp, silenced():
            rename = pb.write_project(case, Path(tmp))
            pipeline = load_sqlx_project(Path(tmp))
            pipeline.source_schema.update(pb.source_schema())
            schema = from_pipeline(pipeline)
            status = pb._prove_output(pipeline, item["output"], rename, 5000, schema)[0]
        if status != "proof":
            return None
        translated = translate_models(pipeline)
        if translated is None:
            return None  # the eval's executed check raises on this project: the case is an error, never a proof
        left = compose_models(pipeline, translated, f"{pb.PROJECT}.{pb.DATASET}.{item['output']}")
        right = compose_models(pipeline, translated, f"{pb.PROJECT}.{pb.DATASET}.{rename[item['output']]}")
        types = {"status": "VARCHAR", "region": "VARCHAR", "tier": "VARCHAR", "kind": "VARCHAR"}
        tables = {
            f"raw__{t}": Table(f"raw__{t}", [Column(c, "text" if c in types else "int", sql_type=types.get(c, "BIGINT")) for c in cols])
            for t, cols in pb.SOURCES.items()
        }
        meta = {"label": case.label, "family": case.family}
        return Case(self.name, item["pair"], left, right, tables, setup=bq_setup(), held_out=item["held_out"],
                    source=(pipeline.models[f"{pb.PROJECT}.{pb.DATASET}.{item['output']}"].sql,
                            pipeline.models[f"{pb.PROJECT}.{pb.DATASET}.{rename[item['output']]}"].sql),
                    dialect="bigquery", meta=meta)


# ------------------------------------------------------------------ incremental proofs

_EVENT_FLOOR = "2001-01-01"  # every proof assumes event times after the COALESCE default (a date no later than 2000)


def _rename(tree: exp.Expression, rename: dict[str, str]) -> exp.Expression:
    """``tree`` with each table named in ``rename`` (by last part, lower case) read under its new bare name,
    keeping the old name as the alias so that qualified columns still bind."""

    def swap(node):
        if isinstance(node, exp.Table) and node.name.lower() in rename:
            alias = node.args.get("alias") or exp.TableAlias(this=exp.to_identifier(node.name))
            return exp.Table(this=exp.to_identifier(rename[node.name.lower()]), alias=alias)
        return node

    return tree.copy().transform(swap)


def _incremental_case(case: dict, verdict) -> tuple[str, str, dict[str, Table], callable, dict]:
    import datetime as dt

    import incremental_bench as ib

    model, sources = ib.build(case)
    kinds = set(case["contract"]["kinds"])
    changing = set(case["contract"].get("tables") or sources)
    dup = "duplicate" in kinds
    group_not_null = set(model.unique_key) if (verdict.rule or "").startswith("R5") else set()
    tables: dict[str, Table] = {}
    now_sql: dict[str, str] = {}
    plan: dict[str, dict] = {}
    for name, spec in sources.items():
        key, tc = tuple(spec.key), spec.time_column
        required = set(key) | ({tc} if tc else set())
        if "null_key" in kinds and name in changing:
            required -= set(key)  # earlier batches may have left rows with a NULL key

        def columns(nn=required, src=name):
            return [bq_column(c, t, not_null=(c in nn or (c in group_not_null))) for c, t in sources[src].columns.items()]

        tables[name] = Table(name, columns(), [key] if key and not dup else [])
        parts = []
        kinds_here = kinds if name in changing else set()
        ins = kinds_here & {"insert_new", "insert_boundary", "insert_late", "duplicate", "null_key"}
        upd = kinds_here & {"update", "update_touch"}
        dele = "delete" in kinds_here
        cols = ", ".join(f'"{c}"' for c in spec.columns)
        match = " AND ".join(f's."{k}" = u."{k}"' for k in key)
        removed = []
        if upd and key:
            tables[f"{name}__upd"] = Table(f"{name}__upd", columns(), [key])
            removed.append(f'SELECT 1 FROM "{name}__upd" u WHERE {match}')
        if dele and key:
            tables[f"{name}__del"] = Table(f"{name}__del", [c for c in columns() if c.name in key], [key])
            removed.append(f'SELECT 1 FROM "{name}__del" u WHERE {match}')
        where = " AND ".join(f"NOT EXISTS ({r})" for r in removed)
        parts.append(f'SELECT {cols} FROM "{name}" s' + (f" WHERE {where}" if where else ""))
        if upd and key:
            # every old copy of an updated row becomes the new version (unless the row is also deleted)
            gone = f' WHERE NOT EXISTS (SELECT 1 FROM "{name}__del" d WHERE ' + " AND ".join(f's."{k}" = d."{k}"' for k in key) + ")" if dele else ""
            parts.append(f'SELECT {", ".join(f"u.{chr(34)}{c}{chr(34)}" for c in spec.columns)} FROM "{name}" s JOIN "{name}__upd" u ON {match}{gone}')
        if ins:
            nn = set(required) - (set(key) if "null_key" in ins else set())
            tables[f"{name}__ins"] = Table(f"{name}__ins", columns(nn), [key] if key and not ({"duplicate", "null_key"} & ins) else [])
            parts.append(f'SELECT {cols} FROM "{name}__ins"')
        now_sql[name] = " UNION ALL ".join(parts)
        plan[name] = {"key": key, "tc": tc, "ins": ins, "upd": upd, "del": dele, "columns": list(spec.columns)}

    floor = dt.datetime.fromisoformat(_EVENT_FLOOR)

    def as_time(value):
        if value is None:
            return None
        if isinstance(value, dt.datetime):
            return value
        if isinstance(value, dt.date):
            return dt.datetime.combine(value, dt.time(0))
        return value

    def legal(data) -> bool:
        for name, info in plan.items():
            key, tc, columns = info["key"], info["tc"], info["columns"]
            kpos = [columns.index(k) for k in key]
            tpos = columns.index(tc) if tc else None
            old = data.get(name, [])
            every = list(old) + list(data.get(f"{name}__ins", [])) + list(data.get(f"{name}__upd", []))
            if tpos is not None and any(as_time(r[tpos]) is not None and as_time(r[tpos]) < floor for r in every):
                return False
            if dup and key:  # rows sharing a key are exact copies (re-deliveries)
                first = {}
                for r in old:
                    k = tuple(r[p] for p in kpos)
                    if first.setdefault(k, r) != r:
                        return False
            newest = max((as_time(r[tpos]) for r in old), default=None) if tpos is not None else None
            old_keys = {tuple(r[p] for p in kpos) for r in old}
            if info["upd"]:
                for r in data.get(f"{name}__upd", []):
                    k = tuple(r[p] for p in kpos)
                    if k not in old_keys:
                        return False
                    olds = [o for o in old if tuple(o[p] for p in kpos) == k]
                    if "update_touch" in info["upd"] and "update" not in info["upd"]:
                        if newest is not None and not as_time(r[tpos]) > newest:
                            return False
                    elif "update" in info["upd"] and "update_touch" not in info["upd"] and tpos is not None:
                        if any(o[tpos] != r[tpos] for o in olds):
                            return False
                    elif tpos is not None:  # either kind
                        if not (all(o[tpos] == r[tpos] for o in olds) or newest is None or as_time(r[tpos]) > newest):
                            return False
                    if all(o == r for o in olds):
                        return False  # an update changes the row
            if info["del"]:
                for r in data.get(f"{name}__del", []):
                    if tuple(r[p] for p in kpos) not in old_keys:
                        return False
            if info["ins"]:
                ins_rows = list(data.get(f"{name}__ins", []))
                allowed = info["ins"]
                fresh_keys = []
                for r in sorted(set(ins_rows), key=repr):
                    copies = ins_rows.count(r)
                    if r in old:  # re-deliveries of a row already there
                        if "duplicate" not in allowed:
                            return False
                        continue
                    if copies > 1 and "duplicate" not in allowed:
                        return False  # one new row, the other copies re-deliver it
                    k = tuple(r[p] for p in kpos)
                    t = as_time(r[tpos]) if tpos is not None else None
                    if key and all(v is None for v in k):
                        if "null_key" not in allowed or not (newest is None or t is None or t > newest):
                            return False
                        continue
                    if key and (None in k or k in old_keys):
                        return False
                    if tpos is None or newest is None:
                        ok = bool(allowed & {"insert_new", "insert_late", "insert_boundary"})
                    else:
                        ok = (("insert_new" in allowed and t > newest) or ("insert_boundary" in allowed and t >= newest)
                              or ("insert_late" in allowed and t < newest))
                    if not ok:
                        return False
                    if key:
                        fresh_keys.append(k)
                if len(set(fresh_keys)) != len(fresh_keys):
                    return False
        return True

    # the target before the run, the run's own query, the full refresh after it: each translated by the
    # simulator's own ``_pin_clock`` (per-run clock pinned, qualifiers dropped, BigQuery read by ``faithful``)
    from kumosql.incremental import IncrementalError, _pin_clock

    clock_old, clock_new = dt.datetime(2030, 1, 1, 1), dt.datetime(2030, 1, 1, 2)
    rename_now = {name.lower(): f"{name}__now" for name in sources}
    target = model.target.split(".")[-1].lower()

    def piece(sql: str, clock, rename: dict[str, str]) -> str:
        tree = _rename(sqlglot.parse_one(sql, read=model.dialect), rename)
        try:
            return _pin_clock(tree, clock, model.dialect).sql(dialect="duckdb")
        except IncrementalError as error:  # the simulator cannot run it either
            raise NotRechecked(f"not re-checkable: {error}"[:300]) from error

    pieces = {
        "t0": piece(model.full_sql, clock_old, {}),
        "new": piece(model.full_sql, clock_new, rename_now),
        "inc": piece(model.incremental_sql, clock_new, {**rename_now, target: "kumo_t0"}),
    }
    names, _ = describe(tables, pieces["t0"], bq_setup())
    keep = [n for n in names if n.lower() not in {c.lower() for c in model.ignore_columns}]
    cols = ", ".join(f'"{n}"' for n in keep)
    with_ = ", ".join(f'"{name}__now" AS ({sql})' for name, sql in now_sql.items())
    with_ += f', kumo_t0 AS ({pieces["t0"]}), kumo_inc AS ({pieces["inc"]})'
    if not model.unique_key:
        # an append: the target keeps its rows and gains the run's
        left = f"WITH {with_} SELECT {cols} FROM kumo_t0 UNION ALL SELECT {cols} FROM kumo_inc"
        right = f'WITH {", ".join(f"{chr(34)}{n}__now{chr(34)} AS ({s})" for n, s in now_sql.items())} SELECT {cols} FROM ({pieces["new"]}) kumo_f'
    else:
        # Dataform's MERGE as the simulator runs it: matches are decided once against the table before the run;
        # a target row that matches several source rows fails the run, which the eval counts as a divergence, so
        # the left side then returns one marked row that no full refresh returns
        on = " AND ".join(f't."{k}" = s."{k}"' for k in model.unique_key)
        s_cols = ", ".join(f's."{n}"' for n in keep)
        t_cols = ", ".join(f't."{n}"' for n in keep)
        merged = (f"SELECT {t_cols} FROM kumo_t0 t WHERE NOT EXISTS (SELECT 1 FROM kumo_inc s WHERE {on}) "
                  f"UNION ALL SELECT {s_cols} FROM kumo_t0 t JOIN kumo_inc s ON {on} "
                  f"UNION ALL SELECT {s_cols} FROM kumo_inc s WHERE NOT EXISTS (SELECT 1 FROM kumo_t0 t WHERE {on})")
        conflict = ("kumo_t0n AS (SELECT *, ROW_NUMBER() OVER () AS kumo_rid FROM kumo_t0), "
                    "kumo_conflict AS (SELECT COUNT(*) > 0 AS c FROM (SELECT t.kumo_rid FROM kumo_t0n t JOIN kumo_inc s "
                    f"ON {on} GROUP BY t.kumo_rid HAVING COUNT(*) > 1) kumo_dup)")
        nulls = ", ".join("NULL" for _ in keep)
        left = (f"WITH {with_}, {conflict} SELECT {cols}, 'ok' AS kumo_status FROM ({merged}) kumo_m WHERE NOT (SELECT c FROM kumo_conflict) "
                f"UNION ALL SELECT {nulls}, 'MERGE CONFLICT' AS kumo_status WHERE (SELECT c FROM kumo_conflict)")
        right = (f'WITH {", ".join(f"{chr(34)}{n}__now{chr(34)} AS ({s})" for n, s in now_sql.items())} '
                 f"SELECT {cols}, 'ok' AS kumo_status FROM ({pieces['new']}) kumo_f")
    meta = {"rule": verdict.rule, "kinds": sorted(kinds), "tables": sorted(changing), "unique_key": list(model.unique_key)}
    return left, right, tables, legal, meta


class IncrementalProofs(Adapter):
    name = "incremental-proofs"

    def items(self) -> list[dict]:
        import incremental_bench as ib

        return [{"pair": c["id"], "held_out": c["split"] == "held_out", "label": c["label"]} for c in ib.load_cases()]

    def case(self, item: dict) -> Case | None:
        import incremental_bench as ib
        from kumosql.incremental import check_incremental

        case = next(c for c in ib.load_cases() if c["id"] == item["pair"])
        model, sources = ib.build(case)
        contract = case["contract"]
        verdict = check_incremental(model, sources, contract["kinds"], seeds=60,
                                    tables=tuple(contract["tables"]) if contract.get("tables") else None)
        if verdict.outcome != "safe":
            return None
        if model.pre_operations:  # statements the eval's simulator runs before a run; not modelled here
            raise NotRechecked("not re-checkable: pre_operations are not modelled")
        left, right, tables, legal, meta = _incremental_case(case, verdict)
        meta["label"] = case["label"]
        return Case(self.name, item["pair"], left, right, tables, legal=legal, setup=bq_setup(), held_out=item["held_out"],
                    source=(model.incremental_sql, model.full_sql), dialect="bigquery", meta=meta)


# ------------------------------------------------------------------ table minimization


@functools.lru_cache(maxsize=None)
def _minimization_cases() -> dict:
    import minimization_cases as mc

    return {c["id"]: c for c in mc.load_cases()}


MINIMIZER_SECONDS, MINIMIZER_GB = 300.0, 5.0  # tools/minimization_isolated.py's limits for the real-pipeline cases


def _minimize_in_child(case: dict) -> dict:
    """The minimizer's output for ``case`` and the harness's verdict on it (``minimization_bench.check_output``,
    100 databases, proofs on)."""

    import minimization_bench as mb
    import minimization_cases as mc

    record: dict = {"id": case["id"], "split": case["split"]}
    try:
        with silenced():
            out = mb.table_minimizer(mc.case_input(case))
        status, reason, proofs = mb.check_output(case, out, mb.DATABASES, True, 5000)
        record.update(out=out, status=status, reason=reason, proofs=proofs)
    except Exception as error:  # an error is its own outcome, never a pass
        record.update(status="error", reason=f"{type(error).__name__}: {error}"[:300])
    return record


def _minimized(case: dict) -> dict:
    """The minimizer's output and the harness's verdict on it, computed once per case and cached on disk under
    ``$RECHECK_SCRATCH``. The case runs in its own process with the 5 GB memory cap and 300 s limit of
    ``tools/minimization_isolated.py`` (the eval counts a case over either as not improved)."""

    import resource
    import subprocess

    folder = SCRATCH / "minimizer"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{case['id']}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    cap = int(MINIMIZER_GB * 2**30)
    command = [sys.executable, __file__, "--minimize", case["id"], str(path) + ".part"]
    record: dict = {"id": case["id"], "split": case["split"]}
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=MINIMIZER_SECONDS,
                              preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_AS, (cap, cap)))
        part = Path(str(path) + ".part")
        if proc.returncode == 0 and part.exists():
            record = json.loads(part.read_text(encoding="utf-8"))
        else:
            tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no output"]
            record.update(status="error", reason=("memory: " if "MemoryError" in (proc.stderr or "") else "crashed: ") + tail[0][:200])
    except subprocess.TimeoutExpired:
        record.update(status="error", reason=f"time: over {MINIMIZER_SECONDS:.0f} s")
    path.write_text(json.dumps(record, default=str), encoding="utf-8")
    return record


def _world_sql(sources: dict, tables: dict[str, str], prefix: str) -> list[str]:
    """CTE definitions for one pipeline, each table read as ``<prefix>__<name>`` and translated as the harness's
    ``minimization_cases.Engine`` builds its views (plain sqlglot to DuckDB)."""

    import minimization_cases as mc

    ctes = []
    for name in mc.topological_order(tables, sources):
        tree = sqlglot.parse_one(tables[name], read="bigquery")
        for table, ref in list(mc._bare_tables(tree)):
            if ref in tables:
                if not table.alias:  # qualified columns keep naming the table
                    table.set("alias", exp.TableAlias(this=exp.to_identifier(table.name)))
                table.set("this", exp.to_identifier(f"{prefix}__{ref}"))
        ctes.append(f'"{prefix}__{name}" AS ({tree.sql(dialect="duckdb")})')
    return ctes


def _packed(name: str, prefix: str, columns: list[str], types: list[str]) -> str:
    """Every row of one table as ``(table, column names, one text column)``: the harness compares a table's
    column names and its bag of rows (floats and decimals rounded to 9 digits)."""

    parts = []
    for column, kind in zip(columns, types):
        ref = f'"{column}"'
        if kind.upper() in ("DOUBLE", "FLOAT", "REAL"):
            text = f"printf('%.9g', {ref})"
        elif kind.upper().startswith("DECIMAL"):
            text = f"printf('%.9g', CAST({ref} AS DOUBLE))"
        else:
            text = f"CAST({ref} AS VARCHAR)"
        parts.append(f"COALESCE({text}, '<NULL>')")
    row = " || chr(31) || ".join(parts) if parts else "''"
    names = "|".join(c.lower() for c in columns).replace("'", "''")
    return f"SELECT '{name}' AS kumo_table, '{names}' AS kumo_columns, {row} AS kumo_row FROM \"{prefix}__{name}\""


def minimization_pair(case: dict, out: dict[str, str], names: list[str]):
    """``(left, right, tables)`` comparing the protected tables ``names`` of the original pipeline (left) and of
    ``out`` (right), each row tagged with its table and column names and packed into one text column."""

    original, sources = case["tables"], case["sources"]
    tables = {}
    for source, spec in sources.items():
        required = set(spec.get("key", [])) | set(spec.get("not_null", []))
        tables[source] = Table(source, [bq_column(c, t, c in required) for c, t in spec["columns"].items()],
                               [tuple(spec["key"])] if spec.get("key") else [])
    o_ctes, n_ctes = _world_sql(sources, original, "o"), _world_sql(sources, out, "n")
    left_parts, right_parts = [], []
    for name in names:
        o_cols, o_types = describe(tables, f'WITH {", ".join(o_ctes)} SELECT * FROM "o__{name}"')
        n_cols, n_types = describe(tables, f'WITH {", ".join(n_ctes)} SELECT * FROM "n__{name}"')
        left_parts.append(_packed(name, "o", o_cols, o_types))
        right_parts.append(_packed(name, "n", n_cols, n_types))
    return (f'WITH {", ".join(o_ctes)} ' + " UNION ALL ".join(left_parts),
            f'WITH {", ".join(n_ctes)} ' + " UNION ALL ".join(right_parts), tables)


class TableMinimization(Adapter):
    """Two items per case. ``<id>`` compares every protected table the harness proved (not merely checked) equal;
    ``<id>@agreed`` the protected tables the minimizer changed but the harness could not prove (the eval counts the
    case as simplified on the 100-database check alone)."""

    name = "table-minimization"

    def items(self) -> list[dict]:
        out = []
        for c in _minimization_cases().values():
            held = c["split"] == "held_out"
            out.append({"pair": c["id"], "held_out": held, "kind": "proved"})
            out.append({"pair": f"{c['id']}@agreed", "held_out": held, "kind": "agreed", "case": c["id"]})
        return out

    def case(self, item: dict) -> Case | None:
        case = _minimization_cases()[item.get("case", item["pair"])]
        record = _minimized(case)
        if record.get("status") not in ("proved", "agreed"):
            return None  # same (nothing changed), wrong or error: not a counted simplification
        out = {k.lower(): v for k, v in record["out"].items()}
        proofs = record["proofs"]
        original = case["tables"]
        if item["kind"] == "proved":
            names = [p for p in case["protected"] if (proofs.get(p) or {}).get("status") == "proved"]
        else:
            names = [p for p in case["protected"] if (proofs.get(p) or {}).get("status") == "unknown"]
        if not names:
            return None
        left, right, tables = minimization_pair(case, out, names)
        meta = {"changed": names, "proofs": {k: v.get("status") for k, v in proofs.items()}, "harness": record["status"]}
        return Case(self.name, item["pair"], left, right, tables, setup=(), held_out=item["held_out"],
                    source=(json.dumps({p: original.get(p) for p in names}), json.dumps({p: out.get(p) for p in names})),
                    dialect="bigquery", meta=meta)


# ------------------------------------------------------------------ output properties


def _violations(sql: str, claims: list[list], names: list[str], outer: str | None) -> str:
    """One query returning a tagged row for every way the claims fail (``sql`` is DuckDB)."""

    width = len(names)
    aliases = ", ".join(f"kc{i}" for i in range(width))
    lower = [n.lower() for n in names]

    def position(column: str) -> int:
        if column.lower() in lower:
            return lower.index(column.lower())
        return int(column)

    parts = []
    for claim in claims:
        tag = json.dumps(claim).replace("'", "''")
        kind = claim[0]
        if outer:
            if kind != "rows":
                continue
            test = "n > 1" if claim[1] == "at_most_one" else "n <> 1"
            parts.append(f"SELECT '{tag}' AS kumo_claim, n AS kumo_value FROM (SELECT (SELECT COUNT(*) FROM ({sql}) AS kq_inner) AS n "
                         f"FROM {outer}) AS kq WHERE {test}")
            continue
        body = f"({sql}) AS kq({aliases})"
        if kind == "rows":
            test = "COUNT(*) > 1" if claim[1] == "at_most_one" else "COUNT(*) <> 1"
            parts.append(f"SELECT '{tag}' AS kumo_claim, COUNT(*) AS kumo_value FROM {body} HAVING {test}")
        elif kind == "non_null":
            parts.append(f"SELECT '{tag}' AS kumo_claim, COUNT(*) AS kumo_value FROM {body} WHERE kc{position(claim[1])} IS NULL HAVING COUNT(*) > 0")
        else:
            cols = ", ".join(f"kc{position(c)}" for c in claim[1])
            parts.append(f"SELECT '{tag}' AS kumo_claim, COUNT(*) AS kumo_value FROM (SELECT {cols} FROM {body} GROUP BY ALL HAVING COUNT(*) > 1) AS kd HAVING COUNT(*) > 0")
    return " UNION ALL ".join(parts)


class OutputProperties(Adapter):
    name = "output-properties"

    def items(self) -> list[dict]:
        import output_properties_bench as ob

        out = []
        for filename, held in (("cases.json", False), ("held_out.json", True)):
            data = ob.load_cases(filename)
            for case in data["cases"]:
                out.append({"pair": case["id"], "file": filename, "held_out": held})
        return out

    def case(self, item: dict) -> Case | None:
        import output_properties_bench as ob
        from kumosql.output_properties import infer_properties

        data = ob.load_cases(item["file"])
        case = next(c for c in data["cases"] if c["id"] == item["pair"])
        schema = data["schemas"][case["schema"]]
        columns, constraints = ob.constraints_of(schema)
        try:
            props = infer_properties(case["sql"], constraints, columns)
        except Exception:
            return None
        if props.unsupported:
            return None
        made = ob.claims_made(props)
        if case.get("outer"):
            made = [m for m in made if m[0] == "rows"]
        if not made:
            return None
        tables = {}
        for name, spec in schema["tables"].items():
            nn = set(spec.get("not_null", ()))
            tables[name] = Table(name, [bq_column(c, t, c in nn) for c, t in spec["columns"].items()],
                                 [tuple(k) for k in spec.get("keys", ())])
        sql = ob.to_duck(case["sql"])  # the eval's own translation: query parameters become 5, plain sqlglot to DuckDB
        outer = case.get("outer")
        names = []
        if not outer:
            try:
                names, _ = describe(tables, sql)
            except Exception:
                return None
        left = _violations(sql, made, names, outer)
        if not left:
            return None
        right = f"SELECT * FROM ({left}) AS kumo_v WHERE FALSE"
        labelled = {json.dumps(c[:-1]): c[-1] for c in case["claims"]}
        meta = {"claims": made, "labels": {json.dumps(m): labelled.get(json.dumps(m)) for m in made}, "schema": case["schema"]}
        return Case(self.name, item["pair"], left, right, tables, held_out=item["held_out"],
                    source=(case["sql"], ""), dialect="bigquery", meta=meta)


@functools.lru_cache(maxsize=None)
def _adapted_queries() -> list[tuple[str, str]]:
    import sqlsolver_bench as sb

    out = []
    for suite, (pairs_file, _) in sb.SUITES.items():
        seen = set()
        for left, right in sb.load_pairs(sb.FIXTURES / pairs_file):
            for sql in (left, right):
                sql = sb.spark_days(sql)
                if sql in seen:
                    continue
                seen.add(sql)
                out.append((suite, sql))
    return out


@functools.lru_cache(maxsize=None)
def _sqlsolver_schema(suite: str):
    import sqlsolver_bench as sb

    return sb.load_schema(sb.FIXTURES / sb.SUITES[suite][1])


class OutputPropertiesAdapted(Adapter):
    name = "output-properties-adapted"

    def items(self) -> list[dict]:
        counters: dict[str, int] = {}
        out = []
        for index, (suite, _) in enumerate(_adapted_queries()):
            counters[suite] = counters.get(suite, 0) + 1
            out.append({"pair": f"{suite}#{counters[suite]}", "index": index})
        return out

    def case(self, item: dict) -> Case | None:
        import sqlsolver_bench as sb
        from kumosql.output_properties import infer_properties
        from kumosql.smt_equivalence import TableConstraints

        import output_properties_bench as ob

        suite, sql = _adapted_queries()[item["index"]]
        tables = _sqlsolver_schema(suite)
        columns = {t.name: [c.name for c in t.columns] for t in tables.values()}
        constraints = {
            t.name: TableConstraints(not_null=frozenset(c.name for c in t.columns if c.not_null),
                                     keys=tuple(k for k in ([t.primary_key] if t.primary_key else []) + list(t.unique)))
            for t in tables.values()
        }
        try:
            props = infer_properties(sql, constraints, columns, dialect="mysql")
        except Exception:
            return None
        if props.unsupported:
            return None
        made = ob.claims_made(props, positional=True)
        if not made:
            return None
        try:
            duck_sql = sb.to_dialect(sql, "duckdb")
            if suite == "calcite":
                duck_sql = sb.constant_groupings(sb.to_dialect(sb.name_values(sql), "duckdb"))
        except sqlglot.errors.SqlglotError:
            return None
        used = {n for n in sb.referenced_tables(sql) if n in tables}
        engine_tables = {}
        for name in sorted(used):
            t = tables[name]
            cols = [Column(c.name, {"VARCHAR": "text", "DOUBLE": "float", "DATE": "date", "BIGINT": "int"}[sb._duck_type(c)],
                           not_null=c.not_null, sql_type=sb._duck_type(c)) for c in t.columns]
            keys = ([tuple(t.primary_key)] if t.primary_key else []) + [tuple(u) for u in t.unique]
            engine_tables[name] = Table(t.name, cols, keys)
        try:
            names, _ = describe(engine_tables, duck_sql)
        except Exception:
            return None
        if len(names) != len(props.columns):
            return None  # the harness skips it too
        positional = [str(i) for i in range(len(names))]
        left = _violations(duck_sql, made, positional, None)
        right = f"SELECT * FROM ({left}) AS kumo_v WHERE FALSE"
        return Case(self.name, item["pair"], left, right, engine_tables, source=(sql, ""), dialect="mysql",
                    meta={"claims": made, "suite": suite})


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--minimize":  # the child of ``_minimized``
    _case = _minimization_cases()[sys.argv[2]]
    Path(sys.argv[3]).write_text(json.dumps(_minimize_in_child(_case), default=str), encoding="utf-8")
    raise SystemExit(0)

ADAPTERS = {
    a.name: a
    for a in [
        SqlfluffSemantic(), DupExact(), DupRefactor(), PipelineEquivalence(), IncrementalProofs(), TableMinimization(),
        OutputProperties(), OutputPropertiesAdapted(),
    ]
}
