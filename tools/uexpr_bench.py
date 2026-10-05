"""Run the SQLSolver suites through the bag-equivalence backend alone (``kumosql.uexpr``).

    python tools/uexpr_bench.py                 # all suites, 60 random-database trials per proof
    python tools/uexpr_bench.py calcite --trials 20 --show

The backend is measured without the rule modules. Foreign keys are not passed: the suites'
random databases do not respect them, so an FK-based proof would be flagged as wrong.
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, __import__("pathlib").Path(__file__).resolve().parent.as_posix())

import sqlsolver_bench as bench  # noqa: E402


def prove_result(left: str, right: str, tables, constants: bool = False):
    from kumosql.smt_equivalence import TableConstraints
    from kumosql.uexpr import prove_bag_equivalent

    schema = {t.name: [c.name for c in t.columns] for t in tables.values()}
    constraints = {
        t.name: TableConstraints(
            not_null=frozenset(c.name for c in t.columns if c.not_null),
            keys=tuple(k for k in ([t.primary_key] if t.primary_key else []) + list(t.unique)),
        )
        for t in tables.values()
    }
    types = {t.name: {c.name: c.type for c in t.columns} for t in tables.values()}
    return prove_bag_equivalent(
        bench.spark_days(left), bench.spark_days(right), schema=schema, constraints=constraints, types=types,
        dialect="mysql", exact_arithmetic=True, compare_names=False, group_by_constants=constants, use_foreign_keys=False,
    )


def prove(left, right, tables, constants=False):
    return prove_result(left, right, tables, constants).proven


def order_check(names, seed: int) -> int:
    """Prove every pair in file order and again in a shuffled order; the two must agree pair by pair.

    A proof must not depend on what the process translated before it. No random-database check runs here:
    this compares the proofs only, so it is quick.
    """

    import random

    differ = 0
    for name in names:
        pairs_file, schema_file = bench.SUITES[name]
        tables = bench.load_schema(bench.FIXTURES / schema_file)
        pairs = bench.load_pairs(bench.FIXTURES / pairs_file)

        def run(order):
            found = {}
            for index in order:
                left, right = pairs[index][:2]
                constants = name in bench.CONSTANT_GROUPING
                if constants:
                    left, right = bench.calcite_operators(left), bench.calcite_operators(right)
                found[index] = prove(left, right, tables, constants)
            return found

        order = list(range(len(pairs)))
        first = run(order)
        random.Random(seed).shuffle(order)
        second = run(order)
        moved = [i for i in first if first[i] != second[i]]
        print(f"{name:8} in order {sum(first.values())}  shuffled (seed {seed}) {sum(second.values())}  differ at {moved}")
        differ += len(moved)
    return 1 if differ else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("suites", nargs="*", default=list(bench.SUITES))
    parser.add_argument("--trials", type=int, default=60)
    parser.add_argument("--show", action="store_true", help="list the unproved pairs with their reason")
    parser.add_argument("--order-check", type=int, metavar="SEED", help="prove every pair in file order and in a shuffled order and compare (no random-database check)")
    args = parser.parse_args(argv)
    if args.order_check is not None:
        return order_check(args.suites, args.order_check)
    print(f"{'suite':8} {'pairs':>6} {'scored':>7} {'proved':>7} {'unknown':>8} {'wrong':>6} {'unchecked':>10} {'sec':>6}")
    total = wrong = 0
    for name in args.suites:
        r = bench.run_suite(name, prove, trials=args.trials)
        print(f"{r.name:8} {r.total:6} {r.scored:7} {r.proved:7} {r.scored - r.proved:8} {len(r.wrong):6} {r.unchecked:10} {r.seconds:6.1f}")
        total += r.proved
        wrong += len(r.wrong)
        for index, why in r.wrong:
            print(f"  WRONG {name}[{index}]: {why}")
        if args.show:
            _, schema_file = bench.SUITES[name]
            tables = bench.load_schema(bench.FIXTURES / schema_file)
            pairs = bench.load_pairs(bench.FIXTURES / bench.SUITES[name][0])
            constants = name in bench.CONSTANT_GROUPING  # reprove exactly as run_suite did
            for index, why in r.unproved:
                left, right = pairs[index][:2]
                if constants:
                    left, right = bench.calcite_operators(left), bench.calcite_operators(right)
                reason = prove_result(left, right, tables, constants).reason if why == "not proven" else why
                print(f"  {name}[{index}]: {reason}")
    print(f"proved {total}, wrong {wrong}")
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())
