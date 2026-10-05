"""Lint a Dataform project for results that change with the order of tied rows.

``python -m kumosql ties PROJECT`` reads every query model of a Dataform project, asks
:func:`kumosql.tie_determinism.analyze` where its result may depend on how ties are broken, and for
each model with such a site asks :func:`kumosql.tie_witness.find_tie_witness` for a small database
and two storage orders that give different results.

* A **finding** is a model with an ``unknown`` tie site and a witness that replays (the database keeps
  the declared facts, both orders return the recorded rows with the optimizer on and off, and the
  results differ). Nothing else is called a finding.
* An ``unknown`` site with no witness (no database found in the time allowed, or DuckDB cannot run
  the query) is listed apart as *unwitnessed*: the analysis found no reason the result is stable and
  the search found no example. It is not reported as a problem.

What the lint assumes, and where it can be wrong:

* Each model is run on its direct inputs, not on the raw sources: an input is a table whose rows the
  witness chooses. A table that is itself a model keeps what its query guarantees (keys from
  ``GROUP BY``/``DISTINCT``/one-row-per-key dedups and NOT NULL columns, found by
  :mod:`kumosql.output_properties`), plus the project's declared assertions, so a witness never gives
  an input duplicate keys its own query rules out. Incremental models and models whose query has
  Dataform expressions the loader could not resolve are skipped (listed with the reason): their
  stored rows are not their query's output.
* Column types are the declared ones when the project knows them, otherwise guessed from the column
  name (``*_at`` is a timestamp, ``*_date`` a date, most else an integer). A model that needs another
  type (an array column it unnests) is unwitnessed, never a finding.
* A witness is for the model's query as a whole. When a model has several ``unknown`` sites the
  finding lists all of them; it does not say which one the database exercises.
* DuckDB breaks ties by storage order and BigQuery may break them another way: a finding says the
  result *can* change, not that BigQuery returns different rows today.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Callable, Iterable, Mapping

import sqlglot
from sqlglot import exp

from .ast_utils import binding_cte
from .result_equivalence import DataRules
from .smt_equivalence import TableConstraints
from .tie_determinism import TieSite, analyze

_CLEAN_KINDS = {"table", "view", "sql", "assertion", "unknown"}
_DERIVE_KINDS = {"table", "view"}


@dataclass(frozen=True)
class TieFinding:
    """A model with ``unknown`` tie sites and a database that shows its result changing."""

    model: str
    path: str
    sites: tuple[TieSite, ...]
    witness: dict

    def to_json(self) -> dict:
        return {"model": self.model, "path": self.path, "sites": [s.to_json() for s in self.sites], "witness": self.witness}


@dataclass(frozen=True)
class Unwitnessed:
    """A model with ``unknown`` tie sites for which no witness was found."""

    model: str
    path: str
    sites: tuple[TieSite, ...]
    reason: str

    def to_json(self) -> dict:
        return {"model": self.model, "path": self.path, "sites": [s.to_json() for s in self.sites], "reason": self.reason}


@dataclass
class TieLint:
    models: int = 0  # query models read
    sites: int = 0  # tie sites over those models
    deterministic: int = 0  # sites the analysis shows deterministic
    findings: list[TieFinding] = field(default_factory=list)
    unwitnessed: list[Unwitnessed] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (model, why it was not read)
    seconds: float = 0.0

    @property
    def finding_sites(self) -> int:
        return sum(len(f.sites) for f in self.findings)

    @property
    def unwitnessed_sites(self) -> int:
        return sum(len(u.sites) for u in self.unwitnessed)

    def summary(self) -> dict:
        return {
            "models": self.models, "sites": self.sites, "deterministic_sites": self.deterministic,
            "findings": len(self.findings), "finding_sites": self.finding_sites,
            "unwitnessed": len(self.unwitnessed), "unwitnessed_sites": self.unwitnessed_sites,
            "skipped": len(self.skipped), "seconds": round(self.seconds, 1),
        }

    def to_json(self) -> dict:
        return {
            "summary": self.summary(),
            "findings": [f.to_json() for f in self.findings],
            "unwitnessed": [u.to_json() for u in self.unwitnessed],
            "skipped": [{"model": m, "reason": r} for m, r in self.skipped],
        }


# ----- the project's facts -----------------------------------------------------------------------


def _spellings(key: str) -> list[str]:
    parts = key.strip("`").lower().split(".")
    return [".".join(parts[start:]) for start in range(len(parts))]


def _table_spelling(table: exp.Table) -> str:
    return ".".join(part for part in (table.catalog, table.db, table.name) if part).lower()


def _read_tables(tree: exp.Expression) -> list[exp.Table]:
    return [t for t in tree.find_all(exp.Table) if t.name and binding_cte(t) is None]


def _clean(model: Any) -> str | None:
    """Why the model's query cannot be read as written, or ``None``."""

    if model.kind == "incremental":
        return "incremental model: its stored rows are not its query's output"
    if model.kind not in _CLEAN_KINDS or not model.sql.strip():
        return f"not a query ({model.kind})"
    if "${" in model.sql or model.masked_expressions:
        return "query has Dataform expressions that were not resolved"
    return None


