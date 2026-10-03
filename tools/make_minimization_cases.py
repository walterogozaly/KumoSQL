"""Write the generated table-minimization cases, each one verified on DuckDB (and proved where possible).

Each case is a pipeline of 3 to 20 tables built from *modules*, one redundancy pattern each
(pass-through chains, duplicated logic, dead tables, unused columns and joins, CTEs that repeat a
table, mergeable tables, redundant filters, and irreducible tables that only carry traps). A module
gives its original tables, its reference (minimized) tables, its protected tables and its traps:
tempting simplifications that change a protected output. Modules share only the source tables, so a
case's reference is the union of its modules' references and a trap is the other modules' reference
plus one module's trap.

Verification, per case: the reference must give every protected table the same column names and
the same bag of rows as the original on every database (random ones respecting keys and NOT NULL,
the targeted ones and every trap witness), run on DuckDB with the optimizer off; every trap must
differ on a database, which is shrunk and stored as its witness; the reference must score lower
than the original unless the case is irreducible. KumoSQL's ``prove_models`` is then tried on each
protected table and the outcome recorded. A case that fails verification stops the run: the
generator is wrong, not the case.

The split is fixed by a hash of the case id (``minimization_cases.held_out_split``). The output is
committed; nothing here runs at evaluation time.

    python tools/make_minimization_cases.py                     # writes benchmarks/table_minimization/generated.jsonl
    python tools/make_minimization_cases.py --count 20 --out /tmp/x.jsonl --jobs 4
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import random
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

import minimization_cases as mc  # noqa: E402

SEED = 20261002
COUNT = 300
DATABASES = 300
EXTRA_TRAP_SEARCH = 3000
MAX_TABLES = 20

SOURCES = {
    "orders": {
        "columns": {"id": "INT64", "customer_id": "INT64", "amount": "INT64", "status": "STRING", "region": "STRING"},
        "key": ["id"],
        "values": {"status": ["paid", "open", "void"], "region": ["eu", "us", "apac"]},
    },
    "customers": {
        "columns": {"id": "INT64", "region": "STRING", "tier": "STRING"},
        "key": ["id"],
        "values": {"region": ["eu", "us", "apac"], "tier": ["gold", "basic"]},
    },
    "items": {
        "columns": {"order_id": "INT64", "sku": "STRING", "qty": "INT64", "price": "INT64"},
        "values": {"sku": ["s1", "s2", "s3"]},
    },
    "products": {
        "columns": {"sku": "STRING", "category": "STRING", "cost": "INT64"},
        "key": ["sku"],
        "values": {"sku": ["s1", "s2", "s3"], "category": ["toys", "food"]},
    },
    "payments": {
        "columns": {"id": "INT64", "order_id": "INT64", "amount": "INT64", "method": "STRING"},
        "key": ["id"],
        "not_null": ["order_id"],
        "values": {"method": ["card", "cash"]},
    },
    "events": {
        "columns": {"user_id": "INT64", "kind": "STRING", "value": "INT64"},
        "values": {"kind": ["buy", "refund", "view"]},
    },
}
ORDER_COLUMNS = list(SOURCES["orders"]["columns"])


def q(select: str, frm: str, where=(), group: str = "", having: str = "", distinct: bool = False) -> str:
    sql = f"SELECT {'DISTINCT ' if distinct else ''}{select} FROM {frm}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if group:
        sql += f" GROUP BY {group}"
    if having:
        sql += f" HAVING {having}"
    return sql


class Namer:
    def __init__(self) -> None:
        self.used = set(SOURCES)

    def __call__(self, stem: str) -> str:
        name, n = stem, 1
        while name in self.used:
            n += 1
            name = f"{stem}_{n}"
        self.used.add(name)
        return name


@dataclass
class Module:
    family: str
    original: dict[str, str]
    reference: dict[str, str]
    protected: list[str]
    traps: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.original)


# ------------------------------------------------------------ consumers of an orders-shaped relation

# Each consumer reads a relation with the orders columns (possibly renamed: ``c`` maps the logical
# column to the name in that relation) and gives (select, where, group, having). Output columns are
# always aliased, so a reference reading ``orders`` directly keeps the same column names.
CONSUMERS = [
    lambda c: (f"{c['customer_id']} AS customer_id, SUM({c['amount']}) AS total", [], c["customer_id"], ""),
    lambda c: (f"{c['status']} AS status, COUNT(*) AS n", [], c["status"], ""),
    lambda c: (f"{c['region']} AS region, MAX({c['amount']}) AS top", [f"{c['status']} = 'paid'"], c["region"], ""),
    lambda c: (f"{c['id']} AS id, {c['amount']} AS amount", [f"{c['amount']} > 2"], "", ""),
    lambda c: (f"COUNT(DISTINCT {c['customer_id']}) AS buyers", [], "", ""),
    lambda c: (f"{c['customer_id']} AS customer_id, COUNT(*) AS orders", [f"{c['status']} <> 'void'"], c["customer_id"],
               "COUNT(*) > 1"),
    lambda c: (f"{c['region']} AS region, SUM({c['amount']}) AS total, MIN({c['amount']}) AS smallest", [], c["region"], ""),
]
REPORT_STEMS = ["rpt_customer_totals", "rpt_status_counts", "rpt_region_top", "rpt_big_orders", "rpt_buyers",
                "rpt_repeat_customers", "rpt_region_totals"]


def consumer(rng: random.Random, index: int | None = None):
    index = rng.randrange(len(CONSUMERS)) if index is None else index
    return index, CONSUMERS[index]


def consumer_sql(index: int, columns: dict[str, str], relation: str, extra_where=()) -> str:
    select, where, group, having = CONSUMERS[index](columns)
    return q(select, relation, [*extra_where, *where], group, having)


IDENTITY = {c: c for c in ORDER_COLUMNS}
FILTERS = [  # filters a chain link may apply, on the logical column
    lambda c: f"{c['amount']} > 0",
    lambda c: f"{c['status']} <> 'void'",
    lambda c: f"{c['region']} IN ('eu', 'us')",
    lambda c: f"{c['customer_id']} IS NOT NULL",
]
IMPLIED_BY = [{3}, {2, 5}, set(), {4}]  # consumers whose own query already implies each filter


# ------------------------------------------------------------ module: pass-through chains


def passthrough_chain(rng: random.Random, name: Namer) -> Module:
    depth = rng.randint(1, 4)
    readers = rng.randint(1, 3)
    with_filter = rng.random() < 0.45
    with_rename = rng.random() < 0.4
    protect_middle = depth >= 2 and rng.random() < 0.3
    filter_at = rng.randrange(depth) if with_filter else -1
    rename_at = rng.randrange(depth) if with_rename else -1
    middle = rng.randrange(depth - 1) if protect_middle else -1  # never the last link
    if protect_middle:
        # renames and filters happen at or before the protected link
        filter_at = min(filter_at, middle) if with_filter else -1
        rename_at = min(rename_at, middle) if with_rename else -1
    indexes = [consumer(rng)[0] for _ in range(readers)]
    # a chain filter some report already implies (its own "amount > 2" after "amount > 0") is left out
    filter_pool = [f for f, implied in zip(FILTERS, IMPLIED_BY) if not implied & set(indexes)]
    stems = ["stg_orders", "int_orders", "orders_clean", "orders_v", "base_orders"]
    original: dict[str, str] = {}
    links: list[tuple[str, dict, list[str]]] = []  # (name, columns after the link, filters so far)
    previous, cols, filters = "orders", dict(IDENTITY), []
    for i in range(depth):
        link = name(stems[i % len(stems)])
        if i == rename_at:
            new = {**cols, "amount": "order_amount", "customer_id": "cust_id"}
            select = ", ".join(f"{cols[c]} AS {new[c]}" if new[c] != cols[c] else cols[c] for c in ORDER_COLUMNS)
            original[link] = q(select, previous)
            cols = new
        elif i == filter_at:
            f = rng.choice(filter_pool)
            original[link] = q("*", previous, [f(cols)])
            filters = [*filters, f(IDENTITY)]
        else:
            original[link] = q("*", previous)
        links.append((link, dict(cols), list(filters)))
        previous = link
    reports = []
    for index in indexes:
        report = name(REPORT_STEMS[index])
        original[report] = consumer_sql(index, cols, previous)
        reports.append((report, index))
    protected = [r for r, _ in reports]
    reference: dict[str, str] = {}
    trap: dict[str, str] = {}
    if protect_middle:
        mid, mid_cols, mid_filters = links[middle]
        protected.append(mid)
        select = "*" if mid_cols == IDENTITY else ", ".join(
            f"{c} AS {mid_cols[c]}" if mid_cols[c] != c else c for c in ORDER_COLUMNS)
        reference[mid] = q(select, "orders", mid_filters)
        trap[mid] = q(select, "orders")
        for report, index in reports:
            reference[report] = consumer_sql(index, mid_cols, mid)
            trap[report] = reference[report]
    else:
        for report, index in reports:
            reference[report] = consumer_sql(index, IDENTITY, "orders", filters)
            trap[report] = consumer_sql(index, IDENTITY, "orders")
    traps = [("drops the chain together with the filter one link applies", trap)] if filters else []
    return Module("passthrough_chain", original, reference, protected, traps)


# ------------------------------------------------------------ shared logic used by several modules

# Each logic: spellings of the same query over the sources, the columns it returns, readers of it
# (``{t}`` is the table read), and a near-copy that differs on some database.
LOGICS = [
    {
        "stem": "paid_customer_totals",
        "spellings": [
            "SELECT customer_id, SUM(amount) AS total, COUNT(*) AS n FROM orders WHERE status = 'paid' GROUP BY customer_id",
            "SELECT o.customer_id, SUM(o.amount) AS total, COUNT(*) AS n FROM orders AS o WHERE 'paid' = o.status GROUP BY o.customer_id",
            "SELECT customer_id, SUM(amount) AS total, COUNT(1) AS n FROM orders WHERE status = 'paid' GROUP BY 1",
        ],
        "near": ("counts non-NULL amounts instead of rows",
                 "SELECT customer_id, SUM(amount) AS total, COUNT(amount) AS n FROM orders WHERE status = 'paid' GROUP BY customer_id"),
        "readers": [
            "SELECT customer_id, total FROM {t} WHERE total > 3",
            "SELECT COUNT(*) AS customers, SUM(n) AS orders FROM {t}",
            "SELECT MAX(total) AS best FROM {t}",
            "SELECT n, COUNT(*) AS customers FROM {t} GROUP BY n",
        ],
        "telling": [1, 3],  # readers on which the near-copy gives other rows
    },
    {
        "stem": "order_tiers",
        "spellings": [
            "SELECT o.id, o.amount, c.tier FROM orders AS o JOIN customers AS c ON o.customer_id = c.id WHERE o.status <> 'void'",
            "SELECT ord.id, ord.amount, cus.tier FROM orders AS ord INNER JOIN customers AS cus ON cus.id = ord.customer_id WHERE ord.status <> 'void'",
            "SELECT orders.id, orders.amount, customers.tier FROM orders JOIN customers ON orders.customer_id = customers.id WHERE NOT orders.status = 'void'",
        ],
        "near": ("keeps orders with no customer (LEFT JOIN)",
                 "SELECT o.id, o.amount, c.tier FROM orders AS o LEFT JOIN customers AS c ON o.customer_id = c.id WHERE o.status <> 'void'"),
        "readers": [
            "SELECT tier, SUM(amount) AS total FROM {t} GROUP BY tier",
            "SELECT id FROM {t} WHERE tier = 'gold'",
            "SELECT tier, COUNT(*) AS n FROM {t} GROUP BY tier",
            "SELECT COUNT(*) AS n, SUM(amount) AS total FROM {t}",
        ],
        "telling": [0, 2, 3],
    },
    {
        "stem": "toy_revenue",
        "spellings": [
            "SELECT i.order_id, SUM(i.qty * i.price) AS revenue FROM items AS i JOIN products AS p ON i.sku = p.sku WHERE p.category = 'toys' GROUP BY i.order_id",
            "SELECT it.order_id, SUM(it.price * it.qty) AS revenue FROM items AS it JOIN products AS pr ON pr.sku = it.sku WHERE pr.category = 'toys' GROUP BY it.order_id",
        ],
        "near": ("treats every non-food category, but not NULL, as toys",
                 "SELECT i.order_id, SUM(i.qty * i.price) AS revenue FROM items AS i JOIN products AS p ON i.sku = p.sku WHERE p.category <> 'food' GROUP BY i.order_id"),
        "readers": [
            "SELECT order_id FROM {t} WHERE revenue > 4",
            "SELECT SUM(revenue) AS revenue FROM {t}",
            "SELECT COUNT(*) AS orders FROM {t}",
        ],
        "telling": [0, 1, 2],
    },
    {
        "stem": "card_payments",
        "spellings": [
            "SELECT order_id, SUM(amount) AS paid FROM payments WHERE method = 'card' GROUP BY order_id",
            "SELECT p.order_id, SUM(p.amount) AS paid FROM payments AS p WHERE p.method = 'card' GROUP BY p.order_id",
        ],
        "near": ("counts every method but cash, so NULL methods drop and others stay",
                 "SELECT order_id, SUM(amount) AS paid FROM payments WHERE method <> 'cash' GROUP BY order_id"),
        "readers": [
            "SELECT order_id, paid FROM {t} WHERE paid > 2",
            "SELECT COUNT(*) AS orders, MAX(paid) AS biggest FROM {t}",
        ],
        "telling": [0, 1],
    },
]


def _reader(logic: dict, rng: random.Random, table: str) -> str:
    return rng.choice(logic["readers"]).format(t=table)


# ------------------------------------------------------------ module: duplicated logic


def duplicated_logic(rng: random.Random, name: Namer) -> Module:
    logic = rng.choice(LOGICS)
    copies = rng.randint(2, 4)
    near = rng.random() < 0.4
    original, reference, protected = {}, {}, []
    tables = [name(logic["stem"] if i == 0 else f"{logic['stem']}_copy") for i in range(copies)]
    keeper = tables[0]
    readers = rng.sample(logic["readers"] * 2, copies)
    protect_copy = 1 if copies > 2 and rng.random() < 0.3 else -1
    for i, table in enumerate(tables):
        original[table] = logic["spellings"][i % len(logic["spellings"])]
        report = name(f"rpt_{logic['stem']}")
        original[report] = readers[i].format(t=table)
        protected.append(report)
        reference[report] = readers[i].format(t=keeper)
        if i == protect_copy:
            protected.append(table)
            reference[table] = f"SELECT * FROM {keeper}"
    reference[keeper] = original[keeper]
    traps = []
    if near:
        note, near_sql = logic["near"]
        twin = name(f"{logic['stem']}_alt")
        report = name(f"rpt_{logic['stem']}")
        reader = logic["readers"][rng.choice(logic["telling"])]
        original[twin] = near_sql
        original[report] = reader.format(t=twin)
        protected.append(report)
        reference[twin] = near_sql
        reference[report] = reader.format(t=twin)
        trap = {**reference, report: reader.format(t=keeper)}
        del trap[twin]
        traps.append((f"merges a near-copy that {note}", trap))
    return Module("duplicated_logic", original, reference, protected, traps)


# ------------------------------------------------------------ module: dead tables


DEAD = [
    ("tmp_order_tiers", "SELECT o.id, c.tier FROM orders AS o JOIN customers AS c ON o.customer_id = c.id"),
    ("old_daily_totals", "SELECT region, SUM(amount) AS total FROM orders GROUP BY region"),
    ("bak_payments", "SELECT * FROM payments"),
    ("scratch_events", "SELECT user_id, COUNT(*) AS n FROM events WHERE kind = 'view' GROUP BY user_id"),
    ("tmp_sku_costs", "SELECT i.sku, SUM(i.qty * p.cost) AS cost FROM items AS i LEFT JOIN products AS p ON i.sku = p.sku GROUP BY i.sku"),
    ("old_status_mix", "SELECT status, CASE WHEN amount > 3 THEN 'big' ELSE 'small' END AS size, COUNT(*) AS n FROM orders GROUP BY 1, 2"),
    ("unused_customer_rank", "SELECT id, ROW_NUMBER() OVER (PARTITION BY region ORDER BY id) AS rn FROM customers"),
]
LIVE = [
    ("rpt_gold_customers", "SELECT id, region FROM customers WHERE tier = 'gold'"),
    ("rpt_refunds", "SELECT user_id, SUM(value) AS refunded FROM events WHERE kind = 'refund' GROUP BY user_id"),
    ("rpt_method_totals", "SELECT method, SUM(amount) AS total FROM payments GROUP BY method"),
]


def dead_tables(rng: random.Random, name: Namer) -> Module:
    count = rng.randint(1, 5)
    live_stem, live_sql = rng.choice(LIVE)
    live = name(live_stem)
    original = {live: live_sql}
    reference = {live: live_sql}
    dead = []
    for stem, sql in rng.sample(DEAD, min(count, len(DEAD))):
        table = name(stem)
        original[table] = sql
        dead.append(table)
    # some dead tables read other dead tables or the live one: still dead
    for _ in range(rng.randint(0, 2)):
        table = name(rng.choice(["old_report", "tmp_extract", "deprecated_view"]))
        target = rng.choice([*dead, live])
        original[table] = f"SELECT * FROM {target}"
        dead.append(table)
    protected = [live]
    traps = []
    if rng.random() < 0.4:
        vip = name("vip_customers")
        report = name("rpt_vip_orders")
        original[vip] = "SELECT id FROM customers WHERE tier = 'gold' AND region = 'eu'"
        original[report] = f"SELECT id, amount FROM orders WHERE customer_id IN (SELECT id FROM {vip})"
        reference[vip] = original[vip]
        reference[report] = original[report]
        protected.append(report)
        trap = {k: v for k, v in reference.items() if k != vip}
        trap[report] = "SELECT id, amount FROM orders"
        traps.append(("drops a table that looks unused but feeds a protected IN filter", trap))
    return Module("dead_tables", original, reference, protected, traps)


# ------------------------------------------------------------ module: unused columns and joins


def unused_columns_joins(rng: random.Random, name: Namer) -> Module:
    join = rng.choice(["left_key", "left_key", "inner", "left_many"])
    wide = name(rng.choice(["int_orders_enriched", "orders_wide", "fct_orders"]))
    size_case = "CASE WHEN o.amount > 3 THEN 'big' ELSE 'small' END AS size"
    window = "ROW_NUMBER() OVER (PARTITION BY o.customer_id ORDER BY o.id) AS rn"
    plain = "o.id, o.customer_id, o.amount, o.status"
    if join == "left_many":
        joined, extra = "orders AS o LEFT JOIN items AS i ON o.id = i.order_id", "i.sku"
    elif join == "inner":
        joined, extra = "orders AS o JOIN customers AS c ON o.customer_id = c.id", "c.tier"
    else:
        joined, extra = "orders AS o LEFT JOIN customers AS c ON o.customer_id = c.id", "c.tier"
    original = {wide: q(f"{plain}, {extra}, {size_case}, {window}", joined)}
    readers = [
        ("rpt_customer_totals", "SELECT customer_id, SUM(amount) AS total FROM {t} GROUP BY customer_id"),
        ("rpt_status_counts", "SELECT status, COUNT(*) AS n FROM {t} GROUP BY status"),
        ("rpt_big_orders", "SELECT id, amount FROM {t} WHERE amount > 2"),
    ]
    uses_size = rng.random() < 0.35
    chosen = rng.sample(readers, rng.randint(1, 3))
    if uses_size:
        chosen.append(("rpt_size_mix", "SELECT size, COUNT(*) AS n FROM {t} GROUP BY size"))
    protected = []
    for stem, sql in chosen:
        report = name(stem)
        original[report] = sql.format(t=wide)
        protected.append(report)
    removable = join == "left_key"  # a LEFT JOIN on the other table's key keeps every row once
    reference = {}
    if uses_size or not removable:
        slim = f"{plain}, {size_case}" if uses_size else plain
        reference[wide] = q(slim, "orders AS o" if removable else joined)
        source = wide
    else:
        source = "orders"
    for (stem, sql), report in zip(chosen, protected):
        reference[report] = sql.format(t=source)
    traps = []
    if not removable:
        trap = dict(reference)
        if uses_size:
            trap[wide] = q(f"{plain}, {size_case}", "orders AS o")
        else:
            trap.pop(wide)
            for (stem, sql), report in zip(chosen, protected):
                trap[report] = sql.format(t="orders")
        why = "an inner join that drops orders without a customer" if join == "inner" else \
            "a join to items that repeats an order once per item"
        traps.append((f"removes {why}", trap))
    return Module("unused_columns_joins", original, reference, protected, traps)


# ------------------------------------------------------------ module: CTE that repeats a table


def cte_repeats_table(rng: random.Random, name: Namer) -> Module:
    logic = rng.choice(LOGICS)
    near = rng.random() < 0.4
    table = name(logic["stem"])
    protect_table = rng.random() < 0.6
    original = {table: logic["spellings"][0]}
    protected = []
    if protect_table:
        protected.append(table)
    else:  # another protected table reads it, so it has to stay
        other = name(f"rpt_{logic['stem']}")
        original[other] = logic["readers"][0].format(t=table)
        protected.append(other)
    reader = logic["readers"][rng.choice([i for i in logic["telling"] if i]) if near else rng.randrange(1, len(logic["readers"]))]
    report = name(f"rpt_{logic['stem']}_summary")
    body = logic["near"][1] if near else logic["spellings"][-1]
    original[report] = f"WITH base AS ({body}) " + reader.format(t="base")
    protected.append(report)
    reference = dict(original)
    traps = []
    if near:
        trap = {**reference, report: reader.format(t=table)}
        traps.append((f"replaces a CTE with a table it only resembles: the CTE {logic['near'][0]}", trap))
    else:
        reference[report] = reader.format(t=table)
    return Module("cte_repeats_table", original, reference, protected, traps)


# ------------------------------------------------------------ module: mergeable tables


def mergeable_tables(rng: random.Random, name: Namer) -> Module:
    kind = rng.choice(["stages", "aggregates", "aggregates", "split_union"])
    if kind == "stages":
        stages = rng.randint(2, 4)
        steps = rng.sample(["status = 'paid'", "amount > 0", "region <> 'apac'", "customer_id IS NOT NULL"], stages)
        cols = "id, customer_id, amount, status, region"
        original, previous, filters = {}, "orders", []
        for cond in steps:
            stage = name({"status = 'paid'": "stg_paid_orders", "amount > 0": "int_positive_orders",
                          "region <> 'apac'": "int_regional_orders", "customer_id IS NOT NULL": "int_known_customers"}[cond])
            original[stage] = q(cols, previous, [cond])
            filters.append(cond)
            previous = stage
        index = rng.choice([0, 1, 2, 5, 6])
        report = name(REPORT_STEMS[index])
        select, where, group, having = CONSUMERS[index]({**IDENTITY})
        original[report] = q(select, previous, where, group, having)
        reference = {report: q(select, "orders", [*filters, *where], group, having)}
        trap = {report: q(select, "orders", where, group, having)}
        return Module("mergeable_tables", original, reference, [report],
                      [("folds the stages into the report but loses their filters", trap)])
    if kind == "aggregates":
        aggs = rng.sample([("total", "SUM(amount)"), ("n", "COUNT(*)"), ("biggest", "MAX(amount)"), ("smallest", "MIN(amount)")],
                          rng.randint(2, 3))
        where = ["status = 'paid'"] if rng.random() < 0.5 else []
        original, parts = {}, []
        for alias, agg in aggs:
            table = name(f"customer_{alias}")
            original[table] = q(f"customer_id, {agg} AS {alias}", "orders", where, "customer_id")
            parts.append((table, alias))
        first = parts[0][0]
        joins = first + "".join(f" JOIN {t} ON {first}.customer_id = {t}.customer_id" for t, _ in parts[1:])
        select = f"{first}.customer_id, " + ", ".join(f"{t}.{a}" for t, a in parts)
        report = name("rpt_customer_profile")
        original[report] = q(select, joins)
        merged = "customer_id, " + ", ".join(f"{agg} AS {alias}" for alias, agg in aggs)
        reference = {report: q(merged, "orders", [*where, "customer_id IS NOT NULL"], "customer_id")}
        trap = {report: q(merged, "orders", where, "customer_id")}
        return Module("mergeable_tables", original, reference, [report],
                      [("merges the per-customer aggregates but keeps the NULL customer the joins dropped", trap)])
    # split_union: per-region tables put back together
    regions = rng.sample(["eu", "us", "apac"], rng.randint(2, 3))
    distinct = rng.random() < 0.35
    cols = "customer_id, amount" if distinct else "id, customer_id, amount"
    original, tables = {}, []
    for region in regions:
        table = name(f"orders_{region}")
        original[table] = q(cols, "orders", [f"region = '{region}'"])
        tables.append(table)
    report = name("rpt_orders_by_region")
    op = " UNION DISTINCT " if distinct else " UNION ALL "
    original[report] = op.join(f"SELECT * FROM {t}" for t in tables)
    inlist = ", ".join(f"'{r}'" for r in regions)
    reference = {report: q(cols, "orders", [f"region IN ({inlist})"], distinct=distinct)}
    if distinct:
        trap = {report: q(cols, "orders", [f"region IN ({inlist})"])}
        note = "merges UNION DISTINCT branches without DISTINCT"
    else:
        trap = {report: q(cols, "orders", ["region IS NOT NULL"]) if len(regions) == 3 else q(cols, "orders")}
        note = "merges the region branches but widens the filter"
    return Module("mergeable_tables", original, reference, [report], [(note, trap)])


# ------------------------------------------------------------ module: redundant filters


REDUNDANT = [  # (upstream filter, implied filter, independent filter)
    ("amount > 3", "amount > 0", "status = 'paid'"),
    ("amount > 3", "amount IS NOT NULL", "region = 'eu'"),
    ("status = 'paid'", "status IS NOT NULL", "amount > 1"),
    ("region IN ('eu', 'us')", "region <> 'apac'", "amount >= 2"),
    ("customer_id > 2", "customer_id > 1", "status <> 'void'"),
]


def redundant_filters(rng: random.Random, name: Namer) -> Module:
    strong, weak, other = rng.choice(REDUNDANT)
    stage = name("stg_filtered_orders")
    report = name("rpt_filtered_orders")
    protect_stage = rng.random() < 0.4
    original = {stage: q("*", "orders", [strong]), report: q("id, amount", stage, [weak, other])}
    if protect_stage:
        reference = {stage: original[stage], report: q("id, amount", stage, [other])}
        protected = [stage, report]
        trap = {stage: q("*", "orders", [weak]), report: reference[report]}
    else:
        reference = {report: q("id, amount", "orders", [strong, other])}
        protected = [report]
        trap = {report: q("id, amount", "orders", [weak, other])}
    return Module("redundant_filters", original, reference, protected,
                  [("keeps the implied filter and drops the one that implies it", trap)])


# ------------------------------------------------------------ module: irreducible tables with traps


IRREDUCIBLE = [
    ("rpt_known_amounts", "SELECT id FROM orders WHERE amount > 0 OR amount <= 0", "SELECT id FROM orders",
     "drops a filter that looks always true but removes NULL amounts"),
    ("rpt_counted_amounts", "SELECT customer_id, COUNT(amount) AS n FROM orders GROUP BY customer_id",
     "SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id", "counts rows instead of non-NULL amounts"),
    ("rpt_regions", "SELECT DISTINCT region FROM customers", "SELECT region FROM customers", "drops a DISTINCT that removes duplicates"),
    ("rpt_customer_orders", "SELECT o.id, o.amount FROM orders AS o JOIN customers AS c ON o.customer_id = c.id",
     "SELECT id, amount FROM orders", "removes an inner join that drops orders without a customer"),
    ("rpt_order_lines", "SELECT o.id, o.amount FROM orders AS o JOIN items AS i ON o.id = i.order_id",
     "SELECT id, amount FROM orders", "removes a join that repeats an order once per item"),
    ("rpt_repeat_buyers", "SELECT customer_id, SUM(amount) AS total FROM orders GROUP BY customer_id HAVING COUNT(*) > 1",
     "SELECT customer_id, SUM(amount) AS total FROM orders GROUP BY customer_id", "drops a HAVING filter"),
    ("rpt_self_status", "SELECT id, status FROM orders WHERE status = status", "SELECT id, status FROM orders",
     "drops a self-comparison that removes NULL statuses"),
    ("rpt_unpaid", "SELECT id FROM orders WHERE id NOT IN (SELECT order_id FROM payments)",
     "SELECT id FROM orders", "drops a NOT IN filter"),
    ("rpt_paid_union", "SELECT customer_id FROM orders WHERE status = 'paid' UNION DISTINCT SELECT customer_id FROM orders WHERE status = 'open'",
     "SELECT customer_id FROM orders WHERE status IN ('paid', 'open')", "turns a UNION DISTINCT into one filter without DISTINCT"),
]


def irreducible(rng: random.Random, name: Namer) -> Module:
    picks = rng.sample(IRREDUCIBLE, rng.randint(1, 3))
    original, traps, protected = {}, [], []
    for stem, sql, _trap, _note in picks:
        table = name(stem)
        original[table] = sql
        protected.append(table)
    for (stem, sql, trap_sql, note), table in zip(picks, protected):
        traps.append((note, {**original, table: trap_sql}))
    return Module("irreducible", original, dict(original), protected, traps)


MODULES = {
    "passthrough_chain": passthrough_chain,
    "duplicated_logic": duplicated_logic,
    "dead_tables": dead_tables,
    "unused_columns_joins": unused_columns_joins,
    "cte_repeats_table": cte_repeats_table,
    "mergeable_tables": mergeable_tables,
    "redundant_filters": redundant_filters,
    "irreducible": irreducible,
}
SIZE_BUCKETS = ((3, 5), (6, 10), (11, 15), (16, 20))


# ------------------------------------------------------------ cases


def build_case(number: int, seed: int = SEED) -> dict:
    """The case's pipeline, before verification (no witnesses, no proofs)."""

    rng = random.Random(f"{seed}-{number}")
    low, high = SIZE_BUCKETS[number % len(SIZE_BUCKETS)]
    lead = list(MODULES)[number % len(MODULES)]  # every family leads in turn
    for _attempt in range(200):
        name = Namer()
        modules = [MODULES[lead](rng, name)]
        while sum(m.size for m in modules) < low:
            modules.append(MODULES[rng.choice(list(MODULES))](rng, name))
        if sum(m.size for m in modules) <= high:
            break
    else:
        raise RuntimeError(f"case {number}: no composition of {low} to {high} tables")
    reference_all = {k: v for m in modules for k, v in m.reference.items()}
    traps = []
    for m in modules:
        for note, tables in m.traps:
            others = {k: v for other in modules if other is not m for k, v in other.reference.items()}
            traps.append({"note": note, "tables": {**others, **tables}})
    case_id = f"gen-{number:04d}"
    original = {k: v for m in modules for k, v in m.original.items()}
    used = set()
    for sql in [*original.values(), *reference_all.values()]:
        used |= mc.reads(sql) & set(SOURCES)
    return {
        "id": case_id,
        "source": "generated",
        "families": sorted({m.family for m in modules}),
        "split": mc.held_out_split(case_id),
        "dialect": "bigquery",
        "sources": {s: SOURCES[s] for s in SOURCES if s in used},
        "tables": original,
        "protected": [p for m in modules for p in m.protected],
        "reference": {"tables": reference_all},
        "traps": traps,
    }


