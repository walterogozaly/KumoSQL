"""Hard-case corpus for overlap detection (synthetic; expected labels written down).

Each case has one target query, a few candidate models and the expected label of
every candidate: ``same_meaning``, ``contains``, ``partial``, ``unknown`` or
``none`` (ruled out, so it is not in the matches). The run asserts:

* no candidate is reported ``same_meaning`` unless the corpus says it is;
* every label equals the one written down;
* enough candidates were decided, so a change that turns everything into
  ``unknown`` fails (``MIN_DECIDED``); the counts are printed.

Cases are synthetic: generic names, no identifying SQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from kumosql import Pipeline, Target, find_overlaps
from kumosql.pipeline import Model

SALES = Target("p", "raw", "sales")
REGIONS = Target("p", "raw", "regions")
SOURCES = {SALES.key: SALES, REGIONS.key: REGIONS}
SCHEMA = {
    SALES.key: {"sale_id": "INT64", "region_id": "INT64", "county_id": "INT64", "amount": "FLOAT64",
                "status": "STRING", "sold_on": "DATE", "cost": "FLOAT64"},
    REGIONS.key: {"region_id": "INT64", "region_name": "STRING", "label": "STRING"},
}
BASE = "SELECT region_id, SUM(amount) AS total FROM p.raw.sales GROUP BY region_id"


@dataclass(frozen=True)
class Case:
    name: str
    target: str
    candidates: dict[str, str]
    expected: dict[str, str]
    kinds: dict[str, str] = field(default_factory=dict)


CASES = [
    Case("long chain of views and CTEs with renames", BASE,
         {"v1": "SELECT region_id AS a, amount AS b FROM p.raw.sales",
          "v2": "WITH t AS (SELECT a AS c, b AS d FROM p.core.v1) SELECT c AS e, d AS f FROM t",
          "out": "WITH u AS (SELECT e AS g, f AS h FROM p.core.v2) SELECT g AS area, SUM(h) AS revenue FROM u GROUP BY g"},
         {"v1": "unknown", "v2": "unknown", "out": "same_meaning"}, kinds={"v1": "view", "v2": "view"}),
    Case("dimension joined by two routes", BASE,
         {"via_alias": "SELECT s.region_id, SUM(s.amount) AS total FROM p.raw.sales s GROUP BY s.region_id",
          "via_using": "SELECT region_id, SUM(amount) AS total FROM p.raw.sales JOIN p.raw.regions USING (region_id) GROUP BY region_id"},
         # USING takes the left table's key, so this reads like the same join written with ON: the inner join
         # filters (and may repeat) rows, so the row scope differs.
         {"via_alias": "same_meaning", "via_using": "partial"}),
    Case("join that fans out changes the grain", "SELECT sale_id, amount FROM p.raw.sales",
         {"fanned": "SELECT s.sale_id, s.amount FROM p.raw.sales s JOIN p.raw.regions r ON s.status = r.label"},
         {"fanned": "unknown"}),
    Case("same source and grain, different filter", BASE,
         {"paid_only": "SELECT region_id, SUM(amount) AS total FROM p.raw.sales WHERE status = 'paid' GROUP BY region_id"},
         {"paid_only": "partial"}),
    Case("same source, different grain", BASE,
         {"by_county": "SELECT county_id, SUM(amount) AS total FROM p.raw.sales GROUP BY county_id"},
         {"by_county": "none"}),
    Case("date-sharded, snapshot and incremental tables", BASE,
         {"snap": "SELECT region_id, SUM(amount) AS total FROM p.raw.sales WHERE sold_on = CURRENT_DATE() GROUP BY region_id",
          "recent": "SELECT region_id, SUM(amount) AS total FROM p.raw.sales WHERE sold_on >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY) GROUP BY region_id",
          "shard": "SELECT region_id, SUM(amount) AS total FROM `p.raw.sales_*` WHERE _TABLE_SUFFIX = '20240101' GROUP BY region_id"},
         {"snap": "partial", "recent": "partial", "shard": "none"}, kinds={"snap": "incremental"}),
    Case("average of averages is not the average", "SELECT region_id, AVG(amount) AS mean FROM p.raw.sales GROUP BY region_id",
         {"county_avg": "SELECT region_id, county_id, AVG(amount) AS mean FROM p.raw.sales GROUP BY region_id, county_id",
          "rolled": "WITH c AS (SELECT region_id, county_id, AVG(amount) AS m FROM p.raw.sales GROUP BY region_id, county_id) "
                    "SELECT region_id, AVG(m) AS mean FROM c GROUP BY region_id"},
         {"county_avg": "none", "rolled": "partial"}),
    Case("one column name, two meanings", BASE,
         {"cost_total": "SELECT region_id, SUM(cost) AS total FROM p.raw.sales GROUP BY region_id"},
         {"cost_total": "partial"}),
    Case("different names, same meaning", BASE,
         {"renamed": "SELECT region_id AS zone, SUM(amount) AS gross FROM p.raw.sales GROUP BY region_id"},
         {"renamed": "same_meaning"}),
    Case("unparsed model breaks the trace", BASE,
         {"opaque": "SELECT region_id, SUM(amount) AS total FROM p.raw.sales GROUP BY region_id ??? not sql"},
         {"opaque": "unknown"}),
    Case("unexpanded star breaks the trace", BASE,
         {"starred": "SELECT * FROM p.raw.undeclared"},
         {"starred": "unknown"}),
    Case("external table breaks the trace", BASE,
         {"ext": "SELECT region_id, SUM(amount) AS total FROM p.ext.elsewhere GROUP BY region_id"},
         {"ext": "none"}),
]

#: a change that makes everything unknown must fail here
MIN_DECIDED = 8


def build(case: Case) -> Pipeline:
    models = {}
    for name, sql in case.candidates.items():
        target = Target("p", "core", name)
        models[target.key] = Model(target, case.kinds.get(name, "table"), sql)
    return Pipeline(models, sources=dict(SOURCES), source_schema=dict(SCHEMA))


def label(case: Case) -> dict[str, str]:
    result = find_overlaps(build(case), case.target)
    found = {m.table.split(".")[-1]: m.kind for m in result.matches}
    return {name: found.get(name, "none") for name in case.candidates}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_hard_case_labels(case):
    assert label(case) == case.expected


def test_no_false_same_meaning_and_enough_decided():
    counts = {"same_meaning": 0, "contains": 0, "partial": 0, "unknown": 0, "none": 0}
    for case in CASES:
        for name, got in label(case).items():
            counts[got] += 1
            if got == "same_meaning":
                assert case.expected[name] == "same_meaning", f"false same_meaning: {case.name}/{name}"
    decided = sum(counts.values()) - counts["unknown"]
    print(f"hard cases: {decided} decided, {counts['unknown']} unknown, {counts}")
    assert decided >= MIN_DECIDED
