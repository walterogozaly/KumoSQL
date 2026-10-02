"""Extract Calcite's materialized-view rewriting tests into a JSON corpus.

    python tools/extract_calcite_mv.py            # downloads the pinned sources, writes the fixture
    python tools/extract_calcite_mv.py DIR        # reads the two .java files from DIR

Each test gives a materialized view, a query, and Calcite's verdict: ``ok`` (Calcite rewrites the
query over the view) or ``noMat`` (Calcite finds no rewrite; that is not a proof that none exists).
Calcite is Apache-2.0; the SQL text is copied unchanged apart from Java string joining.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

TAG = "calcite-1.37.0"
TAG_OBJECT = "14ab7c28ddd4c9e742fcba5108d20108386e5edb"  # git ls-remote refs/tags/calcite-1.37.0
BASE = f"https://raw.githubusercontent.com/apache/calcite/{TAG}/core/src/test/java/org/apache/calcite/test/"
FILES = {
    "MaterializedViewRelOptRulesTest": "rules",
    "MaterializedViewSubstitutionVisitorTest": "substitution",
}
OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "mv_reuse" / "calcite_mv_cases.json"

STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')


def unescape(text: str) -> str:
    return text.encode().decode("unicode_escape") if "\\" in text else text


def split_args(text: str) -> list[str]:
    """Split a call's argument text at top-level commas."""

    args, depth, current, in_string, escaped = [], 0, [], False, False
    for char in text:
        if in_string:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            args.append("".join(current))
            current = []
            continue
        current.append(char)
    args.append("".join(current))
    return args


def literal(arg: str, env: dict[str, str] | None = None) -> str | None:
    """The string a Java expression of joined literals and known local strings evaluates to, else None."""

    env = env or {}
    out, index, text = [], 0, arg.strip()
    while index < len(text):
        if text[index].isspace() or text[index] == "+":
            index += 1
            continue
        match = STRING.match(text, index)
        if match:
            out.append(unescape(match.group(1)))
            index = match.end()
            continue
        name = re.match(r"[A-Za-z_]\w*", text[index:])
        if name and name.group(0) in env:
            out.append(env[name.group(0)])
            index += name.end()
            continue
        return None
    return "".join(out)


def local_strings(body: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for match in re.finditer(r"(?:final\s+)?String\s+(\w+)\s*=\s*(.*?);\s*\n", body, re.S):
        value = literal(match.group(2), env)
        if value is not None:
            env[match.group(1)] = value
    return env


def call_args(body: str, start: int) -> tuple[str, int]:
    """Text between the parentheses opening at ``start`` and the index after the closing one."""

    depth, in_string, escaped = 0, False, False
    for index in range(start, len(body)):
        char = body[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return body[start + 1 : index], index + 1
    raise ValueError("unbalanced parentheses")


def extract(source: str, origin: str) -> tuple[list[dict], list[str]]:
    cases, skipped = [], []
    methods = list(re.finditer(r"((?:@Disabled\s*)?)@Test(?:\s*\(.*?\))?\s+(?:public\s+)?void\s+(\w+)\s*\(\)\s*\{", source, re.S))
    for number, match in enumerate(methods):
        end = methods[number + 1].start() if number + 1 < len(methods) else len(source)
        body = source[match.end() : end]
        name = match.group(2)
        disabled = bool(match.group(1)) or "@Disabled" in source[max(0, match.start() - 60) : match.start()]
        call = re.search(r"\b(sql|fixture)\(", body)
        if not call:
            skipped.append(f"{name}: no sql() call")
            continue
        args_text, after = call_args(body, call.end() - 1)
        args = split_args(args_text)
        tail = body[after:]
        verdict = None
        if re.search(r"\.noMat\(\)", tail):
            verdict = "noMat"
        elif re.search(r"\.ok\(\)", tail):
            verdict = "ok"
        env = local_strings(body[: call.start()])
        texts = [literal(a, env) for a in args]
        if call.group(1) == "fixture":
            # fixture(query).withMaterializations(ImmutableList.of(Pair.of(mv, "MV0")))
            pairs = re.search(r"Pair\.of\((.*?),\s*\"MV0\"\)", tail, re.S)
            query = texts[0]
            mv = literal(pairs.group(1), env) if pairs else None
            if "MV1" in tail:
                skipped.append(f"{name}: several materializations")
                continue
            texts = [mv, query]
        if len(texts) != 2 or None in texts or verdict is None:
            skipped.append(f"{name}: shape not read")
            continue
        extra = re.search(r"withDefaultSchemaSpec\(\s*CalciteAssert\.SchemaSpec\.(\w+)\)", tail)
        cases.append(
            {
                "id": f"{origin}.{name}",
                "name": name,
                "origin": origin,
                "materialization": texts[0],
                "query": texts[1],
                "calcite": verdict,
                "schema": (extra.group(1).lower() if extra else "hr"),
                "disabled": disabled,
            }
        )
    return cases, skipped


def fetch(name: str, directory: Path | None) -> str:
    if directory:
        return (directory / f"{name}.java").read_text(encoding="utf-8")
    with urllib.request.urlopen(BASE + f"{name}.java") as response:  # noqa: S310 - fixed pinned URL
        return response.read().decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    directory = Path(argv[0]) if argv else None
    cases, skipped = [], []
    for name, origin in FILES.items():
        found, missed = extract(fetch(name, directory), origin)
        cases += found
        skipped += [f"{origin}: {m}" for m in missed]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps({"source": "https://github.com/apache/calcite", "tag": TAG, "tag_object": TAG_OBJECT, "license": "Apache-2.0", "cases": cases}, indent=1) + "\n",
        encoding="utf-8",
    )
    print(f"{len(cases)} cases written to {OUT}; {len(skipped)} tests not read")
    for line in skipped:
        print("  skipped", line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
