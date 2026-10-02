"""Offline statistics for cardinality estimation.

Everything an estimator needs at planning time is gathered once, ahead of time,
from the data itself (never from query results):

* per table: row count and a uniform row sample (the whole table when small);
* per join-key domain (columns that are joined to each other, found by union-find
  over the join edges): bins over the key values. The most frequent values get
  a bin each; the rest are split into value ranges of equal total frequency;
* per (table, key column) and bin: number of rows and of distinct key values.

Collection needs a DuckDB connection (a dev/benchmark dependency); loading and
using the statistics needs only the standard library.
"""

from __future__ import annotations

import bisect
import gzip
import pickle
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class Domain:
    name: str
    heavy: dict[Any, int]        # value -> bin id
    bounds: list[Any]            # sorted upper bounds of the range bins
    nbins: int

    def bin_of(self, value: Any) -> int:
        if value is None:
            return -1
        hit = self.heavy.get(value)
        if hit is not None:
            return hit
        i = bisect.bisect_left(self.bounds, value)
        return len(self.heavy) + min(i, len(self.bounds) - 1) if self.bounds else 0


@dataclass
class KeyStats:
    domain: str
    count: list[float]           # rows per bin
    distinct: list[float]        # distinct key values per bin
    sample_bins: list[int]       # bin of each stored row (-1 for NULL)
    exact_bins: frozenset[int] = frozenset()  # bins whose rows are all stored


@dataclass
class TableStats:
    name: str
    rows: int
    columns: list[str]
    sample: list[dict[str, Any]]   # pinned rows first, then a uniform sample of the rest
    pinned: int = 0                # rows stored because their key is a frequent value
    weight: float = 1.0            # table rows each uniform-sample row stands for
    keys: dict[str, KeyStats] = field(default_factory=dict)

    @property
    def exact(self) -> bool:
        return self.weight == 1.0

    def row_weight(self, i: int) -> float:
        return 1.0 if i < self.pinned else self.weight


@dataclass
class Statistics:
    tables: dict[str, TableStats]
    domains: dict[str, Domain]
    column_domain: dict[tuple[str, str], str]

    def save(self, path: str) -> None:
        with gzip.open(path, "wb") as fh:
            pickle.dump(self, fh, protocol=pickle.HIGHEST_PROTOCOL)

    @staticmethod
    def load(path: str) -> "Statistics":
        # Local cache written by ``collect_statistics``; never load files from elsewhere.
        with gzip.open(path, "rb") as fh:
            return pickle.load(fh)


def key_domains(pairs: Iterable[tuple[tuple[str, str], tuple[str, str]]]) -> dict[tuple[str, str], str]:
    """Union-find over joined (table, column) pairs; returns column -> domain name."""
    parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(x: tuple[str, str]) -> tuple[str, str]:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    return {col: "{}.{}".format(*find(col)) for col in parent}


def collect_statistics(con: Any, tables: list[str],
                       join_pairs: Iterable[tuple[tuple[str, str], tuple[str, str]]],
                       sample_rows: int = 100_000, heavy_bins: int = 400,
                       range_bins: int = 600, seed: int = 42) -> Statistics:
    """Gather statistics from a DuckDB connection."""
    column_domain = key_domains(join_pairs)
    members: dict[str, list[tuple[str, str]]] = {}
    for col, dom in column_domain.items():
        members.setdefault(dom, []).append(col)

    domains: dict[str, Domain] = {}
    for dom, cols in members.items():
        union = " UNION ALL ".join(
            f'SELECT "{c}" AS v, COUNT(*) AS n FROM "{t}" WHERE "{c}" IS NOT NULL GROUP BY 1'
            for t, c in cols)
        totals = con.execute(f"SELECT v, SUM(n) AS n FROM ({union}) GROUP BY v ORDER BY v").fetchall()
        by_freq = sorted(totals, key=lambda r: -r[1])[:heavy_bins]
        heavy = {v: i for i, (v, _) in enumerate(by_freq)}
        rest = [(v, n) for v, n in totals if v not in heavy]
        total = sum(n for _, n in rest) or 1
        bounds: list[Any] = []
        acc, step = 0.0, total / range_bins
        for v, n in rest:
            acc += n
            if acc >= step * (len(bounds) + 1) and len(bounds) < range_bins - 1:
                bounds.append(v)
        if rest and (not bounds or bounds[-1] != rest[-1][0]):
            bounds.append(rest[-1][0])
        domains[dom] = Domain(dom, heavy, bounds, len(heavy) + max(len(bounds), 1))

    out: dict[str, TableStats] = {}
    for table in tables:
        rows = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        key_cols = [c for (t, c) in column_domain if t == table]
        counts: dict[str, list[tuple[Any, int]]] = {
            c: con.execute(f'SELECT "{c}", COUNT(*) FROM "{table}" WHERE "{c}" IS NOT NULL GROUP BY 1').fetchall()
            for c in key_cols}
        # Rows whose unique key is a frequent join value are stored in full: one
        # such row (say, a very active user) decides a large share of many joins.
        pin_col, pin_values = None, []
        if rows > sample_rows:
            for c in key_cols:
                if all(n == 1 for _, n in counts[c]) and len(counts[c]) == rows:
                    dom = domains[column_domain[(table, c)]]
                    pin_col, pin_values = c, sorted(dom.heavy, key=dom.heavy.get)
                    break
        cols_cur = con.execute(f'SELECT * FROM "{table}" LIMIT 0')
        columns = [d[0].lower() for d in cols_cur.description]
        pinned: list[dict[str, Any]] = []
        rest = f'"{table}"'
        if pin_col is not None and pin_values:
            con.execute("CREATE OR REPLACE TEMP TABLE _kumosql_pin (v %s)" % _sql_type(pin_values[0]))
            con.executemany("INSERT INTO _kumosql_pin VALUES (?)", [[v] for v in pin_values])
            pinned = [dict(zip(columns, r)) for r in con.execute(
                f'SELECT * FROM "{table}" WHERE "{pin_col}" IN (SELECT v FROM _kumosql_pin)').fetchall()]
            rest = f'(SELECT * FROM "{table}" WHERE "{pin_col}" NOT IN (SELECT v FROM _kumosql_pin))'
        remaining = rows - len(pinned)
        if remaining <= sample_rows:
            sample = con.execute(f"SELECT * FROM {rest}").fetchall()
        else:
            sample = con.execute(f"SELECT * FROM {rest} USING SAMPLE reservoir({sample_rows} ROWS) REPEATABLE ({seed})").fetchall()
        stored = pinned + [dict(zip(columns, r)) for r in sample]
        weight = remaining / len(sample) if sample else 1.0
        ts = TableStats(table, rows, columns, stored, len(pinned), weight)
        for c in key_cols:
            d = domains[column_domain[(table, c)]]
            count = [0.0] * d.nbins
            distinct = [0.0] * d.nbins
            for v, n in counts[c]:
                b = d.bin_of(v)
                count[b] += n
                distinct[b] += 1
            exact = frozenset(d.heavy[v] for v in pin_values) if c == pin_col else frozenset()
            ts.keys[c] = KeyStats(d.name, count, distinct, [d.bin_of(r.get(c)) for r in stored], exact)
        out[table] = ts
    return Statistics(out, domains, column_domain)


def _sql_type(value: Any) -> str:
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, int):
        return "BIGINT"
    if isinstance(value, float):
        return "DOUBLE"
    return "VARCHAR"
