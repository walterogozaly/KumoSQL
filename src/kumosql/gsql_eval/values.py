"""Value semantics: arithmetic limits, comparison, grouping keys, text forms and casts, all by static type.

Payloads by type: INT64 ``int``; NUMERIC and BIGNUMERIC ``Decimal`` (already rounded to 9 and 38 digits);
FLOAT64 ``float``; BOOL ``bool``; STRING ``str``; BYTES ``bytes``; DATE ``datetime.date``; DATETIME a
naive ``datetime.datetime``; TIME ``datetime.time``; TIMESTAMP ``int`` microseconds since
1970-01-01 00:00:00 UTC; INTERVAL :class:`Interval`; ARRAY ``tuple`` (:class:`UnorderedArray` when its
order is not determined); STRUCT ``tuple``. ``None`` is NULL for every type.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Context, Decimal, InvalidOperation, localcontext
from functools import lru_cache
from typing import Any, Callable

from . import types as T
from .errors import AnalysisError, EvalError, Unsupported

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

DEC = Context(prec=120, rounding=ROUND_HALF_UP, Emin=-999999, Emax=999999)
NUMERIC_SCALE = Decimal("1e-9")
BIGNUMERIC_SCALE = Decimal("1e-38")
NUMERIC_LIMIT = Decimal("1e29")  # exclusive
BIGNUMERIC_MAX = Decimal("578960446186580977117854925043439539266.34992332820282019728792003956564819967")
BIGNUMERIC_MIN = Decimal("-578960446186580977117854925043439539266.34992332820282019728792003956564819968")

EPOCH = datetime(1970, 1, 1)
DATETIME_MIN = datetime(1, 1, 1)
DATETIME_MAX = datetime(9999, 12, 31, 23, 59, 59, 999999)
TIMESTAMP_MIN = -62135596800 * 1_000_000
TIMESTAMP_MAX = 253402300799 * 1_000_000 + 999_999
MICROS_PER_DAY = 86_400_000_000


class UnorderedArray(tuple):
    """An array whose element order BigQuery leaves unspecified (``ARRAY_AGG`` or ``ARRAY(subquery)`` without ``ORDER BY``)."""

    __slots__ = ()


def ordered_kind(value: tuple) -> bool:
    return not isinstance(value, UnorderedArray) or len(set(map(repr, value))) <= 1


# --- integers and decimals ---------------------------------------------------------------------


def int64(value: int) -> int:
    if value < INT64_MIN or value > INT64_MAX:
        raise EvalError("int64 overflow")
    return value


def numeric(value: Decimal) -> Decimal:
    """``value`` rounded to NUMERIC's 9 digits (half away from zero); raises on overflow."""

    if not value.is_finite():
        raise EvalError("numeric overflow")
    rounded = value.quantize(NUMERIC_SCALE, rounding=ROUND_HALF_UP, context=DEC)
    if not (-NUMERIC_LIMIT < rounded < NUMERIC_LIMIT):  # exact comparison; abs() would round to the default 28 digits
        raise EvalError("numeric overflow")
    return rounded


def bignumeric(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise EvalError("BIGNUMERIC overflow")
    rounded = value.quantize(BIGNUMERIC_SCALE, rounding=ROUND_HALF_UP, context=DEC)
    if rounded > BIGNUMERIC_MAX or rounded < BIGNUMERIC_MIN:
        raise EvalError("BIGNUMERIC overflow")
    return rounded


def decimal_of(kind: str) -> Callable[[Decimal], Decimal]:
    return numeric if kind == "NUMERIC" else bignumeric


def float64(value: float) -> float:
    """An arithmetic result: an infinity from finite operands is BigQuery's ``double overflow``."""

    if math.isinf(value):
        raise EvalError("double overflow")
    return value


def round_half_away(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_HALF_UP, context=DEC))


def format_decimal(value: Decimal) -> str:
    """NUMERIC as BigQuery prints it: no exponent, no trailing zeros after the point."""

    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("-0", ""):
        text = "0"
    return text


def format_double(value: float) -> str:
    """FLOAT64 as text: the shortest of 15 or 17 significant digits that reads back the same (ZetaSQL's rule)."""

    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    text = "%.15g" % value
    if float(text) != value:
        text = "%.17g" % value
    return text


# --- strings and bytes -------------------------------------------------------------------------


