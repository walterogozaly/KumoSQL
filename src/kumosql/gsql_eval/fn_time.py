"""DATE, DATETIME, TIME, TIMESTAMP and INTERVAL functions (see functions.py for the registry).

Arithmetic lives in :mod:`datetimes`; this module types the arguments, propagates NULL and reads the sqlglot nodes
(units arrive as ``Var``, ``Literal`` or ``WeekStart``; ``FORMAT_*`` arrive as ``TimeToStr(TsOrDsTo*(x), format)``;
``PARSE_*`` as ``StrToDate``/``ParseTime``/``ParseDatetime``/``StrToTime``; ``INTERVAL n part`` arguments of ``*_ADD``
are flattened into ``expression`` and ``unit``). Forms where BigQuery's signature is unsure (an extended signature
that only some engines have) raise ``Unsupported``; a type that coerces nowhere raises ``AnalysisError``.
"""

from __future__ import annotations

import re
import time as _clock
from datetime import date, datetime, time, timezone

from sqlglot import exp

from . import datetimes as D
from . import types as T
from . import values as V
from .compiler import _only
from .errors import AnalysisError, EvalError, Unsupported
from .functions import REGISTRY, register, register_node
from .runtime import E, const

DATE_PART_NAMES = ("DAY", "WEEK", "MONTH", "QUARTER", "YEAR")
SUB_DAY = ("MICROSECOND", "MILLISECOND", "SECOND", "MINUTE", "HOUR")


# --- helpers ---------------------------------------------------------------------------------------------


def _strict(typ: T.Type, es: list, fn) -> E:
    """``fn(env, *values)`` for non-NULL values; NULL if any argument is NULL (all arguments are evaluated)."""

    fns = [e.fn for e in es]

    def run(env):
        values = [f(env) for f in fns]
        for value in values:
            if value is None:
                return None
        return fn(env, *values)

    return E(typ, run)


def _unit(node, what: str) -> tuple[str, int | None]:
    """A date part node as ``(PART, week start)``: ``WEEK(MONDAY)`` is ``("WEEK", 0)``, a bare WEEK has no start."""

    if node is None:
        raise AnalysisError(f"{what} needs a date part")
    if isinstance(node, exp.WeekStart):
        day = node.this.name.upper() if node.this is not None else ""
        if day not in D.WEEKDAYS:
            raise AnalysisError(f"Invalid week start {day}")
        return "WEEK", D.WEEKDAYS[day]
    if isinstance(node, (exp.Var, exp.Literal, exp.Identifier, exp.Column)):
        return node.name.upper(), None
    raise Unsupported(f"{what} with a date part of {type(node).__name__}")


def _typed(c, value: E, target: T.Type, what: str, extended: tuple = ()) -> E:
    """``value`` as ``target``; ``extended`` kinds are signatures only some engines have (``Unsupported``)."""

    if value.type.kind in extended and value.lit != "null":
        raise Unsupported(f"{what} of a {value.type}")
    return c.coerce(value, target, what)


def _count(c, source, value: E, what: str) -> E:
    """The count of ``INTERVAL n part``: sqlglot reads a literal count as a string literal."""

    if isinstance(source, exp.Literal) and source.is_string:
        text = source.this
        if not re.fullmatch(r"[+-]?[0-9]+", text):
            raise AnalysisError(f"INTERVAL value for {what} must be an INT64")
        number = int(text)
        if number < V.INT64_MIN or number > V.INT64_MAX:
            raise AnalysisError(f"INTERVAL value {text} out of INT64 range")
        return const(T.INT64, number)
    return c.coerce(value, T.INT64, f"INTERVAL value for {what}")


def _literal_format(source, value: E) -> E:
    """A literal format string as the query wrote it (sqlglot rewrites some elements while parsing a literal)."""

    if isinstance(source, exp.Literal) and source.is_string and value.value is not None:
        return const(T.STRING, D.unmap_format(source.this))
    return value


def _zone(env, name):
    return D.zone(name)


def _now_micros(env) -> int:
    ctx = env.ctx
    if ctx.now is None:
        ctx.now = _clock.time_ns() // 1000
    now = ctx.now
    if isinstance(now, datetime):
        if now.tzinfo is not None:
            now = now.astimezone(timezone.utc).replace(tzinfo=None)
        return V.utc_to_micros(now)
    if isinstance(now, int):
        return now
    raise Unsupported("a clock value of this kind")


# --- constructors ------------------------------------------------------------------------------------------


@register_node(exp.DateFromParts)
def _m_date_parts(node):
    _only(node, "year", "month", "day")
    return "DATE", [node.args[k] for k in ("year", "month", "day") if node.args.get(k) is not None]


@register_node(exp.Date)
def _m_date(node):
    _only(node, "this", "zone")
    return "DATE", [node.args[k] for k in ("this", "zone") if node.args.get(k) is not None]