class _Facts:
    """Declared and derived constraints of every table of a project, under every spelling."""

    def __init__(self, pipeline: Any, schema: Any):
        self.pipeline = pipeline
        self.columns: dict[str, list[str]] = dict(schema.columns)
        self.types: dict[str, dict[str, str]] = dict(schema.types)
        self.constraints: dict[str, TableConstraints] = dict(schema.constraints)
        owners: dict[str, set[str]] = {}
        for key, model in pipeline.models.items():
            for spelling in _spellings(model.target.key or key):
                owners.setdefault(spelling, set()).add(key)
        self._owner = {spelling: next(iter(keys)) for spelling, keys in owners.items() if len(keys) == 1}
        self._done: set[str] = set()
        self._active: set[str] = set()

    def model_of(self, spelling: str):
        key = self._owner.get(spelling)
        return None if key is None else self.pipeline.models[key]

    def prepare(self, tree: exp.Expression) -> None:
        """Derive the facts of every model ``tree`` reads (and, first, of what those read)."""

        for table in _read_tables(tree):
            self._derive(_table_spelling(table))

    def _derive(self, spelling: str) -> None:
        key = self._owner.get(spelling)
        if key is None or key in self._done or key in self._active:
            return
        model = self.pipeline.models[key]
        self._active.add(key)
        try:
            if model.kind in _DERIVE_KINDS and _clean(model) is None:
                try:
                    tree = sqlglot.parse_one(model.sql, read="bigquery")
                except sqlglot.errors.SqlglotError:
                    return
                self.prepare(tree)
                from .output_properties import infer_properties

                typed = _typed_columns(model.sql, self)
                schema = {t: list(c) for t, c in typed.items()} if isinstance(typed, dict) else {}
                found = infer_properties(model.sql, self.constraints, schema)
                if not found.unsupported:
                    self._record(model, key, found)
        finally:
            self._active.discard(key)
            self._done.add(key)

    def _record(self, model: Any, key: str, found: Any) -> None:
        keys = tuple(tuple(k.columns) for k in found.keys if k.columns and all(found.column(c) for c in k.columns))
        not_null = frozenset(c.name for c in found.columns if c.non_null and found.column(c.name) is c)
        if not keys and not not_null:
            return
        for spelling in _spellings(model.target.key or key):
            if self._owner.get(spelling) != key:
                continue
            old = self.constraints.get(spelling)
            self.constraints[spelling] = TableConstraints(
                not_null=frozenset(old.not_null if old else ()) | not_null,
                keys=tuple(dict.fromkeys((*(old.keys if old else ()), *keys))),
                foreign_keys=old.foreign_keys if old else (),
            )

    def lookup(self, spelling: str) -> TableConstraints | None:
        return self.constraints.get(spelling)


def _guess_type(column: str) -> str:
    from .incremental_scan import guess_type

    return guess_type(column)


def _typed_columns(sql: str, facts: _Facts) -> dict[str, dict[str, str]] | str:
    """The tables ``sql`` reads and their typed columns, by the spelling the query uses, or why not.

    Columns are the project's when it knows the table, plus the ones the query reads; a column's type is the
    declared one, otherwise guessed from its name.
    """

    from .refute import infer_schema

    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except sqlglot.errors.SqlglotError as error:
        return f"parse error: {error}"
    by_bare: dict[str, str] = {}
    for table in _read_tables(tree):
        spelling = _table_spelling(table)
        if by_bare.setdefault(table.name.lower(), spelling) != spelling:
            return "two tables share a name"
    known = {bare: facts.columns.get(spelling) for bare, spelling in by_bare.items() if facts.columns.get(spelling)}
    types = {bare: facts.types[spelling] for bare, spelling in by_bare.items() if spelling in facts.types}
    schema: dict[str, dict[str, str]] = {}
    for bare, columns in infer_schema([sql], known, types).items():
        spelling = by_bare.get(bare)
        if spelling is not None:
            declared = types.get(bare, {})
            schema[spelling] = {c: declared.get(c) or _guess_type(c) for c in columns}
    return schema or "reads no table"


