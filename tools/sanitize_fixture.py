"""Create a deterministic, generic JSON fixture from the private workbook CSV.

The sanitizer preserves row count, SQL statement structure, and repeated-reference
consistency without retaining project-specific identifiers or business text.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

from sqlglot.dialects.bigquery import BigQuery
from sqlglot import exp
from sqlglot.tokens import TokenType


_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_SQLX_STRUCTURAL_STRINGS = {
    "assertion",
    "incremental",
    "operations",
    "table",
    "view",
}
_SQLX_FUNCTIONS = {"assert", "incremental", "ref", "self", "when"}
_SQLX_STRUCTURAL_IDENTIFIERS = {
    "config",
    "database",
    "dependencies",
    "description",
    "disabled",
    "hermetic",
    "js",
    "name",
    "post_operations",
    "pre_operations",
    "schema",
    "tags",
    "type",
}
_BUILTIN_FUNCTIONS = {name.lower() for name in exp.FUNCTION_BY_NAME}
_RESERVED_WORDS = {
    word.lower()
    for word, token_type in BigQuery.Tokenizer.KEYWORDS.items()
    if token_type not in {TokenType.VAR, TokenType.IDENTIFIER, TokenType.UNKNOWN}
}
_RESERVED_WORDS.update(
    {
        "by",
        "group",
        "order",
        "qualify",
        "rows",
        "sample",
        "values",
        "window",
    }
)


class GenericNames:
    """Stable mappings shared across every row in one generated fixture."""

    def __init__(self) -> None:
        self.identifiers: dict[str, str] = {}
        self.paths: dict[str, str] = {}
        self.datasets: dict[str, str] = {}
        self.tables: dict[str, str] = {}
        self.strings: dict[str, str] = {}
        self.functions: dict[str, str] = {}
        self.counters: dict[str, int] = {}

    def _next(self, prefix: str) -> str:
        self.counters[prefix] = self.counters.get(prefix, 0) + 1
        return f"{prefix}_{self.counters[prefix]:04d}"

    def identifier(self, value: str) -> str:
        key = value.casefold()
        if key not in self.identifiers:
            self.identifiers[key] = self._next("field")
        return self.identifiers[key]

    def dataset(self, value: str) -> str:
        key = value.casefold()
        if key not in self.datasets:
            self.datasets[key] = self._next("ops_dataset")
        return self.datasets[key]

    def table(self, value: str) -> str:
        key = value.casefold()
        if key not in self.tables:
            self.tables[key] = self._next("ops_table")
        return self.tables[key]

    def path(self, value: str) -> str:
        key = value.casefold()
        if key in self.paths:
            return self.paths[key]

        parts = value.split(".")
        if len(parts) == 3:
            replacement = f"generic_project.{self.dataset(parts[1])}.{self.table(value)}"
        elif len(parts) == 2:
            replacement = f"{self.dataset(parts[0])}.{self.table(value)}"
        else:
            replacement = self.identifier(value)
        self.paths[key] = replacement
        return replacement

    def string(self, value: str) -> str:
        key = value
        if key.casefold() in _SQLX_STRUCTURAL_STRINGS:
            return value
        if key not in self.strings:
            self.strings[key] = self._next("sample_text")
        return self.strings[key]

    def function(self, value: str) -> str:
        key = value.casefold()
        if key not in self.functions:
            self.functions[key] = self._next("generic_function")
        return self.functions[key]


def _find_quoted_end(text: str, opening: int, delimiter: str) -> int:
    size = len(delimiter)
    index = opening + size
    while index < len(text):
        if text.startswith(delimiter, index):
            if index + size < len(text) and text[index + size] == delimiter[-1]:
                index += size + 1
                continue
            return index + size
        if text[index] == "\\":
            index += 2
        else:
            index += 1
    return len(text)


def _find_interpolation_end(text: str, opening: int) -> int:
    depth = 1
    index = opening + 2
    quote: str | None = None
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if text.startswith(quote, index):
                index += len(quote)
                quote = None
                continue
        elif char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return len(text) - 1


def _generic_line_comment(comment: str) -> str:
    marker = "#" if comment.startswith("#") else "--"
    return f"{marker} generic comment"


def _generic_block_comment(comment: str) -> str:
    if not comment.startswith("/*") or not comment.endswith("*/"):
        return "/* generic comment */"
    body = comment[2:-2]
    lines = body.splitlines(keepends=True)
    if not lines:
        return "/* generic comment */"
    rendered: list[str] = ["/*"]
    for line in lines:
        newline = "\n" if line.endswith("\n") else ""
        if newline:
            line = line[:-1]
        if line.endswith("\r"):
            line = line[:-1]
            newline = "\r\n"
        rendered.append(f" generic comment{newline}")
    rendered.append("*/")
    return "".join(rendered)


def _sanitize_string(
    text: str, opening: int, names: GenericNames, delimiter: str
) -> tuple[str, int]:
    end = _find_quoted_end(text, opening, delimiter)
    closing_start = max(opening + len(delimiter), end - len(delimiter))
    raw_value = text[opening + len(delimiter) : closing_start]
    replacement = names.string(raw_value)
    return f"{delimiter}{replacement}{delimiter}", end


def _sanitize_sql(text: str, names: GenericNames, *, sqlx_expression: bool = False) -> str:
    output: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        if text.startswith("${", index):
            end = _find_interpolation_end(text, index)
            inner = text[index + 2 : end]
            output.append("${")
            output.append(_sanitize_sql(inner, names, sqlx_expression=True))
            if end < length:
                output.append("}")
            index = min(end + 1, length)
            continue

        if text.startswith("--", index) or text[index] == "#":
            end = text.find("\n", index)
            if end == -1:
                end = length
            output.append(_generic_line_comment(text[index:end]))
            index = end
            continue

        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            end = length if end == -1 else end + 2
            output.append(_generic_block_comment(text[index:end]))
            index = end
            continue

        if text[index] in "'\"" or (text[index] in "rRbB" and index + 1 < length and text[index + 1] in "'\""):
            if text[index] in "rRbB":
                output.append(text[index])
                index += 1
            quote = text[index]
            delimiter = quote * 3 if text.startswith(quote * 3, index) else quote
            replacement, index = _sanitize_string(text, index, names, delimiter)
            output.append(replacement)
            continue

        if text[index] == "`":
            if sqlx_expression:
                replacement, index = _sanitize_string(text, index, names, "`")
                output.append(replacement)
            else:
                end = text.find("`", index + 1)
                end = length if end == -1 else end
                raw_value = text[index + 1 : end]
                output.append(f"`{names.path(raw_value)}`")
                index = min(end + 1, length)
            continue

        match = _IDENTIFIER_RE.match(text, index)
        if match:
            value = match.group(0)
            lower = value.casefold()
            next_index = match.end()
            while next_index < length and text[next_index].isspace():
                next_index += 1
            if lower in _RESERVED_WORDS:
                replacement = value
            elif (
                lower in _SQLX_FUNCTIONS
                or lower in _SQLX_STRUCTURAL_IDENTIFIERS
                or lower in _BUILTIN_FUNCTIONS
            ):
                replacement = value
            elif next_index < length and text[next_index] == "(":
                replacement = names.function(value)
            else:
                replacement = names.identifier(value)
            output.append(replacement)
            index = match.end()
            continue

        output.append(text[index])
        index += 1

    return "".join(output)


def _token_pattern(sql: str) -> tuple[tuple[str, str], ...]:
    ignored_values = {"VAR", "IDENTIFIER", "STRING", "NUMBER", "BIT_STRING", "BYTE_STRING"}
    tokens = BigQuery.Tokenizer().tokenize(sql)
    return tuple(
        (token.token_type.name, "" if token.token_type.name in ignored_values else token.text)
        for token in tokens
    )


def sanitize_csv(source: Path, destination: Path) -> tuple[int, int]:
    csv.field_size_limit(100_000_000)
    names = GenericNames()
    destination.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    source_index = 0
    seen_patterns: set[tuple[tuple[str, str], ...]] = set()
    rows: list[dict[str, str]] = []
    with source.open(encoding="utf-8-sig", newline="") as input_file:
        reader = csv.DictReader(input_file)
        if not reader.fieldnames or "sql_text" not in reader.fieldnames:
            raise ValueError("expected a source CSV with a sql_text column")
        for source_index, row in enumerate(reader, start=1):
            sql = _sanitize_sql(row["sql_text"] or "", names)
            pattern = _token_pattern(sql)
            if pattern in seen_patterns:
                continue
            seen_patterns.add(pattern)
            count += 1
            rows.append({"id": str(100000 + source_index), "sql_text": sql})
    destination.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return count, len(names.identifiers) + len(names.paths) + len(names.strings)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    rows, mappings = sanitize_csv(args.source, args.destination)
    print(f"wrote {rows} rows to {args.destination} using {mappings} stable mappings")


if __name__ == "__main__":
    main()
