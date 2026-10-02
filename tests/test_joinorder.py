import itertools
import random

import pytest

from kumosql.joinorder.estimator import FactorEstimator
from kumosql.joinorder.planner import greedy, optimize, plan_cost, to_sql
from kumosql.joinorder.predicates import compile_filters
from kumosql.joinorder.query import parse_join_query

duckdb = pytest.importorskip("duckdb")

STAR = """
SELECT COUNT(*) FROM users AS u, posts AS p, comments AS c, votes AS v
WHERE p.owner = u.id AND c.user_id = u.id AND v.post_id = p.id
  AND u.rep > 10 AND c.score = 0 AND p.title LIKE 'a%'
"""


def make_db():
    con = duckdb.connect()
    rng = random.Random(7)
    con.execute("CREATE TABLE users (id INT, rep INT)")
    con.execute("CREATE TABLE posts (id INT, owner INT, title VARCHAR)")
    con.execute("CREATE TABLE comments (id INT, user_id INT, score INT)")
    con.execute("CREATE TABLE votes (id INT, post_id INT)")
    con.executemany("INSERT INTO users VALUES (?, ?)", [[i, rng.randint(0, 40)] for i in range(300)])
    # skewed ownership: user 0 owns many posts
    con.executemany("INSERT INTO posts VALUES (?, ?, ?)",
                    [[i, 0 if i % 4 == 0 else rng.randint(0, 299), rng.choice(["ab", "ba", "aa"])]
                     for i in range(1200)])
    con.executemany("INSERT INTO comments VALUES (?, ?, ?)",
                    [[i, rng.randint(0, 299), rng.randint(0, 2)] for i in range(2000)])
    con.executemany("INSERT INTO votes VALUES (?, ?)", [[i, rng.randint(0, 1199)] for i in range(3000)])
    return con


def test_parse_join_graph():
    q = parse_join_query(STAR)
    assert q.tables == {"u": "users", "p": "posts", "c": "comments", "v": "votes"}
    assert len(q.edges) == 3
    assert [f.sql() for f in q.filters["u"]] == ["u.rep > 10"]
    assert q.is_connected(frozenset({"u", "p", "v"}))
    assert not q.is_connected(frozenset({"c", "v"}))


def test_predicates_follow_sql_three_valued_logic():
    q = parse_join_query(
        "SELECT * FROM t WHERE t.a IN (1, 2) AND t.b LIKE '%x_' AND NOT t.c IS NULL "
        "AND t.d BETWEEN 3 AND 5 AND t.e <> 'no'")
    fn = compile_filters(q.filters["t"])
    assert fn({"a": 1, "b": "zxy", "c": 0, "d": 4, "e": "yes"})
    assert not fn({"a": 3, "b": "zxy", "c": 0, "d": 4, "e": "yes"})
    assert not fn({"a": 1, "b": "zx", "c": 0, "d": 4, "e": "yes"})
    assert not fn({"a": 1, "b": "zxy", "c": None, "d": 4, "e": "yes"})
    assert not fn({"a": 1, "b": "zxy", "c": 0, "d": 4, "e": None})


def _brute_cost(q, card, subset):
    subset = frozenset(subset)
    if len(subset) == 1:
        return 0.0
    best = None
    items = sorted(subset)
    for r in range(1, len(items)):
        for left in itertools.combinations(items, r):
            left = frozenset(left)
            right = subset - left
            if min(left) != min(subset) or not (q.is_connected(left) and q.is_connected(right)
                                                and q.edges_between(left, right)):
                continue
            cost = _brute_cost(q, card, left) + _brute_cost(q, card, right) + card(subset)
            best = cost if best is None else min(best, cost)
    return best


def test_dpccp_finds_the_cheapest_bushy_tree():
    q = parse_join_query(
        "SELECT 1 FROM a, b, c, d, e, f WHERE a.x = b.x AND b.y = c.y AND c.z = d.z "
        "AND b.w = e.w AND e.v = f.v AND a.u = e.u")
    rng = random.Random(3)
    for _ in range(20):
        weights = {a: rng.random() for a in q.tables}
        noise = {}

        def card(s):
            key = frozenset(s)
            if key not in noise:
                noise[key] = rng.random() * 100 * sum(weights[a] for a in s)
            return noise[key]
        plan = optimize(q, card)
        assert plan.cost == pytest.approx(_brute_cost(q, card, q.tables))
        assert plan_cost(plan, card) == pytest.approx(plan.cost)
        assert greedy(q, card).aliases == frozenset(q.tables)


def test_forced_plan_sql_gives_the_same_answer():
    con = make_db()
    q = parse_join_query(STAR)
    expected = con.execute(STAR).fetchone()[0]
    plan = optimize(q, lambda s: float(len(s)))
    con.execute("SET disabled_optimizers='join_order'")
    assert con.execute(to_sql(q, plan)).fetchone()[0] == expected


def test_exact_counts_and_estimates():
    from kumosql.joinorder.bench.endtoend import connected_subsets, subset_sql
    from kumosql.joinorder.bench.truth import exact_count, materialize_filtered
    from kumosql.joinorder.stats import collect_statistics

    con = make_db()
    q = parse_join_query(STAR)
    pairs = [((q.tables[e.left], e.left_col), (q.tables[e.right], e.right_col)) for e in q.edges]
    pairs.append((("users", "id"), ("users", "id")))
    stats = collect_statistics(con, ["users", "posts", "comments", "votes"], pairs,
                               sample_rows=500, heavy_bins=5, range_bins=10)
    assert stats.tables["users"].pinned == 0  # users fit in the sample
    est = FactorEstimator(stats)
    filtered = materialize_filtered(con, q)
    for s in connected_subsets(q):
        true = con.execute(subset_sql(q, s, "duckdb")).fetchone()[0]
        assert exact_count(con, q, s) == true
        assert exact_count(con, q, s, prefiltered=filtered) == true
        guess = est.estimate(q, s)
        assert guess > 0
        if len(s) == 1 and q.tables[next(iter(s))] == "users":
            assert guess == pytest.approx(true)  # whole table stored: exact
        assert max(guess, 1) / max(true, 1) < 5 and max(true, 1) / max(guess, 1) < 5


def test_frequent_keys_are_pinned_when_sampling():
    from kumosql.joinorder.stats import collect_statistics

    con = make_db()
    stats = collect_statistics(con, ["users", "posts"], [(("posts", "owner"), ("users", "id"))],
                               sample_rows=100, heavy_bins=3, range_bins=10)
    users = stats.tables["users"]
    assert users.pinned == 3
    assert 0 in {r["id"] for r in users.sample[:users.pinned]}
    assert users.weight == pytest.approx((300 - 3) / 100)