@register("DATE")
def date_fn(c, node, args, cx):
    if len(args) == 3:
        ints = [c.coerce(a, T.INT64, "DATE argument") for a in args]

        def build(env, y, m, d):
            try:
                if not 1 <= y <= 9999:
                    raise ValueError
                return date(y, m, d)
            except (ValueError, OverflowError):
                raise EvalError(f"Invalid DATE value: {y}-{m}-{d}") from None

        return _strict(T.DATE, ints, build)
    if len(args) not in (1, 2):
        raise AnalysisError("No matching signature for function DATE")
    first = args[0]
    kind = first.type.kind
    if first.lit == "null":
        raise Unsupported("DATE(NULL)")
    if kind == "DATETIME":
        if len(args) == 2:
            raise AnalysisError("DATE(DATETIME, time zone) does not exist")
        return _strict(T.DATE, [first], lambda env, v: v.date())
    if kind == "STRING":
        if first.lit != "literal":
            raise Unsupported("DATE of a non-literal STRING")
        first = c.coerce(first, T.TIMESTAMP, "DATE argument")  # a string literal reads as the TIMESTAMP signature
    elif kind == "DATE":
        raise Unsupported("DATE of a DATE")
    elif kind != "TIMESTAMP":
        raise AnalysisError(f"No matching signature for function DATE for argument type {first.type}")
    rest = [c.coerce(a, T.STRING, "time zone") for a in args[1:]]
    return _strict(
        T.DATE, [first] + rest,
        lambda env, v, *z: V.to_civil(v, _zone(env, z[0]) if z else env.ctx.tz).date(),
    )


@register_node(exp.TimestampFromParts)
def _m_datetime_parts(node):
    _only(node, "year", "month", "day", "hour", "min", "sec")
    return "DATETIME", [node.args[k] for k in ("year", "month", "day", "hour", "min", "sec") if node.args.get(k) is not None]


@register_node(exp.TsOrDsToDatetime)
def _m_datetime_one(node):
    _only(node, "this")
    return "DATETIME", [node.this]


@register_node(exp.Datetime)
def _m_datetime_two(node):
    _only(node, "this", "expression")
    return "DATETIME", [node.this] + ([node.expression] if node.args.get("expression") is not None else [])


@register("DATETIME")
def datetime_fn(c, node, args, cx):
    if len(args) == 6:
        ints = [c.coerce(a, T.INT64, "DATETIME argument") for a in args]

        def build(env, y, mo, d, h, mi, s):
            try:
                if not 1 <= y <= 9999:
                    raise ValueError
                return datetime(y, mo, d, h, mi, s)
            except (ValueError, OverflowError):
                raise EvalError(f"Invalid DATETIME value: {y}-{mo}-{d} {h}:{mi}:{s}") from None

        return _strict(T.DATETIME, ints, build)
    if len(args) not in (1, 2):
        raise AnalysisError("No matching signature for function DATETIME")
    first = args[0]
    kind = first.type.kind
    if first.lit == "null":
        raise Unsupported("DATETIME(NULL)")
    if kind == "STRING":
        raise Unsupported("DATETIME of a STRING (the signature BigQuery picks is not documented)")
    if kind == "DATE":
        rest = [c.coerce(a, T.TIME, "DATETIME time argument") for a in args[1:]]
        return _strict(
            T.DATETIME, [first] + rest,
            lambda env, d, *t: datetime.combine(d, t[0]) if t else datetime(d.year, d.month, d.day),
        )
    if kind == "TIMESTAMP":
        rest = [c.coerce(a, T.STRING, "time zone") for a in args[1:]]
        return _strict(
            T.DATETIME, [first] + rest,
            lambda env, v, *z: V.to_civil(v, _zone(env, z[0]) if z else env.ctx.tz),
        )
    if kind == "DATETIME":
        raise Unsupported("DATETIME of a DATETIME")
    raise AnalysisError(f"No matching signature for function DATETIME for argument type {first.type}")


@register_node(exp.TimeFromParts)
def _m_time_parts(node):
    _only(node, "hour", "min", "sec")
    return "TIME", [node.args[k] for k in ("hour", "min", "sec")]


@register_node(exp.TsOrDsToTime)
def _m_time_one(node):
    _only(node, "this")
    return "TIME", [node.this]


@register_node(exp.Time)
def _m_time_two(node):
    _only(node, "this", "zone")
    return "TIME", [node.args[k] for k in ("this", "zone") if node.args.get(k) is not None]


@register("TIME")
def time_fn(c, node, args, cx):
    if len(args) == 3:
        ints = [c.coerce(a, T.INT64, "TIME argument") for a in args]

        def build(env, h, m, s):
            try:
                return time(h, m, s)
            except (ValueError, OverflowError):
                raise EvalError(f"Invalid TIME value: {h}:{m}:{s}") from None

        return _strict(T.TIME, ints, build)
    if len(args) not in (1, 2):
        raise AnalysisError("No matching signature for function TIME")
    first = args[0]
    kind = first.type.kind
    if first.lit == "null":
        raise Unsupported("TIME(NULL)")
    if kind == "STRING":
        raise Unsupported("TIME of a STRING (the signature BigQuery picks is not documented)")
    if kind == "DATETIME":
        if len(args) == 2:
            raise AnalysisError("TIME(DATETIME, time zone) does not exist")
        return _strict(T.TIME, [first], lambda env, v: v.time())
    if kind == "TIMESTAMP":
        rest = [c.coerce(a, T.STRING, "time zone") for a in args[1:]]
        return _strict(
            T.TIME, [first] + rest,
            lambda env, v, *z: V.to_civil(v, _zone(env, z[0]) if z else env.ctx.tz).time(),
        )
    if kind == "TIME":
        raise Unsupported("TIME of a TIME")
    raise AnalysisError(f"No matching signature for function TIME for argument type {first.type}")


