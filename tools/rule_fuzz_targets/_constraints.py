"""Constraint combinations for the key, foreign-key and membership generators.

``rule_fuzz_gen.make_schema`` seldom declares a NOT NULL foreign key (8% of cases) and never a nullable unique key or
a composite one, so the rules that need those facts hardly fire on it. Here a template names the constraint sets it
runs under, and :func:`expand_constrained` draws one of them per case. A set is a function of the random generator,
returning the schema and the constraints of one case; the base schema (``t``, ``u``, ``p`` of ``rule_fuzz_gen``) is
the default, ``cd`` adds the composite-key tables ``c(id, a, b, v)`` and ``d(a, b, w)``.
"""

from __future__ import annotations

import random

from rule_fuzz_gen import BASE_SCHEMA, INT

from ._base import fill

COMPOSITE_SCHEMA = {
    "c": [["id", INT], ["a", INT], ["b", INT], ["v", INT]],
    "d": [["a", INT], ["b", INT], ["w", INT]],
}


def _base(extra: dict | None = None) -> dict:
    constraints = {
        "t": {"not_null": ["id"], "keys": [["id"]]},
        "p": {"not_null": ["id"], "keys": [["id"]]},
        "u": {"not_null": ["k"], "keys": [["k"]]},
    }
    for table, facts in (extra or {}).items():
        for kind, values in facts.items():
            constraints[table].setdefault(kind, [])
            constraints[table][kind] = constraints[table][kind] + values
    return constraints


def fk(rng: random.Random) -> dict:
    """p.tid -> t.id and t.y -> u.k, the children's columns NOT NULL: the shape ``drop_fk_join`` needs."""

    extra = {
        "p": {"not_null": ["tid"], "foreign_keys": [[["tid"], "t", ["id"]]]},
        "t": {"not_null": ["y"] + (["x"] if rng.random() < 0.3 else []), "foreign_keys": [[["y"], "u", ["k"]]]},
    }
    return _base(extra)


def fk_nullable(rng: random.Random) -> dict:
    """The same foreign keys with nullable child columns: a join that loses the NULL rows must stay."""

    return _base({"p": {"foreign_keys": [[["tid"], "t", ["id"]]]}, "t": {"foreign_keys": [[["y"], "u", ["k"]]]}})


def not_null(rng: random.Random) -> dict:
    """Every column the membership rules read is NOT NULL."""

    return _base({"t": {"not_null": ["x", "y"]}, "u": {"not_null": ["w"]}, "p": {"not_null": ["tid", "n"]}})


def left_not_null(rng: random.Random) -> dict:
    """The outer side of a membership test is NOT NULL, the subquery side admits NULL."""

    return _base({"t": {"not_null": ["x", "y"]}})


def right_not_null(rng: random.Random) -> dict:
    """The subquery side is NOT NULL, the outer side admits NULL."""

    return _base({"u": {"not_null": ["w"]}, "p": {"not_null": ["tid"]}})


def nullable_key(rng: random.Random) -> dict:
    """``u.k`` is a UNIQUE key that admits NULLs; ``t.id`` stays a primary key."""

    constraints = _base()
    constraints["u"] = {"keys": [["k"]]}
    return constraints


def no_key(rng: random.Random) -> dict:
    """``u`` has no key: its rows may repeat, so joins to it multiply."""

    constraints = _base()
    constraints["u"] = {}
    return constraints


def plain(rng: random.Random) -> dict:
    constraints = _base()
    if rng.random() < 0.5:
        constraints["t"]["not_null"] = constraints["t"]["not_null"] + ["x"]
    return constraints


BASE_SETS = {"fk": fk, "fk_nullable": fk_nullable, "not_null": not_null, "left_not_null": left_not_null, "right_not_null": right_not_null, "nullable_key": nullable_key, "no_key": no_key, "plain": plain}


def composite_fk(rng: random.Random) -> dict:
    return {
        "c": {"not_null": ["id", "a", "b"], "keys": [["id"]], "foreign_keys": [[["a", "b"], "d", ["a", "b"]]]},
        "d": {"not_null": ["a", "b"], "keys": [["a", "b"]]},
    }


def composite_nullable(rng: random.Random) -> dict:
    """The same keys, but ``b`` admits NULL on both sides."""

    return {
        "c": {"not_null": ["id", "a"], "keys": [["id"]], "foreign_keys": [[["a", "b"], "d", ["a", "b"]]]},
        "d": {"not_null": ["a"], "keys": [["a", "b"]]},
    }


COMPOSITE_SETS = {"composite_fk": composite_fk, "composite_nullable": composite_nullable}


class T(str):
    """A template with the constraint sets (names) it runs under; the default is every base set."""

    sets: tuple

    def __new__(cls, text: str, *sets: str):
        obj = super().__new__(cls, text)
        obj.sets = sets or tuple(BASE_SETS)
        return obj


def expand_constrained(templates: list[T], seed: int, count: int, source: str) -> list[dict]:
    """``count`` cases cycling through ``templates``; each draws one of the template's constraint sets."""

    rng = random.Random(seed)
    cases = []
    for index in range(count):
        template = templates[index % len(templates)]
        name = rng.choice(template.sets)
        if name in COMPOSITE_SETS:
            schema = {t: [list(c) for c in cols] for t, cols in COMPOSITE_SCHEMA.items()}
            constraints = COMPOSITE_SETS[name](rng)
        else:
            schema = {t: [list(c) for c in cols] for t, cols in BASE_SCHEMA.items()}
            constraints = BASE_SETS[name](rng)
        cases.append(
            {
                "sql": fill(str(template), rng),
                "dialect": "bigquery",
                "schema": schema,
                "constraints": constraints,
                "options": {},
                "source": f"{source}:{seed}:{index % len(templates)}:{index}:{name}",
            }
        )
    return cases
