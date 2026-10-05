"""Result types of GoogleSQL operators and functions, for :mod:`kumosql.googlesql_types`.

Each rule maps a call's argument types to its result type, or to unknown when the arguments do not fix one. The
table follows the GoogleSQL function reference (``docs/*_functions.md`` and ``docs/operators.md`` in google/googlesql
at d82db99): the "Return type" of each function and, for numeric functions, its input-to-output tables. Functions are
named as written (sqlglot keeps the written name of a call in ``meta["name"]``); a call sqlglot built itself, with no
written name, is unknown unless its node class has a single source in BigQuery syntax.

A call to a user-defined function (``Catalog.functions``) has the type the catalog gives it (unknown when it gives none),
never the type of a built-in of the same name.
"""

from __future__ import annotations

from sqlglot import exp

from .googlesql_types import (
    BOOL, BYTES, DATE, DATETIME, FLOAT64, GEOGRAPHY, INT64, INTERVAL, JSON, NUMERIC, BIGNUMERIC, STRING, TIME,
    TIMESTAMP, GType, StructField, T, UNKNOWN, NULL_LITERAL, coercible, known, supertype,
)

F64, I64 = "FLOAT64", "INT64"
_NUM = ("INT32", "INT64", "UINT32", "UINT64", "NUMERIC", "BIGNUMERIC", "FLOAT32", "FLOAT64")


def _row(*outputs: str) -> dict[str, str]:
    return {i: o for i, o in zip(_NUM, outputs) if o != "ERROR"}


# Unary numeric tables (docs/mathematical_functions.md and aggregate_functions.md), input kind -> output kind.
_TO_DOUBLE = _row(F64, F64, F64, F64, "NUMERIC", "BIGNUMERIC", F64, F64)
_SAME = {k: k for k in _NUM}
UNARY_NUMERIC = {
    "ABS": _SAME, "SIGN": _SAME,
    "CEIL": _TO_DOUBLE, "CEILING": _TO_DOUBLE, "FLOOR": _TO_DOUBLE, "ROUND": _TO_DOUBLE, "TRUNC": _TO_DOUBLE,
    "EXP": _TO_DOUBLE, "RADIANS": _TO_DOUBLE, "DEGREES": _TO_DOUBLE, "LN": _TO_DOUBLE, "LOG10": _TO_DOUBLE, "SQRT": _TO_DOUBLE,
    "SAFE_NEGATE": _row("INT32", "INT64", "ERROR", "ERROR", "NUMERIC", "BIGNUMERIC", "FLOAT32", F64),
    "AVG": {**_TO_DOUBLE, "INTERVAL": "INTERVAL"},
    "ARRAY_AVG": {**_TO_DOUBLE, "INTERVAL": "INTERVAL"},
    "SUM": {**_row("INT64", "INT64", "UINT64", "UINT64", "NUMERIC", "BIGNUMERIC", F64, F64), "INTERVAL": "INTERVAL"},
    "ARRAY_SUM": {**_row("INT64", "INT64", "UINT64", "UINT64", "NUMERIC", "BIGNUMERIC", F64, F64), "INTERVAL": "INTERVAL"},
}


def _table(rows: dict[str, tuple[str, ...]], columns=_NUM) -> dict[tuple[str, str], str]:
    out = {}
    for left, outputs in rows.items():
        for right, result in zip(columns, outputs):
            if result != "ERROR":
                out[(left, right)] = result
    return out


_ADD_ROWS = {
    "INT32": ("INT64", "INT64", "INT64", "ERROR", "NUMERIC", "BIGNUMERIC", F64, F64),
    "INT64": ("INT64", "INT64", "INT64", "ERROR", "NUMERIC", "BIGNUMERIC", F64, F64),
    "UINT32": ("INT64", "INT64", "UINT64", "UINT64", "NUMERIC", "BIGNUMERIC", F64, F64),
    "UINT64": ("ERROR", "ERROR", "UINT64", "UINT64", "NUMERIC", "BIGNUMERIC", F64, F64),
    "NUMERIC": ("NUMERIC", "NUMERIC", "NUMERIC", "NUMERIC", "NUMERIC", "BIGNUMERIC", F64, F64),
    "BIGNUMERIC": ("BIGNUMERIC",) * 6 + (F64, F64),
    "FLOAT32": (F64,) * 8,
    "FLOAT64": (F64,) * 8,
}
_SUB_ROWS = dict(_ADD_ROWS, UINT32=("INT64", "INT64", "INT64", "INT64", "NUMERIC", "BIGNUMERIC", F64, F64),
                 UINT64=("ERROR", "ERROR", "INT64", "INT64", "NUMERIC", "BIGNUMERIC", F64, F64))
_DIVIDE_ROWS = {k: tuple(F64 if r in ("INT64", "UINT64", "ERROR") and k not in ("NUMERIC", "BIGNUMERIC") else r
                         for r in v) for k, v in _ADD_ROWS.items()}
_DIVIDE_ROWS["NUMERIC"] = ("NUMERIC",) * 5 + ("BIGNUMERIC", F64, F64)
_DIVIDE_ROWS["UINT64"] = (F64, F64, F64, F64, "NUMERIC", "BIGNUMERIC", F64, F64)
_INT_DIV_ROWS = {
    "INT32": ("INT64", "INT64", "INT64", "ERROR", "NUMERIC", "BIGNUMERIC"),
    "INT64": ("INT64", "INT64", "INT64", "ERROR", "NUMERIC", "BIGNUMERIC"),
    "UINT32": ("INT64", "INT64", "UINT64", "UINT64", "NUMERIC", "BIGNUMERIC"),
    "UINT64": ("ERROR", "ERROR", "UINT64", "UINT64", "NUMERIC", "BIGNUMERIC"),
    "NUMERIC": ("NUMERIC",) * 5 + ("BIGNUMERIC",),
    "BIGNUMERIC": ("BIGNUMERIC",) * 6,
}
_POW_ROWS = {k: (F64, F64, F64, F64, "NUMERIC", "BIGNUMERIC", F64, F64) for k in ("INT32", "INT64", "UINT32", "UINT64")}
_POW_ROWS.update({"NUMERIC": ("NUMERIC",) * 5 + ("BIGNUMERIC", F64, F64), "BIGNUMERIC": ("BIGNUMERIC",) * 6 + (F64, F64),
                  "FLOAT32": (F64,) * 8, "FLOAT64": (F64,) * 8})
BINARY_NUMERIC = {
    "+": _table(_ADD_ROWS), "SAFE_ADD": _table(_ADD_ROWS),
    "*": _table(_ADD_ROWS), "SAFE_MULTIPLY": _table(_ADD_ROWS),
    "-": _table(_SUB_ROWS), "SAFE_SUBTRACT": _table(_SUB_ROWS),
    "/": _table(_DIVIDE_ROWS), "SAFE_DIVIDE": _table(_DIVIDE_ROWS),
    "DIV": _table(_INT_DIV_ROWS, _NUM[:6]), "MOD": _table(_INT_DIV_ROWS, _NUM[:6]),
    "POW": _table(_POW_ROWS), "POWER": _table(_POW_ROWS), "LOG": _table(_POW_ROWS),
}

# Functions with one result type whatever their (valid) arguments.
FIXED: dict[str, GType] = {}


def _fixed(t: GType, *names: str) -> None:
    for name in names:
        FIXED[name] = t