@register_node(exp.Timestamp)
def _m_timestamp(node):
    _only(node, "this", "zone", "with_tz")
    return "TIMESTAMP", [node.args[k] for k in ("this", "zone") if node.args.get(k) is not None]


@register("TIMESTAMP")
def timestamp_fn(c, node, args, cx):
    if len(args) not in (1, 2):
        raise AnalysisError("No matching signature for function TIMESTAMP")
    first = args[0]
    kind = first.type.kind
    if first.lit == "null":
        raise Unsupported("TIMESTAMP(NULL)")
    rest = [c.coerce(a, T.STRING, "time zone") for a in args[1:]]
    if kind == "STRING":
        return _strict(
            T.TIMESTAMP, [first] + rest,
            lambda env, s, *z: V.parse_timestamp(s, _zone(env, z[0]) if z else env.ctx.tz),
        )
    if kind == "DATE":
        return _strict(
            T.TIMESTAMP, [first] + rest,
            lambda env, d, *z: V.timestamp(
                V.from_civil(datetime(d.year, d.month, d.day), _zone(env, z[0]) if z else env.ctx.tz)
            ),
        )
    if kind == "DATETIME":
        return _strict(
            T.TIMESTAMP, [first] + rest,
            lambda env, v, *z: V.timestamp(V.from_civil(v, _zone(env, z[0]) if z else env.ctx.tz)),
        )
    if kind == "TIMESTAMP":
        raise Unsupported("TIMESTAMP of a TIMESTAMP")
    raise AnalysisError(f"No matching signature for function TIMESTAMP for argument type {first.type}")


# --- the clock ----------------------------------------------------------------------------------------------


def _current(name: str, typ: T.Type, convert):
    def mapper(node):
        _only(node, "this")
        return name, [node.this] if node.args.get("this") is not None else []

    @register(name)
    def handler(c, node, args, cx):
        if len(args) > 1:
            raise AnalysisError(f"No matching signature for function {name}")
        zone_args = [c.coerce(a, T.STRING, "time zone") for a in args]
        fns = [a.fn for a in zone_args]

        def run(env):
            env.ctx.nondet(name)
            names = [f(env) for f in fns]
            if names and names[0] is None:
                return None
            tz = D.zone(names[0]) if names else env.ctx.tz
            return convert(_now_micros(env), tz)

        return E(typ, run)

    return mapper


_clock_specs = [
    (exp.CurrentDate, "CURRENT_DATE", T.DATE, lambda m, tz: V.to_civil(m, tz).date()),
    (exp.CurrentDatetime, "CURRENT_DATETIME", T.DATETIME, lambda m, tz: V.to_civil(m, tz)),
    (exp.CurrentTime, "CURRENT_TIME", T.TIME, lambda m, tz: V.to_civil(m, tz).time()),
]
for _cls, _name, _typ, _convert in _clock_specs:
    register_node(_cls)(_current(_name, _typ, _convert))


@register_node(exp.CurrentTimestamp)
def _m_current_timestamp(node):
    _only(node, "this", "sysdate")
    if node.args.get("this") is not None:
        raise Unsupported("CURRENT_TIMESTAMP with an argument")
    return "CURRENT_TIMESTAMP", []


@register("CURRENT_TIMESTAMP")
def current_timestamp(c, node, args, cx):
    def run(env):
        env.ctx.nondet("CURRENT_TIMESTAMP")
        return _now_micros(env)

    return E(T.TIMESTAMP, run)


# --- adding and subtracting parts --------------------------------------------------------------------------


