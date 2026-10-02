"""Write tests/fixtures/mv_reuse/adapted_cases.json: shared-model reuse cases written for KumoSQL.

Each case is a model (the SQL of an existing table or view) and a query that should, or cannot, read
from it. Schema: Calcite's HR test schema (see tools/mv_reuse_bench.py). ``expect`` is ``rewrite``
(the query can be answered from the model) or ``none`` (the model lacks something the query needs, so
a rewrite would be wrong). Kept apart from the Calcite cases, which are extracted unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "mv_reuse" / "adapted_cases.json"

EMP = "SELECT empid, deptno, name, salary FROM emps"
BY_DEPT = "SELECT deptno, SUM(salary) AS total, COUNT(*) AS n, MIN(salary) AS lo, MAX(salary) AS hi FROM emps GROUP BY deptno"
BY_DEPT_NAME = "SELECT deptno, name, SUM(salary) AS total, COUNT(*) AS n FROM emps GROUP BY deptno, name"
JOINED = "SELECT e.empid, e.salary, d.name AS dept FROM emps e JOIN depts d ON e.deptno = d.deptno"

CASES = [
    ("same-query", EMP, EMP, "rewrite"),
    ("column-subset", EMP, "SELECT empid, salary FROM emps", "rewrite"),
    ("extra-filter", EMP, "SELECT empid FROM emps WHERE salary > 100", "rewrite"),
    ("filter-on-model-column", EMP, "SELECT name FROM emps WHERE deptno = 10 AND salary < 50", "rewrite"),
    ("expression-of-columns", EMP, "SELECT empid, salary * 2 AS double_pay FROM emps", "rewrite"),
    ("missing-column", EMP, "SELECT empid, commission FROM emps", "none"),
    ("model-filtered-too-much", "SELECT empid, deptno, salary FROM emps WHERE deptno = 10", "SELECT empid FROM emps WHERE salary > 1", "none"),
    ("model-filter-implied", "SELECT empid, deptno, salary FROM emps WHERE deptno = 10", "SELECT empid FROM emps WHERE deptno = 10 AND salary > 1", "rewrite"),
    ("rollup-sum", BY_DEPT, "SELECT SUM(salary) FROM emps", "rewrite"),
    ("rollup-count-star", BY_DEPT, "SELECT COUNT(*) FROM emps", "rewrite"),
    ("rollup-min", BY_DEPT, "SELECT MIN(salary) FROM emps", "rewrite"),
    ("rollup-max-by-dept", BY_DEPT, "SELECT deptno, MAX(salary) FROM emps GROUP BY deptno", "rewrite"),
    ("rollup-two-level", BY_DEPT_NAME, "SELECT deptno, SUM(salary) FROM emps GROUP BY deptno", "rewrite"),
    ("rollup-two-level-count", BY_DEPT_NAME, "SELECT deptno, COUNT(*) FROM emps GROUP BY deptno", "rewrite"),
    ("rollup-global-two-level", BY_DEPT_NAME, "SELECT SUM(salary) FROM emps", "rewrite"),
    ("rollup-filter-on-key", BY_DEPT, "SELECT deptno, SUM(salary) FROM emps WHERE deptno > 10 GROUP BY deptno", "rewrite"),
    ("rollup-filter-on-measure", BY_DEPT, "SELECT deptno, SUM(salary) FROM emps WHERE salary > 10 GROUP BY deptno", "none"),
    ("rollup-finer-grain", BY_DEPT, "SELECT deptno, name, SUM(salary) FROM emps GROUP BY deptno, name", "none"),
    ("rollup-avg-needs-count", "SELECT deptno, SUM(salary) AS total FROM emps GROUP BY deptno", "SELECT deptno, AVG(salary) FROM emps GROUP BY deptno", "none"),
    ("rollup-count-distinct-not-summable", BY_DEPT_NAME, "SELECT deptno, COUNT(DISTINCT name) FROM emps GROUP BY deptno", "rewrite"),
    ("rollup-count-distinct-lost", BY_DEPT, "SELECT COUNT(DISTINCT deptno) FROM emps", "rewrite"),
    ("join-reuse", JOINED, "SELECT dept, SUM(salary) FROM (SELECT e.salary, d.name AS dept FROM emps e JOIN depts d ON e.deptno = d.deptno) t GROUP BY dept", "rewrite"),
    ("join-model-for-single-table", JOINED, "SELECT empid FROM emps", "none"),
    ("join-swapped-order", "SELECT e.empid, d.name FROM emps e JOIN depts d ON e.deptno = d.deptno", "SELECT e.empid, d.name FROM depts d JOIN emps e ON d.deptno = e.deptno", "rewrite"),
    ("distinct-model-plain-query", "SELECT DISTINCT deptno FROM emps", "SELECT deptno FROM emps", "none"),
    ("plain-model-distinct-query", "SELECT deptno FROM emps", "SELECT DISTINCT deptno FROM emps", "rewrite"),
    ("union-branch", "SELECT empid FROM emps UNION ALL SELECT empid FROM dependents", "SELECT empid FROM emps UNION ALL SELECT empid FROM dependents", "rewrite"),
    ("model-limited", "SELECT empid, salary FROM emps ORDER BY salary LIMIT 10", "SELECT empid FROM emps", "none"),
    ("model-aggregated-plain-query", BY_DEPT, "SELECT deptno, salary FROM emps", "none"),
]


def main() -> None:
    cases = [{"id": f"shared.{name}", "name": name, "origin": "adapted", "materialization": model, "query": query, "expect": expect, "schema": "hr", "disabled": False} for name, model, query, expect in CASES]
    OUT.write_text(json.dumps({"description": __doc__.strip().splitlines()[0], "cases": cases}, indent=1) + "\n", encoding="utf-8")
    print(f"{len(cases)} cases written to {OUT}")


if __name__ == "__main__":
    main()