class GeneratorError(Exception):
    pass


def verify_case(case: dict, databases: int = DATABASES, prove: bool = True) -> dict:
    """Check the reference and the traps on DuckDB, store witnesses, complexities and proofs."""

    from kumosql.formatting import pipeline_complexity

    protected = case["protected"]
    original, reference = case["tables"], case["reference"]["tables"]
    worlds = {"o": original, "r": reference, **{f"t{i}": t["tables"] for i, t in enumerate(case["traps"])}}
    engine = mc.Engine(case["sources"], worlds)
    try:
        if engine.errors:
            raise GeneratorError(f"{case['id']}: a pipeline does not run: {engine.errors}")
        for world in worlds.values():
            missing = [p for p in protected if p not in world]
            if missing:
                raise GeneratorError(f"{case['id']}: protected tables missing: {missing}")
        dbs = mc.check_databases({**case, "traps": []}, worlds.values(), databases)
        found = mc.compare(engine, dbs, "o", worlds.keys() - {"o"}, protected, stable_only=mc.order_sensitive(original))
        if found["r"]:
            raise GeneratorError(f"{case['id']}: reference differs on {found['r']['table']}: {found['r']['reason']}"
                                 f" on {found['r']['database']}")
        rng = random.Random(f"{case['id']}-traps")
        pools = mc.domains(case["sources"], [s for w in worlds.values() for s in w.values()])
        for i, trap in enumerate(case["traps"]):
            label = f"t{i}"
            hit = found[label]
            for _ in range(EXTRA_TRAP_SEARCH if not hit else 0):
                db = mc.random_database(case["sources"], pools, rng)
                if mc.differs_on(engine, db, "o", label, protected):
                    hit = {"database": db}
                    break
            if not hit:
                raise GeneratorError(f"{case['id']}: trap {trap['note']!r} never differs")
            witness = mc.shrink(engine, hit["database"], "o", label, protected)
            trap["changes"] = mc.differs_on(engine, witness, "o", label, protected)
            trap["witness"] = mc.witness_json(witness)
            # the reference must agree on every witness too
            if mc.differs_on(engine, witness, "o", "r", protected):
                raise GeneratorError(f"{case['id']}: reference differs on the witness of {trap['note']!r}")
    finally:
        engine.close()
    case["original"] = {"complexity": pipeline_complexity(original)}
    case["reference"]["complexity"] = pipeline_complexity(reference)
    gain = case["original"]["complexity"]["score"] - case["reference"]["complexity"]["score"]
    if gain < 0 or (gain == 0 and reference != original):
        raise GeneratorError(f"{case['id']}: the reference is not simpler")
    proved = {}
    if prove:
        for table, outcome in mc.prove(case["sources"], original, reference, protected).items():
            proved[table] = outcome["status"] in ("proved", "same")
    case["verification"] = {
        "engine": "duckdb, optimizer off",
        "databases": len(dbs) + len(case["traps"]),
        "seed": case["id"],
        "proved": proved,
    }
    case["note"] = "Generated by tools/make_minimization_cases.py; reference and traps verified on DuckDB."
    # field order as in the format
    order = ["id", "source", "families", "split", "dialect", "sources", "tables", "protected", "original",
             "reference", "traps", "verification", "note"]
    return {key: case[key] for key in order}