def _add_family(add_class, sub_class, name: str, kind: str):
    node_classes = (add_class, sub_class)

    def mapper(node):
        _only(node, "this", "expression", "unit")
        return (name + "_ADD" if isinstance(node, add_class) else name + "_SUB"), [node.this, node.expression]

    register_node(*node_classes)(mapper)

    def make(sign: int, fname: str):
        @register(fname)
        def handler(c, node, args, cx):
            part, start = _unit(node.args.get("unit"), fname)
            if start is not None or part in ("ISOWEEK", "ISOYEAR"):
                raise AnalysisError(f"{fname} does not support the date part {part}")
            count = _count(c, node.args.get("expression"), args[1], fname)
            if kind == "DATE":
                if part not in DATE_PART_NAMES:
                    raise AnalysisError(f"{fname} does not support the date part {part}")
                value = _typed(c, args[0], T.DATE, fname, ("DATETIME", "TIMESTAMP"))
                return _strict(T.DATE, [value, count], lambda env, v, n: D.add_date_part(v, part, sign * n))
            if kind == "DATETIME":
                if part not in DATE_PART_NAMES + SUB_DAY:
                    raise AnalysisError(f"{fname} does not support the date part {part}")
                value = _typed(c, args[0], T.DATETIME, fname, ("TIMESTAMP",))
                if part in SUB_DAY:
                    return _strict(T.DATETIME, [value, count], lambda env, v, n: D.add_micros_part(v, part, sign * n))
                return _strict(T.DATETIME, [value, count], lambda env, v, n: D.add_date_part(v, part, sign * n))
            if kind == "TIME":
                if part not in SUB_DAY:
                    raise AnalysisError(f"{fname} does not support the date part {part}")
                value = _typed(c, args[0], T.TIME, fname)
                unit = D.MICROS_OF[part]
                return _strict(
                    T.TIME, [value, count],
                    lambda env, v, n: D.time_of_micros(D.time_micros(v) + sign * n * unit),
                )
            if part not in SUB_DAY + ("DAY",):
                raise AnalysisError(f"{fname} does not support the date part {part}")
            value = _typed(c, args[0], T.TIMESTAMP, fname, ("DATE", "DATETIME"))
            unit = D.MICROS_OF[part]
            return _strict(T.TIMESTAMP, [value, count], lambda env, v, n: V.timestamp(v + sign * n * unit))

        return handler

    make(1, name + "_ADD")
    make(-1, name + "_SUB")


_add_family(exp.DateAdd, exp.DateSub, "DATE", "DATE")
_add_family(exp.DatetimeAdd, exp.DatetimeSub, "DATETIME", "DATETIME")
_add_family(exp.TimeAdd, exp.TimeSub, "TIME", "TIME")
_add_family(exp.TimestampAdd, exp.TimestampSub, "TIMESTAMP", "TIMESTAMP")


# --- differences ----------------------------------------------------------------------------------------------


def _diff_family(cls, name: str, kind: str):
    @register_node(cls)
    def mapper(node):
        _only(node, "this", "expression", "unit", "date_part_boundary")
        if kind in ("TIME", "TIMESTAMP") and node.args.get("date_part_boundary"):
            raise Unsupported(f"{cls.__name__} with date_part_boundary")
        return name, [node.this, node.expression]

    @register(name)
    def handler(c, node, args, cx):
        part, start = _unit(node.args.get("unit"), name)
        if kind in ("DATE", "DATETIME"):
            valid = D.DATE_PARTS + (SUB_DAY if kind == "DATETIME" else ())
            if part not in valid or (start is not None and part != "WEEK"):
                raise AnalysisError(f"{name} does not support the date part {part}")
            target = T.DATE if kind == "DATE" else T.DATETIME
            extended = ("DATETIME", "TIMESTAMP") if kind == "DATE" else ("TIMESTAMP",)
            a = _typed(c, args[0], target, name, extended)
            b = _typed(c, args[1], target, name, extended)
            if kind == "DATE":
                return _strict(T.INT64, [a, b], lambda env, x, y: D.date_diff(x, y, part, start))
            return _strict(T.INT64, [a, b], lambda env, x, y: D.datetime_diff(x, y, part, start))
        if start is not None:
            raise AnalysisError(f"{name} does not support the date part WEEK({start})")
        if kind == "TIME":
            if part not in SUB_DAY:
                raise AnalysisError(f"{name} does not support the date part {part}")
            a, b = _typed(c, args[0], T.TIME, name), _typed(c, args[1], T.TIME, name)
            return _strict(T.INT64, [a, b], lambda env, x, y: D.time_diff(x, y, part))
        if part not in SUB_DAY + ("DAY",):
            raise AnalysisError(f"{name} does not support the date part {part}")
        a = _typed(c, args[0], T.TIMESTAMP, name, ("DATE", "DATETIME"))
        b = _typed(c, args[1], T.TIMESTAMP, name, ("DATE", "DATETIME"))
        return _strict(T.INT64, [a, b], lambda env, x, y: D.timestamp_diff(x, y, part))


_diff_family(exp.DateDiff, "DATE_DIFF", "DATE")
_diff_family(exp.DatetimeDiff, "DATETIME_DIFF", "DATETIME")
_diff_family(exp.TimeDiff, "TIME_DIFF", "TIME")
_diff_family(exp.TimestampDiff, "TIMESTAMP_DIFF", "TIMESTAMP")


# --- truncation ---------------------------------------------------------------------------------------------------


@register_node(exp.DateTrunc)
def _m_date_trunc(node):
    _only(node, "this", "unit")
    return "DATE_TRUNC", [node.this]


@register_node(exp.DatetimeTrunc)
def _m_datetime_trunc(node):
    _only(node, "this", "unit")
    return "DATETIME_TRUNC", [node.this]


@register_node(exp.TimeTrunc)
def _m_time_trunc(node):
    _only(node, "this", "unit")
    return "TIME_TRUNC", [node.this]


@register_node(exp.TimestampTrunc)
def _m_timestamp_trunc(node):
    _only(node, "this", "unit", "zone")
    return "TIMESTAMP_TRUNC", [node.this] + ([node.args["zone"]] if node.args.get("zone") is not None else [])


