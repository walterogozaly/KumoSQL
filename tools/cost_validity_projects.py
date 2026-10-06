"""Synthetic Dataform projects and job histories for the advisor part of the cost-recommendation validity eval.

Everything here is invented: table names, rows, job counts and byte figures describe no real warehouse.
Each :class:`Case` is a small project (models in BigQuery SQL, declared sources with a few rows of data that
grow every simulated day), the read queries people run against it, and a 14-day job history with measured
bytes, so ``kumosql.advisor.advise`` has something to rank. ``tools/cost_validity_bench.py`` then executes the
advice in DuckDB.

The cases are chosen so that the advisor meets each kind of evidence:

* ``retail``: a hot aggregate view that should be stored (proven), a view reading ``CURRENT_DATE`` (refused), a
  view over a declared source (conditional), a table nothing reads that could be a view (proven un-store);
* ``sampling``: views reading ``RAND``, ``GENERATE_UUID`` and a view over the UUID view (refused, directly and
  through a view), a hot aggregate chain (proven);
* ``staleness``: schedules decide freshness: a view joining tables refreshed by different schedules
  (conditional), a view over one table (proven), un-storing a table that reads a declared source (conditional),
  one that stamps a clock (refused) and one built from a same-schedule table (proven).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from kumosql import Pipeline, Target
from kumosql.advisor import TableSize
from kumosql.costs import ObservedJob
from kumosql.pipeline import Model

START = datetime(2026, 9, 1, tzinfo=timezone.utc)
HISTORY_DAYS = 14
ROWS = 1_000_000  # rows the warehouse tables are claimed to hold in the size figures (the DuckDB sample is far smaller)
DUCKDB_TYPES = {"INT64": "BIGINT", "FLOAT64": "DOUBLE", "STRING": "VARCHAR", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP"}


@dataclass(frozen=True)
class Source:
    """A declared source: its columns and the rows it holds at the start and after each simulated day."""

    schema: dict[str, str]
    initial: Callable[[], list[tuple]]
    daily: Callable[[int], list[tuple]]


@dataclass(frozen=True)
class Reader:
    """A query people run ``per_day`` times a day, with the bytes and slot time one run measured."""

    sql: str
    per_day: int
    bytes_per_run: float
    slot_ms: float = 5_000.0
    hour: float = 9.0


@dataclass(frozen=True)
class Build:
    """One refresh of a stored model per day: when it runs and what it measured."""

    hour: float
    bytes_per_run: float
    slot_ms: float = 20_000.0


@dataclass
class Case:
    name: str
    description: str
    models: dict[str, tuple[str, str]]  # key -> (kind, BigQuery SQL)
    sources: dict[str, Source]
    sizes: dict[str, TableSize]
    builds: dict[str, Build]
    readers: list[Reader]
    schedules: dict[str, list[str]] | None = None
    sim_days: int = 3
    _pipeline: Pipeline | None = field(default=None, repr=False)

    def pipeline(self) -> Pipeline:
        if self._pipeline is None:
            self._pipeline = Pipeline(
                {key: Model(_target(key), kind, sql) for key, (kind, sql) in self.models.items()},
                sources={key: _target(key) for key in self.sources},
                source_schema={key: dict(src.schema) for key, src in self.sources.items()},
            )
        return self._pipeline

    def jobs(self) -> list[ObservedJob]:
        return [ObservedJob.from_record(r) for r in self.job_records()]

    def job_records(self) -> list[dict]:
        pipeline = self.pipeline()
        records: list[dict] = []
        for day in range(HISTORY_DAYS):
            for key, build in self.builds.items():
                records.append({
                    "job_id": f"build-{key}-{day}", "creation_time": _at(day, build.hour), "destination_table": key,
                    "referenced_tables": sorted(_ancestors(pipeline, key)),
                    "total_bytes_processed": build.bytes_per_run, "total_bytes_billed": build.bytes_per_run,
                    "total_slot_ms": build.slot_ms,
                })
            for number, reader in enumerate(self.readers):
                refs = _read_tables(pipeline, reader.sql)
                for k in range(reader.per_day):
                    records.append({
                        "job_id": f"read-{number}-{day}-{k}", "creation_time": _at(day, reader.hour + k * 0.1),
                        "user_email": f"analyst{k % 4}@example.test", "query": reader.sql, "referenced_tables": refs,
                        "total_bytes_processed": reader.bytes_per_run, "total_bytes_billed": reader.bytes_per_run,
                        "total_slot_ms": reader.slot_ms,
                    })
        return records


def _target(key: str) -> Target:
    database, schema, name = key.split(".")
    return Target(database, schema, name)


def _at(day: int, hour: float) -> str:
    return (START + timedelta(days=day, hours=hour)).isoformat().replace("+00:00", "Z")


def _ancestors(pipeline: Pipeline, key: str) -> set[str]:
    seen: set[str] = set()
    stack = [key]
    while stack:
        for parent in pipeline.upstream.get(stack.pop(), ()):
            if parent not in seen:
                seen.add(parent)
                stack.append(parent)
    return seen


def _read_tables(pipeline: Pipeline, sql: str) -> list[str]:
    """The nodes a reader names, plus everything they read (BigQuery lists both)."""

    import sqlglot
    from sqlglot import exp

    named = {pipeline.resolve(t) for t in sqlglot.parse_one(sql, read="bigquery").find_all(exp.Table)} - {None}
    refs = set(named)
    for node in named:
        refs |= _ancestors(pipeline, node)
    return sorted(refs)


def _sizes(rows: float, table: dict[str, float], per_row: float = 1.0) -> TableSize:
    return TableSize(rows * per_row, None, {name: rows * per_row * width for name, width in table.items()})


# ------------------------------------------------------------------ cases


def _event_rows(start: int, count: int) -> list[tuple]:
    # amounts are multiples of 0.25 so sums are exact in any order
    return [(i, i % 7, 1 + (i % 11) * 0.25, "test" if i % 9 == 0 else "web") for i in range(start, start + count)]


def retail() -> Case:
    raw, base, daily, top, recent, direct, mart = (f"p.{x}" for x in (
        "raw.events", "core.base", "core.daily", "core.top_users", "core.recent", "core.direct", "core.mart"))
    models = {
        base: ("table", f"SELECT id, user_id, amount, kind FROM `{raw}` WHERE kind != 'test'"),
        daily: ("view", f"SELECT user_id, SUM(amount) AS total FROM `{base}` GROUP BY user_id"),
        top: ("view", f"SELECT user_id, total FROM `{daily}` WHERE total > 20"),
        recent: ("view", f"SELECT id, amount, CURRENT_DATE() AS d FROM `{base}`"),
        direct: ("view", f"SELECT user_id, amount FROM `{raw}`"),
        mart: ("table", f"SELECT user_id, total FROM `{daily}` WHERE total > 0"),
    }
    sizes = {
        raw: _sizes(ROWS, {"id": 8, "user_id": 8, "amount": 8, "kind": 6}),
        base: _sizes(ROWS * 0.9, {"id": 8, "user_id": 8, "amount": 8, "kind": 6}),
        daily: TableSize(10_000, None, {"user_id": 80_000.0, "total": 80_000.0}),
        top: TableSize(2_000, None, {"user_id": 16_000.0, "total": 16_000.0}),
        mart: TableSize(9_000, None, {"user_id": 72_000.0, "total": 72_000.0}),
    }
    return Case(
        "retail", "a hot aggregate, a clock view, a view over a source and an unread table",
        models,
        {raw: Source({"id": "INT64", "user_id": "INT64", "amount": "FLOAT64", "kind": "STRING"},
                     lambda: _event_rows(0, 60), lambda day: _event_rows(1000 * day, 12))},
        sizes,
        {base: Build(6.0, 30 * ROWS, 60_000.0), mart: Build(6.2, 16 * ROWS * 0.9)},
        [
            Reader(f"SELECT user_id, total FROM `{daily}` WHERE user_id = 3", 40, 16 * ROWS * 0.9, 9_000.0, 8.0),
            Reader(f"SELECT user_id FROM `{top}`", 2, 16 * ROWS * 0.9, 9_000.0, 12.0),
            Reader(f"SELECT id, d FROM `{recent}`", 5, 8 * ROWS * 0.9, 2_000.0, 9.0),
            Reader(f"SELECT SUM(amount) AS total FROM `{direct}`", 3, 8 * ROWS, 3_000.0, 10.0),
        ],
    )


def _click_rows(start: int, count: int) -> list[tuple]:
    return [(i, i % 5, (i * 37) % 900 - 50) for i in range(start, start + count)]


def sampling() -> Case:
    raw, clean, sample, tagged, tagged_totals, per_session, busy = (f"p.{x}" for x in (
        "raw.clicks", "stage.clean", "stage.sample", "stage.tagged", "stage.tagged_totals", "stage.per_session", "stage.busy"))
    models = {
        clean: ("table", f"SELECT id, session, ms FROM `{raw}` WHERE ms >= 0"),
        sample: ("view", f"SELECT id, session, ms FROM `{clean}` WHERE RAND() < 0.5"),
        tagged: ("view", f"SELECT id, session, GENERATE_UUID() AS tag FROM `{clean}`"),
        tagged_totals: ("view", f"SELECT session, COUNT(tag) AS n FROM `{tagged}` GROUP BY session"),
        per_session: ("view", f"SELECT session, COUNT(*) AS n, SUM(ms) AS ms FROM `{clean}` GROUP BY session"),
        busy: ("view", f"SELECT session, ms FROM `{per_session}` WHERE n > 20"),
    }
    sizes = {
        raw: _sizes(ROWS, {"id": 8, "session": 8, "ms": 8}),
        clean: _sizes(ROWS * 0.95, {"id": 8, "session": 8, "ms": 8}),
        per_session: TableSize(5_000, None, {"session": 40_000.0, "n": 40_000.0, "ms": 40_000.0}),
        busy: TableSize(3_000, None, {"session": 24_000.0, "ms": 24_000.0}),
    }
    return Case(
        "sampling", "random and UUID views refused, directly and through a view; a hot aggregate chain",
        models,
        {raw: Source({"id": "INT64", "session": "INT64", "ms": "INT64"},
                     lambda: _click_rows(0, 80), lambda day: _click_rows(500 * day, 15))},
        sizes,
        {clean: Build(5.0, 24 * ROWS)},
        [
            Reader(f"SELECT COUNT(*) AS n FROM `{sample}`", 25, 19 * ROWS * 0.95, 6_000.0, 8.0),
            Reader(f"SELECT session, tag FROM `{tagged}`", 6, 19 * ROWS * 0.95, 4_000.0, 9.0),
            Reader(f"SELECT session, n FROM `{tagged_totals}`", 6, 19 * ROWS * 0.95, 4_000.0, 10.0),
            Reader(f"SELECT session, n, ms FROM `{per_session}` WHERE session = 2", 30, 16 * ROWS * 0.95, 7_000.0, 11.0),
            Reader(f"SELECT session FROM `{busy}`", 12, 16 * ROWS * 0.95, 7_000.0, 13.0),
        ],
    )


def _order_rows(start: int, count: int) -> list[tuple]:
    return [(i, i % 6, 5 + (i % 8) * 0.5) for i in range(start, start + count)]


def _customer_rows(start: int, count: int) -> list[tuple]:
    return [(i, "gold" if i % 3 == 0 else "basic") for i in range(start, start + count)]


def staleness() -> Case:
    orders, customers, orders_clean, customers_clean, facts, revenue, snapshot, copy, rollup = (f"p.{x}" for x in (
        "raw.orders", "raw.customers", "sales.orders_clean", "sales.customers_clean", "sales.order_facts",
        "sales.revenue_by_customer", "sales.snapshot", "sales.order_copy", "sales.rollup"))
    models = {
        orders_clean: ("table", f"SELECT id, customer_id, total FROM `{orders}` WHERE total > 0"),
        customers_clean: ("table", f"SELECT id, tier FROM `{customers}`"),
        facts: ("view", f"SELECT o.id, o.total, c.tier FROM `{orders_clean}` AS o JOIN `{customers_clean}` AS c ON o.customer_id = c.id"),
        revenue: ("view", f"SELECT customer_id, SUM(total) AS revenue FROM `{orders_clean}` GROUP BY customer_id"),
        snapshot: ("table", f"SELECT id, total, CURRENT_TIMESTAMP() AS loaded_at FROM `{orders_clean}`"),
        copy: ("table", f"SELECT id, customer_id, total FROM `{orders}`"),
        rollup: ("table", f"SELECT customer_id, COUNT(*) AS n FROM `{orders_clean}` GROUP BY customer_id"),
    }
    sizes = {
        orders: _sizes(ROWS, {"id": 8, "customer_id": 8, "total": 8}),
        customers: _sizes(50_000, {"id": 8, "tier": 5}),
        orders_clean: _sizes(ROWS * 0.9, {"id": 8, "customer_id": 8, "total": 8}),
        customers_clean: _sizes(50_000, {"id": 8, "tier": 5}),
        revenue: TableSize(6_000, None, {"customer_id": 48_000.0, "revenue": 48_000.0}),
        facts: _sizes(ROWS * 0.9, {"id": 8, "total": 8, "tier": 5}),
        snapshot: _sizes(ROWS * 0.9, {"id": 8, "total": 8, "loaded_at": 8}),
        copy: _sizes(ROWS, {"id": 8, "customer_id": 8, "total": 8}),
        rollup: TableSize(5_000, None, {"customer_id": 40_000.0, "n": 40_000.0}),
    }
    return Case(
        "staleness", "schedules decide freshness: stale copies, a stamped clock and a same-schedule rollup",
        models,
        {
            orders: Source({"id": "INT64", "customer_id": "INT64", "total": "FLOAT64"},
                           lambda: _order_rows(0, 70), lambda day: _order_rows(1000 * day, 10)),
            customers: Source({"id": "INT64", "tier": "STRING"},
                              lambda: _customer_rows(0, 6), lambda day: _customer_rows(100 * day, 1)),
        },
        sizes,
        {
            orders_clean: Build(5.0, 24 * ROWS), customers_clean: Build(5.1, 6 * ROWS * 0.05),
            snapshot: Build(5.2, 16 * ROWS * 0.9), copy: Build(5.3, 24 * ROWS), rollup: Build(5.4, 16 * ROWS * 0.9),
        },
        [
            Reader(f"SELECT tier, SUM(total) AS total FROM `{facts}` GROUP BY tier", 20, 22 * ROWS * 0.9, 8_000.0, 8.0),
            Reader(f"SELECT revenue FROM `{revenue}` WHERE customer_id = 2", 35, 16 * ROWS * 0.9, 8_000.0, 9.0),
            Reader(f"SELECT COUNT(*) AS n FROM `{snapshot}`", 1, 8 * ROWS * 0.9, 1_000.0, 14.0),
        ],
        schedules={orders_clean: ["nightly"], customers_clean: ["weekly"], snapshot: ["nightly"], copy: ["nightly"], rollup: ["nightly"]},
    )


def cases() -> list[Case]:
    return [retail(), sampling(), staleness()]
