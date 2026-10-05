"""Shared by the ``*_variants`` target modules: the cases behind their ``FIRES`` lists.

A ``FIRES`` entry is ``(target, sql)``: ``target`` is a rule name ``normalize`` calls (``unnest_grouped_source``) or a
rewrite inside one (``distinct_rules.drop_membership_dedup``, module and function), and ``sql`` is a concrete query
that must make it fire. ``tests/test_rule_fuzz_big_rules.py`` traces each one without running DuckDB, so a template
change that quietly stops a variant from firing is caught.
"""

from __future__ import annotations

from ._base import expand


def fire_cases(module: str, fires: list[tuple[str, str]], finish=None) -> list[tuple[str, dict]]:
    """``(target, case)`` for every entry of ``fires``, with the schema ``expand`` draws at seed 1."""

    out = []
    for target, sql in fires:
        case = expand([sql], 1, 1, f"{module}:fires")[0]
        if finish is not None:
            finish(case, target)
        out.append((target, case))
    return out