_fixed(INT64,
       "COUNT", "COUNTIF", "COUNT_IF", "APPROX_COUNT_DISTINCT", "ROW_NUMBER", "RANK", "DENSE_RANK", "NTILE",
       "GROUPING", "LENGTH", "CHAR_LENGTH", "CHARACTER_LENGTH", "BYTE_LENGTH", "OCTET_LENGTH", "STRPOS", "INSTR",
       "ASCII", "UNICODE", "REGEXP_INSTR", "EDIT_DISTANCE", "FARM_FINGERPRINT", "ARRAY_LENGTH", "BIT_COUNT",
       "DATE_DIFF", "DATETIME_DIFF", "TIMESTAMP_DIFF", "TIME_DIFF", "UNIX_DATE", "UNIX_SECONDS", "UNIX_MILLIS",
       "UNIX_MICROS", "RANGE_BUCKET", "ARRAY_OFFSET", "HLL_COUNT.EXTRACT", "HLL_COUNT.MERGE", "S2_CELLIDFROMPOINT",
       "ST_DIMENSION", "ST_NUMGEOMETRIES", "ST_NUMPOINTS", "ST_CLUSTERDBSCAN", "NET.IPV4_TO_INT64",
       "LAX_INT64", "BIT_CAST_TO_INT64", "KEYS.KEYSET_LENGTH")
_fixed(FLOAT64,
       "PERCENT_RANK", "CUME_DIST", "CORR", "COVAR_POP", "COVAR_SAMP", "STDDEV", "STDDEV_POP", "STDDEV_SAMP",
       "VARIANCE", "VAR_POP", "VAR_SAMP", "IEEE_DIVIDE", "RAND", "PI", "COS", "SIN", "TAN", "ACOS", "ASIN", "ATAN",
       "ATAN2", "COSH", "SINH", "TANH", "ACOSH", "ASINH", "ATANH", "COT", "COTH", "CSC", "CSCH", "SEC", "SECH", "CBRT",
       "COSINE_DISTANCE", "EUCLIDEAN_DISTANCE", "MONTHS_BETWEEN", "ST_AREA", "ST_DISTANCE", "ST_LENGTH",
       "ST_PERIMETER", "ST_MAXDISTANCE", "ST_X", "ST_Y", "ST_AZIMUTH", "ST_ANGLE", "ST_LINELOCATEPOINT",
       "ST_HAUSDORFFDISTANCE", "LAX_DOUBLE", "FLOAT64", "DOUBLE", "LAX_FLOAT64")
_fixed(BOOL,
       "LOGICAL_AND", "LOGICAL_OR", "STARTS_WITH", "ENDS_WITH", "REGEXP_CONTAINS", "CONTAINS_SUBSTR", "IS_INF",
       "IS_NAN", "ARRAY_INCLUDES", "ARRAY_INCLUDES_ALL", "ARRAY_INCLUDES_ANY", "ARRAY_IS_DISTINCT", "RANGE_CONTAINS",
       "RANGE_OVERLAPS", "RANGE_IS_START_UNBOUNDED", "RANGE_IS_END_UNBOUNDED", "IS_FIRST", "IS_LAST", "ISERROR",
       "JSON_CONTAINS", "LAX_BOOL", "NET.IP_IN_NET", "ST_CONTAINS", "ST_COVEREDBY", "ST_COVERS", "ST_DISJOINT",
       "ST_DWITHIN", "ST_EQUALS", "ST_INTERSECTS", "ST_INTERSECTSBOX", "ST_ISCLOSED", "ST_ISCOLLECTION",
       "ST_ISEMPTY", "ST_ISRING", "ST_TOUCHES", "ST_WITHIN", "ST_HAUSDORFFDWITHIN")
_fixed(STRING,
       "FORMAT", "TO_HEX", "TO_BASE64", "TO_BASE32", "INITCAP", "SOUNDEX", "CHR", "CODE_POINTS_TO_STRING",
       "SAFE_CONVERT_BYTES_TO_STRING", "NORMALIZE", "NORMALIZE_AND_CASEFOLD", "SPLIT_SUBSTR", "COLLATE",
       "GENERATE_UUID", "SESSION_USER", "TYPEOF", "FORMAT_DATE", "FORMAT_DATETIME", "FORMAT_TIME", "FORMAT_TIMESTAMP",
       "JSON_EXTRACT_SCALAR", "JSON_VALUE", "TO_JSON_STRING", "JSON_TYPE", "LAX_STRING", "NET.HOST",
       "NET.PUBLIC_SUFFIX", "NET.REG_DOMAIN", "NET.IP_TO_STRING", "NET.MAKE_NET", "ST_ASGEOJSON", "ST_ASKML",
       "ST_ASTEXT", "ST_GEOHASH", "ST_GEOMETRYTYPE")
_fixed(BYTES,
       "FROM_HEX", "FROM_BASE64", "FROM_BASE32", "CODE_POINTS_TO_BYTES", "MD5", "SHA1", "SHA256", "SHA512",
       "HLL_COUNT.INIT", "HLL_COUNT.MERGE_PARTIAL", "NET.IPV4_FROM_INT64", "NET.IP_FROM_STRING",
       "NET.SAFE_IP_FROM_STRING", "NET.IP_NET_MASK", "NET.IP_TRUNC", "ST_ASBINARY", "KEYS.NEW_KEYSET",
       "KEYS.ADD_KEY_FROM_RAW_BYTES", "KEYS.ROTATE_KEYSET", "KEYS.KEYSET_CHAIN", "KEYS.KEYSET_FROM_JSON",
       "AEAD.ENCRYPT", "AEAD.DECRYPT_BYTES", "DETERMINISTIC_ENCRYPT", "DETERMINISTIC_DECRYPT_BYTES",
       "HIGHWAY_FINGERPRINT128")
_fixed(DATE,
       "CURRENT_DATE", "DATE", "DATE_FROM_UNIX_DATE", "PARSE_DATE", "LAST_DAY", "NEXT_DAY")
_fixed(DATETIME, "CURRENT_DATETIME", "DATETIME", "PARSE_DATETIME")
_fixed(TIME, "CURRENT_TIME", "TIME", "PARSE_TIME")
_fixed(TIMESTAMP,
       "CURRENT_TIMESTAMP", "TIMESTAMP", "PARSE_TIMESTAMP", "TIMESTAMP_SECONDS", "TIMESTAMP_MILLIS",
       "TIMESTAMP_MICROS", "TIMESTAMP_FROM_UNIX_SECONDS", "TIMESTAMP_FROM_UNIX_MILLIS",
       "TIMESTAMP_FROM_UNIX_MICROS")
_fixed(INTERVAL, "MAKE_INTERVAL", "JUSTIFY_DAYS", "JUSTIFY_HOURS", "JUSTIFY_INTERVAL")
_fixed(JSON,
       "TO_JSON", "SAFE_TO_JSON", "PARSE_JSON", "JSON_ARRAY", "JSON_OBJECT", "JSON_SET", "JSON_REMOVE",
       "JSON_STRIP_NULLS", "JSON_ARRAY_APPEND", "JSON_ARRAY_INSERT")
