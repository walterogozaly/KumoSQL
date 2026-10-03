"""Project-reduction eval: keep chosen outputs of a Dataform project, prove the smallest project that makes them.

``kumosql.project_reduction.reduce_project`` gets a whole Dataform project and the outputs to keep, and
returns a patch. Two families of cases, each split into dev and held out up front:

* **converted**: every table-minimization case (``benchmarks/table_minimization/*.jsonl``) written out as a
  Dataform project: sources as declarations, every table as a ``.sqlx`` file with ``ref()``, protected
  tables kept. A hash of the case id adds Dataform features: standalone assertions (over a kept output, over
  an intermediate), ``assertions`` in a config block, a project variable in place of a literal, an
  incremental table, a kept output that lists an assertion in ``dependencies``, views and tags. The case's
  own split is kept, so held-out minimization cases are held out here too.
* **real**: the open-source Dataform projects in ``tests/fixtures/bq_corpora`` with one or all of their
  final actions kept. One case in five, by a hash of its id, is held out.

Each patch is applied to a copy of the project and checked:

* the patch is valid for ``git apply`` and the patched project loads;
* converted cases are compiled by this harness's own small compiler (``ref()`` to names, project variables
  to their values, ``self()`` to the action, incremental branches dropped) and every kept output must give
  the same column names and bag of rows as the original on the targeted databases, ``--databases`` random
  ones and every trap witness, on DuckDB with the optimizer off (``tools/minimization_bench.py``'s
  checker). A difference, a missing kept output or a project that does not run is **wrong**;
* real cases cannot be run (no data), so their check is the reducer's own re-proof of every kept output of
  the patched project against the original, plus ``git apply``.

Scores, kept apart: correctness (wrong must be 0), how many cases were reduced, the share of project
complexity removed (``kumosql.project_reduction.project_score``: sqlfluff structure of every action plus
one per action) against dropping unneeded actions alone, the share of search steps proved, and quality
against the minimization reference for converted cases. No LLM runs at evaluation time.

    python tools/reduction_bench.py                     # dev split, both families
    python tools/reduction_bench.py --family real
    python tools/reduction_bench.py --split held_out    # once, at the end
"""

from __future__ import annotations

import argparse
from collections import Counter
import contextlib
from dataclasses import asdict, dataclass, field
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

import minimization_bench as mb  # noqa: E402
import minimization_cases as mc  # noqa: E402

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CORPORA = ROOT / "tests" / "fixtures" / "bq_corpora"
PROJECT, DATASET, RAW = "kumo-eval", "analytics", "raw"
DATABASES = 60
REAL_PER_PROJECT = 12


# ------------------------------------------------------------------ converted cases


def _features(case_id: str) -> set[str]:
    digest = int(hashlib.sha256(("reduction:" + case_id).encode("utf-8")).hexdigest(), 16)
    out = set()
    for index, (name, share) in enumerate((("assert_kept", 2), ("assert_inner", 2), ("config_assertion", 2),
                                           ("vars", 3), ("incremental", 5), ("dependency", 3), ("views", 2))):
        if (digest >> (index * 8)) % share == 0:
            out.add(name)
    return out


def _first_column(sql: str) -> str | None:
    tree = sqlglot.parse_one(sql, read="bigquery")
    while isinstance(tree, (exp.Subquery,)) or isinstance(tree, exp.SetOperation):
        tree = tree.this
    if isinstance(tree, exp.Select) and tree.expressions:
        name = tree.expressions[0].alias_or_name
        return name if name and re.fullmatch(r"[A-Za-z_]\w*", name) else None
    return None


def _readers(tables: Mapping[str, str]) -> dict[str, set[str]]:
    out = {name: set() for name in tables}
    for name, sql in tables.items():
        for read in mc.reads(sql):
            if read in out and read != name:
                out[read].add(name)
    return out