def like_regex(pattern: str | bytes) -> re.Pattern:
    """A LIKE pattern as a regular expression: ``%`` any run, ``_`` one character, ``\\`` escapes the next one."""

    is_bytes = isinstance(pattern, bytes)
    text = pattern.decode("latin-1") if is_bytes else pattern
    out = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\":
            if i + 1 >= len(text):
                raise EvalError("LIKE pattern ends with a backslash")
            out.append(re.escape(text[i + 1]))
            i += 2
            continue
        if char == "%":
            out.append(".*")
        elif char == "_":
            out.append(".")
        else:
            out.append(re.escape(char))
        i += 1
    regex = "".join(out)
    flags = re.DOTALL
    if is_bytes:
        return re.compile(("(?s)" + regex).encode("latin-1"))
    return re.compile(regex, flags)


def utf8(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        raise EvalError("invalid UTF-8") from None


# --- dates and times ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Interval:
    """GoogleSQL ``INTERVAL``: months, days and microseconds kept apart (BigQuery's precision)."""

    months: int = 0
    days: int = 0
    micros: int = 0

    def total(self) -> int:
        """Comparison value: a month is 30 days and a day 24 hours, as GoogleSQL compares intervals."""

        return (self.months * 30 + self.days) * MICROS_PER_DAY + self.micros

    def check(self) -> "Interval":
        if abs(self.months) > 120000 or abs(self.days) > 3660000 or abs(self.micros) > 87840000 * 3600 * 1_000_000:
            raise EvalError("Interval overflow")
        return self

    def __neg__(self) -> "Interval":
        return Interval(-self.months, -self.days, -self.micros)


def format_interval(value: Interval) -> str:
    months, days, micros = value.months, value.days, value.micros
    sign_ym = "-" if months < 0 else ""
    ym = f"{sign_ym}{abs(months) // 12}-{abs(months) % 12}"
    sign_t = "-" if micros < 0 else ""
    total = abs(micros)
    hours, rest = divmod(total, 3600 * 1_000_000)
    minutes, rest = divmod(rest, 60 * 1_000_000)
    seconds, fraction = divmod(rest, 1_000_000)
    text = f"{ym} {days} {sign_t}{hours}:{minutes}:{seconds}"
    if fraction:
        text += "." + f"{fraction:06d}".rstrip("0")
    return text


def _fraction(micros: int) -> str:
    """Sub-seconds with 0, 3 or 6 digits, as GoogleSQL prints them."""

    if micros == 0:
        return ""
    if micros % 1000 == 0:
        return f".{micros // 1000:03d}"
    return f".{micros:06d}"


def format_date(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}-{value.day:02d}"


def format_time(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}{_fraction(value.microsecond)}"


def format_datetime(value: datetime, sep: str = " ") -> str:
    return f"{format_date(value)}{sep}{format_time(value.time())}"


def micros_to_utc(micros: int) -> datetime:
    return EPOCH + timedelta(microseconds=micros)


def utc_to_micros(value: datetime) -> int:
    delta = value - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def timestamp(micros: int) -> int:
    if micros < TIMESTAMP_MIN or micros > TIMESTAMP_MAX:
        raise EvalError("Timestamp out of range")
    return micros


_OFFSET = re.compile(r"^(?:UTC)?([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


class FixedZone(tzinfo):
    def __init__(self, minutes: int):
        self.minutes = minutes

    def utcoffset(self, dt):
        return timedelta(minutes=self.minutes)

    def dst(self, dt):
        return timedelta(0)

    def tzname(self, dt):
        return None

    def __repr__(self):
        return f"FixedZone({self.minutes})"


# names of the tz database's "backward" links that a machine's database may lack, with the zone each one names
_TZ_LINKS = {
    "nz-chat": "Pacific/Chatham", "us/eastern": "America/New_York", "us/central": "America/Chicago",
    "us/mountain": "America/Denver", "us/pacific": "America/Los_Angeles", "us/alaska": "America/Anchorage",
    "us/hawaii": "Pacific/Honolulu", "us/arizona": "America/Phoenix",
}


@lru_cache(maxsize=None)
def zone(name: str) -> tzinfo:
    """A time zone by GoogleSQL name: ``UTC``, an offset (``+05:30``, ``-8``) or an IANA name."""

    text = name.strip()
    if text.upper() in ("UTC", "Z", "ZULU", "ETC/UTC", "GMT"):
        return FixedZone(0)
    match = _OFFSET.match(text)
    if match:
        sign, hours, minutes = match.groups()
        total = int(hours) * 60 + int(minutes or 0)
        if int(hours) > 14 or int(minutes or 0) > 59:
            raise EvalError(f"Invalid time zone: {name}")
        return FixedZone(-total if sign == "-" else total)
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:  # pragma: no cover - Python without zoneinfo
        raise Unsupported("named time zones need zoneinfo") from None
    text = _TZ_LINKS.get(text.lower(), text)
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        if re.fullmatch(r"[A-Za-z_]+(/[A-Za-z_+\-0-9]+)*", text):
            # On a machine without the tz database every name fails; only a real check can tell a bad name.
            try:
                ZoneInfo("America/Los_Angeles")
            except Exception:  # noqa: BLE001
                raise Unsupported("no time zone database on this machine") from None
        raise EvalError(f"Invalid time zone: {name}") from None


def to_civil(micros: int, tz: tzinfo) -> datetime:
    """The local civil time of a TIMESTAMP in ``tz``."""

    utc = micros_to_utc(micros)
    try:
        if isinstance(tz, FixedZone):
            return utc + timedelta(minutes=tz.minutes)
        return utc.replace(tzinfo=timezone.utc).astimezone(tz).replace(tzinfo=None)
    except (OverflowError, ValueError):
        raise Unsupported("timestamp too close to the ends of the range for time zone arithmetic") from None


def from_civil(value: datetime, tz: tzinfo) -> int:
    """The TIMESTAMP of a civil time in ``tz`` (a repeated or skipped local time uses the earlier offset)."""

    if isinstance(tz, FixedZone):
        return utc_to_micros(value - timedelta(minutes=tz.minutes))
    try:
        offset = value.replace(tzinfo=tz, fold=0).utcoffset()
    except (OverflowError, ValueError):
        raise Unsupported("time zone arithmetic at the ends of the range") from None
    return utc_to_micros(value - offset)


def zone_offset(micros: int, tz: tzinfo) -> int:
    """Offset of ``tz`` from UTC at the instant, in minutes."""

    if isinstance(tz, FixedZone):
        return tz.minutes
    local = to_civil(micros, tz)
    return round((local - micros_to_utc(micros)).total_seconds() / 60)


def format_offset(minutes: int) -> str:
    sign = "-" if minutes < 0 else "+"
    hours, mins = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}" + (f":{mins:02d}" if mins else "")


def format_timestamp(micros: int, tz: tzinfo) -> str:
    local = to_civil(micros, tz)
    return format_datetime(local) + format_offset(zone_offset(micros, tz))


def check_date(value: date | datetime) -> Any:
    return value


def add_months(value: date, months: int) -> date:
    """GoogleSQL month arithmetic: the day is clamped to the end of the month."""

    total = value.year * 12 + (value.month - 1) + months
    year, month = divmod(total, 12)
    if year < 1 or year > 9999:
        raise EvalError("Date out of range")
    month += 1
    last = _days_in_month(year, month)
    return value.replace(year=year, month=month, day=min(value.day, last))


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def date_add_days(value: date, days: int) -> date:
    try:
        return value + timedelta(days=days)
    except OverflowError:
        raise EvalError("Date out of range") from None


def datetime_add(value: datetime, micros: int) -> datetime:
    try:
        result = value + timedelta(microseconds=micros)
    except OverflowError:
        raise EvalError("DATETIME out of range") from None
    return result


# --- parsing text --------------------------------------------------------------------------------

_INT = re.compile(r"^\s*([+-]?)\s*(0[xX][0-9a-fA-F]+|[0-9]+)\s*$")
_FLOAT = re.compile(r"^\s*[+-]?(([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?|inf|infinity|nan)\s*$", re.IGNORECASE)
_DECIMAL = re.compile(r"^\s*[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?\s*$")
_DATE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$")
_TIME = r"(\d{1,2}):(\d{1,2})(?::(\d{1,2})(?:\.(\d{1,9}))?)?"
_TIME_RE = re.compile(r"^\s*" + _TIME + r"\s*$")
_DATETIME = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:(?:[ T]|\s+)" + _TIME + r")?\s*$")
_TIMESTAMP = re.compile(
    r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:(?:[ T]|\s+)" + _TIME + r")?\s*(Z|[+-]\d{1,2}(?::?\d{2})?|\s*[A-Za-z_][A-Za-z_/+\-0-9]*)?\s*$"
)