_fixed(NUMERIC, "PARSE_NUMERIC", "PI_NUMERIC")
_fixed(BIGNUMERIC, "PARSE_BIGNUMERIC", "PI_BIGNUMERIC")
_fixed(GEOGRAPHY,
       "ST_GEOGPOINT", "ST_GEOGFROM", "ST_GEOGFROMTEXT", "ST_GEOGFROMGEOJSON", "ST_GEOGFROMWKB",
       "ST_GEOGPOINTFROMGEOHASH", "ST_MAKELINE", "ST_MAKEPOLYGON", "ST_MAKEPOLYGONORIENTED", "ST_BOUNDARY",
       "ST_BUFFER", "ST_BUFFERWITHTOLERANCE", "ST_CENTROID", "ST_CLOSESTPOINT", "ST_CONVEXHULL", "ST_DIFFERENCE",
       "ST_ENDPOINT", "ST_STARTPOINT", "ST_POINTN", "ST_EXTERIORRING", "ST_INTERSECTION", "ST_SIMPLIFY",
       "ST_SNAPTOGRID", "ST_UNION", "ST_UNION_AGG", "ST_LINEINTERPOLATEPOINT", "ST_LINESUBSTRING")
_fixed(GType.array(STRING), "JSON_EXTRACT_STRING_ARRAY", "JSON_VALUE_ARRAY", "JSON_KEYS", "LAX_STRING_ARRAY")
_fixed(GType.array(INT64), "TO_CODE_POINTS", "ARRAY_OFFSETS", "S2_COVERINGCELLIDS", "LAX_INT64_ARRAY")
_fixed(GType.array(JSON), "JSON_FLATTEN")
_fixed(GType.array(DATE), "GENERATE_DATE_ARRAY")
_fixed(GType.array(TIMESTAMP), "GENERATE_TIMESTAMP_ARRAY")
_fixed(GType.array(BOOL), "LAX_BOOL_ARRAY", "BOOL_ARRAY")
_fixed(GType.array(FLOAT64), "LAX_DOUBLE_ARRAY", "DOUBLE_ARRAY", "FLOAT64_ARRAY", "LAX_FLOAT64_ARRAY")
_fixed(GType.array(GEOGRAPHY), "ST_DUMP", "ST_DUMPPOINTS", "ST_INTERIORRINGS")
_fixed(GType.struct([("xmin", FLOAT64), ("ymin", FLOAT64), ("xmax", FLOAT64), ("ymax", FLOAT64)]),
       "ST_BOUNDINGBOX", "ST_EXTENT")
# KLL sketches (kll_functions.md): INIT and MERGE_PARTIAL give the sketch, MERGE and EXTRACT the quantiles or one point.
for _kind, _type in (("INT64", INT64), ("DOUBLE", FLOAT64), ("FLOAT64", FLOAT64)):
    FIXED[f"KLL_QUANTILES.INIT_{_kind}"] = BYTES
    for _verb in ("MERGE", "EXTRACT"):
        FIXED[f"KLL_QUANTILES.{_verb}_{_kind}"] = GType.array(_type)
        FIXED[f"KLL_QUANTILES.{_verb}_POINT_{_kind}"] = _type
FIXED["KLL_QUANTILES.INIT_UINT64"] = BYTES
FIXED["KLL_QUANTILES.MERGE_PARTIAL"] = BYTES
for _verb in ("MERGE", "EXTRACT"):
    FIXED[f"KLL_QUANTILES.{_verb}_UINT64"] = GType.array(GType("UINT64"))
    FIXED[f"KLL_QUANTILES.{_verb}_POINT_UINT64"] = GType("UINT64")
    for _kind in ("INT64", "UINT64", "DOUBLE", "FLOAT64"):
        FIXED[f"KLL_QUANTILES.{_verb}_RELATIVE_RANK_{_kind}"] = FLOAT64
_fixed(BOOL, "AI.IF", "REGEXP_MATCH")
_fixed(FLOAT64, "AI.SCORE")
_fixed(STRING, "AEAD.DECRYPT_STRING", "DETERMINISTIC_DECRYPT_STRING", "ZSTD_DECOMPRESS_TO_STRING")
_fixed(BYTES, "BIT_CAST_TO_BYTES", "ZSTD_COMPRESS", "ZSTD_DECOMPRESS_TO_BYTES")
# GoogleSQL-only types, exact as documented (json_functions.md, bit_functions.md).
for _name, _type in (("INT32", "INT32"), ("UINT32", "UINT32"), ("UINT64", "UINT64"), ("FLOAT", "FLOAT32"),
                     ("LAX_INT32", "INT32"), ("LAX_UINT32", "UINT32"), ("LAX_UINT64", "UINT64"),
                     ("LAX_FLOAT", "FLOAT32"), ("BIT_CAST_TO_INT32", "INT32"), ("BIT_CAST_TO_UINT32", "UINT32"),
                     ("BIT_CAST_TO_UINT64", "UINT64")):
    FIXED[_name] = GType(_type)
for _name, _type in (("INT32_ARRAY", "INT32"), ("UINT32_ARRAY", "UINT32"), ("UINT64_ARRAY", "UINT64"),
                     ("FLOAT_ARRAY", "FLOAT32"), ("LAX_INT32_ARRAY", "INT32"), ("LAX_UINT32_ARRAY", "UINT32"),
                     ("LAX_UINT64_ARRAY", "UINT64"), ("LAX_FLOAT_ARRAY", "FLOAT32"), ("INT64_ARRAY", "INT64"),
                     ("STRING_ARRAY", "STRING")):
    FIXED[_name] = GType.array(GType(_type))

# Functions whose result has the type of their first argument (string or bytes ones are checked below).
SAME_AS_FIRST = {
    "MIN", "MAX", "ANY_VALUE", "LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE", "NTH_VALUE", "PERCENTILE_DISC",
    "ARRAY_CONCAT_AGG", "ARRAY_REVERSE", "ARRAY_SLICE", "ARRAY_FILTER", "ARRAY_FIND_ALL", "NULLIFZERO", "ZEROIFNULL",
    "NULLIFERROR", "BIT_AND", "BIT_OR", "BIT_XOR", "RANGE_INTERSECT", "HAVING_MAX", "HAVING_MIN",
}
ELEMENT_OF_FIRST = {"ARRAY_FIRST", "ARRAY_LAST", "ARRAY_MIN", "ARRAY_MAX", "ARRAY_FIND", "RANGE_START", "RANGE_END",
                    "OFFSET", "ORDINAL", "SAFE_OFFSET", "SAFE_ORDINAL"}
