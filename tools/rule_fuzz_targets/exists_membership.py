"""EXISTS, membership and declared-key rules: implied tests, projected IN and FK-backed joins."""

from ._base import expand


TEMPLATES = [
    # A grouped source already applies the same correlated test that the outer query repeats.
    "SELECT p.id, g.n FROM p JOIN (SELECT t.y AS k, SUM(t.x) AS n FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y) GROUP BY t.y) AS g ON g.k = p.tid WHERE EXISTS (SELECT 1 FROM u WHERE u.k = p.tid)",
    # With both operands known non-NULL, scalar IN can become equality EXISTS.
    "SELECT (t.id) IN (SELECT u.k FROM u) AS present FROM t",
    "SELECT (t.id) NOT IN (SELECT u.k FROM u) AS absent FROM t",
    # A nullable operand must retain IN's three-valued semantics.
    "SELECT t.x IN (SELECT u.w FROM u) AS maybe_present FROM t",
    # Parent keys plus a non-NULL child FK make this inner join redundant.
    "SELECT p.id FROM p JOIN t ON p.tid = t.id",
    # Outer-join padding is a near miss for the same FK implication.
    "SELECT p.id FROM p LEFT JOIN t ON p.tid = t.id",
    # Every p row has a referenced t row, so this plain existence test is witnessed by the FK.
    "SELECT p.id FROM p WHERE EXISTS (SELECT 1 FROM t WHERE t.id IS NOT NULL)",
    # An unrelated table's existence test is not implied by the p-to-t FK.
    "SELECT p.id FROM p WHERE EXISTS (SELECT 1 FROM u WHERE u.k IS NOT NULL)",
]


def cases(seed: int, count: int) -> list[dict]:
    generated = expand(TEMPLATES, seed, count, "exists_membership")
    for case in generated:
        template = int(case["source"].split(":")[2])
        constraints = case["constraints"]
        # Make the intended memberships and FK joins available on every seed while preserving
        # the base generator's other declarations. The non-NULL facts are also required by the
        # rule guards; the near-miss queries deliberately use nullable x/w columns instead.
        constraints.setdefault("t", {}).setdefault("not_null", []).append("id")
        constraints.setdefault("t", {}).setdefault("keys", []).append(["id"])
        constraints.setdefault("u", {}).setdefault("not_null", []).append("k")
        constraints.setdefault("u", {}).setdefault("keys", []).append(["k"])
        p_rules = constraints.setdefault("p", {})
        p_rules["not_null"] = [column for column in p_rules.get("not_null", []) if column.lower() != "tid"]
        p_rules["foreign_keys"] = []
        if template in (4, 5, 6):
            p_rules["foreign_keys"].append([["tid"], "t", ["id"]])
        if template in (4, 6):
            p_rules["not_null"].append("tid")
        for table in ("t", "u", "p"):
            rules = constraints[table]
            rules["not_null"] = sorted(set(rules.get("not_null", [])))
            rules["keys"] = [list(key) for key in dict.fromkeys(tuple(key) for key in rules.get("keys", []))]
            unique_foreign_keys = []
            seen_foreign_keys = set()
            for columns, parent, parent_columns in rules.get("foreign_keys", []):
                key = (tuple(columns), parent, tuple(parent_columns))
                if key not in seen_foreign_keys:
                    seen_foreign_keys.add(key)
                    unique_foreign_keys.append([list(columns), parent, list(parent_columns)])
            rules["foreign_keys"] = unique_foreign_keys
    return generated