def parse_int64(text: str) -> int:
    match = _INT.match(text)
    if not match:
        raise EvalError(f"Bad int64 value: {text}")
    sign, digits = match.groups()
    value = int(digits, 16) if digits[:2].lower() == "0x" else int(digits)
    return int64(-value if sign == "-" else value)


def parse_float64(text: str) -> float:
    if not _FLOAT.match(text):
        raise EvalError(f"Bad double value: {text}")
    return float(text.strip())


def parse_decimal(text: str, kind: str) -> Decimal:
    if not _DECIMAL.match(text):
        raise EvalError(f"Invalid {kind} value: {text}")
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        raise EvalError(f"Invalid {kind} value: {text}") from None
    try:
        return decimal_of(kind)(value)
    except EvalError:
        raise EvalError(f"Invalid {kind} value: {text}") from None


def parse_bool(text: str) -> bool:
    lowered = text.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    raise EvalError(f"Bad bool value: {text}")


def _micros_of(fraction: str | None) -> int:
    if not fraction:
        return 0
    if len(fraction) > 6:
        if fraction[6:].strip("0"):
            raise Unsupported("sub-microsecond precision")
        fraction = fraction[:6]
    return int(fraction.ljust(6, "0"))


def parse_date(text: str) -> date:
    match = _DATE.match(text)
    if not match:
        raise EvalError(f"Invalid date: '{text}'")
    try:
        return date(*map(int, match.groups()))
    except ValueError:
        raise EvalError(f"Invalid date: '{text}'") from None