def _sqlx_body(sql: str, tables: set[str], variable: str | None = None) -> str:
    """The SQL with ``ref()`` for tables and sources, and one string literal read from a project variable."""

    tree = sqlglot.parse_one(sql, read="bigquery")
    for table, name in list(mc._bare_tables(tree)):
        marker = f"zzref_{name}_zz" if name in tables else f"zzsrc_{name}_zz"
        if not table.alias:
            table.set("alias", exp.TableAlias(this=exp.to_identifier(table.name)))
        table.set("this", exp.to_identifier(marker))
    if variable is not None:
        for literal in tree.find_all(exp.Literal):
            if literal.is_string and literal.this == variable:
                literal.replace(exp.Literal.string("zzvar_v1_zz"))
                break
    text = tree.sql(dialect="bigquery", pretty=True)
    text = re.sub(r"zzref_([a-z0-9_]+?)_zz", lambda m: '${ref("%s")}' % m.group(1), text)
    text = re.sub(r"zzsrc_([a-z0-9_]+?)_zz", lambda m: '${ref("%s", "%s")}' % (RAW, m.group(1)), text)
    return text.replace("'zzvar_v1_zz'", '"${dataform.projectConfig.vars.v1}"')


def render(case: Mapping) -> tuple[dict[str, str], dict]:
    """A Dataform project for a table-minimization case: ``({path: text}, facts about it)``."""

    tables, protected = case["tables"], list(case["protected"])
    features = _features(case["id"])
    readers = _readers(tables)
    inner = sorted(n for n in tables if n not in protected and readers[n])
    files: dict[str, str] = {}
    meta = {"features": sorted(features), "assertions": []}
    variable = None
    var_table = None
    if "vars" in features:
        for name in [*inner, *protected]:
            literal = next((lit.this for lit in sqlglot.parse_one(tables[name], read="bigquery").find_all(exp.Literal)
                            if lit.is_string and re.fullmatch(r"[\w ]+", lit.this or "")), None)
            if literal:
                variable, var_table = literal, name
                break
    settings = f"defaultProject: {PROJECT}\ndefaultDataset: {DATASET}\ndefaultAssertionDataset: {DATASET}_assertions\n"
    if variable is not None:
        settings += f"vars:\n  v1: \"{variable}\"\n"
    else:
        features.discard("vars")
    files["workflow_settings.yaml"] = settings
    for name in case["sources"]:
        files[f"definitions/sources/{name}.sqlx"] = (
            f'config {{\n  type: "declaration",\n  schema: "{RAW}",\n  name: "{name}"\n}}\n')
    incremental = inner[0] if "incremental" in features and inner else None
    if incremental is None:
        features.discard("incremental")
    config_assert = None
    if "config_assertion" in features:
        config_assert = next((n for n in inner if n != incremental and _first_column(tables[n])), None)
        if config_assert is None:
            features.discard("config_assertion")
    inner_assert = None
    if "assert_inner" in features:
        inner_assert = next((n for n in reversed(inner) if _first_column(tables[n])), None)
        if inner_assert is None:
            features.discard("assert_inner")
    kept_assert = protected[0] if "assert_kept" in features and _first_column(tables[protected[0]]) else None
    if kept_assert is None:
        features.discard("assert_kept")
    depends = protected[-1] if "dependency" in features and inner_assert else None
    if depends is None:
        features.discard("dependency")
    names = set(tables)
    for index, (name, sql) in enumerate(tables.items()):
        kind = "incremental" if name == incremental else "view" if "views" in features and index % 2 else "table"
        lines = [f'  type: "{kind}"']
        if name in protected:
            lines.append('  tags: ["reports"]')
        if name == config_assert:
            lines.append(f'  assertions: {{\n    nonNull: ["{_first_column(sql)}"]\n  }}')
        if name == depends:
            lines.append(f'  dependencies: ["assert_{inner_assert}_not_null"]')
        folder = "reports" if name in protected else "staging"
        body = _sqlx_body(sql, names, variable if name == var_table else None)
        files[f"definitions/{folder}/{name}.sqlx"] = "config {\n" + ",\n".join(lines) + "\n}\n\n" + body + "\n"
    for target in (kept_assert, inner_assert):
        if target is None:
            continue
        column = _first_column(tables[target])
        assertion = f"assert_{target}_not_null"
        files[f"definitions/assertions/{assertion}.sqlx"] = (
            'config {\n  type: "assertion"\n}\n\n'
            f'SELECT *\nFROM ${{ref("{target}")}}\nWHERE {column} IS NULL\n')
        meta["assertions"].append(assertion)
    meta["features"] = sorted(features)
    return files, meta