def _trunc_part(node, name: str, allowed: tuple) -> tuple[str, int | None]:
    part, start = _unit(node.args.get("unit"), name)
    if part not in allowed or (start is not None and part != "WEEK"):
        raise AnalysisError(f"{name} does not support the date part {part}")
    return part, start


@register("DATE_TRUNC")
def date_trunc(c, node, args, cx):
    part, start = _trunc_part(node, "DATE_TRUNC", D.DATE_PARTS)
    value = _typed(c, args[0], T.DATE, "DATE_TRUNC", ("DATETIME", "TIMESTAMP"))
    return _strict(T.DATE, [value], lambda env, v: D.trunc_date(v, part, start))


@register("DATETIME_TRUNC")
def datetime_trunc(c, node, args, cx):
    part, start = _trunc_part(node, "DATETIME_TRUNC", D.DATE_PARTS + SUB_DAY)
    value = _typed(c, args[0], T.DATETIME, "DATETIME_TRUNC", ("TIMESTAMP",))
    return _strict(T.DATETIME, [value], lambda env, v: D.trunc_datetime(v, part, start))


@register("TIME_TRUNC")
def time_trunc(c, node, args, cx):
    part, _ = _trunc_part(node, "TIME_TRUNC", SUB_DAY)
    value = _typed(c, args[0], T.TIME, "TIME_TRUNC")
    return _strict(T.TIME, [value], lambda env, v: D.trunc_time(v, part))


@register("TIMESTAMP_TRUNC")
def timestamp_trunc(c, node, args, cx):
    part, start = _trunc_part(node, "TIMESTAMP_TRUNC", D.DATE_PARTS + SUB_DAY)
    value = _typed(c, args[0], T.TIMESTAMP, "TIMESTAMP_TRUNC", ("DATE", "DATETIME"))
    rest = [c.coerce(a, T.STRING, "time zone") for a in args[1:]]
    return _strict(
        T.TIMESTAMP, [value] + rest,
        lambda env, v, *z: D.trunc_timestamp(v, part, start, _zone(env, z[0]) if z else env.ctx.tz),
    )


@register_node(exp.LastDay)
def _m_last_day(node):
    _only(node, "this", "unit")
    return "LAST_DAY", [node.this]


@register("LAST_DAY")
def last_day(c, node, args, cx):
    if node.args.get("unit") is None:
        part, start = "MONTH", None
    else:
        part, start = _unit(node.args["unit"], "LAST_DAY")
    if part not in ("YEAR", "QUARTER", "MONTH", "WEEK", "ISOWEEK", "ISOYEAR") or (start is not None and part != "WEEK"):
        raise AnalysisError(f"LAST_DAY does not support the date part {part}")
    first = args[0]
    if first.type.kind == "DATETIME" and first.lit != "null":
        return _strict(T.DATE, [first], lambda env, v: D.last_day(v.date(), part, start))
    value = _typed(c, first, T.DATE, "LAST_DAY", ("TIMESTAMP",))
    return _strict(T.DATE, [value], lambda env, v: D.last_day(v, part, start))


# --- EXTRACT -----------------------------------------------------------------------------------------------------


@register_node(exp.Extract)
def _m_extract(node):
    _only(node, "this", "expression")
    inner = node.expression
    if isinstance(inner, exp.AtTimeZone):
        _only(inner, "this", "zone")
        return "EXTRACT", [inner.this, inner.args["zone"]]
    return "EXTRACT", [inner]


_DATE_EXTRACT = ("YEAR", "ISOYEAR", "QUARTER", "MONTH", "WEEK", "ISOWEEK", "DAY", "DAYOFWEEK", "DAYOFYEAR")
_TIME_EXTRACT = ("HOUR", "MINUTE", "SECOND", "MILLISECOND", "MICROSECOND")
_INTERVAL_EXTRACT = ("YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND", "MILLISECOND", "MICROSECOND")


