"""A deterministic, realistic-looking query workload over the messy models.

Each entry is ``(role, sql)``. Roles become the ``role`` job label so the job
history can be grouped later. Queries only read the testbed dataset.
"""

from __future__ import annotations

import random
import re

STATES = ["California", "Texas", "New York", "Florida", "Ohio", "Illinois", "Washington", "Georgia"]
CATEGORIES = ["Jeans", "Accessories", "Outerwear & Coats", "Sweaters", "Swim", "Socks"]

# Dashboard tiles: refreshed every round, so the same text repeats.
DASHBOARD = [
    "SELECT * FROM {t}agg_sales_by_state ORDER BY revenue DESC LIMIT 20",
    "SELECT * FROM {t}rpt_state_leaderboard WHERE revenue_rank <= 10",
    "SELECT * FROM {t}agg_sales_by_category ORDER BY revenue DESC",
    "SELECT * FROM {t}rpt_weekly_sales ORDER BY week DESC LIMIT 26",
    "SELECT * FROM {t}mart_top_products_a LIMIT 25",
]

# Scheduled-style rollups that repeat work already done by a model.
ETL = [
    "SELECT state_name, total_revenue FROM {t}mart_state_revenue WHERE total_revenue > 1000",
    "SELECT state, SUM(revenue) AS revenue FROM {t}agg_sales_by_state_month GROUP BY state",
    "SELECT c.state, SUM(f.revenue) AS revenue FROM {t}fct_order_items f JOIN {t}dim_customer c USING (user_id) "
    "WHERE f.status != 'Cancelled' GROUP BY c.state",
    "SELECT u.state, SUM(i.sale_price) AS revenue FROM {t}stg_order_items i JOIN {t}stg_users u USING (user_id) "
    "WHERE i.status <> 'Cancelled' GROUP BY u.state",
    "SELECT week, revenue FROM {t}rpt_weekly_sales_from_items ORDER BY week DESC LIMIT 12",
    "SELECT * FROM {t}mart_customer_ltv_v2 WHERE lifetime_revenue > 500",
    "SELECT * FROM {t}mart_top_products_b LIMIT 100",
    "SELECT state, revenue FROM {t}tbl_sales_by_state ORDER BY revenue DESC",
    "SELECT order_month, category, SUM(revenue) AS revenue FROM {t}chain_08_final GROUP BY 1, 2",
]

# Ad-hoc analyst queries, templated so each round differs slightly.
ADHOC = [
    "SELECT category, SUM(revenue) AS revenue FROM {t}chain_05_completed WHERE state = '{state}' GROUP BY category",
    "SELECT c.age_band, COUNT(*) AS customers FROM {t}dim_customers c WHERE c.state = '{state}' GROUP BY 1",
    "SELECT state, revenue FROM {t}agg_sales_by_state WHERE state = '{state}'",
    "SELECT p.category, SUM(f.revenue) AS revenue FROM {t}fct_order_items f JOIN {t}dim_product_legacy p USING (product_id) "
    "WHERE p.category = '{category}' GROUP BY 1",
    "SELECT s.country, SUM(f.revenue) AS revenue FROM {t}fct_order_items f "
    "JOIN {t}stg_users_v2 u USING (user_id) JOIN {t}dim_state_tbl s ON s.state = u.state GROUP BY 1",
    "SELECT order_date, revenue FROM {t}tbl_daily_sales WHERE order_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {days} DAY)",
    "SELECT * FROM {t}mart_product_returns ORDER BY returned DESC LIMIT 10",
    "SELECT * FROM {t}mart_repeat_buyers ORDER BY lifetime_revenue DESC LIMIT {n}",
    "SELECT * FROM {t}agg_sales_by_country_state WHERE revenue > {threshold}",
    "SELECT * FROM {t}agg_sales_by_category_dept WHERE department = 'Women'",
]


def build(table_prefix: str, rounds: int, seed: int = 7) -> list[tuple[str, str]]:
    """Return the workload; ``table_prefix`` is e.g. ``proj.dataset.`` (tables are backtick-quoted here)."""
    rng = random.Random(seed)

    def render(q: str, **kw: object) -> str:
        q = re.sub(r"\{t\}(\w+)", lambda m: f"`{table_prefix}{m.group(1)}`", q)
        return q.format(**kw)

    jobs: list[tuple[str, str]] = []
    for _ in range(rounds):
        jobs += [("dashboard", render(q)) for q in DASHBOARD]
        jobs += [("etl", render(q)) for q in ETL]
        for q in ADHOC:
            jobs.append(("adhoc", render(
                q, state=rng.choice(STATES), category=rng.choice(CATEGORIES),
                days=rng.choice([30, 90, 365]), n=rng.choice([10, 20, 50]),
                threshold=rng.choice([100, 500, 1000]),
            )))
    return jobs