def write_files(root: Path, files: Mapping[str, str]) -> None:
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


_REF = re.compile(r"\$\{\s*ref\(\s*([^()]*?)\s*\)\s*\}")
_VAR = re.compile(r"\$\{\s*dataform\.projectConfig\.vars\.(\w+)\s*\}")


def _blocks(text: str):
    from kumosql.sqlx import split_sqlx_sections

    return split_sqlx_sections(text)


def compile_project(root: Path) -> dict[str, str]:
    """``{action name: SQL}`` for every table, view, incremental table and assertion, compiled as a full
    refresh: ``ref()`` to the action's name, project variables to their values, ``self()`` to the action."""

    settings = (root / "workflow_settings.yaml").read_text(encoding="utf-8") if (root / "workflow_settings.yaml").exists() else ""
    variables = dict(re.findall(r"(?m)^\s+(\w+):\s*\"?([^\"\n]*)\"?\s*$", settings.split("vars:", 1)[1])) if "vars:" in settings else {}
    out: dict[str, str] = {}
    for path in sorted((root / "definitions").rglob("*.sqlx")):
        text = path.read_text(encoding="utf-8")
        config = next((piece for kind, piece in _blocks(text) if kind == "block" and piece.lstrip().startswith("config")), "")
        kind = (re.search(r'\btype:\s*"(\w+)"', config) or [None, "table"])[1]
        if kind == "declaration":
            continue
        name = (re.search(r'\bname:\s*"(\w+)"', config) or [None, path.stem])[1]
        body = "".join(piece for kind_, piece in _blocks(text) if kind_ == "sql")
        body = _REF.sub(lambda m: re.findall(r'"([^"]+)"', m.group(1))[-1], body)
        body = _VAR.sub(lambda m: variables[m.group(1)], body)
        body = re.sub(r"\$\{\s*self\(\)\s*\}", name, body)
        if "${" in body:
            raise ValueError(f"{path.name}: an expression this compiler does not know")
        out[name.lower()] = body.strip()
    return out


def _source_columns(case: Mapping) -> dict:
    out = {}
    for name, spec in case["sources"].items():
        out[f"{PROJECT}.{RAW}.{name}"] = {"columns": dict(spec["columns"]), "key": list(spec.get("key") or []),
                                          "not_null": list(spec.get("not_null") or [])}
    return out


def _git_apply_check(original: Path, patch: str) -> str:
    """``""`` when ``git apply`` accepts the patch on a copy of the project, else git's message."""

    if not patch:
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "p"
        shutil.copytree(original, copy)
        found = subprocess.run(["git", "apply", "--check", "-"], cwd=copy, input=patch.encode("utf-8"),
                               capture_output=True)
        return found.stderr.decode("utf-8", "replace").strip()[:300] if found.returncode else ""


@dataclass
class CaseResult:
    id: str
    family: str
    split: str
    features: list[str] = field(default_factory=list)
    status: str = "error"  # reduced | unchanged | wrong | unverified | error
    reason: str = ""
    verified: bool = False
    executed: str = ""  # same | agreed | wrong | not run
    actions_before: int = 0
    actions_after: int = 0
    graph_only_actions: int = 0
    score_before: float = 0.0
    score_after: float = 0.0
    graph_only_score: float = 0.0
    tables_before: float | None = None
    tables_after: float | None = None
    reference: float | None = None
    quality: float | None = None
    moves: list[str] = field(default_factory=list)
    added: int = 0
    changed: int = 0
    tried: int = 0
    rejected: int = 0
    dropped_assertions: int = 0
    seconds: float = 0.0
    check_seconds: float = 0.0


