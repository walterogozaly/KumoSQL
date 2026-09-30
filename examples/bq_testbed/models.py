"""The deliberately messy model layer, as ordered (name, kind, sql) steps.

Kinds: ``raw`` (sampled copy of a public table, created once), ``table``
(small materialised table) and ``view``. Steps are ordered so every step only
references earlier ones. ``{p}`` is the project, ``{d}`` the dataset and
``{src}`` the public source dataset; ``{pct}`` is the user sample percentage.
"""

from __future__ import annotations

from dataclasses import dataclass

SOURCE = "bigquery-public-data.thelook_ecommerce"


@dataclass(frozen=True)
class Step:
    name: str
    kind: str  # raw | table | view
    sql: str
    note: str = ""


def _t(name: str) -> str:
    return "`{p}.{d}." + name + "`"


_USERS_SAMPLE = "MOD(ABS(FARM_FINGERPRINT(CAST(id AS STRING))), 100) < {pct}"

STEPS: list[Step] = [
    # ---- raw: small sampled copies, so later layers never touch the public data
    Step("raw_users", "raw",
         "SELECT id, first_name, last_name, age, gender, state, country, city, traffic_source, created_at\n"
         "FROM `{src}.users` WHERE " + _USERS_SAMPLE),
    Step("raw_orders", "raw",
         "SELECT o.order_id, o.user_id, o.status, o.created_at, o.num_of_item\n"
         "FROM `{src}.orders` AS o JOIN " + _t("raw_users") + " AS u ON u.id = o.user_id"),
    Step("raw_order_items", "raw",
         "SELECT i.id, i.order_id, i.user_id, i.product_id, i.status, i.created_at, i.sale_price\n"
         "FROM `{src}.order_items` AS i JOIN " + _t("raw_users") + " AS u ON u.id = i.user_id"),
    Step("raw_products", "raw",
         "SELECT id, name, brand, category, department, cost, retail_price, distribution_center_id\n"
         "FROM `{src}.products`"),

    # ---- staging: thin renames, plus near-duplicate variants
    Step("stg_users", "view",
         "SELECT id AS user_id, first_name, last_name, age, gender, state, country, city, traffic_source, created_at\n"
         "FROM " + _t("raw_users")),
    Step("stg_users_v2", "view",
         "SELECT id AS user_id, first_name, last_name, age, gender, state, country, city, traffic_source, created_at\n"
         "FROM " + _t("raw_users") + " WHERE country IS NOT NULL",
         "near-duplicate of stg_users with an extra filter"),
    Step("stg_orders", "view",
         "SELECT order_id, user_id, status, created_at, num_of_item FROM " + _t("raw_orders")),
    Step("stg_order_items", "view",
         "SELECT id AS order_item_id, order_id, user_id, product_id, status, created_at, sale_price\n"
         "FROM " + _t("raw_order_items")),
    Step("stg_order_items_legacy", "view",
         "SELECT id AS order_item_id, order_id, user_id, product_id, status, created_at, sale_price\n"
         "FROM " + _t("raw_order_items") + " WHERE status != 'Cancelled'",
         "near-duplicate of stg_order_items that drops cancelled rows"),
    Step("stg_products", "view",
         "SELECT id AS product_id, name, brand, category, department, cost, retail_price, distribution_center_id\n"
         "FROM " + _t("raw_products")),

    # ---- dimensions, used inconsistently downstream
    Step("dim_customer", "view",
         "SELECT user_id, state, country, gender, traffic_source, created_at AS signup_at,\n"
         "  CASE WHEN age < 25 THEN 'under_25' WHEN age < 45 THEN '25_44' ELSE '45_plus' END AS age_band\n"
         "FROM " + _t("stg_users")),
    Step("dim_customers", "view",
         "SELECT user_id, state, country, gender, traffic_source, created_at AS signup_at,\n"
         "  CASE WHEN age < 25 THEN 'under_25' WHEN age < 45 THEN '25_44' ELSE '45_plus' END AS age_band\n"
         "FROM " + _t("stg_users_v2"),
         "plural twin of dim_customer built from the v2 staging view"),
    Step("dim_product", "view",
         "SELECT product_id, name, brand, category, department, cost, retail_price,\n"
         "  retail_price - cost AS unit_margin\n"
         "FROM " + _t("stg_products")),
    Step("dim_product_legacy", "view",
         "SELECT id AS product_id, name, category, department, cost, retail_price\n"
         "FROM " + _t("raw_products"),
         "skips staging and drops brand"),
    Step("dim_state_tbl", "table",
         "SELECT DISTINCT state, country FROM " + _t("stg_users") + " WHERE state IS NOT NULL",
         "tiny lookup table, joined by only some marts"),

    # ---- facts
    Step("fct_order_items", "view",
         "SELECT i.order_item_id, i.order_id, i.user_id, i.product_id, i.status, i.created_at,\n"
         "  DATE(i.created_at) AS order_date, i.sale_price AS revenue, i.sale_price - p.cost AS margin\n"
         "FROM " + _t("stg_order_items") + " AS i JOIN " + _t("dim_product") + " AS p USING (product_id)"),
    Step("fct_orders", "view",
         "SELECT o.order_id, o.user_id, o.status, DATE(o.created_at) AS order_date, o.num_of_item,\n"
         "  SUM(f.revenue) AS order_revenue\n"
         "FROM " + _t("stg_orders") + " AS o LEFT JOIN " + _t("fct_order_items") + " AS f USING (order_id)\n"
         "GROUP BY 1, 2, 3, 4, 5"),

    # ---- a deliberately long view chain
    Step("chain_01_items", "view",
         "SELECT * FROM " + _t("fct_order_items") + " WHERE status != 'Cancelled'"),
    Step("chain_02_customer", "view",
         "SELECT f.*, c.state, c.country FROM " + _t("chain_01_items") + " AS f\n"
         "JOIN " + _t("dim_customer") + " AS c USING (user_id)"),
    Step("chain_03_month", "view",
         "SELECT *, DATE_TRUNC(order_date, MONTH) AS order_month FROM " + _t("chain_02_customer")),
    Step("chain_04_category", "view",
         "SELECT m.*, p.category, p.department FROM " + _t("chain_03_month") + " AS m\n"
         "JOIN " + _t("dim_product") + " AS p USING (product_id)"),
    Step("chain_05_completed", "view",
         "SELECT * FROM " + _t("chain_04_category") + " WHERE status IN ('Complete', 'Shipped', 'Processing')"),
    Step("chain_06_bucket", "view",
         "SELECT *, CASE WHEN revenue < 25 THEN 'low' WHEN revenue < 100 THEN 'mid' ELSE 'high' END AS price_bucket\n"
         "FROM " + _t("chain_05_completed")),
    Step("chain_07_slim", "view",
         "SELECT order_item_id, order_date, order_month, state, country, category, department,\n"
         "  price_bucket, revenue, margin FROM " + _t("chain_06_bucket")),
    Step("chain_08_final", "view",
         "SELECT * FROM " + _t("chain_07_slim") + " WHERE revenue > 0"),

    # ---- overlapping state-level aggregates (same answer, different paths)
    Step("agg_sales_by_state", "view",
         "SELECT c.state, COUNT(DISTINCT f.order_id) AS orders, SUM(f.revenue) AS revenue, SUM(f.margin) AS margin\n"
         "FROM " + _t("fct_order_items") + " AS f JOIN " + _t("dim_customer") + " AS c USING (user_id)\n"
         "WHERE f.status != 'Cancelled' AND c.state IS NOT NULL GROUP BY c.state"),
    Step("mart_state_revenue", "view",
         "SELECT u.state AS state_name, COUNT(DISTINCT i.order_id) AS order_count, SUM(i.sale_price) AS total_revenue,\n"
         "  SUM(i.sale_price - p.cost) AS total_margin\n"
         "FROM " + _t("stg_order_items") + " AS i\n"
         "JOIN " + _t("stg_users") + " AS u ON u.user_id = i.user_id\n"
         "JOIN " + _t("stg_products") + " AS p ON p.product_id = i.product_id\n"
         "WHERE i.status <> 'Cancelled' AND u.state IS NOT NULL GROUP BY u.state",
         "same result as agg_sales_by_state, computed from staging"),
    Step("agg_sales_by_state_month", "view",
         "SELECT c.state, DATE_TRUNC(f.order_date, MONTH) AS order_month, COUNT(DISTINCT f.order_id) AS orders,\n"
         "  SUM(f.revenue) AS revenue, SUM(f.margin) AS margin\n"
         "FROM " + _t("fct_order_items") + " AS f JOIN " + _t("dim_customer") + " AS c USING (user_id)\n"
         "WHERE f.status != 'Cancelled' AND c.state IS NOT NULL GROUP BY 1, 2",
         "finer grain: the state rollup can be derived from it"),
    Step("agg_sales_by_country_state", "view",
         "SELECT c.country, c.state, SUM(f.revenue) AS revenue\n"
         "FROM " + _t("fct_order_items") + " AS f JOIN " + _t("dim_customers") + " AS c USING (user_id)\n"
         "WHERE f.status != 'Cancelled' GROUP BY 1, 2"),
    Step("tbl_sales_by_state", "table",
         "SELECT * FROM " + _t("agg_sales_by_state"),
         "materialised snapshot of a view that other marts still recompute"),
    Step("rpt_state_leaderboard", "view",
         "SELECT state, revenue, RANK() OVER (ORDER BY revenue DESC) AS revenue_rank FROM " + _t("tbl_sales_by_state")),
    Step("rpt_state_leaderboard_live", "view",
         "SELECT state, revenue, RANK() OVER (ORDER BY revenue DESC) AS revenue_rank FROM " + _t("agg_sales_by_state")),

    # ---- category, customer, product and time marts
    Step("agg_sales_by_category", "view",
         "SELECT p.category, SUM(f.revenue) AS revenue, SUM(f.margin) AS margin, COUNT(*) AS items\n"
         "FROM " + _t("fct_order_items") + " AS f JOIN " + _t("dim_product") + " AS p USING (product_id)\n"
         "WHERE f.status != 'Cancelled' GROUP BY p.category"),
    Step("agg_sales_by_category_dept", "view",
         "SELECT p.department, p.category, SUM(f.revenue) AS revenue, SUM(f.margin) AS margin\n"
         "FROM " + _t("fct_order_items") + " AS f JOIN " + _t("dim_product_legacy") + " AS p USING (product_id)\n"
         "WHERE f.status != 'Cancelled' GROUP BY 1, 2"),
    Step("mart_customer_ltv", "view",
         "SELECT c.user_id, c.state, c.age_band, SUM(f.revenue) AS lifetime_revenue, COUNT(DISTINCT f.order_id) AS orders\n"
         "FROM " + _t("dim_customer") + " AS c JOIN " + _t("fct_order_items") + " AS f USING (user_id)\n"
         "WHERE f.status != 'Cancelled' GROUP BY 1, 2, 3"),
    Step("mart_customer_ltv_v2", "view",
         "SELECT c.user_id, c.state, c.age_band, SUM(f.revenue) AS lifetime_revenue, COUNT(DISTINCT f.order_id) AS orders\n"
         "FROM " + _t("dim_customers") + " AS c JOIN " + _t("fct_order_items") + " AS f USING (user_id)\n"
         "WHERE f.status != 'Cancelled' GROUP BY 1, 2, 3",
         "near-duplicate that reads the plural dimension"),
    Step("mart_repeat_buyers", "view",
         "SELECT user_id, orders, lifetime_revenue FROM " + _t("mart_customer_ltv") + " WHERE orders > 1"),
    Step("mart_top_products_a", "view",
         "SELECT product_id, SUM(revenue) AS revenue FROM " + _t("fct_order_items") + "\n"
         "WHERE status != 'Cancelled' GROUP BY product_id ORDER BY revenue DESC LIMIT 50"),
    Step("mart_top_products_b", "view",
         "SELECT product_id, SUM(revenue) AS revenue FROM " + _t("fct_order_items") + "\n"
         "WHERE status != 'Cancelled' GROUP BY product_id ORDER BY revenue DESC LIMIT 100",
         "near-duplicate of the previous view, different limit"),
    Step("mart_product_returns", "view",
         "SELECT p.category, COUNTIF(f.status = 'Returned') AS returned, COUNT(*) AS items\n"
         "FROM " + _t("fct_order_items") + " AS f JOIN " + _t("dim_product") + " AS p USING (product_id) GROUP BY 1"),
    Step("agg_daily_sales", "view",
         "SELECT order_date, SUM(revenue) AS revenue, COUNT(DISTINCT order_id) AS orders\n"
         "FROM " + _t("fct_order_items") + " WHERE status != 'Cancelled' GROUP BY order_date"),
    Step("tbl_daily_sales", "table", "SELECT * FROM " + _t("agg_daily_sales")),
    Step("rpt_weekly_sales", "view",
         "SELECT DATE_TRUNC(order_date, WEEK) AS week, SUM(revenue) AS revenue FROM " + _t("agg_daily_sales") + " GROUP BY 1"),
    Step("rpt_weekly_sales_from_items", "view",
         "SELECT DATE_TRUNC(order_date, WEEK) AS week, SUM(revenue) AS revenue FROM " + _t("fct_order_items") + "\n"
         "WHERE status != 'Cancelled' GROUP BY 1",
         "recomputes the weekly rollup instead of reusing the daily one"),
]