ARRAY_OF_FIRST = {"ARRAY_AGG", "APPROX_QUANTILES"}
# STRING in, STRING out; BYTES in, BYTES out (the first argument decides).
STRING_OR_BYTES = {
    "LOWER", "UPPER", "LCASE", "UCASE", "LTRIM", "RTRIM", "TRIM", "LPAD", "RPAD", "LEFT", "RIGHT", "REPEAT", "REPLACE", "REVERSE",
    "SUBSTR", "SUBSTRING", "TRANSLATE", "REGEXP_EXTRACT", "REGEXP_REPLACE", "REGEXP_SUBSTR", "STRING_AGG",
}
ARRAY_OF_STRING_OR_BYTES = {"SPLIT", "REGEXP_EXTRACT_ALL"}
SUPERTYPE_OF_ALL = {"COALESCE", "IFNULL", "GREATEST", "LEAST", "IFERROR", "NULLIF"}
# Functions that keep the argument's type only for some input types.
DATE_PART_FUNCTIONS = {
    "DATE_ADD": {"DATE", "DATETIME"}, "DATE_SUB": {"DATE", "DATETIME"},
    "DATETIME_ADD": {"DATETIME", "TIMESTAMP"}, "DATETIME_SUB": {"DATETIME", "TIMESTAMP"},
    "TIMESTAMP_ADD": {"TIMESTAMP"}, "TIMESTAMP_SUB": {"TIMESTAMP"}, "TIME_ADD": {"TIME"}, "TIME_SUB": {"TIME"},
    "DATE_TRUNC": {"DATE", "DATETIME", "TIMESTAMP"}, "DATETIME_TRUNC": {"DATETIME", "DATE", "TIMESTAMP"},
    "TIMESTAMP_TRUNC": {"TIMESTAMP", "DATE", "DATETIME"}, "TIME_TRUNC": {"TIME"},
    "ADD_MONTHS": {"DATE", "DATETIME"},
    # The bucket functions keep their input's type (DATE_BUCKET of a DATETIME is a DATETIME).
    "DATE_BUCKET": {"DATE", "DATETIME", "TIMESTAMP"}, "DATETIME_BUCKET": {"DATE", "DATETIME", "TIMESTAMP"},
    "TIMESTAMP_BUCKET": {"DATE", "DATETIME", "TIMESTAMP"},
}
# Date-part functions whose first argument is a string literal or NULL (it coerces to the function's home type):
# name -> (home type, literal kinds that take it). NULL is left out where the function has several overloads.
LITERAL_HOME = {
    "ADD_MONTHS": (DATE, {"string"}), "TIMESTAMP_TRUNC": (TIMESTAMP, {"string"}),
    "DATE_BUCKET": (DATE, {"string", "null"}), "DATETIME_BUCKET": (DATETIME, {"string", "null"}),
    "TIMESTAMP_BUCKET": (TIMESTAMP, {"string", "null"}),
}

# Node classes sqlglot builds from special syntax (no written function name), each with a single BigQuery source.
CLASS_NAMES = {
    "If": "IF", "Case": "CASE", "Extract": "EXTRACT", "GroupConcat": "STRING_AGG", "Ceil": "CEIL", "Floor": "FLOOR",
    "Substring": "SUBSTR", "Translate": "TRANSLATE", "Chr": "CHR", "JSONArray": "JSON_ARRAY",
    "JSONObject": "JSON_OBJECT", "MakeInterval": "MAKE_INTERVAL", "Collate": "COLLATE",
    "CurrentDate": "CURRENT_DATE", "CurrentDatetime": "CURRENT_DATETIME", "CurrentTime": "CURRENT_TIME",
    "CurrentTimestamp": "CURRENT_TIMESTAMP", "Trim": "TRIM",
    "StrToDate": "PARSE_DATE",  # a nameless one is CAST(.. AS DATE FORMAT ..); PARSE_DATE keeps its written name
}


class Call:
    """A function call: its name, the nodes of its arguments in call order where sqlglot keeps it, and their types."""

    def __init__(self, typer, node, name, scope, ctes):
        self.typer, self.node, self.name, self.scope, self.ctes = typer, node, name, scope, ctes
        self.args = _arguments(node)
        self.ts = [typer.expr(a, scope, ctes) for a in self.args]

    def first(self) -> T:
        return self.ts[0] if self.ts else UNKNOWN


_MODIFIERS = (exp.Distinct, exp.Order, exp.Limit, exp.IgnoreNulls, exp.RespectNulls, exp.HavingMax)


def _value(node):
    """The value argument inside aggregate modifiers (DISTINCT, ORDER BY, LIMIT, IGNORE NULLS, HAVING MAX)."""

    while isinstance(node, _MODIFIERS):
        if isinstance(node, exp.Distinct):
            return node.expressions[0] if len(node.expressions) == 1 else None
        node = node.this
    return node


def _arguments(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.Anonymous):
        return [_value(e) for e in node.expressions if _value(e) is not None]
    out = []
    first = node.args.get("this")
    if isinstance(first, exp.Expression):
        value = _value(first)
        if value is not None:
            out.append(value)
    for key, value in node.args.items():
        if key == "this":
            continue
        values = value if isinstance(value, list) else [value]
        for v in values:
            if isinstance(v, exp.Expression) and not isinstance(v, (exp.Var, exp.JSONPath)):
                out.append(_value(v) if _value(v) is not None else v)
    return out


def function_name(node: exp.Expression) -> str | None:
    meta = node.meta if node._meta is not None else {}  # noqa: SLF001 - reading only
    name = meta.get("name") if isinstance(meta, dict) else None
    if name:
        return str(name).upper()
    if isinstance(node, exp.Anonymous) and isinstance(node.this, str):
        return node.this.upper()
    return CLASS_NAMES.get(type(node).__name__)


# --- dispatch --------------------------------------------------------------------------------------------------------

_COMPARISONS = (
    exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NullSafeEQ, exp.NullSafeNEQ, exp.Is, exp.Like, exp.ILike,
    exp.In, exp.Between, exp.And, exp.Or, exp.Not, exp.Xor, exp.SimilarTo,
)
_ARITHMETIC = {exp.Add: "+", exp.Sub: "-", exp.Mul: "*", exp.Div: "/", exp.IntDiv: "DIV", exp.Mod: "MOD"}
_BITWISE = (exp.BitwiseAnd, exp.BitwiseOr, exp.BitwiseXor, exp.BitwiseLeftShift, exp.BitwiseRightShift)


# sqlglot before 30 has no SafeFunc / NetFunc: it reads SAFE.f(x) and NET.f(x) as a Dot, which type_call handles below.
_SAFE_FUNC = getattr(exp, "SafeFunc", ())
_NET_FUNC = getattr(exp, "NetFunc", ())


def _is_json_literal(node: exp.ParseJSON) -> bool:
    """``JSON '...'``: sqlglot 30 flags it ``is_literal``; sqlglot 26 gives a ParseJSON of a string with no written name."""

    return bool(node.args.get("is_literal")) or (
        node._meta is None and isinstance(node.this, exp.Literal) and node.this.is_string and len(node.args) == 1  # noqa: SLF001
    )