def _witness_inputs(schema: dict[str, dict[str, str]], facts: _Facts) -> tuple[dict, dict, list]:
    """``(schema, rules, foreign_keys)`` for the tables a query reads: the declared and derived facts about them."""

    rules: dict[str, DataRules] = {}
    foreign_keys: list[tuple] = []
    for spelling in schema:
        constraint = facts.lookup(spelling)
        if constraint is None:
            continue
        keys = tuple(tuple(c.lower() for c in key) for key in constraint.keys)
        not_null = frozenset(c.lower() for c in constraint.not_null) | {c.lower() for key in keys for c in key}
        rules[spelling] = DataRules(not_null=not_null, keys=keys)
        for cols, parent, parent_cols in constraint.foreign_keys:
            parent_spelling = next((s for s in schema if s == parent.lower() or s.endswith("." + parent.lower().split(".")[-1])), None)
            if parent_spelling is not None:
                foreign_keys.append((spelling, tuple(c.lower() for c in cols), parent_spelling, tuple(c.lower() for c in parent_cols)))
    return schema, rules, foreign_keys


def _canonical(sql: str, schema: dict, rules: dict, foreign_keys: list) -> tuple[tuple, str, dict[str, str]]:
    """A key shared by models that read different tables the same way, its SQL, and the renaming ``table -> t0, t1, ...``.

    Dataform projects copy a model's shape over many tables; the witness search gives the same answer for
    each copy, so it runs once per shape and the witness is renamed (and replayed) for the others.
    """

    tree = sqlglot.parse_one(sql, read="bigquery")
    names: dict[str, str] = {}
    for table in _read_tables(tree):
        spelling = _table_spelling(table)
        names.setdefault(spelling, f"t{len(names)}")
        for part in ("catalog", "db"):
            table.set(part, None)
        table.set("this", exp.to_identifier(names[spelling]))
    renamed = {names[s]: columns for s, columns in schema.items() if s in names}
    declared = {names[s]: (sorted(r.not_null), sorted(r.keys)) for s, r in rules.items() if s in names}
    links = [(names.get(c), cols, names.get(p), pcols) for c, cols, p, pcols in foreign_keys]
    canonical = tree.sql(dialect="bigquery")
    return (canonical, json.dumps(renamed, sort_keys=True), repr(sorted(declared.items())), repr(links)), canonical, names


def _rename(witness: dict, sql: str, names: Mapping[str, str]) -> dict:
    """``witness`` with ``sql`` as its query and every table renamed through ``names`` (old name to new name)."""

    swap = lambda mapping: {names.get(k, k): v for k, v in mapping.items()}  # noqa: E731
    return {
        **witness,
        "sql": sql,
        "schema": swap(witness["schema"]),
        "rules": swap(witness["rules"]),
        "foreign_keys": [[names.get(c, c), cols, names.get(p, p), pcols] for c, cols, p, pcols in witness["foreign_keys"]],
        "tables": swap(witness["tables"]),
        "orders": {order: swap(rows) for order, rows in witness["orders"].items()},
    }


# ----- the lint ----------------------------------------------------------------------------------