@register("EXTRACT")
def extract(c, node, args, cx):
    part, start = _unit(node.this, "EXTRACT")
    if start is not None and part != "WEEK":
        raise AnalysisError(f"EXTRACT does not support the date part {part}")
    value = args[0]
    if value.lit == "null":
        raise Unsupported("EXTRACT from NULL")
    kind = value.type.kind
    zone_args = args[1:]
    if kind == "STRING":
        raise Unsupported("EXTRACT from a STRING (BigQuery's choice of type is not documented)")
    if zone_args and kind != "TIMESTAMP":
        raise AnalysisError("AT TIME ZONE is only valid on a TIMESTAMP")
    if kind == "INTERVAL":
        if part not in _INTERVAL_EXTRACT and part != "NANOSECOND":
            raise AnalysisError(f"EXTRACT does not support {part} from an INTERVAL")
        D.extract_interval(V.Interval(), part)  # NANOSECOND is Unsupported
        return _strict(T.INT64, [value], lambda env, v: D.extract_interval(v, part))
    if kind == "TIME":
        if part not in _TIME_EXTRACT:
            raise AnalysisError(f"EXTRACT does not support {part} from a TIME")
        return _strict(T.INT64, [value], lambda env, v: D.extract_time_part(v, part))
    if kind == "DATE":
        if part not in _DATE_EXTRACT:
            raise AnalysisError(f"EXTRACT does not support {part} from a DATE")
        return _strict(T.INT64, [value], lambda env, v: D.extract_date_part(v, part, start))
    if kind not in ("DATETIME", "TIMESTAMP"):
        raise AnalysisError(f"EXTRACT from {value.type} is not supported")
    if part in ("DATE", "TIME") or part == "DATETIME" and kind == "TIMESTAMP":
        result = {"DATE": T.DATE, "TIME": T.TIME, "DATETIME": T.DATETIME}[part]
        pick = {"DATE": lambda d: d.date(), "TIME": lambda d: d.time(), "DATETIME": lambda d: d}[part]
    elif part in _DATE_EXTRACT:
        result = T.INT64
        pick = lambda d: D.extract_date_part(d.date(), part, start)  # noqa: E731
    elif part in _TIME_EXTRACT:
        result = T.INT64
        pick = lambda d: D.extract_time_part(d.time(), part)  # noqa: E731
    else:
        raise AnalysisError(f"EXTRACT does not support {part} from a {value.type}")
    if kind == "DATETIME":
        return _strict(result, [value], lambda env, v: pick(v))
    zones = [c.coerce(z, T.STRING, "time zone") for z in zone_args]
    return _strict(
        result, [value] + zones,
        lambda env, v, *z: pick(V.to_civil(v, _zone(env, z[0]) if z else env.ctx.tz)),
    )


# --- FORMAT_* and PARSE_* ----------------------------------------------------------------------------------------------

_FORMAT_WRAPPERS = {
    "TsOrDsToDate": ("FORMAT_DATE", T.DATE),
    "TsOrDsToTime": ("FORMAT_TIME", T.TIME),
    "TsOrDsToDatetime": ("FORMAT_DATETIME", T.DATETIME),
    "TsOrDsToTimestamp": ("FORMAT_TIMESTAMP", T.TIMESTAMP),
}


@register_node(exp.TimeToStr)
def _m_format(node):
    _only(node, "this", "format", "zone")
    inner = node.this
    spec = _FORMAT_WRAPPERS.get(type(inner).__name__)
    if spec is None:
        raise Unsupported(f"TimeToStr of {type(inner).__name__}")
    _only(inner, "this")
    name = spec[0]
    zone = node.args.get("zone")
    if zone is not None and name != "FORMAT_TIMESTAMP":
        raise Unsupported(f"{name} with a time zone")
    return name, [node.args["format"], inner.this] + ([zone] if zone is not None else [])


def _format_handler(name: str, target: T.Type):
    @register(name)
    def handler(c, node, args, cx):
        fmt = _literal_format(node.args.get("format"), c.coerce(args[0], T.STRING, f"{name} format"))
        extended = {"DATE": ("DATETIME", "TIMESTAMP"), "DATETIME": ("TIMESTAMP",), "TIME": ("DATETIME", "TIMESTAMP"),
                    "TIMESTAMP": ("DATE", "DATETIME")}[target.kind]
        value = _typed(c, args[1], target, name, extended)
        kind = target.kind
        if kind == "TIMESTAMP":
            zones = [c.coerce(z, T.STRING, "time zone") for z in args[2:]]

            def run(env, f, v, *z):
                tz = _zone(env, z[0]) if z else env.ctx.tz
                local = V.to_civil(v, tz)
                return D.format_elements(f, "TIMESTAMP", local.date(), local.time(), (v, tz))

            return _strict(T.STRING, [fmt, value] + zones, run)
        if kind == "DATE":
            return _strict(T.STRING, [fmt, value], lambda env, f, v: D.format_elements(f, "DATE", v, None))
        if kind == "TIME":
            return _strict(T.STRING, [fmt, value], lambda env, f, v: D.format_elements(f, "TIME", None, v))
        return _strict(
            T.STRING, [fmt, value], lambda env, f, v: D.format_elements(f, "DATETIME", v.date(), v.time())
        )

    return handler


for _name, _target in (("FORMAT_DATE", T.DATE), ("FORMAT_TIME", T.TIME), ("FORMAT_DATETIME", T.DATETIME),
                       ("FORMAT_TIMESTAMP", T.TIMESTAMP)):
    _format_handler(_name, _target)


def _default_year(node) -> None:
    year = node.args.get("default_year")
    if year is not None and not (isinstance(year, exp.Literal) and not year.is_string and year.this == "1970"):
        raise Unsupported("PARSE with a default year other than 1970")


@register_node(exp.StrToDate)
def _m_parse_date(node):
    _only(node, "this", "format", "default_year")
    _default_year(node)
    if node.args.get("format") is None:
        raise Unsupported("StrToDate without a format")
    return "PARSE_DATE", [node.args["format"], node.this]


@register_node(exp.ParseTime)
def _m_parse_time(node):
    _only(node, "this", "format")
    return "PARSE_TIME", [node.args["format"], node.this]


@register_node(exp.ParseDatetime)
def _m_parse_datetime(node):
    _only(node, "this", "format", "default_year")
    _default_year(node)
    if node.args.get("format") is None:
        raise Unsupported("ParseDatetime without a format")
    return "PARSE_DATETIME", [node.args["format"], node.this]