def parse_time(text: str) -> time:
    match = _TIME_RE.match(text)
    if not match:
        raise EvalError(f"Invalid time string \"{text}\"")
    h, m, s, f = match.groups()
    try:
        return time(int(h), int(m), int(s or 0), _micros_of(f))
    except ValueError:
        raise EvalError(f"Invalid time string \"{text}\"") from None


def parse_datetime(text: str) -> datetime:
    match = _DATETIME.match(text)
    if not match:
        raise EvalError(f"Invalid datetime string \"{text}\"")
    y, mo, d, h, mi, s, f = match.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0), _micros_of(f))
    except ValueError:
        raise EvalError(f"Invalid datetime string \"{text}\"") from None


def parse_timestamp(text: str, default_zone: tzinfo) -> int:
    match = _TIMESTAMP.match(text)
    if not match:
        raise EvalError(f"Invalid timestamp: '{text}'")
    y, mo, d, h, mi, s, f, zone_text = match.groups()
    try:
        civil = datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0), _micros_of(f))
    except ValueError:
        raise EvalError(f"Invalid timestamp: '{text}'") from None
    tz = default_zone
    if zone_text:
        zone_text = zone_text.strip()
        tz = zone("UTC" if zone_text.upper() == "Z" else zone_text)
    return timestamp(from_civil(civil, tz))


def parse_interval_text(text: str) -> Interval:
    """``'Y-M D H:M:S.F'`` and its parts, as CAST(STRING AS INTERVAL) reads them."""

    from . import datetimes

    return datetimes.interval_from_string(text)


# --- comparison, grouping and equality ---------------------------------------------------------


def sql_equal(t: T.Type, a: Any, b: Any) -> bool | None:
    """``a = b`` with three-valued logic."""

    if a is None or b is None:
        return None
    kind = t.kind
    if kind == "STRUCT":
        unknown = False
        for (_, ft), x, y in zip(t.fields, a, b):
            result = sql_equal(ft, x, y)
            if result is False:
                return False
            if result is None:
                unknown = True
        return None if unknown else True
    if kind == "INTERVAL":
        return a.total() == b.total()
    if kind == "ARRAY":
        raise Unsupported("array equality")
    return a == b


def sort_key(t: T.Type, value: Any) -> Any:
    """A key ordering non-NULL values as ORDER BY does (NaN first among floats, -0.0 equal to 0.0)."""

    kind = t.kind
    if kind == "FLOAT64":
        return (0, 0.0) if math.isnan(value) else (1, value)
    if kind == "INTERVAL":
        return value.total()
    if kind == "BOOL":
        return int(value)
    if kind in ("STRUCT", "ARRAY"):
        raise AnalysisError(f"ORDER BY does not support {t}")
    return value


def compare(t: T.Type, a: Any, b: Any) -> int:
    ka, kb = sort_key(t, a), sort_key(t, b)
    return (ka > kb) - (ka < kb)


_NAN_KEY = ("NaN",)