def _graph_only(root: Path, keep: list[str], source_columns=None) -> tuple[int, float]:
    from kumosql.project_reduction import reduce_project

    result = reduce_project(root, keep, factor=False, max_seconds=0.0, source_columns=source_columns)
    return result.actions_after, result.score_after


def check_project(case: Mapping, original: Path, reduced: Path, databases: int = DATABASES) -> tuple[str, str, dict, dict]:
    """``(status, reason, original tables, reduced tables)``: both projects compiled by this harness and every kept
    output, and every assertion both still have, compared on DuckDB. Status is ``same``, ``agreed`` or ``wrong``."""

    original_tables = compile_project(original)
    try:
        reduced_tables = compile_project(reduced)
    except Exception as error:  # noqa: BLE001 - a patched project that does not compile is wrong
        return "wrong", f"the patched project does not compile: {error}"[:300], original_tables, {}
    assertions = sorted(n for n in original_tables if n.startswith("assert_") and n in reduced_tables)
    probe = {**case, "tables": original_tables, "protected": [*case["protected"], *assertions]}
    status, reason, _proofs = mb.check_output(probe, reduced_tables, databases, prove=False)
    return status, reason, original_tables, reduced_tables


def run_converted(case: Mapping, databases: int = DATABASES, max_seconds: float = 60.0) -> CaseResult:
    from kumosql.formatting import pipeline_complexity
    from kumosql.project_reduction import reduce_project

    files, meta = render(case)
    result = CaseResult(case["id"], "converted", case["split"], meta["features"])
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "project"
        write_files(root, files)
        started = time.time()
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                reduction = reduce_project(root, list(case["protected"]), source_columns=_source_columns(case),
                                           max_seconds=max_seconds)
        except Exception as error:  # a reducer that fails is an error, never a pass
            result.seconds = time.time() - started
            result.reason = f"reducer failed: {type(error).__name__}: {error}"[:300]
            return result
        result.seconds = time.time() - started
        checked = time.time()
        _fill(result, reduction)
        problem = _git_apply_check(root, reduction.patch())
        reduced = Path(tmp) / "reduced"
        shutil.copytree(root, reduced)
        reduction.apply(reduced)
        status, reason, original_tables, reduced_tables = check_project(case, root, reduced, databases)
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                result.graph_only_actions, result.graph_only_score = _graph_only(root, list(case["protected"]), _source_columns(case))
        except Exception:  # noqa: BLE001 - the baseline is informative only
            result.graph_only_actions, result.graph_only_score = result.actions_before, result.score_before
    result.check_seconds = time.time() - checked
    result.executed = status
    if problem:
        status, reason = "wrong", f"git apply rejects the patch: {problem}"
    if status == "wrong":
        result.status, result.reason = "wrong", reason
        return result
    plain = lambda tables: {k: v for k, v in tables.items() if not k.startswith("assert_")}  # noqa: E731
    try:
        result.tables_before = pipeline_complexity(plain(original_tables))["score"]
        result.tables_after = pipeline_complexity(plain(reduced_tables))["score"]
    except ValueError:
        pass
    result.reference = case["reference"]["complexity"]["score"]
    gain = (case["original"]["complexity"]["score"] - result.reference)
    if gain > 0 and result.tables_after is not None:
        result.quality = round((case["original"]["complexity"]["score"] - result.tables_after) / gain, 3)
    result.status = "unverified" if not reduction.verified else "reduced" if reduction.files else "unchanged"
    result.reason = "; ".join(reduction.notes)[:300]
    return result


def _fill(result: CaseResult, reduction) -> None:
    result.verified = reduction.verified
    result.actions_before, result.actions_after = reduction.actions_before, reduction.actions_after
    result.score_before, result.score_after = reduction.score_before, reduction.score_after
    result.moves = list(reduction.moves)
    result.added, result.changed = len(reduction.added), len(reduction.changed)
    result.tried, result.rejected = reduction.tried, reduction.rejected
    result.dropped_assertions = len(reduction.dropped_assertions)


