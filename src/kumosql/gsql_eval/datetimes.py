"""Date, time and interval arithmetic for GoogleSQL (BigQuery) values.

Payloads are those of :mod:`values`: ``datetime.date`` for DATE, a naive ``datetime.datetime`` for DATETIME, ``datetime.time``
for TIME, microseconds since the epoch for TIMESTAMP and :class:`values.Interval` (months, days and microseconds kept apart)
for INTERVAL. Every function here either computes what BigQuery computes or raises ``Unsupported``: where the reference
behaviour is not certain (sub-microsecond digits, a form of the text that BigQuery may or may not accept, a date-time
function whose boundary rule is not documented) the function declines instead of guessing.

The SQL-facing wrappers (argument typing, NULL handling, node shapes) live in :mod:`fn_time`.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, tzinfo
from fractions import Fraction

from . import values as V
from .errors import AnalysisError, EvalError, Unsupported
from .values import EPOCH, MICROS_PER_DAY, Interval

MICROS_PER_SECOND = 1_000_000
MICROS_PER_MINUTE = 60 * MICROS_PER_SECOND
MICROS_PER_HOUR = 3600 * MICROS_PER_SECOND

# the sub-day parts and their length in microseconds (a DAY of a TIMESTAMP is 24 hours)
MICROS_OF = {
    "MICROSECOND": 1,
    "MILLISECOND": 1000,
    "SECOND": MICROS_PER_SECOND,
    "MINUTE": MICROS_PER_MINUTE,
    "HOUR": MICROS_PER_HOUR,
    "DAY": MICROS_PER_DAY,
}

MONTHS_LIMIT = 120000
DAYS_LIMIT = 3660000
NANOS_LIMIT = 87840000 * 3600 * 10**9

WEEKDAYS = {"MONDAY": 0, "TUESDAY": 1, "WEDNESDAY": 2, "THURSDAY": 3, "FRIDAY": 4, "SATURDAY": 5, "SUNDAY": 6}
SUNDAY = 6  # ``date.weekday()`` numbers Monday 0; the default WEEK starts on Sunday

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
    "December",
]


# --- INTERVAL ------------------------------------------------------------------------------------


def check_interval(value: Interval) -> Interval:
    """The interval, or the error BigQuery raises for a field out of its range."""

    if abs(value.months) > MONTHS_LIMIT:
        raise EvalError(f"Interval field months '{value.months}' is out of range -{MONTHS_LIMIT} to {MONTHS_LIMIT}")
    if abs(value.days) > DAYS_LIMIT:
        raise EvalError(f"Interval field days '{value.days}' is out of range -{DAYS_LIMIT} to {DAYS_LIMIT}")
    if abs(value.micros) * 1000 > NANOS_LIMIT:
        raise EvalError(
            f"Interval field nanoseconds '{value.micros * 1000}' is out of range -{NANOS_LIMIT} to {NANOS_LIMIT}"
        )
    return value


_INTERVAL_UNITS = {
    "YEAR": lambda n: Interval(months=12 * n),
    "QUARTER": lambda n: Interval(months=3 * n),
    "MONTH": lambda n: Interval(months=n),
    "WEEK": lambda n: Interval(days=7 * n),
    "DAY": lambda n: Interval(days=n),
    "HOUR": lambda n: Interval(micros=n * MICROS_PER_HOUR),
    "MINUTE": lambda n: Interval(micros=n * MICROS_PER_MINUTE),
    "SECOND": lambda n: Interval(micros=n * MICROS_PER_SECOND),
    "MILLISECOND": lambda n: Interval(micros=n * 1000),
    "MICROSECOND": lambda n: Interval(micros=n),
}


def interval_of(unit: str):
    """``INTERVAL n <unit>`` as a function of the count ``n`` (an INT64)."""

    unit = unit.upper()
    make = _INTERVAL_UNITS.get(unit)
    if make is None:
        if unit == "NANOSECOND":
            raise Unsupported("INTERVAL in NANOSECOND")
        raise AnalysisError(f"INTERVAL does not accept the date part {unit}")
    return lambda n: check_interval(make(n))


_COUNT = re.compile(r"^[+-]?[0-9]+$")
_SECONDS = re.compile(r"^([+-]?)([0-9]+)(?:\.([0-9]+))?$")


def _micros_of_fraction(fraction: str | None) -> int:
    if not fraction:
        return 0
    if len(fraction) > 6:
        if fraction[6:].strip("0"):
            raise Unsupported("INTERVAL with sub-microsecond digits")
        fraction = fraction[:6]
    return int(fraction.ljust(6, "0"))


def interval_from_text(text: str, name: str) -> Interval:
    """``INTERVAL '<text>' <unit>``: the text is an integer (seconds may carry a fraction)."""

    name = name.upper()
    make = _INTERVAL_UNITS.get(name)
    if make is None:
        if name == "NANOSECOND":
            raise Unsupported("INTERVAL in NANOSECOND")
        raise AnalysisError(f"INTERVAL does not accept the date part {name}")
    if _COUNT.match(text):
        return check_interval(make(int(text)))
    match = _SECONDS.match(text)
    if match and name == "SECOND":
        sign, whole, fraction = match.groups()
        micros = int(whole) * MICROS_PER_SECOND + _micros_of_fraction(fraction)
        return check_interval(Interval(micros=-micros if sign == "-" else micros))
    raise Unsupported(f"INTERVAL text {text!r} for {name}")


_SPAN_ORDER = ["YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND"]
_SIGNED = r"([+-]?)"
_UINT = r"([0-9]+)"


def parse_interval_span(text: str, start: str, end: str) -> Interval:
    """``INTERVAL '<text>' <start> TO <end>``: ``[sign]Y-M [sign]D [sign]H:M:S[.F]`` cut to the parts from start to end."""

    start, end = start.upper(), end.upper()
    if start not in _SPAN_ORDER or end not in _SPAN_ORDER or _SPAN_ORDER.index(start) >= _SPAN_ORDER.index(end):
        raise AnalysisError(f"Invalid INTERVAL range {start} TO {end}")
    first, last = _SPAN_ORDER.index(start), _SPAN_ORDER.index(end)
    pieces = []  # regular expression of each space separated piece, and what its groups mean
    if first == 0:
        pieces.append((_SIGNED + _UINT + "-" + _UINT, "ym"))
    elif first == 1:
        pieces.append((_SIGNED + _UINT, "m"))
    if first <= 2 <= last:
        pieces.append((_SIGNED + _UINT, "d"))
    time_fields = [f for f in range(3, 6) if first <= f <= last or (f > first and f <= last)]
    if first >= 3:
        time_fields = list(range(first, last + 1))
    else:
        time_fields = list(range(3, last + 1)) if last >= 3 else []
    if time_fields:
        body = ":".join(_UINT if f < 5 else _UINT + r"(?:\.([0-9]+))?" for f in time_fields)
        pieces.append((_SIGNED + body, "t"))
    if not pieces:
        raise AnalysisError(f"Invalid INTERVAL range {start} TO {end}")
    regex = re.compile("^" + " ".join(p for p, _ in pieces) + "$")
    match = regex.match(text)
    if not match:
        raise Unsupported(f"INTERVAL text {text!r} for {start} TO {end}")
    groups = list(match.groups())
    months = days = micros = 0
    for _, kind in pieces:
        if kind == "ym":
            sign, years, mons = groups[:3]
            groups = groups[3:]
            if int(mons) > 11:
                raise Unsupported("INTERVAL month field above 11 in Y-M")
            total = int(years) * 12 + int(mons)
            months = -total if sign == "-" else total
        elif kind == "m":
            sign, mons = groups[:2]
            groups = groups[2:]
            months = -int(mons) if sign == "-" else int(mons)
        elif kind == "d":
            sign, d = groups[:2]
            groups = groups[2:]
            days = -int(d) if sign == "-" else int(d)
        else:
            sign = groups[0]
            count = len(time_fields)
            has_seconds = time_fields[-1] == 5
            values = [int(v) for v in groups[1 : 1 + count]]
            fraction = groups[1 + count] if has_seconds else None
            groups = groups[2 + count :] if has_seconds else groups[1 + count :]
            total = 0
            units = [MICROS_PER_HOUR, MICROS_PER_MINUTE, MICROS_PER_SECOND]
            for index, field, number in zip(range(count), time_fields, values):
                bounded = index > 0  # only the leading field may exceed its natural range
                limit = 59
                if bounded and number > limit:
                    raise Unsupported("INTERVAL minute or second field above 59")
                total += number * units[field - 3]
            total += _micros_of_fraction(fraction)
            micros = -total if sign == "-" else total
    return check_interval(Interval(months, days, micros))


def interval_divide(value: Interval, divisor: int) -> Interval:
    """``INTERVAL / INT64``: exact when no field has a remainder to carry into the next smaller one."""

    if divisor == 0:
        raise EvalError("division by zero: INTERVAL / 0")
    if value.months % divisor == 0 and value.days % divisor == 0:
        # microseconds divide toward zero (a fraction of a microsecond is dropped)
        micros = abs(value.micros) // abs(divisor)
        if (value.micros < 0) != (divisor < 0):
            micros = -micros
        return check_interval(Interval(value.months // divisor, value.days // divisor, micros))
    raise Unsupported("INTERVAL / INT64 with a month or day remainder")


def difference_interval(kind: str):
    """``a - b`` of two DATEs or two TIMESTAMPs as an INTERVAL (days only, resp. microseconds only)."""

    if kind == "DATE":
        return lambda a, b: check_interval(Interval(0, (a - b).days, 0))
    if kind == "TIMESTAMP":
        return lambda a, b: check_interval(Interval(0, 0, a - b))
    raise Unsupported(f"{kind} - {kind} as an INTERVAL")


def _sign_fix_pair(high: int, low: int, factor: int) -> tuple[int, int]:
    if high > 0 and low < 0:
        return high - 1, low + factor
    if high < 0 and low > 0:
        return high + 1, low - factor
    return high, low


def _trunc_div(a: int, b: int) -> int:
    quotient = abs(a) // abs(b)
    return -quotient if (a < 0) != (b < 0) else quotient


def justify_hours(value: Interval) -> Interval:
    whole = _trunc_div(value.micros, MICROS_PER_DAY)
    days = value.days + whole
    micros = value.micros - whole * MICROS_PER_DAY
    days, micros = _sign_fix_pair(days, micros, MICROS_PER_DAY)
    return check_interval(Interval(value.months, days, micros))


def justify_days(value: Interval) -> Interval:
    whole = _trunc_div(value.days, 30)
    months = value.months + whole
    days = value.days - whole * 30
    months, days = _sign_fix_pair(months, days, 30)
    return check_interval(Interval(months, days, value.micros))


def justify_interval(value: Interval) -> Interval:
    """Hours into days, days into months, then the signs of the three parts made to agree (the Postgres algorithm)."""

    months, days, micros = value.months, value.days, value.micros
    whole = _trunc_div(micros, MICROS_PER_DAY)
    days += whole
    micros -= whole * MICROS_PER_DAY
    whole = _trunc_div(days, 30)
    months += whole
    days -= whole * 30
    if months > 0 and (days < 0 or (days == 0 and micros < 0)) or months < 0 and (days > 0 or (days == 0 and micros > 0)):
        if days == 0 and micros != 0:
            # whether BigQuery borrows a month for a pure time remainder is not documented
            raise Unsupported("JUSTIFY_INTERVAL of a month with a time remainder of opposite sign")
        months, days = _sign_fix_pair(months, days, 30)
    days, micros = _sign_fix_pair(days, micros, MICROS_PER_DAY)
    return check_interval(Interval(months, days, micros))


def extract_interval(value: Interval, part: str) -> int:
    part = part.upper()
    if part == "YEAR":
        return _trunc_div(value.months, 12)
    if part == "MONTH":
        return value.months - _trunc_div(value.months, 12) * 12
    if part == "DAY":
        return value.days
    if part == "HOUR":
        return _trunc_div(value.micros, MICROS_PER_HOUR)
    sign = -1 if value.micros < 0 else 1
    rest = abs(value.micros)
    if part == "MINUTE":
        return sign * (rest // MICROS_PER_MINUTE % 60)
    if part == "SECOND":
        return sign * (rest // MICROS_PER_SECOND % 60)
    if part == "MILLISECOND":
        return sign * (rest % MICROS_PER_SECOND // 1000)
    if part == "MICROSECOND":
        return sign * (rest % MICROS_PER_SECOND)
    if part == "NANOSECOND":
        raise Unsupported("EXTRACT(NANOSECOND FROM INTERVAL)")
    raise AnalysisError(f"EXTRACT from INTERVAL does not support {part}")


# --- adding an INTERVAL ----------------------------------------------------------------------------


def datetime_add_interval(value, interval: Interval) -> datetime:
    """DATE or DATETIME plus INTERVAL: months (day clamped to the month's end), then days, then the time part."""

    check_interval(interval)
    if not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    if interval.months:
        value = V.add_months(value, interval.months)
    if interval.days:
        try:
            value = value + timedelta(days=interval.days)
        except OverflowError:
            raise EvalError("DATETIME out of range") from None
    if interval.micros:
        value = V.datetime_add(value, interval.micros)
    if value < V.DATETIME_MIN or value > V.DATETIME_MAX:
        raise EvalError("DATETIME out of range")
    return value


def timestamp_add_interval(value: int, interval: Interval, tz: tzinfo | None = None) -> int:
    """TIMESTAMP plus INTERVAL: months and days are calendar steps in the time zone, the time part is absolute.

    Without ``tz`` only an interval of pure time is exact; with a fixed offset (UTC included) the calendar steps are
    civil arithmetic at that offset; in a zone with daylight saving time a calendar step is exact only when no
    transition lies between the two instants, otherwise the function declines.
    """

    check_interval(interval)
    if interval.months == 0 and interval.days == 0:
        return V.timestamp(value + interval.micros)
    if tz is None:
        raise Unsupported("TIMESTAMP +/- INTERVAL with months or days needs the session time zone")
    civil = V.to_civil(value, tz)
    if interval.months:
        civil = V.add_months(civil, interval.months)
    if interval.days:
        try:
            civil = civil + timedelta(days=interval.days)
        except OverflowError:
            raise EvalError("TIMESTAMP out of range") from None
    if not (V.DATETIME_MIN <= civil <= V.DATETIME_MAX):
        raise EvalError("TIMESTAMP out of range")
    moved = V.from_civil(civil, tz)
    if not isinstance(tz, V.FixedZone):
        if interval.months:
            raise Unsupported("TIMESTAMP + INTERVAL months in a zone with daylight saving time")
        if moved != value + interval.days * MICROS_PER_DAY:
            raise Unsupported("TIMESTAMP + INTERVAL days across a daylight saving transition")
        if interval.micros and V.zone_offset(moved + interval.micros, tz) != V.zone_offset(moved, tz):
            raise Unsupported("TIMESTAMP + INTERVAL across a daylight saving transition")
    return V.timestamp(moved + interval.micros)


# --- date parts ----------------------------------------------------------------------------------------

DATE_PARTS = ("DAY", "WEEK", "ISOWEEK", "MONTH", "QUARTER", "YEAR", "ISOYEAR")


def week_start_of(part: str, start: int | None) -> int:
    """The weekday (Monday 0) a week of ``part`` begins on."""

    if part == "ISOWEEK":
        return 0
    return SUNDAY if start is None else start


def add_date_part(value, part: str, count: int):
    """DATE or DATETIME plus ``count`` of DAY, WEEK, MONTH, QUARTER or YEAR."""

    if part in ("DAY", "WEEK"):
        days = count * (7 if part == "WEEK" else 1)
        try:
            result = value + timedelta(days=days)
        except OverflowError:
            raise EvalError("Date out of range") from None
        if result.year < 1 or result.year > 9999:
            raise EvalError("Date out of range")
        return result
    months = count * {"MONTH": 1, "QUARTER": 3, "YEAR": 12}[part]
    return V.add_months(value, months)


def add_micros_part(value: datetime, part: str, count: int) -> datetime:
    return V.datetime_add(value, count * MICROS_OF[part])


def week_number(day: date, start: int) -> int:
    """Week of the year, the first ``start`` weekday of the year beginning week 1 (days before it are in week 0)."""

    yday = day.timetuple().tm_yday - 1
    relative = (day.weekday() - start) % 7
    return (yday + 7 - relative) // 7


def date_diff(a: date, b: date, part: str, start: int | None = None) -> int:
    """Number of ``part`` boundaries crossed from ``b`` to ``a``."""

    if part == "DAY":
        return (a - b).days
    if part in ("WEEK", "ISOWEEK"):
        ws = week_start_of(part, start)
        return (a.toordinal() - 1 - ws) // 7 - (b.toordinal() - 1 - ws) // 7
    if part == "MONTH":
        return a.year * 12 + a.month - (b.year * 12 + b.month)
    if part == "QUARTER":
        return a.year * 4 + (a.month - 1) // 3 - (b.year * 4 + (b.month - 1) // 3)
    if part == "YEAR":
        return a.year - b.year
    if part == "ISOYEAR":
        return a.isocalendar()[0] - b.isocalendar()[0]
    raise AnalysisError(f"Unsupported date part {part}")


def _civil_micros(value: datetime) -> int:
    delta = value - V.DATETIME_MIN
    return (delta.days * 86400 + delta.seconds) * MICROS_PER_SECOND + delta.microseconds


def datetime_diff(a: datetime, b: datetime, part: str, start: int | None = None) -> int:
    if part in MICROS_OF:
        unit = MICROS_OF[part]
        return _civil_micros(a) // unit - _civil_micros(b) // unit
    return date_diff(a.date(), b.date(), part, start)


def time_micros(value: time) -> int:
    return ((value.hour * 60 + value.minute) * 60 + value.second) * MICROS_PER_SECOND + value.microsecond


def time_of_micros(micros: int) -> time:
    micros %= MICROS_PER_DAY
    seconds, micro = divmod(micros, MICROS_PER_SECOND)
    minutes, second = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    return time(hour, minute, second, micro)


def time_diff(a: time, b: time, part: str) -> int:
    """TIME_DIFF counts unit boundaries; it is exact here only where that equals the whole units between the two times."""

    unit = MICROS_OF[part]
    ta, tb = time_micros(a), time_micros(b)
    boundaries = ta // unit - tb // unit
    whole = _trunc_div(ta - tb, unit)
    if boundaries != whole:
        raise Unsupported("TIME_DIFF between times that are not a whole number of units apart")
    return boundaries


def timestamp_diff(a: int, b: int, part: str) -> int:
    """TIMESTAMP_DIFF: the whole number of ``part`` between two instants (toward zero)."""

    return _trunc_div(a - b, MICROS_OF[part])


def trunc_date(value: date, part: str, start: int | None = None) -> date:
    """DATE or the date of a DATETIME truncated to the start of its week, month, quarter or year."""

    try:
        if part == "DAY":
            return value
        if part in ("WEEK", "ISOWEEK"):
            ws = week_start_of(part, start)
            return value - timedelta(days=(value.weekday() - ws) % 7)
        if part == "MONTH":
            return value.replace(day=1)
        if part == "QUARTER":
            return value.replace(month=3 * ((value.month - 1) // 3) + 1, day=1)
        if part == "YEAR":
            return value.replace(month=1, day=1)
        if part == "ISOYEAR":
            return date.fromisocalendar(value.isocalendar()[0], 1, 1)
    except (OverflowError, ValueError):
        raise EvalError("Date out of range") from None
    raise AnalysisError(f"Unsupported date part {part}")


def trunc_datetime(value: datetime, part: str, start: int | None = None) -> datetime:
    if part in MICROS_OF and part != "DAY":
        unit = MICROS_OF[part]
        micros = time_micros(value.time())
        return datetime.combine(value.date(), time_of_micros(micros - micros % unit))
    day = trunc_date(value.date(), part, start)
    return datetime(day.year, day.month, day.day)


def trunc_time(value: time, part: str) -> time:
    unit = MICROS_OF[part]
    micros = time_micros(value)
    return time_of_micros(micros - micros % unit)


def trunc_timestamp(micros: int, part: str, start: int | None, tz: tzinfo) -> int:
    civil = V.to_civil(micros, tz)
    if part == "MICROSECOND":
        return micros
    if part in ("MILLISECOND", "SECOND", "MINUTE", "HOUR"):
        unit = MICROS_OF[part]
        remainder = time_micros(civil.time()) % unit
        result = micros - remainder
        if V.to_civil(result, tz) != civil - timedelta(microseconds=remainder):
            raise Unsupported("TIMESTAMP_TRUNC across a time zone transition")
        return result
    return V.timestamp(V.from_civil(trunc_datetime(civil, part, start), tz))


def last_day(value: date, part: str, start: int | None = None) -> date:
    try:
        if part == "MONTH":
            return value.replace(day=V._days_in_month(value.year, value.month))
        if part == "QUARTER":
            month = 3 * ((value.month - 1) // 3) + 3
            return date(value.year, month, V._days_in_month(value.year, month))
        if part == "YEAR":
            return date(value.year, 12, 31)
        if part in ("WEEK", "ISOWEEK"):
            return trunc_date(value, part, start) + timedelta(days=6)
        if part == "ISOYEAR":
            return date.fromisocalendar(value.isocalendar()[0] + 1, 1, 1) - timedelta(days=1)
    except (OverflowError, ValueError):
        raise EvalError("Date out of range") from None
    raise AnalysisError(f"LAST_DAY does not support the date part {part}")


def extract_date_part(value: date, part: str, start: int | None = None) -> int:
    if part == "YEAR":
        return value.year
    if part == "ISOYEAR":
        return value.isocalendar()[0]
    if part == "QUARTER":
        return (value.month - 1) // 3 + 1
    if part == "MONTH":
        return value.month
    if part == "WEEK":
        return week_number(value, SUNDAY if start is None else start)
    if part == "ISOWEEK":
        return value.isocalendar()[1]
    if part == "DAY":
        return value.day
    if part == "DAYOFWEEK":
        return value.isoweekday() % 7 + 1
    if part == "DAYOFYEAR":
        return value.timetuple().tm_yday
    raise AnalysisError(f"EXTRACT does not support {part} from a date")


def extract_time_part(value: time, part: str) -> int:
    if part == "HOUR":
        return value.hour
    if part == "MINUTE":
        return value.minute
    if part == "SECOND":
        return value.second
    if part == "MILLISECOND":
        return value.microsecond // 1000
    if part == "MICROSECOND":
        return value.microsecond
    raise AnalysisError(f"EXTRACT does not support {part} from a time")


# --- sequences ---------------------------------------------------------------------------------------

MAX_GENERATED = 10000


def generate_dates(start: date, end: date, step: int, part: str) -> tuple:
    """GENERATE_DATE_ARRAY: ``start`` plus ``k * step`` parts while not past ``end`` (an empty array when it starts past)."""

    if step == 0:
        raise EvalError("Sequence step cannot be 0.")
    out = []
    k = 0
    while True:
        try:
            current = add_date_part(start, part, k * step)
        except EvalError:
            break
        if (current > end) if step > 0 else (current < end):
            break
        out.append(current)
        if len(out) > MAX_GENERATED:
            raise Unsupported("a generated array of this size")
        k += 1
    if part in ("MONTH", "QUARTER", "YEAR") and start.day > 28:
        # stepping from the previous element (clamped) and from the start give different dates here
        raise Unsupported("GENERATE_DATE_ARRAY stepping months from a day above 28")
    return tuple(out)


def generate_timestamps(start: int, end: int, step: int, part: str) -> tuple:
    if step == 0:
        raise EvalError("Sequence step cannot be 0.")
    unit = MICROS_OF[part]
    delta = step * unit
    out = []
    current = start
    while (current <= end) if delta > 0 else (current >= end):
        out.append(current)
        if len(out) > MAX_GENERATED:
            raise Unsupported("a generated array of this size")
        current += delta
        if current < V.TIMESTAMP_MIN or current > V.TIMESTAMP_MAX:
            break
    return tuple(out)


# --- unix times ------------------------------------------------------------------------------------------

UNIX_DATE_MIN = -719162
UNIX_DATE_MAX = 2932896


def date_from_unix_date(days: int) -> date:
    if days < UNIX_DATE_MIN or days > UNIX_DATE_MAX:
        raise EvalError(f"Invalid DATE value {days}")
    return date(1970, 1, 1) + timedelta(days=days)


def unix_date(value: date) -> int:
    return (value - date(1970, 1, 1)).days


# --- FORMAT_* ----------------------------------------------------------------------------------------------

_ELEMENT = re.compile(r"%(E(?:[0-9]+|\*)[SfY]|Ez|[A-Za-z%])")

_DATE_ONLY = set("AaBbhCdejmQUuwVGgYyDFx") | {"E4Y"}
_TIME_ONLY = set("HIklMPpRSTX")
_BOTH = set("c")
_STAMP_ONLY = set("Zzs") | {"Ez"}


def _year_text(year: int) -> str:
    if year < 1000:
        raise Unsupported("a year below 1000 under %Y (padding not documented)")
    return str(year)


def _two(n: int) -> str:
    return f"{n:02d}"


def _fraction_digits(micro: int, count: int) -> str:
    return f"{micro:06d}".ljust(count, "0")[:count]


def format_elements(fmt: str, kind: str, day: date | None, tod: time | None, stamp: tuple | None = None) -> str:
    """FORMAT_DATE / FORMAT_TIME / FORMAT_DATETIME / FORMAT_TIMESTAMP: the documented format elements, else ``Unsupported``.

    ``kind`` is the argument's type; ``stamp`` is ``(micros, tz)`` for a TIMESTAMP.
    """

    out = []
    pos = 0
    for match in _ELEMENT.finditer(fmt):
        out.append(fmt[pos : match.start()])
        pos = match.end()
        if "%" in fmt[match.start() : match.start() + 1] and False:  # pragma: no cover
            pass
        out.append(_element(match.group(1), kind, day, tod, stamp))
    tail = fmt[pos:]
    if "%" in tail:
        raise Unsupported("a format string with a stray %")
    out.append(tail)
    return "".join(out)


def _element(code: str, kind: str, day: date | None, tod: time | None, stamp: tuple | None) -> str:
    if code == "%":
        return "%"
    if code == "n":
        return "\n"
    if code == "t":
        return "\t"
    needs_date = code in _DATE_ONLY or code in _BOTH or code.startswith("E") and code.endswith("Y")
    needs_time = code in _TIME_ONLY or code in _BOTH or code.startswith("E") and code[-1] in "Sf"
    if needs_date and day is None:
        raise Unsupported(f"format element %{code} on a {kind}")
    if needs_time and tod is None:
        raise Unsupported(f"format element %{code} on a {kind}")
    if code in _STAMP_ONLY:
        if stamp is None:
            raise Unsupported(f"format element %{code} on a {kind}")
        micros, tz = stamp
        if code == "s":
            return str(micros // MICROS_PER_SECOND)
        offset = V.zone_offset(micros, tz)
        sign = "-" if offset < 0 else "+"
        hours, minutes = divmod(abs(offset), 60)
        if code == "z":
            return f"{sign}{hours:02d}{minutes:02d}"
        if code == "Ez":
            return f"{sign}{hours:02d}:{minutes:02d}"
        if isinstance(tz, V.FixedZone):
            if tz.minutes == 0:
                return "UTC"
            raise Unsupported("%Z in a fixed-offset time zone")
        utc = V.micros_to_utc(micros)
        from datetime import timezone

        name = utc.replace(tzinfo=timezone.utc).astimezone(tz).tzname()
        if not name:
            raise Unsupported("%Z without a zone abbreviation")
        return name
    if code in _DATE_ONLY or code == "c" or code == "E4Y":
        d = day
    if code == "A":
        return DAY_NAMES[d.weekday()]
    if code == "a":
        return DAY_NAMES[d.weekday()][:3]
    if code == "B":
        return MONTH_NAMES[d.month - 1]
    if code in ("b", "h"):
        return MONTH_NAMES[d.month - 1][:3]
    if code == "C":
        return _two(d.year // 100)
    if code == "d":
        return _two(d.day)
    if code == "e":
        return f"{d.day:2d}"
    if code == "j":
        return f"{d.timetuple().tm_yday:03d}"
    if code == "m":
        return _two(d.month)
    if code == "Q":
        return str((d.month - 1) // 3 + 1)
    if code == "U":
        return _two(week_number(d, SUNDAY))
    if code == "W":
        return _two(week_number(d, 0))
    if code == "u":
        return str(d.isoweekday())
    if code == "w":
        return str(d.isoweekday() % 7)
    if code == "V":
        return _two(d.isocalendar()[1])
    if code == "G":
        return _year_text(d.isocalendar()[0])
    if code == "g":
        return _two(d.isocalendar()[0] % 100)
    if code == "Y":
        return _year_text(d.year)
    if code == "E4Y":
        return f"{d.year:04d}"
    if code == "y":
        return _two(d.year % 100)
    if code in ("D", "x"):
        return f"{_two(d.month)}/{_two(d.day)}/{_two(d.year % 100)}"
    if code == "F":
        return f"{_year_text(d.year)}-{_two(d.month)}-{_two(d.day)}"
    if code == "H":
        return _two(tod.hour)
    if code == "I":
        return _two((tod.hour + 11) % 12 + 1)
    if code == "k":
        return f"{tod.hour:2d}"
    if code == "l":
        return f"{(tod.hour + 11) % 12 + 1:2d}"
    if code == "M":
        return _two(tod.minute)
    if code == "S":
        return _two(tod.second)
    if code == "p":
        return "AM" if tod.hour < 12 else "PM"
    if code == "P":
        return "am" if tod.hour < 12 else "pm"
    if code == "R":
        return f"{_two(tod.hour)}:{_two(tod.minute)}"
    if code in ("T", "X"):
        return f"{_two(tod.hour)}:{_two(tod.minute)}:{_two(tod.second)}"
    if code == "c":
        return (
            f"{DAY_NAMES[d.weekday()][:3]} {MONTH_NAMES[d.month - 1][:3]} {d.day:2d} "
            f"{_two(tod.hour)}:{_two(tod.minute)}:{_two(tod.second)} {_year_text(d.year)}"
        )
    if code.startswith("E") and code[-1] in "Sf":
        digits = code[1:-1]
        if digits == "*" or not (1 <= int(digits) <= 9):
            raise Unsupported(f"format element %{code}")
        fraction = _fraction_digits(tod.microsecond, int(digits))
        if code[-1] == "f":
            return fraction
        return f"{_two(tod.second)}.{fraction}"
    raise Unsupported(f"format element %{code}")


# --- PARSE_* ---------------------------------------------------------------------------------------------------


class _Fields:
    __slots__ = ("year", "month", "day", "yday", "hour", "hour12", "pm", "minute", "second", "micro", "offset", "kinds")

    def __init__(self):
        self.year = self.month = self.day = self.yday = None
        self.hour = self.hour12 = self.pm = self.minute = self.second = self.micro = self.offset = None
        self.kinds = set()  # "date", "time", "offset"


def _failed(text: str) -> EvalError:
    return EvalError(f'Failed to parse input string "{text}"')


_FORMAT_EXPANSIONS = {"F": "%Y-%m-%d", "T": "%H:%M:%S", "R": "%H:%M", "D": "%m/%d/%y"}


def _tokens(fmt: str) -> list:
    tokens = []
    pos = 0
    expanded = []
    for match in _ELEMENT.finditer(fmt):
        expanded.append(fmt[pos : match.start()])
        pos = match.end()
        code = match.group(1)
        expanded.append(_FORMAT_EXPANSIONS.get(code) and ("\0" + _FORMAT_EXPANSIONS[code]) or "\1" + code + "\2")
    expanded.append(fmt[pos:])
    text = "".join(expanded)
    # re-tokenise: literal characters, whitespace runs and \1code\2 elements (expansions hold plain % elements)
    text = re.sub(r"\0(%[A-Za-z])(.)(%[A-Za-z])(.)?(%[A-Za-z])?", lambda m: "".join(
        "\1" + part[1:] + "\2" if part.startswith("%") else part for part in m.groups() if part), text)
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\1":
            end = text.index("\2", index)
            tokens.append(("e", text[index + 1 : end]))
            index = end + 1
        elif char.isspace():
            while index < len(text) and text[index].isspace():
                index += 1
            tokens.append(("space", None))
        else:
            tokens.append(("lit", char))
            index += 1
    if "%" in "".join(t[1] for t in tokens if t[0] == "lit"):
        raise Unsupported("a format string with a stray %")
    return tokens


def _digits(text: str, pos: int, low: int, high: int, limit: int, original: str) -> tuple[int, int]:
    end = pos
    while end < len(text) and end - pos < limit and text[end].isdigit() and text[end].isascii():
        end += 1
    if end == pos:
        raise _failed(original)
    value = int(text[pos:end])
    if value < low or value > high:
        raise _failed(original)
    return value, end


def _month_name(text: str, pos: int, full: bool, original: str) -> tuple[int, int]:
    end = pos
    while end < len(text) and text[end].isalpha() and text[end].isascii():
        end += 1
    word = text[pos:end].lower()
    names = [m.lower() for m in MONTH_NAMES]
    if word in names:
        if not full and word != "may":
            raise Unsupported("%b over a full month name")
        return names.index(word) + 1, end
    abbreviations = [n[:3] for n in names]
    if word in abbreviations:
        if full:
            raise Unsupported("%B over an abbreviated month name")
        return abbreviations.index(word) + 1, end
    raise _failed(original)


def parse_fields(fmt: str, text: str) -> _Fields:
    """The fields a format picks out of ``text``; elements outside the exact subset are ``Unsupported``."""

    fields = _Fields()
    tokens = _tokens(fmt)
    pos = 0
    while pos < len(text) and text[pos].isspace():
        pos += 1
    for kind, value in tokens:
        if kind == "space":
            while pos < len(text) and text[pos].isspace():
                pos += 1
            continue
        if kind == "lit":
            if pos < len(text) and text[pos] == value:
                pos += 1
                continue
            if pos < len(text) and text[pos].lower() == value.lower():
                raise Unsupported("a literal matched only by ignoring case")
            raise _failed(text)
        code = value
        if code == "%":
            if pos < len(text) and text[pos] == "%":
                pos += 1
                continue
            raise _failed(text)
        if code == "Y":
            fields.year, pos = _digits(text, pos, 1, 9999, 4, text)
            fields.kinds.add("date")
        elif code == "y":
            number, pos = _digits(text, pos, 0, 99, 2, text)
            fields.year = 2000 + number if number < 69 else 1900 + number
            fields.kinds.add("date")
        elif code == "m":
            fields.month, pos = _digits(text, pos, 1, 12, 2, text)
            fields.kinds.add("date")
        elif code == "d":
            fields.day, pos = _digits(text, pos, 1, 31, 2, text)
            fields.kinds.add("date")
        elif code == "e":
            if pos < len(text) and text[pos] == " ":
                pos += 1
            fields.day, pos = _digits(text, pos, 1, 31, 2, text)
            fields.kinds.add("date")
        elif code == "j":
            fields.yday, pos = _digits(text, pos, 1, 366, 3, text)
            fields.kinds.add("date")
        elif code in ("b", "h", "B"):
            fields.month, pos = _month_name(text, pos, code == "B", text)
            fields.kinds.add("date")
        elif code == "H":
            fields.hour, pos = _digits(text, pos, 0, 23, 2, text)
            fields.kinds.add("time")
        elif code == "I":
            fields.hour12, pos = _digits(text, pos, 1, 12, 2, text)
            fields.kinds.add("time")
        elif code == "M":
            fields.minute, pos = _digits(text, pos, 0, 59, 2, text)
            fields.kinds.add("time")
        elif code == "S":
            fields.second, pos = _digits(text, pos, 0, 59, 2, text)
            fields.kinds.add("time")
        elif code == "p":
            word = text[pos : pos + 2].lower()
            if word not in ("am", "pm"):
                raise _failed(text)
            fields.pm = word == "pm"
            pos += 2
            fields.kinds.add("time")
        elif code == "E*S":
            fields.second, pos = _digits(text, pos, 0, 59, 2, text)
            if pos < len(text) and text[pos] == ".":
                end = pos + 1
                while end < len(text) and text[end].isdigit() and text[end].isascii():
                    end += 1
                if end == pos + 1:
                    raise _failed(text)
                fields.micro = _micros_of_fraction(text[pos + 1 : end])
                pos = end
            fields.kinds.add("time")
        elif code in ("z", "Ez"):
            match = re.compile(r"([+-])([0-9]{2}):?([0-9]{2})" if code == "z" else r"([+-])([0-9]{2}):([0-9]{2})").match(
                text, pos
            )
            if not match or int(match.group(3)) > 59 or int(match.group(2)) > 23 or (code == "z" and ":" in match.group(0)):
                raise _failed(text)
            minutes = int(match.group(2)) * 60 + int(match.group(3))
            fields.offset = -minutes if match.group(1) == "-" else minutes
            pos = match.end()
            fields.kinds.add("offset")
        else:
            raise Unsupported(f"parse element %{code}")
    while pos < len(text) and text[pos].isspace():
        pos += 1
    if pos != len(text):
        raise _failed(text)
    return fields


def _civil_date(fields: _Fields, text: str) -> date:
    year = 1970 if fields.year is None else fields.year
    if fields.yday is not None:
        if fields.month is not None or fields.day is not None:
            raise Unsupported("%j together with a month or day")
        try:
            return date(year, 1, 1) + timedelta(days=fields.yday - 1) if fields.yday <= (
                366 if V._days_in_month(year, 2) == 29 else 365
            ) else (_ for _ in ()).throw(ValueError)
        except ValueError:
            raise _failed(text) from None
    try:
        return date(year, 1 if fields.month is None else fields.month, 1 if fields.day is None else fields.day)
    except ValueError:
        raise _failed(text) from None


def _civil_time(fields: _Fields) -> time:
    if fields.hour12 is not None:
        if fields.hour is not None or fields.pm is None:
            raise Unsupported("%I without %p, or with %H")
        hour = fields.hour12 % 12 + (12 if fields.pm else 0)
    else:
        if fields.pm is not None:
            raise Unsupported("%p without %I")
        hour = fields.hour or 0
    return time(hour, fields.minute or 0, fields.second or 0, fields.micro or 0)


def parse_date(fmt: str, text: str) -> date:
    fields = parse_fields(fmt, text)
    if fields.kinds - {"date"}:
        raise Unsupported("PARSE_DATE with time or offset elements")
    return _civil_date(fields, text)


def parse_time(fmt: str, text: str) -> time:
    fields = parse_fields(fmt, text)
    if fields.kinds - {"time"}:
        raise Unsupported("PARSE_TIME with date or offset elements")
    return _civil_time(fields)


def parse_datetime(fmt: str, text: str) -> datetime:
    fields = parse_fields(fmt, text)
    if "offset" in fields.kinds:
        raise Unsupported("PARSE_DATETIME with a time zone element")
    return datetime.combine(_civil_date(fields, text), _civil_time(fields))


def parse_timestamp(fmt: str, text: str, tz: tzinfo) -> int:
    fields = parse_fields(fmt, text)
    civil = datetime.combine(_civil_date(fields, text), _civil_time(fields))
    if fields.offset is not None:
        return V.timestamp(V.utc_to_micros(civil) - fields.offset * MICROS_PER_MINUTE)
    return V.timestamp(V.from_civil(civil, tz))


__all__ = [name for name in dir() if not name.startswith("_") and name not in ("annotations",)]