@register_node(exp.StrToTime)
def _m_parse_timestamp(node):
    _only(node, "this", "format", "zone", "default_year")
    _default_year(node)
    return "PARSE_TIMESTAMP", [node.args["format"], node.this] + ([node.args["zone"]] if node.args.get("zone") is not None else [])


def _parse_handler(name: str, target: T.Type, parse):
    @register(name)
    def handler(c, node, args, cx):
        es = [c.coerce(a, T.STRING, f"{name} argument") for a in args]
        es[0] = _literal_format(node.args.get("format"), es[0])
        if target == T.TIMESTAMP:
            return _strict(
                T.TIMESTAMP, es,
                lambda env, f, s, *z: D.parse_timestamp(f, s, _zone(env, z[0]) if z else env.ctx.tz),
            )
        return _strict(target, es, lambda env, f, s: parse(f, s))

    return handler


_parse_handler("PARSE_DATE", T.DATE, D.parse_date)
_parse_handler("PARSE_TIME", T.TIME, D.parse_time)
_parse_handler("PARSE_DATETIME", T.DATETIME, D.parse_datetime)
_parse_handler("PARSE_TIMESTAMP", T.TIMESTAMP, None)


# --- unix times -----------------------------------------------------------------------------------------------------


@register_node(exp.UnixToTime)
def _m_unix_to_time(node):
    _only(node, "this", "scale")
    scale = node.args.get("scale")
    if scale is None:
        name = "TIMESTAMP_SECONDS"
    elif isinstance(scale, exp.Literal) and scale.this == "3":
        name = "TIMESTAMP_MILLIS"
    elif isinstance(scale, exp.Literal) and scale.this == "6":
        name = "TIMESTAMP_MICROS"
    else:
        raise Unsupported("UnixToTime with this scale")
    return name, [node.this]


def _from_unix(name: str, factor: int):
    @register(name)
    def handler(c, node, args, cx):
        value = c.coerce(args[0], T.INT64, name)
        return _strict(T.TIMESTAMP, [value], lambda env, v: V.timestamp(v * factor))

    return handler


_from_unix("TIMESTAMP_SECONDS", 1_000_000)
_from_unix("TIMESTAMP_MILLIS", 1000)
_from_unix("TIMESTAMP_MICROS", 1)