def type_call(typer, node: exp.Expression, scope, ctes) -> T:
    if isinstance(node, _COMPARISONS):
        _visit_children(typer, node, scope, ctes)
        return T(BOOL)
    if isinstance(node, exp.Exists):
        return T(BOOL)
    if isinstance(node, exp.ParseJSON) and _is_json_literal(node):
        return T(JSON)
    if isinstance(node, (exp.ByteString,)):
        return T(BYTES, "bytes")
    if isinstance(node, exp.RawString):
        return T(STRING, "string")
    if isinstance(node, exp.HexString):
        return T(INT64, "int") if node.args.get("is_integer") else UNKNOWN
    if isinstance(node, exp.Neg):
        return _negate(typer.expr(node.this, scope, ctes))
    if type(node) in _ARITHMETIC:
        left, right = typer.expr(node.this, scope, ctes), typer.expr(node.expression, scope, ctes)
        return arithmetic(_ARITHMETIC[type(node)], left, right)
    if isinstance(node, exp.SafeDivide):
        left, right = typer.expr(node.this, scope, ctes), typer.expr(node.expression, scope, ctes)
        return binary_numeric("SAFE_DIVIDE", left, right)
    if isinstance(node, exp.BitwiseNot):
        return _bitwise(typer.expr(node.this, scope, ctes))
    if isinstance(node, _BITWISE):
        left = typer.expr(node.this, scope, ctes)
        typer.expr(node.expression, scope, ctes)
        if isinstance(node, (exp.BitwiseLeftShift, exp.BitwiseRightShift)):
            return _bitwise(left)
        right = typer.types.get(id(node.expression), (None, None))[1]
        return _bitwise_pair(left, T(right) if right else UNKNOWN)
    if isinstance(node, exp.DPipe):
        return concat([typer.expr(node.this, scope, ctes), typer.expr(node.expression, scope, ctes)])
    if isinstance(node, exp.Case):
        return case(typer, node, scope, ctes)
    if isinstance(node, exp.If):
        return if_(typer, node, scope, ctes)
    if isinstance(node, _SAFE_FUNC):
        return typer.expr(node.this, scope, ctes)
    if isinstance(node, _NET_FUNC):
        inner = node.this
        name = function_name(inner)
        if name is None:
            _visit_children(typer, node, scope, ctes)
            return UNKNOWN
        return _named(typer, inner, "NET." + name, scope, ctes)
    if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Func) and _namespace(node.this):
        name = function_name(node.expression)
        if name is None:
            return UNKNOWN
        path = _namespace(node.this)
        if path[0] == "SAFE":  # SAFE.fn(..) has the type of fn(..); only the error becomes NULL
            path = path[1:]
        return _named(typer, node.expression, ".".join(path + [name]), scope, ctes)
    if isinstance(node, exp.Anonymous) and node.this == "__KUMO_WITH__" and len(node.expressions) >= 2:
        return with_expression(typer, node, scope, ctes)
    if isinstance(node, exp.Extract):
        return extract(typer, node, scope, ctes)
    if isinstance(node, exp.StrToTime) and not (node.meta if node._meta is not None else {}).get("name"):  # noqa: SLF001
        _visit_children(typer, node, scope, ctes)  # CAST(x AS TIMESTAMP | DATETIME | TIME FORMAT ..)
        return known(typer.format_cast_type(node))
    if isinstance(node, exp.Flatten) and isinstance(node.this, exp.Expression) and not node.args.get("expression"):
        flat = typer.array_path(node.this, scope, ctes)  # FLATTEN(arr.field): the array path, flattened
        if flat.type is not None and flat.type.kind == "ARRAY" and flat.type.element is not None and \
                flat.type.element.kind != "ARRAY":
            return T(flat.type)
        return UNKNOWN
    if isinstance(node, exp.Identifier):
        return _lambda_parameter(typer, node, scope)
    if isinstance(node, exp.Var):
        return typer.with_variable(node.name)  # a WITH expression's variable (bigquery_syntax turns its uses into Var)
    if isinstance(node, (exp.Star, exp.Placeholder, exp.Parameter, exp.JSONPath, exp.Lambda)):
        return UNKNOWN
    if not isinstance(node, exp.Func):
        _visit_children(typer, node, scope, ctes)
        return UNKNOWN
    name = function_name(node)
    if name is None:
        _visit_children(typer, node, scope, ctes)
        return UNKNOWN
    return _named(typer, node, name, scope, ctes)


def with_expression(typer, node: exp.Anonymous, scope, ctes) -> T:
    """``WITH(a AS e1, b AS e2, result)`` (written by bigquery_syntax as ``__KUMO_WITH__(__KUMO_WITH_VARIABLE__('a', e1),
    .., result)``): each variable has the type of its expression, seen by the ones after it and by the result, and the
    expression has the type of the result."""

    *definitions, result = node.expressions
    env: dict[str, T] = {}
    typer.with_variables.append(env)
    try:
        for definition in definitions:
            if not (isinstance(definition, exp.Anonymous) and definition.this == "__KUMO_WITH_VARIABLE__"
                    and len(definition.expressions) == 2 and definition.expressions[0].is_string):
                return UNKNOWN
            value = typer.expr(definition.expressions[1], scope, ctes)
            env[definition.expressions[0].this.strip("`").lower()] = _plain(value)
        return _plain(typer.expr(result, scope, ctes))
    finally:
        typer.with_variables.pop()


def _namespace(node) -> list[str] | None:
    """The upper-case names of a function's namespace written as ``A`` or ``A.B``; None for anything else."""

    if isinstance(node, exp.Identifier):
        return [node.name.upper()]
    if isinstance(node, exp.Dot) and isinstance(node.this, exp.Identifier) and isinstance(node.expression, exp.Identifier):
        return [node.this.name.upper(), node.expression.name.upper()]
    return None


def _lambda_parameter(typer, node: exp.Identifier, scope) -> T:
    """sqlglot leaves a lambda parameter used in the body as a bare Identifier (not a Column). Resolve it only when it
    names a value range (the parameter itself) in scope; anything else stays unknown and raises no finding."""

    s = scope
    while s is not None:
        kind, target = s.lookup(node.name)
        if kind == "none":
            s = s.parent
            continue
        if kind == "range" and target.value is not None and target.node is None:
            return target.value
        return UNKNOWN
    return UNKNOWN


def _visit_children(typer, node, scope, ctes) -> None:
    for child in node.iter_expressions():
        if isinstance(child, exp.Query):
            typer.subquery_rel(child, scope, ctes)
        elif isinstance(child, exp.Unnest):
            for e in child.expressions:
                typer.expr(e, scope, ctes)
        else:
            typer.expr(child, scope, ctes)


def _named(typer, node, name: str, scope, ctes) -> T:
    if name.lower() in typer.catalog.functions or name.split(".")[-1].lower() in typer.catalog.functions:
        _visit_children(typer, node, scope, ctes)
        return known(typer.catalog.function_type(name))  # a user-defined function: its declared (or body) type
    if name in ("ARRAY_TRANSFORM",) or (name in ("ARRAY_FILTER",) and _has_lambda(node)):
        return lambda_call(typer, node, name, scope, ctes)
    if name == "ARRAY_ZIP" and isinstance(node, exp.Anonymous):
        return array_zip_call(typer, node, scope, ctes)
    if name in ARRAY_LAMBDA_RESULTS and _has_lambda(node):
        return array_lambda_call(typer, node, name, scope, ctes)
    if _has_lambda(node):
        _visit_children(typer, node, scope, ctes)
        return UNKNOWN
    call = Call(typer, node, name, scope, ctes)
    rule = RULES.get(name)
    if rule is not None:
        return rule(call)
    if name in FIXED:
        return T(FIXED[name])
    if name in UNARY_NUMERIC:
        return unary_numeric(name, call.first())
    if name in BINARY_NUMERIC:
        if len(call.ts) == 1 and name == "LOG":
            return unary_numeric("LN", call.first())
        if len(call.ts) != 2:
            return UNKNOWN
        return binary_numeric(name, call.ts[0], call.ts[1])
    if name in SAME_AS_FIRST:
        first = call.first()
        if first.lit == "null":
            return UNKNOWN
        return _plain(first)
    if name in ELEMENT_OF_FIRST:
        first = call.first().type
        if first is None or first.kind not in ("ARRAY", "RANGE"):
            return UNKNOWN
        return known(first.element)
    if name == "ARRAY_AGG" and call.first().lit == "null":
        return T(GType.array(INT64))  # an untyped NULL is INT64
    if name in ARRAY_OF_FIRST:
        first = call.first()
        if first.type is None or first.lit == "null" or (first.type.kind == "ARRAY" and name != "ARRAY_AGG"):
            return UNKNOWN  # ARRAY_AGG of arrays is an array of arrays (where the feature is on; else an error)
        return T(GType.array(first.type))
    if name in STRING_OR_BYTES:
        return string_or_bytes(call.first(), call.ts[1:])
    if name in ARRAY_OF_STRING_OR_BYTES:
        inner = string_or_bytes(call.first(), call.ts[1:])
        return T(GType.array(inner.type)) if inner.type is not None else UNKNOWN
    if name in SUPERTYPE_OF_ALL:
        result = supertype(call.ts)
        if result is None:
            return UNKNOWN
        return T(result.type) if result.lit != "null" else T(INT64)
    if name in DATE_PART_FUNCTIONS:
        first = call.first()
        if first.lit is None and first.type is not None and first.type.kind in DATE_PART_FUNCTIONS[name]:
            return T(first.type)
        home = LITERAL_HOME.get(name)
        if home is not None and first.lit in home[1]:
            return T(home[0])
        return UNKNOWN
    return UNKNOWN


