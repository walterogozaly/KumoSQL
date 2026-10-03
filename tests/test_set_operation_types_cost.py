"""The set-operation type reader reads each branch once, however deeply derived tables nest (#544).

Each column of a derived table asks for the outputs of the whole query under it, so a chain of set operations over
wide selects once repeated that work per column per level: a pair of the mined Calcite suite took 1,050 s here.
"""

from kumosql import set_operation_types
from kumosql.set_operation_types import mixed_types, unchecked_types

COLUMNS = [f"c{i}" for i in range(9)]
TYPES = {"t": {name: "INT64" for name in COLUMNS}}


def _chain(depth: int) -> str:
    select = ", ".join(COLUMNS)
    sql = f"SELECT {select} FROM t"
    for level in range(depth):
        sql = f"SELECT {select} FROM (SELECT {select} FROM t UNION ALL {sql}) AS d{level}"
    return sql


def test_a_deep_chain_of_set_operations_over_wide_selects_is_read_in_linear_work(monkeypatch):
    reads = []
    original = set_operation_types._Types._read_outputs

    def counting(self, query, depth):
        reads.append(1)
        return original(self, query, depth)

    monkeypatch.setattr(set_operation_types._Types, "_read_outputs", counting)
    sql = _chain(12)
    assert mixed_types(sql, TYPES, "bigquery") is None
    assert not unchecked_types(sql, TYPES, "bigquery")
    assert len(reads) < 2000  # per query node and depth, not per column per level (9**12 before)


def test_the_memo_does_not_hide_a_type_conflict_deep_in_the_chain():
    conflicting = {"t": {**{name: "INT64" for name in COLUMNS}, "c3": "STRING"}}
    select = ", ".join(COLUMNS)
    other = ", ".join("'x' AS c3" if name == "c3" else name for name in COLUMNS)
    sql = f"SELECT {select} FROM (SELECT {select} FROM t UNION ALL SELECT {other} FROM t) AS d"
    assert mixed_types(sql, {"t": {name: "INT64" for name in COLUMNS}}, "bigquery")
    assert mixed_types(sql, conflicting, "bigquery") is None
