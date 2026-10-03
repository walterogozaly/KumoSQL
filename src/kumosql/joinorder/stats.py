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
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Iterable


@dataclass
class Domain:
    name: str
    heavy: dict[Any, int]        # value -> bin id for the values with a bin of their own
    nbins: int
    binning: str = "degree"


class _Binner:
    """Value -> bin during collection only; estimates use the stored rows' bins."""

    def __init__(self, domain: Domain, mapping: dict[Any, int] | None, bounds: list[Any]):
        self.domain = domain
        self.mapping = mapping
        self.bounds = bounds

    def __call__(self, value: Any) -> int:
        if value is None:
            return -1
        hit = self.domain.heavy.get(value)
        if hit is not None:
            return hit
        if self.mapping is not None:
            return self.mapping.get(value, self.domain.nbins - 1)
        i = bisect.bisect_left(self.bounds, value)
        return len(self.domain.heavy) + min(i, len(self.bounds) - 1) if self.bounds else len(self.domain.heavy)


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
        data = {"version": 1,
                "tables": {name: {**asdict(table), "sample": [
                    {k: _encode_scalar(v) for k, v in row.items()} for row in table.sample],
                    "keys": {k: {**asdict(v), "exact_bins": sorted(v.exact_bins)} for k, v in table.keys.items()}}
                           for name, table in self.tables.items()},
                "domains": {name: {"name": domain.name, "nbins": domain.nbins, "binning": domain.binning,
                                   "heavy": [[_encode_scalar(k), v] for k, v in domain.heavy.items()]}
                            for name, domain in self.domains.items()},
                "column_domain": [[t, c, d] for (t, c), d in self.column_domain.items()]}
        # Validate on write too: a saved file must always be readable by this version.
        _decode_statistics(data)
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(data, fh, allow_nan=False)

    @staticmethod
    def load(path: str) -> "Statistics":
        """Read validated gzip JSON; legacy executable pickle files must be regenerated."""

        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                data = json.load(fh)
            return _decode_statistics(data)
        except (OSError, EOFError, UnicodeError, ValueError, TypeError, KeyError, OverflowError) as exc:
            raise ValueError("invalid statistics cache; regenerate statistics") from exc


def _encode_scalar(value: Any) -> Any:
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, (datetime, date, time)):
        return {"type": type(value).__name__, "value": value.isoformat()}
    if isinstance(value, Decimal) and value.is_finite():
        return {"type": "decimal", "value": str(value)}
    if isinstance(value, bytes):
        return {"type": "bytes", "value": value.hex()}
    if isinstance(value, timedelta):
        return {"type": "timedelta", "value": [value.days, value.seconds, value.microseconds]}
    raise ValueError("unsupported statistics value")


def _decode_scalar(value: Any) -> Any:
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    obj = _fields(value, {"type", "value"})
    kind, payload = obj["type"], obj["value"]
    if kind == "timedelta" and isinstance(payload, list) and len(payload) == 3 and all(type(v) is int for v in payload):
        return timedelta(days=payload[0], seconds=payload[1], microseconds=payload[2])
    if not isinstance(payload, str):
        raise ValueError("invalid statistics scalar")
    decoders = {"date": date.fromisoformat, "datetime": datetime.fromisoformat,
                "time": time.fromisoformat, "decimal": Decimal, "bytes": bytes.fromhex}
    if kind not in decoders:
        raise ValueError("unknown statistics scalar type")
    result = decoders[kind](payload)
    if isinstance(result, Decimal) and not result.is_finite():
        raise ValueError("non-finite statistics value")
    return result