def _has_lambda(node) -> bool:
    return any(isinstance(child, exp.Lambda) for child in node.iter_expressions())


def _plain(t: T) -> T:
    if t.lit is None:
        return t
    return T(t.type)


# --- rules -----------------------------------------------------------------------------------------------------------

def _numeric_kind(t: T) -> str | None:
    if t.type is None or t.lit == "null":
        return None
    kind = t.type.kind
    return kind if kind in _NUM or kind == "INTERVAL" else None


def unary_numeric(name: str, t: T) -> T:
    kind = _numeric_kind(t)
    if kind is None:
        return UNKNOWN
    out = UNARY_NUMERIC[name].get(kind)
    return T(GType(out)) if out else UNKNOWN


def binary_numeric(name: str, left: T, right: T) -> T:
    a, b = _numeric_kind(left), _numeric_kind(right)
    if a is None or b is None or a == "INTERVAL" or b == "INTERVAL":
        return UNKNOWN
    if left.lit in ("int", "float") or right.lit in ("int", "float"):
        lit_result = _literal_arithmetic(name, left, right)
        if lit_result is not None:
            return lit_result
        return UNKNOWN
    out = BINARY_NUMERIC[name].get((a, b))
    return T(GType(out)) if out else UNKNOWN


def _literal_arithmetic(name: str, left: T, right: T) -> T | None:
    """Arithmetic with a numeric literal: the literal takes the other operand's type when it coerces to it."""

    table = BINARY_NUMERIC[name]
    if left.lit and right.lit:
        out = table.get((left.type.kind, right.type.kind))
        return T(GType(out)) if out else None
    lit, other = (left, right) if left.lit else (right, left)
    kind = other.type.kind
    if lit.lit == "int" and kind in ("INT64", "NUMERIC", "BIGNUMERIC", "FLOAT64"):
        out = table.get((kind, kind))
        return T(GType(out)) if out else None
    if lit.lit == "int" and kind == "INT32":
        out = table.get(("INT64", "INT64"))
        return T(GType(out)) if out else None
    if lit.lit == "float" and kind in ("FLOAT64", "INT64"):
        out = table.get((F64, F64))
        return T(GType(out)) if out else None
    if lit.lit in ("int", "float") and kind == "FLOAT32":
        # the literal is a FLOAT32 or a FLOAT64 (a FLOAT32 operand coerces to FLOAT64): FLOAT64 either way
        out = table.get(("FLOAT32", "FLOAT32"))
        return T(GType(out)) if out and out == table.get(("FLOAT32", F64)) == table.get((F64, "FLOAT32")) else None
    return None


def arithmetic(op: str, left: T, right: T) -> T:
    """``+ - * / DIV MOD`` on numbers, dates and intervals (docs/operators.md)."""

    a = left.type.kind if left.type is not None else None
    b = right.type.kind if right.type is not None else None
    if a is None or b is None:
        return UNKNOWN
    if left.lit == "null" or right.lit == "null":
        return _null_arithmetic(op, left, right)
    temporal = {"DATE", "DATETIME", "TIMESTAMP", "TIME", "INTERVAL"}
    if a in temporal or b in temporal:
        return _temporal_arithmetic(op, left, right)
    if left.lit == "string" or right.lit == "string":
        return UNKNOWN
    return binary_numeric(op, left, right)


def _null_arithmetic(op, left, right) -> T:
    other = right if left.lit == "null" else left
    if other.lit == "null" or other.type is None:
        return UNKNOWN
    kind = other.type.kind
    if other.lit is None and kind in ("INT64", "NUMERIC", "BIGNUMERIC", "FLOAT64") and op in ("+", "-", "*"):
        return T(other.type)
    return UNKNOWN


def _temporal_arithmetic(op: str, left: T, right: T) -> T:
    a, b = left.type.kind, right.type.kind
    if left.lit == "string" or right.lit == "string":
        return UNKNOWN
    if op == "+":
        pairs = {("DATE", "INT64"): DATE, ("INT64", "DATE"): DATE, ("DATE", "INTERVAL"): DATETIME,
                 ("INTERVAL", "DATE"): DATETIME, ("TIMESTAMP", "INTERVAL"): TIMESTAMP,
                 ("INTERVAL", "TIMESTAMP"): TIMESTAMP, ("DATETIME", "INTERVAL"): DATETIME,
                 ("INTERVAL", "DATETIME"): DATETIME, ("INTERVAL", "INTERVAL"): INTERVAL}
    elif op == "-":
        pairs = {("DATE", "INT64"): DATE, ("DATE", "DATE"): INTERVAL, ("TIMESTAMP", "TIMESTAMP"): INTERVAL,
                 ("DATETIME", "DATETIME"): INTERVAL, ("DATE", "INTERVAL"): DATETIME,
                 ("TIMESTAMP", "INTERVAL"): TIMESTAMP, ("DATETIME", "INTERVAL"): DATETIME,
                 ("INTERVAL", "INTERVAL"): INTERVAL}
    elif op == "*":
        pairs = {("INTERVAL", "INT64"): INTERVAL, ("INT64", "INTERVAL"): INTERVAL}
        # INTERVAL times a FLOAT64 (compliance: interval/multiply_double) is an INTERVAL; the other operand is a number.
        if {a, b} == {"INTERVAL", "FLOAT64"} and "string" not in (left.lit, right.lit):
            return T(INTERVAL)
    elif op == "/":
        pairs = {("INTERVAL", "INT64"): INTERVAL}
    else:
        pairs = {}
    out = pairs.get((a, b))
    return T(out) if out is not None else UNKNOWN


def _negate(t: T) -> T:
    if t.type is None:
        return UNKNOWN
    if t.lit in ("int", "float"):
        return t  # GoogleSQL reads -1 as a literal
    if t.lit is not None:
        return UNKNOWN
    if t.type.kind in ("INT32", "INT64", "NUMERIC", "BIGNUMERIC", "FLOAT32", "FLOAT64", "INTERVAL"):
        return T(t.type)
    return UNKNOWN


def _bitwise(t: T) -> T:
    if t.type is None or t.lit == "null":
        return UNKNOWN
    if t.type.kind in ("INT64", "BYTES", "INT32", "UINT32", "UINT64"):
        return T(t.type)
    return UNKNOWN


def _bitwise_pair(left: T, right: T) -> T:
    if left.type is None or right.type is None:
        return UNKNOWN
    if left.lit is None and right.lit is None and left.type == right.type and left.type.kind in ("INT64", "BYTES"):
        return T(left.type)
    if {left.lit, right.lit} & {"int"} and left.type.kind == "INT64" and right.type.kind == "INT64":
        return T(INT64)
    return UNKNOWN