def group_key(t: T.Type, value: Any) -> Any:
    """A hashable key that is equal exactly for values GROUP BY, DISTINCT and set operations treat as one."""

    if value is None:
        return None
    kind = t.kind
    if kind == "FLOAT64":
        return _NAN_KEY if value != value else (value + 0.0)
    if kind == "STRUCT":
        return tuple(group_key(ft, v) for (_, ft), v in zip(t.fields, value))
    if kind == "ARRAY":
        return ("array",) + tuple(group_key(t.elem, v) for v in value)
    if kind == "INTERVAL":
        return ("interval", value.total())
    if kind == "BOOL":
        return ("bool", value)
    return value


def row_key(types: list[T.Type], row: tuple) -> tuple:
    return tuple(group_key(t, v) for t, v in zip(types, row))


# --- text forms (CAST AS STRING) -----------------------------------------------------------------


def to_string(t: T.Type, value: Any, tz: tzinfo) -> str:
    kind = t.kind
    if kind == "STRING":
        return value
    if kind == "INT64":
        return str(value)
    if kind in ("NUMERIC", "BIGNUMERIC"):
        return format_decimal(value)
    if kind == "FLOAT64":
        return format_double(value)
    if kind == "BOOL":
        return "true" if value else "false"
    if kind == "BYTES":
        return utf8(value)
    if kind == "DATE":
        return format_date(value)
    if kind == "DATETIME":
        return format_datetime(value)
    if kind == "TIME":
        return format_time(value)
    if kind == "TIMESTAMP":
        return format_timestamp(value, tz)
    if kind == "INTERVAL":
        return format_interval(value)
    raise Unsupported(f"CAST({t} AS STRING)")


# --- casts ---------------------------------------------------------------------------------------

_CASTS: dict[tuple[str, str], Callable[[Any, tzinfo], Any]] = {}


def _cast(source: str, target: str):
    def register(fn):
        _CASTS[(source, target)] = fn
        return fn

    return register


def _float_to_int(value: float) -> int:
    if math.isnan(value) or math.isinf(value):
        raise EvalError(f"Illegal conversion of non-finite floating point number to an integer: {format_double(value)}")
    rounded = math.floor(abs(value) + 0.5) * (1 if value >= 0 else -1) if abs(value) < 2**52 else int(value)
    if rounded < INT64_MIN or rounded > INT64_MAX:
        raise EvalError(f"int64 out of range: {format_double(value)}")
    return int(rounded)


def _float_to_decimal(kind: str) -> Callable[[float, tzinfo], Decimal]:
    def convert(value: float, tz: tzinfo) -> Decimal:
        if math.isnan(value) or math.isinf(value):
            raise EvalError(f"Illegal conversion of non-finite floating point number to {kind}: {format_double(value)}")
        try:
            return decimal_of(kind)(Decimal(value))
        except EvalError:
            raise EvalError(f"{kind} out of range: {format_double(value)}") from None

    return convert


def _decimal_to_int(value: Decimal, tz: tzinfo) -> int:
    result = round_half_away(value)
    if result < INT64_MIN or result > INT64_MAX:
        raise EvalError(f"int64 out of range: {format_decimal(value)}")
    return result


for _num in ("NUMERIC", "BIGNUMERIC"):
    _CASTS[("INT64", _num)] = (lambda kind: lambda v, tz: decimal_of(kind)(Decimal(v)))(_num)
    _CASTS[("FLOAT64", _num)] = _float_to_decimal(_num)
    _CASTS[(_num, "INT64")] = _decimal_to_int
    _CASTS[(_num, "FLOAT64")] = lambda v, tz: float(v)
    _CASTS[("STRING", _num)] = (lambda kind: lambda v, tz: parse_decimal(v, kind))(_num)
    _CASTS[(_num, "STRING")] = lambda v, tz: format_decimal(v)