def lint_pipeline(
    pipeline: Any,
    schema: Any = None,
    *,
    budget: float = 10.0,
    limit: int | None = None,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> TieLint:
    """Tie findings for every query model of a loaded Dataform ``pipeline`` (module doc).

    ``schema`` is the project's :class:`~kumosql.prover_schema.ProverSchema` (built from the pipeline when
    omitted). ``budget`` bounds the witness search per model, in seconds; ``limit`` reads only the first
    models, for a quick look.
    """

    from .prover_schema import from_pipeline
    from .tie_witness import find_tie_witness, replay

    started = time.monotonic()
    facts = _Facts(pipeline, schema if schema is not None else from_pipeline(pipeline))
    result = TieLint()
    cache: dict[str, Any] = {}
    for count, (key, model) in enumerate(pipeline.models.items()):
        if limit is not None and result.models >= limit:
            break
        if cancelled is not None and cancelled():
            break
        if not model.is_query:
            continue
        why = _clean(model)
        if why is not None:
            result.skipped.append((key, why))
            continue
        try:
            tree = sqlglot.parse_one(model.sql, read="bigquery")
        except sqlglot.errors.SqlglotError as error:
            result.skipped.append((key, f"parse error: {str(error).splitlines()[0][:100]}"))
            continue
        facts.prepare(tree)
        typed = _typed_columns(model.sql, facts)
        columns = {t: list(c) for t, c in typed.items()} if isinstance(typed, dict) else None
        report = analyze(tree, schema=columns, constraints=facts.constraints)
        if report.unsupported:
            result.skipped.append((key, report.unsupported[:120]))
            continue
        result.models += 1
        result.sites += len(report.sites)
        result.deterministic += sum(1 for s in report.sites if s.deterministic)
        unknown = report.undetermined
        if not unknown:
            continue
        path = model.path or ""
        if isinstance(typed, str):
            result.unwitnessed.append(Unwitnessed(key, path, unknown, typed))
            continue
        schema_, rules, foreign_keys = _witness_inputs(typed, facts)
        shape, canonical_sql, names = _canonical(model.sql, schema_, rules, foreign_keys)
        witness = None
        if cache.get(shape):
            witness = _rename(cache[shape], model.sql, {v: k for k, v in names.items()})
            if not replay(witness):
                witness = None
        if witness is None and cache.get(shape, False) is not None:
            found = find_tie_witness(model.sql, schema_, rules, foreign_keys=foreign_keys, budget=budget)
            if found is not None and replay(found):
                witness = found
                cache[shape] = _rename(found, canonical_sql, names)
            else:
                cache[shape] = None
        if witness is not None:
            result.findings.append(TieFinding(key, path, unknown, witness))
        else:
            result.unwitnessed.append(Unwitnessed(key, path, unknown, "no database found that shows the difference"))
        if progress is not None and (count + 1) % 200 == 0:
            progress(f"{count + 1} of {len(pipeline.models)} models read")
    result.seconds = time.monotonic() - started
    return result


def lint_project(root: str | Path, **options: Any) -> TieLint:
    """Load the Dataform project at ``root`` and lint it."""

    from .pipeline import load_sqlx_project
    from .prover_schema import from_pipeline

    pipeline = load_sqlx_project(root)
    return lint_pipeline(pipeline, from_pipeline(pipeline), **options)


# ----- the loaded project in the app -------------------------------------------------------------

_JOB: dict = {"state": "idle"}
_JOB_LOCK = threading.Lock()
_CANCEL = threading.Event()


def job_status() -> dict:
    """``GET /api/ties``: idle, running, done (with the result), cancelled or error."""

    with _JOB_LOCK:
        data = dict(_JOB)
    if data["state"] == "running":
        data["elapsed"] = round(time.time() - data["started"], 1)
    return data


def cancel_job() -> dict:
    _CANCEL.set()
    return job_status()


def run_loaded(payload: Mapping | None = None) -> dict:
    """``POST /api/ties/run``: lint the project loaded in the app, in the background."""

    from . import live_graph, prover_context

    loaded = live_graph.loaded()
    if not loaded:
        raise ValueError("load a project first")
    payload = payload or {}
    options = {"budget": float(payload.get("budget") or 10.0)}
    if payload.get("limit"):
        options["limit"] = int(payload["limit"])
    pipeline, schema = loaded["pipeline"], prover_context.current_schema()
    with _JOB_LOCK:
        if _JOB["state"] == "running":
            raise ValueError("a lint is already running")
        _CANCEL.clear()
        _JOB.clear()
        _JOB.update(state="running", started=time.time(), line="")

    def update(**values: Any) -> None:
        with _JOB_LOCK:
            _JOB.update(values)

    def work() -> None:
        try:
            result = lint_pipeline(pipeline, schema, cancelled=_CANCEL.is_set, progress=lambda line: update(line=line), **options)
            update(state="cancelled" if _CANCEL.is_set() else "done", result=result.to_json())
        except Exception as error:  # noqa: BLE001 - reported to the caller, never raised into the server
            update(state="error", error=str(error) or type(error).__name__)

    threading.Thread(target=work, name="tie-lint", daemon=True).start()
    return job_status()


# ----- command line ------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m kumosql ties",
        description="Lint a Dataform project for results that change with the order of tied rows (windows, LIMIT, ANY_VALUE)",
    )
    parser.add_argument("root", type=Path, help="Dataform project root")
    parser.add_argument("--budget", type=float, default=10.0, help="Seconds of witness search per model (default 10)")
    parser.add_argument("--limit", type=int, help="Only the first N query models")
    parser.add_argument("--json", action="store_true", help="Machine-readable output, including each witness")
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        print("ties: project folder not found", file=sys.stderr)
        return 2
    result = lint_project(args.root, budget=args.budget, limit=args.limit)
    if args.json:
        print(json.dumps(result.to_json(), indent=1))
        return 0
    s = result.summary()
    print(
        f"{s['models']} query models, {s['sites']} tie sites ({s['deterministic_sites']} deterministic); "
        f"{s['findings']} models with a replaying witness, {s['unwitnessed']} with an unknown site and no witness, "
        f"{s['skipped']} skipped ({s['seconds']} s)"
    )
    for finding in result.findings:
        site = finding.sites[0]
        rows = sum(len(r) for r in finding.witness["tables"].values())
        print(f"  finding {finding.model} ({finding.path}): {site.function} {site.reason}; {len(finding.sites)} site(s), witness of {rows} rows")
        if site.fix:
            print(f"    fix: {site.fix}")
    if result.unwitnessed:
        print(f"{len(result.unwitnessed)} models have an unknown site but no witness (not findings); --json lists them")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