def _make(args: tuple[int, int, int, bool]) -> dict | str:
    number, seed, databases, prove = args
    try:
        return verify_case(build_case(number, seed), databases, prove)
    except GeneratorError as error:
        return str(error)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=COUNT)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--databases", type=int, default=DATABASES)
    parser.add_argument("--no-prove", action="store_true")
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--out", type=Path, default=mc.CASES_DIR / "generated.jsonl")
    args = parser.parse_args(argv)
    started = time.time()
    work = [(n, args.seed, args.databases, not args.no_prove) for n in range(1, args.count + 1)]
    if args.jobs > 1:
        from multiprocessing import Pool

        with Pool(args.jobs) as pool:
            cases = pool.map(_make, work, chunksize=1)
    else:
        cases = [_make(w) for w in work]
    failed = [c for c in cases if isinstance(c, str)]
    if failed:
        print("\n".join(failed), file=sys.stderr)
        print(f"{len(failed)} cases failed verification; nothing written", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, sort_keys=False) + "\n")
    held = sum(c["split"] == "held_out" for c in cases)
    proved = sum(all(c["verification"]["proved"].values()) for c in cases if c["verification"]["proved"])
    print(f"{len(cases)} cases ({held} held out), {sum(len(c['traps']) for c in cases)} traps, "
          f"references proved in full for {proved}; {time.time() - started:.0f} s -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