# ------------------------------------------------------------------ real projects


def real_cases() -> list[dict]:
    """``{id, project, keep, split}`` for the corpora projects: each final action alone, and all of them."""

    from kumosql.pipeline import load_sqlx_project

    cases = []
    for folder in sorted(p for p in CORPORA.iterdir() if p.is_dir()):
        with contextlib.redirect_stderr(io.StringIO()):
            pipeline = load_sqlx_project(folder)
        downstream = pipeline.downstream
        finals = sorted(k for k, m in pipeline.models.items()
                        if m.kind in ("table", "view", "incremental", "operations", "sql")
                        and not any(pipeline.models[d].kind != "assertion" for d in downstream.get(k, ())))
        finals.sort(key=lambda k: hashlib.sha256(f"{folder.name}:{k}".encode()).hexdigest())
        chosen = [[k] for k in finals[:REAL_PER_PROJECT]]
        if len(finals) > 1:
            chosen.append(finals[:REAL_PER_PROJECT])
        for keep in chosen:
            label = keep[0].split(".")[-1] if len(keep) == 1 else f"all-{len(keep)}"
            case_id = f"real-{folder.name}-{label}"
            cases.append({"id": case_id, "project": folder.name, "keep": keep, "split": mc.held_out_split(case_id)})
    return cases


def run_real(case: Mapping, max_seconds: float = 120.0) -> CaseResult:
    from kumosql.project_reduction import reduce_project

    result = CaseResult(case["id"], "real", case["split"], [case["project"]])
    root = CORPORA / case["project"]
    started = time.time()
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            reduction = reduce_project(root, case["keep"], max_seconds=max_seconds)
    except Exception as error:
        result.seconds = time.time() - started
        result.reason = f"reducer failed: {type(error).__name__}: {error}"[:300]
        return result
    result.seconds = time.time() - started
    checked = time.time()
    _fill(result, reduction)
    problem = _git_apply_check(root, reduction.patch())
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            result.graph_only_actions, result.graph_only_score = _graph_only(root, case["keep"])
    except Exception:  # noqa: BLE001
        result.graph_only_actions, result.graph_only_score = result.actions_before, result.score_before
    result.check_seconds = time.time() - checked
    result.executed = "not run"
    if problem:
        result.status, result.reason = "wrong", f"git apply rejects the patch: {problem}"
        return result
    result.status = "unverified" if not reduction.verified else "reduced" if reduction.files else "unchanged"
    result.reason = "; ".join(reduction.notes)[:300]
    return result


# ------------------------------------------------------------------ Jaffle Shop: a real project with real data


JAFFLE_KEEPS = (("customers",), ("orders",), ("customers", "orders"), ("stg_orders",), ("stg_payments",),
                ("stg_customers",))


def jaffle_cases() -> list[dict]:
    """``{id, keep, split}`` for dbt Labs' Jaffle Shop (``tests/fixtures/jaffle_shop``), written as Dataform."""

    out = []
    for keep in JAFFLE_KEEPS:
        case_id = "jaffle-" + "+".join(keep)
        out.append({"id": case_id, "keep": list(keep), "split": mc.held_out_split(case_id)})
    return out


def jaffle_project(root: Path) -> dict:
    """Write the Jaffle Shop project as ``tools/jaffle_shop_bench.py`` loads it; return its seed tables' columns."""

    import jaffle_shop_bench as js

    files = {"workflow_settings.yaml": f"defaultProject: {js.PROJECT}\ndefaultDataset: {js.DATASET}\n"}
    for name, text in js.sqlx_models(js.dbt_models()).items():
        files[f"definitions/{name}.sqlx"] = text
    write_files(root, files)
    return {table: {"columns": dict(columns)} for table, columns in js.SEEDS.items()}


def jaffle_databases(count: int, seed: int = 11) -> list[dict]:
    import jaffle_shop_bench as js

    return [db for _label, db in js.databases(count, seed)]  # the seeds first, then random databases