def string_or_bytes(t: T, rest: list[T] = ()) -> T:
    """STRING in, STRING out; BYTES in, BYTES out. An untyped NULL first argument takes the STRING signature when the
    other arguments are all typed and none is BYTES (compliance: REPEAT(NULL, 10), SPLIT(NULL, ','))."""

    if t.lit == "null":
        if all(r.type is not None and r.type.kind != "BYTES" for r in rest):
            return T(STRING)
        return UNKNOWN
    if t.type is None:
        return UNKNOWN
    if t.type.kind in ("STRING", "BYTES"):
        return T(t.type)
    return UNKNOWN


_CONCAT_CAST_KINDS = {"BOOL", "INT64", "NUMERIC", "BIGNUMERIC", "FLOAT64", "TIMESTAMP", "DATE", "DATETIME", "TIME",
                      "INTERVAL"}


def concat(ts: list[T]) -> T:
    """CONCAT and ``||``: STRING or BYTES by the arguments (BigQuery casts other scalars to STRING); arrays for ``||``."""

    kinds = {t.type.kind for t in ts if t.type is not None and t.lit != "null"}
    if "BYTES" in kinds and kinds - {"BYTES"}:
        return UNKNOWN
    if kinds == {"BYTES"} and all(t.type is not None for t in ts):
        return T(BYTES)
    if "STRING" in kinds:
        return T(STRING)
    if kinds and kinds <= _CONCAT_CAST_KINDS and all(t.type is not None for t in ts):
        return T(STRING)  # CONCAT casts these scalars to STRING (compliance: concat_function)
    if kinds and all(k == "ARRAY" for k in kinds):
        result = supertype(ts)
        return T(result.type) if result is not None and result.lit is None else UNKNOWN
    return UNKNOWN


def case(typer, node: exp.Case, scope, ctes) -> T:
    operand = node.this
    if isinstance(operand, exp.Expression):
        typer.expr(operand, scope, ctes)
    results = []
    for when in node.args.get("ifs") or []:
        typer.expr(when.this, scope, ctes)
        results.append(typer.expr(when.args["true"], scope, ctes))
    default = node.args.get("default")
    results.append(typer.expr(default, scope, ctes) if isinstance(default, exp.Expression) else NULL_LITERAL)
    result = supertype(results)
    if result is None:
        return UNKNOWN
    return T(result.type) if result.lit != "null" else T(INT64)


def if_(typer, node: exp.If, scope, ctes) -> T:
    typer.expr(node.this, scope, ctes)
    results = [typer.expr(node.args["true"], scope, ctes)]
    false = node.args.get("false")
    results.append(typer.expr(false, scope, ctes) if isinstance(false, exp.Expression) else NULL_LITERAL)
    result = supertype(results)
    if result is None:
        return UNKNOWN
    return T(result.type) if result.lit != "null" else T(INT64)


_INTERVAL_PARTS = {"YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND", "MILLISECOND", "MICROSECOND", "NANOSECOND"}


def extract(typer, node: exp.Extract, scope, ctes) -> T:
    zoned = node.expression
    if isinstance(zoned, exp.AtTimeZone):  # EXTRACT(part FROM timestamp AT TIME ZONE zone) takes a TIMESTAMP only
        if isinstance(zoned.args.get("zone"), exp.Expression):
            typer.expr(zoned.args["zone"], scope, ctes)
        source = typer.expr(zoned.this, scope, ctes)
        if source.type is None or source.type.kind != "TIMESTAMP":
            return UNKNOWN
    else:
        source = typer.expr(node.expression, scope, ctes)
    part = node.this.name.upper() if isinstance(node.this, exp.Expression) else str(node.this).upper()
    if source.type is None or source.lit is not None:
        return UNKNOWN
    kind = source.type.kind
    if part == "DATE":
        return T(DATE) if kind in ("TIMESTAMP", "DATETIME") else UNKNOWN
    if part == "TIME":
        return T(TIME) if kind in ("TIMESTAMP", "DATETIME") else UNKNOWN
    if part == "DATETIME":
        return T(DATETIME) if kind == "TIMESTAMP" else UNKNOWN
    if kind in ("DATE", "DATETIME", "TIMESTAMP", "TIME"):
        return T(INT64)
    if kind == "INTERVAL" and part in _INTERVAL_PARTS:
        return T(INT64)
    return UNKNOWN


def lambda_call(typer, node, name: str, scope, ctes) -> T:
    """ARRAY_TRANSFORM(array, e -> expr) and ARRAY_FILTER(array, e -> cond): the lambda reads the element type."""

    from .googlesql_types import _Range, _Scope

    args = list(node.expressions) if isinstance(node, exp.Anonymous) else _arguments(node)
    if len(args) != 2 or not isinstance(args[1], exp.Lambda):
        _visit_children(typer, node, scope, ctes)
        return UNKNOWN
    array = typer.expr(args[0], scope, ctes)
    lam = args[1]
    params = [p.name for p in lam.expressions]
    if array.type is None or array.type.kind != "ARRAY" or array.type.element is None or not 1 <= len(params) <= 2:
        return UNKNOWN
    inner = _Scope(scope)
    inner.ranges.append(_Range(params[0].lower(), None, value=T(array.type.element)))
    if len(params) == 2:
        inner.ranges.append(_Range(params[1].lower(), None, value=T(INT64)))
    body = typer.expr(lam.this, inner, ctes)
    if name == "ARRAY_FILTER":
        return T(array.type)
    if body.type is None:
        return UNKNOWN
    return T(GType.array(body.type))


# Array functions that take a predicate lambda: their result depends on the array argument only (array_functions.md).
ARRAY_LAMBDA_RESULTS = {"ARRAY_FIND", "ARRAY_FIND_ALL", "ARRAY_OFFSET", "ARRAY_OFFSETS", "ARRAY_INCLUDES"}


def _array_syntax(node) -> bool:
    """Whether ``node`` is written as an array: ``[..]``, ``ARRAY<..>[..]`` or a CAST to an ARRAY type."""

    if isinstance(node, exp.Paren):
        return _array_syntax(node.this)
    if isinstance(node, exp.Array):
        return True
    if isinstance(node, (exp.Cast, exp.TryCast)):
        to = node.args.get("to")
        return isinstance(to, exp.DataType) and to.is_type(exp.DataType.Type.ARRAY)
    return False


def array_lambda_call(typer, node, name: str, scope, ctes) -> T:
    """ARRAY_FIND(array, e -> cond [, mode]) is the element type, ARRAY_FIND_ALL the array type, ARRAY_OFFSET an INT64,
    ARRAY_OFFSETS an ARRAY<INT64> and ARRAY_INCLUDES a BOOL. The first argument must be a typed ARRAY."""

    args = list(node.expressions) if isinstance(node, exp.Anonymous) else _arguments(node)
    if not args or isinstance(args[0], exp.Lambda) or not any(isinstance(a, exp.Lambda) for a in args[1:]):
        _visit_children(typer, node, scope, ctes)
        return UNKNOWN
    array = typer.expr(args[0], scope, ctes)
    if name in ("ARRAY_OFFSET", "ARRAY_OFFSETS", "ARRAY_INCLUDES") and (
            array.lit == "empty_array" or (array.type is not None and array.lit is None and array.type.kind == "ARRAY")
            or _array_syntax(args[0])):
        # These results do not depend on the element type, so an array of a type this typer leaves unknown is fine.
        if name == "ARRAY_OFFSET":
            return T(INT64)
        return T(GType.array(INT64)) if name == "ARRAY_OFFSETS" else T(BOOL)
    if array.type is None or array.lit is not None or array.type.kind != "ARRAY" or array.type.element is None:
        return UNKNOWN
    if name == "ARRAY_FIND":
        return known(array.type.element)
    if name == "ARRAY_FIND_ALL":
        return T(array.type)
    if name == "ARRAY_OFFSET":
        return T(INT64)
    if name == "ARRAY_OFFSETS":
        return T(GType.array(INT64))
    return T(BOOL)