_CASTS[("NUMERIC", "BIGNUMERIC")] = lambda v, tz: bignumeric(v)
_CASTS[("BIGNUMERIC", "NUMERIC")] = lambda v, tz: numeric(v)
_CASTS[("INT64", "FLOAT64")] = lambda v, tz: float(v)
_CASTS[("FLOAT64", "INT64")] = lambda v, tz: _float_to_int(v)
_CASTS[("INT64", "BOOL")] = lambda v, tz: v != 0
_CASTS[("BOOL", "INT64")] = lambda v, tz: int(v)
_CASTS[("INT64", "STRING")] = lambda v, tz: str(v)
_CASTS[("FLOAT64", "STRING")] = lambda v, tz: format_double(v)
_CASTS[("BOOL", "STRING")] = lambda v, tz: "true" if v else "false"
_CASTS[("STRING", "INT64")] = lambda v, tz: parse_int64(v)
_CASTS[("STRING", "FLOAT64")] = lambda v, tz: parse_float64(v)
_CASTS[("STRING", "BOOL")] = lambda v, tz: parse_bool(v)
_CASTS[("STRING", "BYTES")] = lambda v, tz: v.encode("utf-8")
_CASTS[("BYTES", "STRING")] = lambda v, tz: utf8(v)
_CASTS[("STRING", "DATE")] = lambda v, tz: parse_date(v)
_CASTS[("STRING", "DATETIME")] = lambda v, tz: parse_datetime(v)
_CASTS[("STRING", "TIME")] = lambda v, tz: parse_time(v)
_CASTS[("STRING", "TIMESTAMP")] = lambda v, tz: parse_timestamp(v, tz)
_CASTS[("DATE", "STRING")] = lambda v, tz: format_date(v)
_CASTS[("DATETIME", "STRING")] = lambda v, tz: format_datetime(v)
_CASTS[("TIME", "STRING")] = lambda v, tz: format_time(v)
_CASTS[("TIMESTAMP", "STRING")] = lambda v, tz: format_timestamp(v, tz)
_CASTS[("INTERVAL", "STRING")] = lambda v, tz: format_interval(v)
_CASTS[("STRING", "INTERVAL")] = lambda v, tz: parse_interval_text(v)
_CASTS[("DATE", "DATETIME")] = lambda v, tz: datetime(v.year, v.month, v.day)
_CASTS[("DATETIME", "DATE")] = lambda v, tz: v.date()
_CASTS[("DATETIME", "TIME")] = lambda v, tz: v.time()
_CASTS[("DATE", "TIMESTAMP")] = lambda v, tz: timestamp(from_civil(datetime(v.year, v.month, v.day), tz))
_CASTS[("DATETIME", "TIMESTAMP")] = lambda v, tz: timestamp(from_civil(v, tz))
_CASTS[("TIMESTAMP", "DATE")] = lambda v, tz: to_civil(v, tz).date()
_CASTS[("TIMESTAMP", "DATETIME")] = lambda v, tz: to_civil(v, tz)
_CASTS[("TIMESTAMP", "TIME")] = lambda v, tz: to_civil(v, tz).time()


def castable(source: T.Type, target: T.Type) -> bool:
    if source == target:
        return True
    if source.foreign or target.foreign:
        return False
    if source.kind == "ARRAY" and target.kind == "ARRAY":
        return castable(source.elem, target.elem)
    if source.kind == "STRUCT" and target.kind == "STRUCT":
        return len(source.fields) == len(target.fields) and all(
            castable(s, t) for (_, s), (_, t) in zip(source.fields, target.fields)
        )
    return (source.kind, target.kind) in _CASTS


def caster(source: T.Type, target: T.Type) -> Callable[[Any, tzinfo], Any]:
    """A function casting non-NULL payloads of ``source`` to ``target`` (raising :class:`EvalError` where BigQuery does)."""

    if source == target:
        return lambda v, tz: v
    if source.foreign or target.foreign:
        raise Unsupported(f"CAST from {source} to {target}")
    if source.kind == "ARRAY" and target.kind == "ARRAY":
        inner = caster(source.elem, target.elem)
        return lambda v, tz: type(v)(None if x is None else inner(x, tz) for x in v)
    if source.kind == "STRUCT" and target.kind == "STRUCT":
        if len(source.fields) != len(target.fields):
            raise AnalysisError(f"Invalid cast from {source} to {target}")
        parts = [caster(s, t) for (_, s), (_, t) in zip(source.fields, target.fields)]
        return lambda v, tz: tuple(None if x is None else p(x, tz) for p, x in zip(parts, v))
    fn = _CASTS.get((source.kind, target.kind))
    if fn is None:
        raise AnalysisError(f"Invalid cast from {source} to {target}")
    return fn


__all__ = [
    "Interval", "UnorderedArray", "int64", "numeric", "bignumeric", "float64", "format_double", "format_decimal",
    "sql_equal", "compare", "sort_key", "group_key", "row_key", "caster", "castable", "to_string", "zone",
]