def run_jaffle(case: Mapping, databases: int = DATABASES, max_seconds: float = 120.0) -> CaseResult:
    from kumosql.project_reduction import reduce_project

    result = CaseResult(case["id"], "jaffle", case["split"], ["dbt"])
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "project"
        seeds = jaffle_project(root)
        columns = {f"jaffle.main.{t}": spec for t, spec in seeds.items()}
        started = time.time()
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                reduction = reduce_project(root, case["keep"], source_columns=columns, max_seconds=max_seconds)
        except Exception as error:
            result.seconds = time.time() - started
            result.reason = f"reducer failed: {type(error).__name__}: {error}"[:300]
            return result
        result.seconds = time.time() - started
        checked = time.time()
        _fill(result, reduction)
        problem = _git_apply_check(root, reduction.patch())
        reduced = Path(tmp) / "reduced"
        shutil.copytree(root, reduced)
        reduction.apply(reduced)
        original_tables = compile_project(root)
        try:
            reduced_tables = compile_project(reduced)
        except Exception as error:  # noqa: BLE001
            reduced_tables, problem = {}, problem or f"the patched project does not compile: {error}"
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                result.graph_only_actions, result.graph_only_score = _graph_only(root, case["keep"], columns)
        except Exception:  # noqa: BLE001
            result.graph_only_actions, result.graph_only_score = result.actions_before, result.score_before
    status, reason = "agreed", ""
    if problem:
        status, reason = "wrong", problem
    else:
        engine = mc.Engine(seeds, {"o": original_tables, "n": reduced_tables})
        try:
            if "o" in engine.errors:
                raise RuntimeError(f"the Jaffle Shop project does not run: {engine.errors['o']}")
            found = mc.compare(engine, jaffle_databases(databases), "o", ["n"], case["keep"],
                               stable_only=mc.order_sensitive(original_tables))["n"]
        finally:
            engine.close()
        if found:
            status, reason = "wrong", f"{found['table']}: {found['reason']}"
    result.check_seconds = time.time() - checked
    result.executed = status
    if status == "wrong":
        result.status, result.reason = "wrong", reason[:300]
        return result
    result.status = "unverified" if not reduction.verified else "reduced" if reduction.files else "unchanged"
    result.reason = "; ".join(reduction.notes)[:300]
    return result


# ------------------------------------------------------------------ summary


def _share(before: float, after: float) -> float | None:
    return round((before - after) / before, 3) if before else None


def summarise(results: list[CaseResult]) -> dict:
    out = {}
    for family in ("converted", "real", "jaffle", "all"):
        rows = [r for r in results if family == "all" or r.family == family]
        if not rows:
            continue
        counts = Counter(r.status for r in rows)
        before = sum(r.score_before for r in rows if r.status in ("reduced", "unchanged"))
        after = sum(r.score_after for r in rows if r.status in ("reduced", "unchanged"))
        graph = sum(r.graph_only_score for r in rows if r.status in ("reduced", "unchanged"))
        tried = sum(r.tried for r in rows)
        rejected = sum(r.rejected for r in rows)
        qualities = [r.quality for r in rows if r.quality is not None]
        out[family] = {
            "cases": len(rows),
            "status": {s: counts.get(s, 0) for s in ("reduced", "unchanged", "wrong", "unverified", "error")},
            "wrong": [{"id": r.id, "reason": r.reason} for r in rows if r.status == "wrong"],
            "errors": [{"id": r.id, "reason": r.reason} for r in rows if r.status in ("error", "unverified")],
            "verified": sum(r.verified for r in rows),
            "executed": dict(Counter(r.executed for r in rows if r.executed)),
            "improved": sum(r.score_after < r.score_before for r in rows if r.status in ("reduced", "unchanged")),
            "beyond_graph": sum(r.score_after < r.graph_only_score for r in rows if r.status in ("reduced", "unchanged")),
            "complexity": {"before": round(before, 1), "graph_only": round(graph, 1), "after": round(after, 1),
                           "removed_share": _share(before, after), "graph_only_share": _share(before, graph)},
            "actions": {"before": sum(r.actions_before for r in rows), "graph_only": sum(r.graph_only_actions for r in rows),
                        "after": sum(r.actions_after for r in rows)},
            "steps": {"tried": tried, "proved": tried - rejected, "share": round((tried - rejected) / tried, 3) if tried else None},
            "shared_tables": sum(r.added for r in rows),
            "dropped_assertions": sum(r.dropped_assertions for r in rows),
            "quality": statistics.mean(qualities) if qualities else None,
            "seconds": {"total": round(sum(r.seconds for r in rows), 1),
                        "median": round(statistics.median([r.seconds for r in rows]), 2),
                        "max": round(max(r.seconds for r in rows), 2),
                        "check": round(sum(r.check_seconds for r in rows), 1)},
        }
        if out[family]["quality"] is not None:
            out[family]["quality"] = round(out[family]["quality"], 3)
    return out