def array_zip_call(typer, node: exp.Anonymous, scope, ctes) -> T:
    """ARRAY_ZIP(a1 [AS n1], a2 [AS n2], .. [, transformation => (e1, e2) -> body] [, mode => 'PAD']): an
    ARRAY<STRUCT<n1 T1, n2 T2, ..>> of the arrays' element types (a field is unnamed unless aliased), or an
    ARRAY of the lambda body's type. Arrays that are not typed ARRAY (a bare NULL) and unaliased paths are unknown."""

    from .googlesql_types import _Range, _Scope

    arrays, lam = [], None
    for arg in node.expressions:
        if isinstance(arg, exp.Kwarg):
            key = arg.this.name.lower() if isinstance(arg.this, exp.Expression) else ""
            if key == "mode":
                typer.expr(arg.expression, scope, ctes)
                continue
            if key != "transformation" or not isinstance(arg.expression, exp.Lambda) or lam is not None:
                return UNKNOWN
            lam = arg.expression
        elif isinstance(arg, exp.Lambda):
            if lam is not None:
                return UNKNOWN
            lam = arg
        elif lam is not None:
            return UNKNOWN
        else:
            arrays.append(arg)
    if len(arrays) < 2:
        return UNKNOWN
    elements, names = [], []
    for arg in arrays:
        value, name = (arg.this, arg.alias) if isinstance(arg, exp.Alias) else (arg, None)
        t = typer.expr(value, scope, ctes)
        if t.lit == "null":  # each array argument is typed on its own; a bare NULL is an ARRAY<INT64> (compliance)
            t = T(GType.array(INT64))
        if t.type is None or t.type.kind != "ARRAY" or t.type.element is None or t.lit not in (None, "empty_array"):
            return UNKNOWN
        if name is None and isinstance(value, (exp.Column, exp.Dot)):
            return UNKNOWN  # a path may lend its name to the field
        elements.append(t.type.element)
        names.append(name)
    if lam is None:
        return T(GType.array(GType.struct([StructField(n, e) for n, e in zip(names, elements)])))
    params = [p.name for p in lam.expressions]
    if len(params) != len(elements):
        return UNKNOWN
    inner = _Scope(scope)
    for param, element in zip(params, elements):
        inner.ranges.append(_Range(param.lower(), None, value=T(element)))
    body = typer.expr(lam.this, inner, ctes)
    if body.type is None or body.lit == "null":
        return UNKNOWN
    return T(GType.array(body.type))


def _generate_array(call: Call) -> T:
    result = supertype(call.ts[:3])
    if result is None or result.lit == "null" or result.type.kind not in ("INT64", "NUMERIC", "BIGNUMERIC", "FLOAT64"):
        return UNKNOWN
    return T(GType.array(result.type))


def _array_concat(call: Call) -> T:
    result = supertype(call.ts)
    if result is None or result.type is None or result.type.kind != "ARRAY":
        return UNKNOWN
    return T(result.type)


def _array_to_string(call: Call) -> T:
    first = call.first().type
    if first is None or first.kind != "ARRAY" or first.element is None:
        return UNKNOWN
    return T(first.element) if first.element.kind in ("STRING", "BYTES") else UNKNOWN


def _approx_top_count(call: Call) -> T:
    first = call.first()
    if first.type is None or first.lit == "null":
        return UNKNOWN
    return T(GType.array(GType.struct([("value", first.type), ("count", INT64)])))


def _approx_top_sum(call: Call) -> T:
    if len(call.ts) < 2:
        return UNKNOWN
    value, weight = call.ts[0], call.ts[1]
    if value.type is None or value.lit == "null" or weight.type is None or weight.lit is not None:
        return UNKNOWN
    sums = {"INT64": INT64, "NUMERIC": NUMERIC, "BIGNUMERIC": BIGNUMERIC, "FLOAT64": FLOAT64}
    total = sums.get(weight.type.kind)
    if total is None:
        return UNKNOWN
    return T(GType.array(GType.struct([("value", value.type), ("sum", total)])))


def _percentile_cont(call: Call) -> T:
    """PERCENTILE_CONT(value, percentile): (FLOAT64, FLOAT64) -> FLOAT64, (NUMERIC, NUMERIC) -> NUMERIC and
    (BIGNUMERIC, BIGNUMERIC) -> BIGNUMERIC; an INT64 coerces to FLOAT64. Mixed or untyped arguments are unknown."""

    if len(call.ts) != 2:
        return UNKNOWN
    value, percentile = call.ts
    if value.type is None or percentile.type is None or value.lit is not None or percentile.lit in ("null", "string"):
        return UNKNOWN
    a, b = value.type.kind, percentile.type.kind
    if a in ("INT64", "FLOAT64") and b in ("INT64", "FLOAT64"):
        return T(FLOAT64)
    if a == b and a in ("NUMERIC", "BIGNUMERIC") and percentile.lit is None:
        return T(value.type)
    return UNKNOWN


def _json_extract(call: Call) -> T:
    first = call.first()
    if first.type is None:
        return UNKNOWN
    if first.type.kind == "JSON":
        return T(JSON)
    if first.type.kind == "STRING":
        return T(STRING)
    return UNKNOWN


def _json_extract_array(call: Call) -> T:
    inner = _json_extract(call)
    return T(GType.array(inner.type)) if inner.type is not None else UNKNOWN


def _range(call: Call) -> T:
    result = supertype(call.ts[:2])
    if result is None or result.lit == "null" or result.type.kind not in ("DATE", "DATETIME", "TIMESTAMP"):
        return UNKNOWN
    return T(GType.range(result.type))


def _string_function(call: Call) -> T:
    """STRING(x): from JSON or from a TIMESTAMP, always STRING."""

    return T(STRING)


def _int64_function(call: Call) -> T:
    return T(INT64)


def _bool_function(call: Call) -> T:
    return T(BOOL)


def _generate_range_array(call: Call) -> T:
    first = call.first().type
    if first is None or first.kind != "RANGE":
        return UNKNOWN
    return T(GType.array(first))


def _date_function(call: Call) -> T:
    return T(DATE)


def _timestamp_function(call: Call) -> T:
    return T(TIMESTAMP)


RULES = {
    "GENERATE_ARRAY": _generate_array,
    "ARRAY_CONCAT": _array_concat,
    "ARRAY_TO_STRING": _array_to_string,
    "APPROX_TOP_COUNT": _approx_top_count,
    "APPROX_TOP_SUM": _approx_top_sum,
    "PERCENTILE_CONT": _percentile_cont,
    "JSON_EXTRACT": _json_extract,
    "JSON_QUERY": _json_extract,
    "JSON_EXTRACT_ARRAY": _json_extract_array,
    "JSON_QUERY_ARRAY": _json_extract_array,
    "RANGE": _range,
    "STRING": _string_function,
    "INT64": _int64_function,
    "BOOL": _bool_function,
    "GENERATE_RANGE_ARRAY": _generate_range_array,
    "CONCAT": lambda call: concat(call.ts),
    "ERROR": lambda call: NULL_LITERAL,  # a value of any type: coerces like an untyped NULL, INT64 on its own
}
