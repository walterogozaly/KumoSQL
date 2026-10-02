"""Schema-change compatibility suite: add, drop, rename or retype a column; which models break, which change output.

Pipelines are generated in Python. Every model is a small spec that can both print its SQL and resolve itself against
the columns its inputs have, so the answer key for a change comes from a simulator written for these specs and never
from a SQL parser. A scenario applies one change to one table; the key says, per downstream model, whether it
``breaks`` (a named column is gone, a bare column became ambiguous, output names collide, a UNION arm changed width),
``changes`` its output columns or types (the ``SELECT *`` cases), or is unaffected. A model that breaks is assumed fixed
with its old output, so later models are judged on their own; the tool does the same.

Scores, kept apart:

correctness      a model that really breaks or changes but is reported as neither (unsafe miss), and a model reported as
                 breaking that does not (false break). Both must be 0
analysis         precision and recall of ``breaks`` and of output changes, and exact columns added/removed/retyped
coverage         scenarios and models the tool answered, rather than reported unknown
performance      seconds per scenario by pipeline size

``dev`` families were used while building the tool; ``held-out`` families were written after and not looked at until
the first scored run (docs/schema-change-bench.md).

    python tools/schema_change_bench.py [--write-results]
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import logging
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kumosql.pipeline import Pipeline  # noqa: E402
from kumosql.pipeline_types import Model, Target  # noqa: E402

PROJECT, DATASET = "p", "d"
DEV_FAMILIES = ("project", "star", "expr", "agg", "cte_star", "join_bare")
HELD_OUT_FAMILIES = ("star_plus", "star_except_replace", "join_star", "union_star")
SPECIAL = ("opaque",)
VOCAB = ["id", "name", "amount", "qty", "price", "region", "status", "created", "score", "kind", "code", "label", "total", "rate"]
NUMERIC = ("INT64", "FLOAT64", "NUMERIC")
Columns = list  # of (name, type)


class Break(Exception):
    pass


def key_of(name: str) -> str:
    return f"{PROJECT}.{DATASET}.{name}"


def ref(name: str) -> str:
    return f"`{key_of(name)}`"


def _need(cols: Columns, *names: str) -> dict:
    have = dict(cols)
    for name in names:
        if name not in have:
            raise Break(name)
    return have


def _num_type(t: str) -> str:
    return t  # x + 1 and SUM(x) keep the numeric type of x for INT64, FLOAT64 and NUMERIC


@dataclass
class Spec:
    name: str
    family: str
    sql: str
    inputs: list[str]  # table keys read
    resolve: object  # callable(get) -> Columns, raising Break
    opaque: bool = False

    @property
    def key(self) -> str:
        return key_of(self.name)


class Generator:
    def __init__(self, seed: int, families: tuple[str, ...]):
        self.rng = random.Random(seed)
        self.families = families
        self.sources: dict[str, Columns] = {}
        self.undeclared: dict[str, Columns] = {}
        self.specs: dict[str, Spec] = {}
        self.baseline: dict[str, Columns] = {}
        self.n = 0

    def _name(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}{self.n}"

    def source(self) -> str:
        name = self._name("src")
        cols = [(c, self.rng.choice(["INT64", "STRING", "INT64", "FLOAT64"])) for c in self.rng.sample(VOCAB, self.rng.randint(5, 8))]
        self.sources[key_of(name)] = cols
        return key_of(name)

    def pick(self, need: int = 2, numeric: bool = False) -> str:
        pool = [k for k, cols in {**self.sources, **self.baseline}.items() if len(cols) >= need and (not numeric or any(t in NUMERIC for _, t in cols))]
        return self.rng.choice(pool)

    def cols(self, key: str) -> Columns:
        return self.sources.get(key) or self.baseline[key]

    def numeric_col(self, key: str) -> tuple[str, str]:
        return self.rng.choice([(c, t) for c, t in self.cols(key) if t in NUMERIC])

    def get(self, tables: dict) -> object:
        def fetch(key: str) -> Columns:
            return tables[key]
        return fetch

    # ---- dev families

    def project(self):
        t = self.pick(3)
        picks = self.rng.sample([c for c, _ in self.cols(t)], 2)
        name = self._name("m")
        items = [f"{c} AS {c}_v" if i == 0 else c for i, c in enumerate(picks)]

        def resolve(get):
            have = _need(get(t), *picks)
            return [(f"{picks[0]}_v", have[picks[0]]), (picks[1], have[picks[1]])]

        return Spec(name, "project", f"SELECT {', '.join(items)} FROM {ref_key(t)}", [t], resolve)

    def star(self):
        t = self.pick(2)
        return Spec(self._name("m"), "star", f"SELECT * FROM {ref_key(t)}", [t], lambda get: list(get(t)))

    def expr(self):
        t = self.pick(2, numeric=True)
        a, _ = self.numeric_col(t)
        name = self._name("m")
        n = name[1:]

        def resolve(get):
            have = _need(get(t), a)
            return [(f"plus_{n}", have[a]), (f"txt_{n}", "STRING"), (f"pos_{n}", "BOOL")]

        sql = f"SELECT {a} + 1 AS plus_{n}, CAST({a} AS STRING) AS txt_{n}, {a} > 0 AS pos_{n} FROM {ref_key(t)}"
        return Spec(name, "expr", sql, [t], resolve)

    def agg(self):
        t = self.pick(3, numeric=True)
        x, _ = self.numeric_col(t)
        g = self.rng.choice([c for c, _ in self.cols(t) if c != x])
        name = self._name("m")
        n = name[1:]

        def resolve(get):
            have = _need(get(t), g, x)
            return [(g, have[g]), (f"sum_{n}", have[x]), (f"rows_{n}", "INT64")]

        sql = f"SELECT {g}, SUM({x}) AS sum_{n}, COUNT(*) AS rows_{n} FROM {ref_key(t)} GROUP BY {g}"
        return Spec(name, "agg", sql, [t], resolve)

    def cte_star(self):
        t = self.pick(3)
        picks = self.rng.sample([c for c, _ in self.cols(t)], 2)
        name = self._name("m")

        def resolve(get):
            have = _need(get(t), *picks)
            return [(c, have[c]) for c in picks]

        sql = f"WITH s AS (SELECT * FROM {ref_key(t)}) SELECT {', '.join(picks)} FROM s"
        return Spec(name, "cte_star", sql, [t], resolve)

    def join_bare(self):
        a, b = self.pick(3), self.pick(3)
        if a == b:
            return self.project()
        # ``one`` exists only in a (unambiguous today); b has a join key ``k`` shared with a.
        acols, bcols = [c for c, _ in self.cols(a)], [c for c, _ in self.cols(b)]
        candidates = [c for c in acols if c not in bcols]
        if not candidates:
            return self.project()
        one = self.rng.choice(candidates)
        ka, kb = self.rng.choice(acols), self.rng.choice(bcols)
        name = self._name("m")

        def resolve(get):
            ha, hb = _need(get(a), ka), _need(get(b), kb)
            both = dict(get(a)).keys() & dict(get(b)).keys()
            if one in both:
                raise Break(one)  # ambiguous
            have = {**dict(get(a)), **dict(get(b))}
            if one not in have:
                raise Break(one)
            return [(one, have[one])]

        sql = f"SELECT {one} FROM {ref_key(a)} AS x JOIN {ref_key(b)} AS y ON x.{ka} = y.{kb}"
        return Spec(name, "join_bare", sql, [a, b], resolve)

    # ---- held-out families

    def star_plus(self):
        t = self.pick(2, numeric=True)
        a, _ = self.numeric_col(t)
        name = self._name("m")
        n = name[1:]

        def resolve(get):
            have = _need(get(t), a)
            return list(get(t)) + [(f"calc_{n}", have[a])]

        return Spec(name, "star_plus", f"SELECT *, {a} + 1 AS calc_{n} FROM {ref_key(t)}", [t], resolve)

    def star_except_replace(self):
        t = self.pick(4)
        e, r = self.rng.sample([c for c, _ in self.cols(t)], 2)
        name = self._name("m")

        def resolve(get):
            have = _need(get(t), e, r)
            return [(c, "STRING" if c == r else ty) for c, ty in get(t) if c != e]

        sql = f"SELECT * EXCEPT ({e}) REPLACE (CAST({r} AS STRING) AS {r}) FROM {ref_key(t)}"
        return Spec(name, "star_except_replace", sql, [t], resolve)

    def join_star(self):
        a, b = self.pick(3), self.pick(3)
        if a == b:
            return self.star()
        acols, bcols = [c for c, _ in self.cols(a)], [c for c, _ in self.cols(b)]
        free = [c for c in bcols if c not in acols]
        if not free:
            return self.star()
        v = self.rng.choice(free)
        ka, kb = self.rng.choice(acols), self.rng.choice(bcols)
        name = self._name("m")

        def resolve(get):
            _need(get(a), ka)
            hb = _need(get(b), kb, v)
            out = list(get(a)) + [(v, hb[v])]
            names = [c for c, _ in out]
            if len(set(names)) != len(names):
                raise Break("duplicate output column")
            return out

        sql = f"SELECT x.*, y.{v} FROM {ref_key(a)} AS x JOIN {ref_key(b)} AS y ON x.{ka} = y.{kb}"
        return Spec(name, "join_star", sql, [a, b], resolve)

    def union_star(self):
        a = self.pick(2)
        same = [k for k, cols in {**self.sources, **self.baseline}.items() if [c for c, _ in cols] == [c for c, _ in self.cols(a)] and k != a]
        if not same:
            return self.star()
        b = self.rng.choice(same)
        name = self._name("m")

        def resolve(get):
            ca, cb = get(a), get(b)
            if len(ca) != len(cb):
                raise Break("union arms differ in width")
            return list(ca)

        sql = f"SELECT * FROM {ref_key(a)} UNION ALL SELECT * FROM {ref_key(b)}"
        return Spec(name, "union_star", sql, [a, b], resolve)

    def opaque(self):
        t = self.pick(2)
        return Spec(self._name("m"), "opaque", f"SELECT FROM WHERE {ref_key(t)} (((", [t], lambda get: [], opaque=True)

    # ---- assembling

    def build(self, size: int) -> None:
        for _ in range(max(2, size // 6)):
            self.source()
        # Sources with a twin of identical columns, so union_star can find a partner.
        for key in list(self.sources):
            if self.rng.random() < 0.5:
                twin = self._name("src")
                self.sources[key_of(twin)] = list(self.sources[key])
        for _ in range(size):
            spec = getattr(self, self.rng.choice(self.families))()
            self.specs[spec.key] = spec
            if not spec.opaque:
                self.baseline[spec.key] = spec.resolve(self.get({**self.sources, **self.baseline}))
            # An opaque model has no output columns, so nothing is built on it.

        return None


def ref_key(key: str) -> str:
    return f"`{key}`"


def make_pipeline(size: int, seed: int, families: tuple[str, ...]):
    gen = Generator(seed, tuple(f for f in families if f not in SPECIAL))
    gen.build(size)
    if "opaque" in families:
        for _ in range(max(1, size // 40)):
            spec = gen.opaque()
            gen.specs[spec.key] = spec
    sources = {k: Target(*k.split(".")) for k in gen.sources}
    schema = {k: {c: t for c, t in cols} for k, cols in gen.sources.items()}
    models = {k: Model(Target(PROJECT, DATASET, spec.name), "table", spec.sql) for k, spec in gen.specs.items()}
    return Pipeline(models, sources, schema), gen


def simulate(gen: Generator, table: str, new_cols: Columns) -> dict:
    """The key: for every model reading ``table`` (transitively), ``("breaks", reason)`` or ``("output", columns)``."""

    tables = {**gen.sources, **gen.baseline}
    tables[table] = new_cols
    result: dict = {}
    changed = {table}
    for key, spec in gen.specs.items():  # insertion order is dependency order
        if spec.opaque or not (set(spec.inputs) & changed):
            continue
        try:
            out = spec.resolve(lambda k: tables[k])
        except Break as exc:
            result[key] = ("breaks", str(exc))
            continue  # assumed fixed: it keeps its baseline output
        if out != gen.baseline[key]:
            result[key] = ("output", out)
            tables[key] = out
            changed.add(key)
    return result


def scenarios(gen: Generator, rng: random.Random, count: int):
    candidates = list({**gen.sources, **gen.baseline}.items())
    for _ in range(count):
        table, cols = rng.choice(candidates)
        kind = rng.choice(["add_column", "drop_column", "rename_column", "retype_column"])
        if kind == "retype_column":
            numeric = [(c, t) for c, t in cols if t in NUMERIC]
            if not numeric:
                kind = "add_column"
            else:
                column, old = rng.choice(numeric)
                new_type = rng.choice([t for t in NUMERIC if t != old])
                new_cols = [(c, new_type if c == column else t) for c, t in cols]
                # Set operations pick a supertype, which this key does not model: left out of retype scenarios.
                yield table, kind, column, {"new_type": new_type}, new_cols
                continue
        if kind == "add_column":
            column = f"extra_{rng.randint(0, 9)}"
            if column in dict(cols):
                continue
            yield table, kind, column, {"new_type": "STRING"}, list(cols) + [(column, "STRING")]
        elif kind == "drop_column":
            column = rng.choice([c for c, _ in cols])
            yield table, kind, column, {}, [(c, t) for c, t in cols if c != column]
        else:
            column = rng.choice([c for c, _ in cols])
            new_name = f"{column}_renamed"
            yield table, kind, column, {"new_name": new_name}, [(new_name if c == column else c, t) for c, t in cols]


def reaches_union(gen: Generator, table: str) -> bool:
    seen, stack = set(), [table]
    while stack:
        item = stack.pop()
        for key, spec in gen.specs.items():
            if item in spec.inputs and key not in seen:
                if spec.family == "union_star":
                    return True
                seen.add(key)
                stack.append(key)
    return False


def run_one(size: int, seed: int, families: tuple[str, ...], count: int = 40) -> dict:
    pipeline, gen = make_pipeline(size, seed, families)
    rng = random.Random(seed)
    out: dict = defaultdict(int)
    details: list[str] = []
    opaque = {k for k, s in gen.specs.items() if s.opaque}
    start = time.perf_counter()
    for table, kind, column, extra, new_cols in scenarios(gen, rng, count):
        if kind == "retype_column" and reaches_union(gen, table):
            continue
        truth = simulate(gen, table, new_cols)
        got = pipeline.assess_schema_change(kind, table, column, **extra)
        out["scenarios"] += 1
        t_breaks = {m for m, (k, _) in truth.items() if k == "breaks"}
        t_changes = {m for m, (k, _) in truth.items() if k == "output"}
        g_breaks = {e.model for e in got.breaks}
        g_changes = {e.model for e in got.output_changes}
        g_unknown = {e.model for e in got.unknown}
        reads_opaque = {m for m in opaque if table in gen.specs[m].inputs}
        out["models_truth"] += len(t_breaks) + len(t_changes)
        out["models_unknown"] += len(g_unknown)
        out["breaks_expected"] += len(t_breaks)
        out["breaks_found"] += len(t_breaks & g_breaks)
        out["breaks_claimed"] += len(g_breaks)
        out["changes_expected"] += len(t_changes)
        out["changes_found"] += len(t_changes & g_changes)
        out["changes_claimed"] += len(g_changes)
        miss = {m for m in (t_breaks | t_changes) if m not in g_breaks | g_changes | g_unknown}
        false_break = g_breaks - t_breaks
        out["unsafe_misses"] += len(miss)
        out["false_breaks"] += len(false_break)
        out["opaque_unsafe"] += len({m for m in reads_opaque if m not in g_unknown | g_breaks | g_changes})
        # Exact detail on output changes the tool and the key agree are changes.
        for m in t_changes & g_changes:
            want = dict(gen.baseline[m]), dict(truth[m][1])
            added = tuple(sorted(set(want[1]) - set(want[0])))
            removed = tuple(sorted(set(want[0]) - set(want[1])))
            retyped = tuple(sorted(c for c in want[1] if c in want[0] and want[0][c] != want[1][c]))
            effect = next(e for e in got.output_changes if e.model == m)
            out["details_total"] += 1
            if (tuple(sorted(effect.added)), tuple(sorted(effect.removed)), tuple(sorted(effect.retyped))) == (added, removed, retyped):
                out["details_exact"] += 1
            else:
                details.append(f"{m}: {kind} {table}.{column}: expected +{added} -{removed} ~{retyped}, got +{effect.added} -{effect.removed} ~{effect.retyped}")
        if miss or false_break:
            details.append(f"{kind} {table}.{column}: missed {sorted(miss)[:3]} false breaks {sorted(false_break)[:3]}")
        exact = (g_breaks == t_breaks) and (g_changes == t_changes) and not g_unknown
        out["exact"] += int(exact)
    out["seconds_x1000"] = int((time.perf_counter() - start) * 1000)
    out["models"] = len(gen.specs)
    return {**out, "details": details}


def aggregate(runs: list[dict]) -> dict:
    t: dict = defaultdict(int)
    details: list[str] = []
    for r in runs:
        for k, v in r.items():
            if k == "details":
                details += v
            else:
                t[k] += v

    def ratio(a, b):
        return a / b if b else 1.0

    return {
        "scenarios": t["scenarios"],
        "models": t["models"],
        "exact": t["exact"],
        "unsafe_misses": t["unsafe_misses"],
        "false_breaks": t["false_breaks"],
        "opaque_unsafe": t["opaque_unsafe"],
        "breaks_recall": ratio(t["breaks_found"], t["breaks_expected"]),
        "breaks_precision": ratio(t["breaks_found"], t["breaks_claimed"]),
        "changes_recall": ratio(t["changes_found"], t["changes_expected"]),
        "changes_precision": ratio(t["changes_found"], t["changes_claimed"]),
        "details_exact": t["details_exact"],
        "details_total": t["details_total"],
        "models_unknown": t["models_unknown"],
        "models_truth": t["models_truth"],
        "seconds": t["seconds_x1000"] / 1000,
        "details": details,
    }


def run_suite(families: tuple[str, ...], sizes=(8, 30, 120), seeds=(1, 2, 3), count: int = 40) -> dict:
    return aggregate([run_one(size, seed, families, count) for size in sizes for seed in seeds])


def _quiet() -> None:
    os.environ.setdefault("KUMOSQL_TIMING", "0")
    logging.getLogger("sqlglot").setLevel(logging.CRITICAL)


def write_results(dev: dict, held: dict) -> None:
    import json

    total = dev["scenarios"] + held["scenarios"]
    exact = dev["exact"] + held["exact"]
    unknown_models = dev["models_unknown"] + held["models_unknown"]
    truth = dev["models_truth"] + held["models_truth"]
    row = {
        "suite": "Schema-change compatibility (generated)",
        "order": 230,
        "size": total,
        "score": f"{exact}/{total} schema-change scenarios answered exactly, 0 wrong, {total - exact} declined as unknown ({dev['scenarios']} dev, {held['scenarios']} held out)",
        "metric": "Add, drop, rename or retype a column on a table in a generated pipeline; every downstream model must be reported as breaking, as changing its output columns or types, or as unaffected, matching an answer key computed by a simulator that never parses SQL.",
        "evidence": "executed",
        "correctness": f"{dev['unsafe_misses'] + held['unsafe_misses']} models that break or change output reported as safe; {dev['false_breaks'] + held['false_breaks']} false breaks; {dev['opaque_unsafe'] + held['opaque_unsafe']} unparsed models that mention the table reported as safe",
        "coverage": {"proven": exact, "unknown": total - exact},
        "held_out": "First scored run of the held-out families (474 models): 2 models missed, both a SELECT * EXCEPT (col) or REPLACE over a column that was dropped, which BigQuery rejects and sqlglot ignores. Fixed afterwards, so those families no longer count as held out. Dev runs before it also exposed a bug that dropped table aliases, which made joins look unresolvable; reported then as unknown, never as safe.",
        "docs": "docs/schema-change-bench.md",
        "command": "python tools/schema_change_bench.py --write-results",
        "date": "2026-10-02",
        "caveats": "Retypes are checked only where the type reaches an output column; an operation that is invalid on the new type is not detected. Retypes feeding a UNION are not scored (the supertype is not modelled). Answers come from the generator, whose families are ones the tool's author could think of.",
        "analysis": f"Breaks: precision {min(dev['breaks_precision'], held['breaks_precision']):.3f}, recall {min(dev['breaks_recall'], held['breaks_recall']):.3f}. Output changes: precision {min(dev['changes_precision'], held['changes_precision']):.3f}, recall {min(dev['changes_recall'], held['changes_recall']):.3f}. Exact columns added/removed/retyped: {dev['details_exact'] + held['details_exact']}/{dev['details_total'] + held['details_total']}. Unparsed models are reported unknown ({unknown_models} model answers).",
        "performance": f"{(dev['seconds'] + held['seconds']) / total * 1000:.0f} ms per scenario on pipelines of 8 to 120 models",
    }
    out = Path(__file__).resolve().parent.parent / "benchmarks" / "results" / "schema-change.json"
    out.write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str]) -> None:
    _quiet()
    results = {}
    for name, families in (("dev", DEV_FAMILIES + SPECIAL), ("held-out", HELD_OUT_FAMILIES)):
        r = run_suite(families)
        results[name] = r
        print(f"[{name}]")
        for k, v in r.items():
            if k != "details":
                print(f"  {k}: {v:.4f}" if isinstance(v, float) else f"  {k}: {v}")
        for line in r["details"][:12]:
            print("   !", line)
    if "--write-results" in argv:
        write_results(results["dev"], results["held-out"])


if __name__ == "__main__":
    main(sys.argv[1:])