def _run_one(args) -> CaseResult:
    logging.disable(logging.INFO)
    os.environ.setdefault("KUMOSQL_TIMING", "0")
    kind, case, databases, max_seconds = args
    if kind == "converted":
        return run_converted(case, databases, max_seconds)
    if kind == "jaffle":
        return run_jaffle(case, databases, max(max_seconds, 120.0))
    return run_real(case, max(max_seconds, 120.0))


def run(cases: list[tuple[str, dict]], databases: int = DATABASES, max_seconds: float = 60.0, jobs: int = 1,
        progress: bool = False) -> list[CaseResult]:
    work = [(kind, case, databases, max_seconds) for kind, case in cases]
    results: list[CaseResult] = []

    def note(result: CaseResult) -> None:
        results.append(result)
        if progress:
            print(f"[{len(results)}/{len(work)}] {result.id}: {result.status} {result.score_before:g} -> "
                  f"{result.score_after:g} ({result.seconds:.1f} s) {result.reason[:120]}", file=sys.stderr, flush=True)

    if jobs > 1:
        from multiprocessing import Pool

        with Pool(jobs) as pool:
            for result in pool.imap_unordered(_run_one, work, chunksize=1):
                note(result)
    else:
        for item in work:
            note(_run_one(item))
    order = {case["id"]: index for index, (_kind, case) in enumerate(cases)}
    return sorted(results, key=lambda r: order[r.id])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--family", default="all", choices=["converted", "real", "jaffle", "all"])
    parser.add_argument("--split", default="dev", choices=["dev", "held_out", "all"])
    parser.add_argument("--only", help="run case ids containing this text")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--databases", type=int, default=DATABASES)
    parser.add_argument("--max-seconds", type=float, default=60.0)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--json", type=Path, help="write the summary and per-case results here")
    args = parser.parse_args(argv)
    cases: list[tuple[str, dict]] = []
    if args.family in ("converted", "all"):
        cases += [("converted", c) for c in mc.load_cases(split=None if args.split == "all" else args.split)]
    if args.family in ("real", "all"):
        cases += [("real", c) for c in real_cases() if args.split == "all" or c["split"] == args.split]
    if args.family in ("jaffle", "all"):
        cases += [("jaffle", c) for c in jaffle_cases() if args.split == "all" or c["split"] == args.split]
    if args.only:
        cases = [(k, c) for k, c in cases if args.only in c["id"]]
    cases = cases[: args.limit] if args.limit else cases
    os.environ.setdefault("KUMOSQL_TIMING", "0")
    results = run(cases, args.databases, args.max_seconds, args.jobs, progress=True)
    summary = summarise(results)
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk not in ("errors",)} for k, v in summary.items()}, indent=2))
    if args.json:
        args.json.write_text(json.dumps({"summary": summary, "cases": [asdict(r) for r in results]}, indent=1), encoding="utf-8")
    return 1 if any(v["status"]["wrong"] for v in summary.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
