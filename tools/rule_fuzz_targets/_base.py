"""Template expansion shared by the targeted generators.

A template is SQL with choice groups ``{a|b|c}``; each group is replaced by one random alternative (groups nest
one level: ``{a|{b|c}}`` is not supported, use two groups). Every generator module lists templates (the shapes its
rules fire on, plus near misses where a guard must stop them) and calls :func:`expand`.
"""

from __future__ import annotations

import random
import re

from rule_fuzz_gen import make_schema

_GROUP = re.compile(r"\{([^{}]*\|[^{}]*)\}")


def fill(template: str, rng: random.Random) -> str:
    return _GROUP.sub(lambda m: rng.choice(m.group(1).split("|")), template)


def expand(templates: list[str], seed: int, count: int, source: str) -> list[dict]:
    """``count`` cases cycling through ``templates``, each choice group drawn at random; schemas as ``make_schema``."""

    rng = random.Random(seed)
    cases = []
    for index in range(count):
        schema, constraints = make_schema(rng)
        template = templates[index % len(templates)]
        cases.append(
            {
                "sql": fill(template, rng),
                "dialect": "bigquery",
                "schema": schema,
                "constraints": constraints,
                "options": {},
                "source": f"{source}:{seed}:{index % len(templates)}:{index}",
            }
        )
    return cases
