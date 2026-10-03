"""Rewrite-composition suite: KumoSQL's rules chained in many orders.

For every generated query (CTE chains, subqueries, duplicated and unused CTEs,
trivial predicates) a random subset of the registered rules runs in a random
order, one to three times over. After **each step** the result is compared with
the step's input on random databases in DuckDB.

Measured, and kept apart as in ``unsafe_fuzz.py``:

correctness
    ``behaviour_changes`` (a step's output differs from its input on some
    database), ``prover_false_proofs`` (the prover proves a pair the oracle
    separates), ``errors`` (a rule raised, or produced SQL that does not run).
coverage
    the prover's verdict on every changed step (proved / unknown / ...).
termination
    the canonical pipeline reaches a fixed point within ``MAX_ROUNDS``.
determinism
    the same chain run twice gives the same text.
idempotence
    ``canonical_rule_order()`` applied to its own output changes nothing (the
    rule list ``canonical_rule_order`` promises is a fixed point).
"""

from __future__ import annotations

from collections import Counter
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import unsafe_fuzz as uf  # noqa: E402

from kumosql.engine import get_rule  # noqa: E402
from kumosql.rewrite import available_rules, canonical_rule_order  # noqa: E402

MAX_ROUNDS = 6


def _apply(names, sql):
    """Apply rules in order; ``[(rule, before, after, success)]``."""

    steps = []
    for name in names:
        output = get_rule(name).apply(sql)
        steps.append((name, sql, output.sql, output.success))
        sql = output.sql
    return steps


def random_chain(rng: random.Random) -> list[str]:
    # qualify_columns is opt in and needs table columns the generated queries do not declare.
    names = [n for n in available_rules() if n != "qualify_columns"]
    chain = rng.sample(names, rng.randint(1, len(names)))
    return chain * rng.randint(1, 3)


def run(count: int, seed: int, trials: int = 30, prove_steps: bool = True) -> dict:
    rng = random.Random(seed)
    oracle = uf.Oracle(trials=trials)
    canonical = list(canonical_rule_order())
    totals: Counter = Counter()
    coverage: Counter = Counter()
    failures: list[dict] = []
    rounds_used: Counter = Counter()

    def fail(kind, **info):
        totals[kind] += 1
        if len(failures) < 50:
            failures.append({"kind": kind, **info})

    for index in range(count):
        query = uf.gen_query(rng)
        try:
            oracle.search(query, query)
        except Exception:
            totals["invalid_generated_queries"] += 1
            continue
        totals["queries"] += 1

        # 1. Random chains: semantic preservation after every step, and determinism.
        chain = random_chain(rng)
        try:
            steps = _apply(chain, query)
            again = _apply(chain, query)
        except Exception as error:
            fail("errors", query=query, chain=chain, detail=f"{type(error).__name__}: {error}"[:200])
            continue
        if [s[2] for s in steps] != [s[2] for s in again]:
            fail("nondeterministic", query=query, chain=chain)
        for name, before, after, _ in steps:
            totals["steps"] += 1
            if after == before:
                continue
            totals["changed_steps"] += 1
            try:
                separating = oracle.search(before, after)
            except Exception as error:
                fail("errors", query=before, rule=name, after=after, detail=f"does not run: {error}"[:200])
                continue
            if separating is not None:
                fail("behaviour_changes", query=before, rule=name, after=after, chain=chain)
            if prove_steps:
                try:
                    verdict = uf.classify(uf.prove(before, after))
                except Exception:
                    verdict = "error"
                coverage[verdict] += 1
                if verdict == "proved" and separating is not None:
                    fail("prover_false_proofs", query=before, rule=name, after=after)
                if verdict == "refuted" and separating is None:
                    fail("prover_refuted_a_preserving_step", query=before, rule=name, after=after)

        # 2. The canonical pipeline: termination and idempotence.
        try:
            current = query
            for round_ in range(1, MAX_ROUNDS + 1):
                nxt = _apply(canonical, current)[-1][2]
                if nxt == current:
                    break
                current = nxt
            else:
                round_ = None
            if round_ is None:
                fail("non_terminating", query=query)
            else:
                rounds_used[round_] += 1
                # Rounds 1 and 2: running the promised-idempotent pipeline on its
                # own output changes nothing, so the fixed point comes at round 2.
                if round_ > 2:
                    fail("not_idempotent", query=query, rounds=round_)
            if oracle.search(query, current) is not None:
                fail("behaviour_changes", query=query, rule="canonical pipeline", after=current)
        except Exception as error:
            fail("errors", query=query, detail=f"canonical: {type(error).__name__}: {error}"[:200])

    return {
        "queries": totals["queries"],
        "steps": totals["steps"],
        "changed_steps": totals["changed_steps"],
        "correctness": {
            "behaviour_changes": totals["behaviour_changes"],
            "prover_false_proofs": totals["prover_false_proofs"],
            "prover_refuted_a_preserving_step": totals["prover_refuted_a_preserving_step"],
            "errors": totals["errors"],
        },
        "coverage": dict(coverage),
        "termination": {"non_terminating": totals["non_terminating"], "rounds_to_fixed_point": dict(rounds_used)},
        "determinism": {"nondeterministic": totals["nondeterministic"]},
        "idempotence": {"not_idempotent": totals["not_idempotent"]},
        "invalid_generated_queries": totals["invalid_generated_queries"],
        "failures": failures,
    }


def report_line(summary: dict) -> str:
    c = summary["correctness"]
    return (
        f"compose: {summary['queries']} queries, {summary['changed_steps']} changed steps; "
        f"{c['behaviour_changes']} behaviour changes, {c['prover_false_proofs']} false proofs, {c['errors']} errors; "
        f"{summary['termination']['non_terminating']} non-terminating, {summary['determinism']['nondeterministic']} nondeterministic, "
        f"{summary['idempotence']['not_idempotent']} not idempotent; prover on steps {summary['coverage']}"
    )