def _fields(value: Any, keys: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid statistics fields")
    return value


def _mapping(value: Any) -> dict:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise ValueError("invalid statistics mapping")
    return value


def _integer(value: Any, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError("invalid statistics integer")
    return value


def _number(value: Any, minimum: float = 0) -> float:
    if type(value) not in (float, int) or not math.isfinite(value) or value < minimum:
        raise ValueError("invalid statistics number")
    return value


def _list(value: Any) -> list:
    if not isinstance(value, list):
        raise ValueError("invalid statistics list")
    return value


def _decode_statistics(value: Any) -> Statistics:
    data = _fields(value, {"version", "tables", "domains", "column_domain"})
    if type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("unsupported statistics version")
    domains = {}
    for name, raw in _mapping(data["domains"]).items():
        obj = _fields(raw, {"name", "heavy", "nbins", "binning"})
        nbins = _integer(obj["nbins"], 1)
        if obj["name"] != name or obj["binning"] not in ("degree", "range"):
            raise ValueError("invalid statistics domain")
        heavy = {}
        for pair in _list(obj["heavy"]):
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError("invalid statistics heavy value")
            scalar, b = _decode_scalar(pair[0]), _integer(pair[1])
            if scalar is None or b >= nbins or scalar in heavy:
                raise ValueError("invalid statistics heavy bin")
            heavy[scalar] = b
        domains[name] = Domain(name, heavy, nbins, obj["binning"])
    tables = {}
    for name, raw in _mapping(data["tables"]).items():
        obj = _fields(raw, {"name", "rows", "columns", "sample", "pinned", "weight", "keys"})
        columns = _list(obj["columns"])
        if obj["name"] != name or not all(isinstance(c, str) for c in columns) or len(set(columns)) != len(columns):
            raise ValueError("invalid statistics columns")
        sample = []
        for row in _list(obj["sample"]):
            if set(_mapping(row)) != set(columns):
                raise ValueError("invalid statistics sample columns")
            sample.append({k: _decode_scalar(v) for k, v in row.items()})
        rows, pinned = _integer(obj["rows"]), _integer(obj["pinned"])
        weight = _number(obj["weight"], 1)
        if pinned > len(sample) or len(sample) > rows:
            raise ValueError("invalid statistics sample size")
        keys = {}
        for column, raw_key in _mapping(obj["keys"]).items():
            key = _fields(raw_key, {"domain", "count", "distinct", "sample_bins", "exact_bins"})
            if column not in columns or not isinstance(key["domain"], str) or key["domain"] not in domains:
                raise ValueError("invalid statistics key domain")
            nbins = domains[key["domain"]].nbins
            count = [_number(v) for v in _list(key["count"])]
            distinct = [_number(v) for v in _list(key["distinct"])]
            bins = [_integer(v, -1) for v in _list(key["sample_bins"])]
            exact = [_integer(v) for v in _list(key["exact_bins"])]
            if len(count) != nbins or len(distinct) != nbins or len(bins) != len(sample) or any(b >= nbins for b in bins + exact):
                raise ValueError("invalid statistics bin shape")
            keys[column] = KeyStats(key["domain"], count, distinct, bins, frozenset(exact))
        tables[name] = TableStats(name, rows, list(columns), sample, pinned, weight, keys)
    column_domain = {}
    for entry in _list(data["column_domain"]):
        if not isinstance(entry, list) or len(entry) != 3 or not all(isinstance(v, str) for v in entry):
            raise ValueError("invalid statistics column domain")
        t, c, d = entry
        if t not in tables or c not in tables[t].keys or d != tables[t].keys[c].domain or (t, c) in column_domain:
            raise ValueError("invalid statistics column domain reference")
        column_domain[(t, c)] = d
    if set(column_domain) != {(t, c) for t, table in tables.items() for c in table.keys}:
        raise ValueError("missing statistics column domain")
    return Statistics(tables, domains, column_domain)


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
                       sample_rows: int = 100_000, heavy_bins: int = 2000,
                       range_bins: int = 1000, seed: int = 42, binning: str = "range") -> Statistics:
    """Gather statistics from a DuckDB connection.

    ``binning="degree"`` groups the less frequent key values by how often they
    occur (so a bin holds keys of similar popularity); ``"range"`` cuts them into
    value ranges of equal total frequency.
    """
    column_domain = key_domains(join_pairs)
    members: dict[str, list[tuple[str, str]]] = {}
    for col, dom in column_domain.items():
        members.setdefault(dom, []).append(col)

    domains: dict[str, Domain] = {}
    binners: dict[str, _Binner] = {}
    for dom, cols in members.items():
        union = " UNION ALL ".join(
            f'SELECT "{c}" AS v, COUNT(*) AS n FROM "{t}" WHERE "{c}" IS NOT NULL GROUP BY 1'
            for t, c in cols)
        totals = con.execute(f"SELECT v, SUM(n) AS n FROM ({union}) GROUP BY v ORDER BY v").fetchall()
        by_freq = sorted(totals, key=lambda r: -r[1])[:heavy_bins]
        heavy = {v: i for i, (v, _) in enumerate(by_freq)}
        rest = [(v, n) for v, n in totals if v not in heavy]
        total = sum(n for _, n in rest) or 1
        step = total / range_bins
        bounds: list[Any] = []
        mapping: dict[Any, int] | None = None
        if binning == "degree":
            mapping = {}
            acc, b = 0.0, 0
            for v, n in sorted(rest, key=lambda r: (r[1], r[0])):
                mapping[v] = len(heavy) + b
                acc += n
                if acc >= step * (b + 1) and b < range_bins - 1:
                    b += 1
            nbins = len(heavy) + b + 1
        else:
            acc = 0.0
            for v, n in rest:
                acc += n
                if acc >= step * (len(bounds) + 1) and len(bounds) < range_bins - 1:
                    bounds.append(v)
            if rest and (not bounds or bounds[-1] != rest[-1][0]):
                bounds.append(rest[-1][0])
            nbins = len(heavy) + max(len(bounds), 1)
        domains[dom] = Domain(dom, heavy, nbins, binning)
        binners[dom] = _Binner(domains[dom], mapping, bounds)

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
            bin_of = binners[d.name]
            count = [0.0] * d.nbins
            distinct = [0.0] * d.nbins
            for v, n in counts[c]:
                b = bin_of(v)
                count[b] += n
                distinct[b] += 1
            exact = frozenset(d.heavy[v] for v in pin_values) if c == pin_col else frozenset()
            ts.keys[c] = KeyStats(d.name, count, distinct, [bin_of(r.get(c)) for r in stored], exact)
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