def _to_unix(cls, name: str, divisor: int):
    @register_node(cls)
    def mapper(node):
        _only(node, "this")
        return name, [node.this]

    @register(name)
    def handler(c, node, args, cx):
        value = _typed(c, args[0], T.TIMESTAMP, name, ("DATE", "DATETIME"))
        return _strict(T.INT64, [value], lambda env, v: v // divisor)

    return handler


_to_unix(exp.UnixSeconds, "UNIX_SECONDS", 1_000_000)
_to_unix(exp.UnixMillis, "UNIX_MILLIS", 1000)
_to_unix(exp.UnixMicros, "UNIX_MICROS", 1)


@register_node(exp.UnixDate)
def _m_unix_date(node):
    _only(node, "this")
    return "UNIX_DATE", [node.this]


@register("UNIX_DATE")
def unix_date(c, node, args, cx):
    value = _typed(c, args[0], T.DATE, "UNIX_DATE", ("DATETIME", "TIMESTAMP"))
    return _strict(T.INT64, [value], lambda env, v: D.unix_date(v))


@register_node(exp.DateFromUnixDate)
def _m_date_from_unix_date(node):
    _only(node, "this")
    return "DATE_FROM_UNIX_DATE", [node.this]


@register("DATE_FROM_UNIX_DATE")
def date_from_unix_date(c, node, args, cx):
    value = c.coerce(args[0], T.INT64, "DATE_FROM_UNIX_DATE")
    return _strict(T.DATE, [value], lambda env, v: D.date_from_unix_date(v))


# --- STRING(timestamp) and AT TIME ZONE -----------------------------------------------------------------------------------

_previous_string = REGISTRY.get("STRING")


@register_node(exp.String)
def _m_string(node):
    _only(node, "this", "zone")
    return "STRING", [node.this] + ([node.args["zone"]] if node.args.get("zone") is not None else [])


@register("STRING")
def string_fn(c, node, args, cx):
    first = args[0]
    if first.type.kind != "TIMESTAMP" or first.lit == "null":
        if _previous_string is not None and len(args) == 1:
            return _previous_string(c, node, args, cx)
        raise Unsupported(f"STRING of a {first.type}")
    zones = [c.coerce(z, T.STRING, "time zone") for z in args[1:]]
    return _strict(
        T.STRING, [first] + zones,
        lambda env, v, *z: V.format_timestamp(v, _zone(env, z[0]) if z else env.ctx.tz),
    )


# --- sequences --------------------------------------------------------------------------------------------------------------


def _step_parts(node, name: str):
    step = node.args.get("step")
    if step is None:
        return None
    if not isinstance(step, exp.Interval) or isinstance(step.args.get("unit"), getattr(exp, "IntervalSpan", ())):
        raise Unsupported(f"{name} with a step that is not INTERVAL <int> <part>")
    _only(step, "this", "unit")
    return step


@register_node(exp.GenerateDateArray)
def _m_generate_dates(node):
    _only(node, "start", "end", "step")
    step = _step_parts(node, "GENERATE_DATE_ARRAY")
    return "GENERATE_DATE_ARRAY", [node.args["start"], node.args["end"]] + ([step.this] if step is not None else [])


@register_node(exp.GenerateTimestampArray)
def _m_generate_timestamps(node):
    _only(node, "start", "end", "step")
    step = _step_parts(node, "GENERATE_TIMESTAMP_ARRAY")
    return "GENERATE_TIMESTAMP_ARRAY", [node.args["start"], node.args["end"], step.this]


@register("GENERATE_DATE_ARRAY")
def generate_date_array(c, node, args, cx):
    step_node = node.args.get("step")
    if step_node is None:
        part, count = "DAY", const(T.INT64, 1)
    else:
        part, start = _unit(step_node.args.get("unit"), "GENERATE_DATE_ARRAY")
        if start is not None or part not in DATE_PART_NAMES:
            raise AnalysisError(f"GENERATE_DATE_ARRAY does not support the date part {part}")
        count = _count(c, step_node.this, args[2], "GENERATE_DATE_ARRAY")
    a = _typed(c, args[0], T.DATE, "GENERATE_DATE_ARRAY", ("DATETIME", "TIMESTAMP"))
    b = _typed(c, args[1], T.DATE, "GENERATE_DATE_ARRAY", ("DATETIME", "TIMESTAMP"))
    return _strict(T.array(T.DATE), [a, b, count], lambda env, x, y, n: D.generate_dates(x, y, n, part))


@register("GENERATE_TIMESTAMP_ARRAY")
def generate_timestamp_array(c, node, args, cx):
    step_node = node.args["step"]
    part, start = _unit(step_node.args.get("unit"), "GENERATE_TIMESTAMP_ARRAY")
    if part == "NANOSECOND":
        raise Unsupported("GENERATE_TIMESTAMP_ARRAY in NANOSECOND")
    if start is not None or part not in SUB_DAY + ("DAY",):
        raise AnalysisError(f"GENERATE_TIMESTAMP_ARRAY does not support the date part {part}")
    count = _count(c, step_node.this, args[2], "GENERATE_TIMESTAMP_ARRAY")
    a = _typed(c, args[0], T.TIMESTAMP, "GENERATE_TIMESTAMP_ARRAY", ("DATE", "DATETIME"))
    b = _typed(c, args[1], T.TIMESTAMP, "GENERATE_TIMESTAMP_ARRAY", ("DATE", "DATETIME"))
    return _strict(T.array(T.TIMESTAMP), [a, b, count], lambda env, x, y, n: D.generate_timestamps(x, y, n, part))


# --- INTERVAL functions -------------------------------------------------------------------------------------------------------


def _justify(cls, name: str, fn):
    @register_node(cls)
    def mapper(node):
        _only(node, "this")
        return name, [node.this]

    @register(name)
    def handler(c, node, args, cx):
        value = args[0]
        if value.type.kind != "INTERVAL" and value.lit != "null":
            raise AnalysisError(f"No matching signature for function {name} for argument type {value.type}")
        value = c.coerce(value, T.INTERVAL, name)
        return _strict(T.INTERVAL, [value], lambda env, v: fn(v))

    return handler


_justify(exp.JustifyDays, "JUSTIFY_DAYS", D.justify_days)
_justify(exp.JustifyHours, "JUSTIFY_HOURS", D.justify_hours)
_justify(exp.JustifyInterval, "JUSTIFY_INTERVAL", D.justify_interval)

_MAKE_SLOTS = ("year", "month", "day", "hour", "minute", "second")


@register_node(exp.MakeInterval)
def _m_make_interval(node):
    _only(node, *_MAKE_SLOTS)
    nodes = []
    for slot in _MAKE_SLOTS:
        value = node.args.get(slot)
        if value is None:
            continue
        if isinstance(value, exp.Kwarg):
            if value.this.name.lower() != slot:
                raise Unsupported("MAKE_INTERVAL named argument in another position")
            value = value.expression
        nodes.append(value)
    return "MAKE_INTERVAL", nodes


@register("MAKE_INTERVAL")
def make_interval(c, node, args, cx):
    slots = [s for s in _MAKE_SLOTS if node.args.get(s) is not None]
    ints = [c.coerce(a, T.INT64, f"MAKE_INTERVAL {s}") for a, s in zip(args, slots)]
    if not ints:
        return const(T.INTERVAL, V.Interval())
    micros_per = {"hour": D.MICROS_PER_HOUR, "minute": D.MICROS_PER_MINUTE, "second": D.MICROS_PER_SECOND}

    def build(env, *values):
        months = days = micros = 0
        for slot, number in zip(slots, values):
            if slot == "year":
                months += 12 * number
            elif slot == "month":
                months += number
            elif slot == "day":
                days += number
            else:
                micros += number * micros_per[slot]
        return D.check_interval(V.Interval(months, days, micros))

    return _strict(T.INTERVAL, ints, build)
