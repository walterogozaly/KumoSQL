"""Write the hand-written table-minimization cases, each one verified on DuckDB (and proved where possible).

The template generator (``tools/make_minimization_cases.py``) composes small modules over one fixed
set of sources. The cases here are written by hand instead, to look like real dbt or Dataform
projects: staging -> intermediate -> marts naming, CTE-heavy models, COALESCE and CASE, window
functions (ROW_NUMBER for dedup and latest-per-key, always ordered on a unique key), LEFT JOINs to
keyed dimensions, UNION ALL of per-source tables, conditional aggregation, HAVING, DISTINCT, IN and
EXISTS subqueries. Each case brings its own sources (e-commerce, SaaS billing, marketing, HR,
support, logistics, finance, inventory) and mixes the redundancy families with traps: tempting
simplifications that change a protected output (NULL semantics, fan-out from non-key joins, inner
versus left joins, DISTINCT, COUNT(col) versus COUNT(*), ranking before filtering).

Every case goes through ``make_minimization_cases.verify_case``: the reference must agree with the
original on 300 DuckDB databases (optimizer off), each trap must differ on some database (shrunk and
stored as its witness), the reference must score lower unless the case is irreducible, and KumoSQL's
provers are tried on each protected table.

    python tools/minimization_handwritten.py      # writes benchmarks/table_minimization/handwritten.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

import make_minimization_cases as gen  # noqa: E402
import minimization_cases as mc  # noqa: E402

KEEP = object()  # in a reference: the table's original SQL

# ------------------------------------------------------------------ sources, by domain

SOURCES = {
    # e-commerce
    "raw_orders": {
        "columns": {"id": "INT64", "customer_id": "INT64", "order_total": "INT64", "status": "STRING",
                    "channel": "STRING", "created_on": "DATE"},
        "key": ["id"],
        "values": {"status": ["completed", "pending", "cancelled"], "channel": ["web", "app", "store"]},
    },
    "raw_customers": {
        "columns": {"id": "INT64", "email": "STRING", "country": "STRING", "is_test": "BOOL"},
        "key": ["id"],
        "values": {"email": ["ann@shop.io", "Ann@Shop.io", "bob@shop.io"], "country": ["US", "DE", "FR"]},
    },
    "raw_order_lines": {
        "columns": {"order_id": "INT64", "line_no": "INT64", "product_id": "INT64", "quantity": "INT64",
                    "unit_price": "INT64"},
        "key": ["order_id", "line_no"],
    },
    "raw_products": {
        "columns": {"id": "INT64", "name": "STRING", "category": "STRING", "is_active": "BOOL"},
        "key": ["id"],
        "values": {"name": ["kite", "lamp", "mug"], "category": ["apparel", "home", "toys"]},
    },
    "raw_refunds": {
        "columns": {"id": "INT64", "order_id": "INT64", "amount": "INT64", "reason": "STRING"},
        "key": ["id"],
        "not_null": ["order_id"],
        "values": {"reason": ["damaged", "late", "other"]},
    },
    # SaaS
    "accounts": {
        "columns": {"id": "INT64", "name": "STRING", "plan": "STRING", "region": "STRING", "is_deleted": "BOOL"},
        "key": ["id"],
        "values": {"name": ["acme", "globex"], "plan": ["free", "pro", "enterprise"], "region": ["amer", "emea"]},
    },
    "subscriptions": {
        "columns": {"id": "INT64", "account_id": "INT64", "plan": "STRING", "mrr": "INT64", "status": "STRING",
                    "started_on": "DATE"},
        "key": ["id"],
        "not_null": ["account_id"],
        "values": {"plan": ["free", "pro", "enterprise"], "status": ["active", "canceled", "trialing"]},
    },
    "invoices": {
        "columns": {"id": "INT64", "account_id": "INT64", "amount": "INT64", "status": "STRING", "issued_on": "DATE"},
        "key": ["id"],
        "values": {"status": ["paid", "open", "void"]},
    },
    "app_users": {
        "columns": {"id": "INT64", "account_id": "INT64", "email": "STRING", "role": "STRING", "is_active": "BOOL"},
        "key": ["id"],
        "values": {"role": ["admin", "member"]},
    },
    "usage_events": {
        "columns": {"user_id": "INT64", "feature": "STRING", "units": "INT64"},
        "values": {"feature": ["export", "search", "share"]},
    },
    # marketing
    **{f"{platform}_ads_daily": {
        "columns": {"campaign_id": "INT64", "spend_date": "DATE", "spend": "INT64", "clicks": "INT64",
                    "impressions": "INT64"},
    } for platform in ("google", "meta", "bing")},
    "campaigns": {
        "columns": {"id": "INT64", "name": "STRING", "channel": "STRING", "budget": "INT64"},
        "key": ["id"],
        "values": {"channel": ["paid_search", "paid_social", "display"]},
    },
    "web_sessions": {
        "columns": {"id": "INT64", "visitor_id": "INT64", "campaign_id": "INT64", "landing_page": "STRING",
                    "started_at": "TIMESTAMP"},
        "key": ["id"],
        "values": {"landing_page": ["home", "pricing", "blog"]},
    },
    "signups": {
        "columns": {"id": "INT64", "session_id": "INT64", "account_id": "INT64", "plan": "STRING"},
        "key": ["id"],
        "values": {"plan": ["free", "pro"]},
    },
    # HR
    "employees": {
        "columns": {"id": "INT64", "department_id": "INT64", "manager_id": "INT64", "salary": "INT64",
                    "status": "STRING", "level": "STRING"},
        "key": ["id"],
        "values": {"status": ["active", "terminated", "leave"], "level": ["ic1", "ic2", "mgr"]},
    },
    "departments": {
        "columns": {"id": "INT64", "name": "STRING", "cost_center": "STRING"},
        "key": ["id"],
        "values": {"name": ["sales", "eng"], "cost_center": ["cc10", "cc20"]},
    },
    "timesheets": {
        "columns": {"employee_id": "INT64", "week": "INT64", "project": "STRING", "hours": "INT64"},
        "not_null": ["employee_id"],
        "values": {"project": ["apollo", "zeus"]},
    },
    # support
    "tickets": {
        "columns": {"id": "INT64", "requester_id": "INT64", "assignee_id": "INT64", "priority": "STRING",
                    "status": "STRING", "channel": "STRING"},
        "key": ["id"],
        "values": {"priority": ["low", "high", "urgent"], "status": ["open", "solved", "closed"],
                   "channel": ["email", "chat"]},
    },
    "ticket_status_history": {
        "columns": {"id": "INT64", "ticket_id": "INT64", "status": "STRING", "agent_id": "INT64"},
        "key": ["id"],
        "not_null": ["ticket_id"],
        "values": {"status": ["open", "pending", "solved"]},
    },
    "ticket_comments": {
        "columns": {"id": "INT64", "ticket_id": "INT64", "author_id": "INT64", "is_public": "BOOL"},
        "key": ["id"],
        "not_null": ["ticket_id"],
    },
    "agents": {
        "columns": {"id": "INT64", "name": "STRING", "team": "STRING", "is_bot": "BOOL"},
        "key": ["id"],
        "values": {"team": ["tier1", "tier2"]},
    },
    "csat_responses": {
        "columns": {"ticket_id": "INT64", "score": "INT64"},
    },
    # logistics
    "shipments": {
        "columns": {"id": "INT64", "order_id": "INT64", "carrier_code": "STRING", "status": "STRING",
                    "weight_g": "INT64"},
        "key": ["id"],
        "values": {"carrier_code": ["ups", "dhl"], "status": ["delivered", "in_transit", "lost"]},
    },
    "shipment_events": {
        "columns": {"id": "INT64", "shipment_id": "INT64", "kind": "STRING", "seq": "INT64"},
        "key": ["id"],
        "not_null": ["shipment_id"],
        "values": {"kind": ["picked", "scanned", "delivered"]},
    },
    "carriers": {
        "columns": {"code": "STRING", "name": "STRING", "is_active": "BOOL"},
        "key": ["code"],
        "values": {"code": ["ups", "dhl", "fedex"], "name": ["UPS", "DHL"]},
    },
    # finance
    "gl_entries": {
        "columns": {"id": "INT64", "account_code": "STRING", "entity": "STRING", "amount": "NUMERIC",
                    "side": "STRING", "posted_on": "DATE"},
        "key": ["id"],
        "values": {"account_code": ["1000", "4000", "5000"], "entity": ["us_inc", "uk_ltd"],
                   "side": ["debit", "credit"]},
    },
    "gl_accounts": {
        "columns": {"code": "STRING", "name": "STRING", "account_type": "STRING", "parent_code": "STRING"},
        "key": ["code"],
        "values": {"code": ["1000", "4000", "5000"], "account_type": ["asset", "revenue", "expense"],
                   "parent_code": ["1000", "4000"]},
    },
    "fx_rates": {
        "columns": {"entity": "STRING", "rate": "NUMERIC"},
        "key": ["entity"],
        "values": {"entity": ["us_inc", "uk_ltd"]},
    },
    # inventory
    "stock_moves": {
        "columns": {"id": "INT64", "sku": "STRING", "warehouse": "STRING", "qty": "INT64", "kind": "STRING"},
        "key": ["id"],
        "values": {"sku": ["sku1", "sku2"], "warehouse": ["ams", "nyc"],
                   "kind": ["receipt", "shipment", "adjustment"]},
    },
    "warehouses": {
        "columns": {"code": "STRING", "region": "STRING", "is_3pl": "BOOL"},
        "key": ["code"],
        "values": {"code": ["ams", "nyc", "sfo"], "region": ["eu", "na"]},
    },
    "skus": {
        "columns": {"sku": "STRING", "category": "STRING", "unit_cost": "INT64"},
        "key": ["sku"],
        "values": {"sku": ["sku1", "sku2", "sku3"], "category": ["bulk", "fragile"]},
    },
}


def case(families, tables, protected, reference, traps=(), note="", data=None):
    """A case before verification. ``reference`` values may be ``KEEP``; each trap is
    ``(note, changes)`` applied to the reference (a ``None`` change drops a table), or
    ``(note, changes, "original")`` applied to the original pipeline. ``data`` is one extra check
    database used only while verifying, to find witnesses for traps that random databases rarely hit
    (the witness is stored; the rows are not)."""

    ref = {name: tables[name] if sql is KEEP else sql for name, sql in reference.items()}
    built = []
    for trap in traps:
        note_, changes = trap[0], trap[1]
        base = tables if len(trap) > 2 and trap[2] == "original" else ref
        pipeline = dict(base)
        for name, sql in changes.items():
            if sql is None:
                pipeline.pop(name, None)
            else:
                pipeline[name] = sql
        built.append({"note": note_, "tables": pipeline})
    return {"families": sorted(families), "tables": dict(tables), "protected": list(protected),
            "reference": {"tables": ref}, "traps": built, "note": note, "data": data}


CASES = []


def add(c):
    CASES.append(c)
    return c


# ================================================================== small cases (3 to 5 tables)

# SaaS soft-delete flag: the CASE turns NULL into FALSE, so `= FALSE` keeps NULL flags.
add(case(
    ["passthrough_chain", "mergeable_tables"],
    {
        "stg_accounts": "SELECT id AS account_id, name AS account_name, plan, region, CASE WHEN is_deleted THEN TRUE ELSE FALSE END AS is_deleted FROM accounts",
        "dim_accounts": "SELECT account_id, account_name, plan, region FROM stg_accounts WHERE is_deleted = FALSE",
        "rpt_accounts_by_plan": "SELECT plan, COUNT(*) AS accounts FROM stg_accounts WHERE NOT is_deleted GROUP BY plan",
    },
    ["dim_accounts", "rpt_accounts_by_plan"],
    {
        "dim_accounts": "SELECT id AS account_id, name AS account_name, plan, region FROM accounts WHERE is_deleted IS NOT TRUE",
        "rpt_accounts_by_plan": "SELECT plan, COUNT(*) AS accounts FROM dim_accounts GROUP BY plan",
    },
    [
        ("reads accounts directly with is_deleted = FALSE, dropping accounts whose flag is NULL",
         {"dim_accounts": "SELECT id AS account_id, name AS account_name, plan, region FROM accounts WHERE is_deleted = FALSE"}),
        ("filters the plan report with NOT is_deleted on the raw flag",
         {"rpt_accounts_by_plan": "SELECT plan, COUNT(*) AS accounts FROM accounts WHERE NOT is_deleted GROUP BY plan"}),
    ],
    "The staging CASE maps a NULL flag to FALSE; the reference keeps that with IS NOT TRUE.",
))

# Irreducible: every table is protected and each join or DISTINCT is needed.
add(case(
    ["irreducible"],
    {
        "dim_customers": "SELECT id AS customer_id, email, country FROM raw_customers WHERE is_test IS NOT TRUE",
        "fct_completed_orders": "SELECT o.id AS order_id, o.customer_id, o.order_total FROM raw_orders AS o JOIN dim_customers AS c ON c.customer_id = o.customer_id WHERE o.status = 'completed'",
        "rpt_country_revenue": "SELECT c.country, SUM(o.order_total) AS revenue, COUNT(DISTINCT o.customer_id) AS buyers FROM fct_completed_orders AS o JOIN dim_customers AS c ON c.customer_id = o.customer_id GROUP BY c.country",
    },
    ["dim_customers", "fct_completed_orders", "rpt_country_revenue"],
    {"dim_customers": KEEP, "fct_completed_orders": KEEP, "rpt_country_revenue": KEEP},
    [
        ("filters test customers with NOT is_test, dropping customers whose flag is NULL",
         {"dim_customers": "SELECT id AS customer_id, email, country FROM raw_customers WHERE NOT is_test"}),
        ("removes the join to dim_customers because only order columns are selected",
         {"fct_completed_orders": "SELECT id AS order_id, customer_id, order_total FROM raw_orders WHERE status = 'completed'"}),
        ("counts buyers without DISTINCT",
         {"rpt_country_revenue": "SELECT c.country, SUM(o.order_total) AS revenue, COUNT(o.customer_id) AS buyers FROM fct_completed_orders AS o JOIN dim_customers AS c ON c.customer_id = o.customer_id GROUP BY c.country"}),
    ],
    "Irreducible: all three tables are protected and every join, filter and DISTINCT matters.",
))

# Fan-out: aggregating lines and refunds after one join double counts.
add(case(
    ["passthrough_chain", "mergeable_tables"],
    {
        "stg_order_lines": "SELECT order_id, line_no, product_id, quantity * unit_price AS line_total FROM raw_order_lines",
        "int_order_line_totals": "SELECT order_id, SUM(line_total) AS gross FROM stg_order_lines GROUP BY order_id",
        "int_order_refunds": "SELECT order_id, SUM(amount) AS refunded FROM raw_refunds GROUP BY order_id",
        "fct_orders": "SELECT o.id AS order_id, o.customer_id, COALESCE(l.gross, 0) AS gross, COALESCE(r.refunded, 0) AS refunded FROM raw_orders AS o LEFT JOIN int_order_line_totals AS l ON l.order_id = o.id LEFT JOIN int_order_refunds AS r ON r.order_id = o.id",
    },
    ["fct_orders"],
    {
        "int_order_line_totals": "SELECT order_id, SUM(quantity * unit_price) AS gross FROM raw_order_lines GROUP BY order_id",
        "int_order_refunds": KEEP,
        "fct_orders": KEEP,
    },
    [
        ("joins lines and refunds to orders first and aggregates once, so each refund repeats per line",
         {"int_order_line_totals": None, "int_order_refunds": None,
          "fct_orders": "SELECT o.id AS order_id, o.customer_id, COALESCE(SUM(l.quantity * l.unit_price), 0) AS gross, COALESCE(SUM(r.amount), 0) AS refunded FROM raw_orders AS o LEFT JOIN raw_order_lines AS l ON l.order_id = o.id LEFT JOIN raw_refunds AS r ON r.order_id = o.id GROUP BY o.id, o.customer_id"}),
    ],
    "Two pre-aggregated children of orders must stay separate aggregates.",
))

# SaaS billing: per-status aggregate tables merged into conditional aggregation.
add(case(
    ["mergeable_tables"],
    {
        "int_paid_invoices": "SELECT account_id, SUM(amount) AS paid_amount, COUNT(*) AS paid_invoices FROM invoices WHERE status = 'paid' GROUP BY account_id",
        "int_open_invoices": "SELECT account_id, SUM(amount) AS open_amount FROM invoices WHERE status = 'open' GROUP BY account_id",
        "dim_accounts_billing": "SELECT a.id AS account_id, a.name, p.paid_amount, p.paid_invoices, o.open_amount FROM accounts AS a LEFT JOIN int_paid_invoices AS p ON p.account_id = a.id LEFT JOIN int_open_invoices AS o ON o.account_id = a.id",
        "rpt_open_balances": "SELECT account_id, open_amount FROM dim_accounts_billing WHERE open_amount > 0",
    },
    ["dim_accounts_billing", "rpt_open_balances"],
    {
        "dim_accounts_billing": "SELECT a.id AS account_id, a.name, SUM(IF(i.status = 'paid', i.amount, NULL)) AS paid_amount, SUM(IF(i.status = 'paid', 1, NULL)) AS paid_invoices, SUM(IF(i.status = 'open', i.amount, NULL)) AS open_amount FROM accounts AS a LEFT JOIN invoices AS i ON i.account_id = a.id GROUP BY a.id, a.name",
        "rpt_open_balances": KEEP,
    },
    [
        ("counts paid invoices with COUNTIF, which gives 0 where the original gives NULL",
         {"dim_accounts_billing": "SELECT a.id AS account_id, a.name, SUM(IF(i.status = 'paid', i.amount, NULL)) AS paid_amount, COUNTIF(i.status = 'paid') AS paid_invoices, SUM(IF(i.status = 'open', i.amount, NULL)) AS open_amount FROM accounts AS a LEFT JOIN invoices AS i ON i.account_id = a.id GROUP BY a.id, a.name"}),
        ("merges with an inner join, dropping accounts that have no invoices",
         {"dim_accounts_billing": "SELECT a.id AS account_id, a.name, SUM(IF(i.status = 'paid', i.amount, NULL)) AS paid_amount, SUM(IF(i.status = 'paid', 1, NULL)) AS paid_invoices, SUM(IF(i.status = 'open', i.amount, NULL)) AS open_amount FROM accounts AS a JOIN invoices AS i ON i.account_id = a.id GROUP BY a.id, a.name"}),
    ],
    "The left joins to per-status aggregates become one conditional aggregation that keeps NULL for missing statuses.",
))

# Marketing: per-platform staging tables unioned, with a filter that SUM does not make redundant.
add(case(
    ["passthrough_chain", "mergeable_tables"],
    {
        "stg_google_ads": "SELECT campaign_id, spend_date, spend, clicks, 'google' AS platform FROM google_ads_daily",
        "stg_meta_ads": "SELECT campaign_id, spend_date, spend, clicks, 'meta' AS platform FROM meta_ads_daily",
        "int_ad_spend": "SELECT * FROM stg_google_ads UNION ALL SELECT * FROM stg_meta_ads",
        "int_ad_spend_clean": "SELECT * FROM int_ad_spend WHERE spend IS NOT NULL",
        "rpt_spend_by_platform": "SELECT platform, SUM(spend) AS spend, SUM(clicks) AS clicks FROM int_ad_spend_clean GROUP BY platform",
    },
    ["rpt_spend_by_platform"],
    {
        "int_ad_spend": "SELECT 'google' AS platform, spend, clicks FROM google_ads_daily UNION ALL SELECT 'meta' AS platform, spend, clicks FROM meta_ads_daily",
        "rpt_spend_by_platform": "SELECT platform, SUM(spend) AS spend, SUM(clicks) AS clicks FROM int_ad_spend WHERE spend IS NOT NULL GROUP BY platform",
    },
    [
        ("drops spend IS NOT NULL because SUM ignores NULLs, but clicks on those rows then count",
         {"rpt_spend_by_platform": "SELECT platform, SUM(spend) AS spend, SUM(clicks) AS clicks FROM int_ad_spend GROUP BY platform"}),
        ("unions the trimmed platform rows with UNION DISTINCT, collapsing identical ad rows",
         {"int_ad_spend": "SELECT 'google' AS platform, spend, clicks FROM google_ads_daily UNION DISTINCT SELECT 'meta' AS platform, spend, clicks FROM meta_ads_daily"}),
    ],
    "Per-platform staging folds into one union; the NULL-spend filter still matters for clicks.",
))

# Support: latest status per ticket by ROW_NUMBER on the history key, repeated as a CTE.
add(case(
    ["cte_repeats_table", "dead_tables", "passthrough_chain"],
    {
        "stg_ticket_history": "SELECT id AS history_id, ticket_id, status, agent_id FROM ticket_status_history",
        "int_ticket_current_status": "SELECT ticket_id, status, agent_id FROM (SELECT ticket_id, status, agent_id, ROW_NUMBER() OVER (PARTITION BY ticket_id ORDER BY history_id DESC) AS rn FROM stg_ticket_history) WHERE rn = 1",
        "int_ticket_first_status": "SELECT ticket_id, status AS first_status FROM (SELECT ticket_id, status, ROW_NUMBER() OVER (PARTITION BY ticket_id ORDER BY history_id) AS rn FROM stg_ticket_history) WHERE rn = 1",
        "rpt_open_tickets_by_agent": "WITH latest AS (SELECT ticket_id, status, agent_id, ROW_NUMBER() OVER (PARTITION BY ticket_id ORDER BY id DESC) AS rn FROM ticket_status_history) SELECT agent_id, COUNT(*) AS open_tickets FROM latest WHERE rn = 1 AND status = 'open' GROUP BY agent_id",
        "rpt_status_mix": "SELECT status, COUNT(*) AS tickets FROM int_ticket_current_status GROUP BY status",
    },
    ["rpt_open_tickets_by_agent", "rpt_status_mix"],
    {
        "int_ticket_current_status": "WITH ranked AS (SELECT ticket_id, status, agent_id, ROW_NUMBER() OVER (PARTITION BY ticket_id ORDER BY id DESC) AS rn FROM ticket_status_history) SELECT ticket_id, status, agent_id FROM ranked WHERE rn = 1",
        "rpt_open_tickets_by_agent": "SELECT agent_id, COUNT(*) AS open_tickets FROM int_ticket_current_status WHERE status = 'open' GROUP BY agent_id",
        "rpt_status_mix": KEEP,
    },
    [
        ("filters to open changes before picking the latest one, so a ticket opened earlier counts",
         {"rpt_open_tickets_by_agent": "WITH latest AS (SELECT ticket_id, agent_id, ROW_NUMBER() OVER (PARTITION BY ticket_id ORDER BY id DESC) AS rn FROM ticket_status_history WHERE status = 'open') SELECT agent_id, COUNT(*) AS open_tickets FROM latest WHERE rn = 1 GROUP BY agent_id"}),
    ],
    "The report's CTE repeats the latest-status table; ranking must happen before the status filter.",
))

# Orders: OR chains become IN lists and an implied NOT NULL filter goes.
add(case(
    ["mergeable_tables", "passthrough_chain", "redundant_filters"],
    {
        "stg_orders": "SELECT id AS order_id, customer_id, order_total, status, channel FROM raw_orders WHERE status = 'completed' OR status = 'pending'",
        "int_digital_orders": "SELECT * FROM stg_orders WHERE (channel = 'web' OR channel = 'app') AND status IS NOT NULL",
        "rpt_digital_revenue": "SELECT channel, SUM(order_total) AS revenue, COUNT(*) AS orders FROM int_digital_orders GROUP BY channel",
    },
    ["rpt_digital_revenue"],
    {
        "rpt_digital_revenue": "SELECT channel, SUM(order_total) AS revenue, COUNT(*) AS orders FROM raw_orders WHERE status IN ('completed', 'pending') AND channel IN ('web', 'app') GROUP BY channel",
    },
    [
        ("keeps status IS NOT NULL and drops the status list it seemed to restate",
         {"rpt_digital_revenue": "SELECT channel, SUM(order_total) AS revenue, COUNT(*) AS orders FROM raw_orders WHERE status IS NOT NULL AND channel IN ('web', 'app') GROUP BY channel"}),
        ("rewrites the channel list as channel <> 'store'",
         {"rpt_digital_revenue": "SELECT channel, SUM(order_total) AS revenue, COUNT(*) AS orders FROM raw_orders WHERE status IN ('completed', 'pending') AND channel <> 'store' GROUP BY channel"}),
    ],
))

# HR: unused left joins to keyed tables (department and a manager self join) and a dead report.
add(case(
    ["dead_tables", "passthrough_chain", "unused_columns_joins"],
    {
        "stg_employees": "SELECT id AS employee_id, department_id, manager_id, salary, status, level FROM employees",
        "stg_departments": "SELECT id AS department_id, name AS department_name, cost_center FROM departments",
        "int_employees_enriched": "SELECT e.employee_id, e.department_id, e.salary, e.status, e.level, d.department_name, d.cost_center, m.level AS manager_level FROM stg_employees AS e LEFT JOIN stg_departments AS d ON e.department_id = d.department_id LEFT JOIN stg_employees AS m ON e.manager_id = m.employee_id",
        "rpt_headcount_by_level": "SELECT level, COUNT(*) AS headcount, SUM(salary) AS payroll FROM int_employees_enriched WHERE status = 'active' GROUP BY level",
        "rpt_cost_center_payroll": "SELECT cost_center, SUM(salary) AS payroll FROM int_employees_enriched WHERE status <> 'terminated' GROUP BY cost_center",
        "rpt_manager_levels": "SELECT manager_level, COUNT(*) AS reports FROM int_employees_enriched GROUP BY manager_level",
    },
    ["rpt_headcount_by_level", "rpt_cost_center_payroll"],
    {
        "rpt_headcount_by_level": "SELECT level, COUNT(*) AS headcount, SUM(salary) AS payroll FROM employees WHERE status = 'active' GROUP BY level",
        "rpt_cost_center_payroll": "SELECT d.cost_center, SUM(e.salary) AS payroll FROM employees AS e LEFT JOIN departments AS d ON d.id = e.department_id WHERE e.status <> 'terminated' GROUP BY d.cost_center",
    },
    [
        ("joins departments with an inner join, dropping employees without a department",
         {"rpt_cost_center_payroll": "SELECT d.cost_center, SUM(e.salary) AS payroll FROM employees AS e JOIN departments AS d ON d.id = e.department_id WHERE e.status <> 'terminated' GROUP BY d.cost_center"}),
    ],
    "Both left joins in the wide table are on the other table's key, so they go where unused.",
))

# Irreducible: logistics reports with anti-join, DISTINCT and COUNT(col).
add(case(
    ["irreducible"],
    {
        "rpt_carrier_volume": "SELECT s.carrier_code, c.name AS carrier_name, COUNT(*) AS shipments FROM shipments AS s LEFT JOIN carriers AS c ON c.code = s.carrier_code GROUP BY s.carrier_code, c.name",
        "rpt_lost_orders": "SELECT DISTINCT order_id FROM shipments WHERE status = 'lost'",
        "rpt_unscanned_shipments": "SELECT s.id, s.carrier_code FROM shipments AS s LEFT JOIN shipment_events AS e ON e.shipment_id = s.id AND e.kind = 'scanned' WHERE e.id IS NULL",
        "rpt_sequenced_scans": "SELECT shipment_id, COUNT(seq) AS sequenced_scans FROM shipment_events WHERE kind = 'scanned' GROUP BY shipment_id",
    },
    ["rpt_carrier_volume", "rpt_lost_orders", "rpt_unscanned_shipments", "rpt_sequenced_scans"],
    {"rpt_carrier_volume": KEEP, "rpt_lost_orders": KEEP, "rpt_unscanned_shipments": KEEP, "rpt_sequenced_scans": KEEP},
    [
        ("joins carriers with an inner join, dropping shipments with an unknown carrier",
         {"rpt_carrier_volume": "SELECT s.carrier_code, c.name AS carrier_name, COUNT(*) AS shipments FROM shipments AS s JOIN carriers AS c ON c.code = s.carrier_code GROUP BY s.carrier_code, c.name"}),
        ("drops the DISTINCT although an order can have several lost shipments",
         {"rpt_lost_orders": "SELECT order_id FROM shipments WHERE status = 'lost'"}),
        ("drops the event-kind condition, treating any event as a scan",
         {"rpt_unscanned_shipments": "SELECT s.id, s.carrier_code FROM shipments AS s LEFT JOIN shipment_events AS e ON e.shipment_id = s.id WHERE e.id IS NULL"}),
        ("counts rows instead of non-NULL sequence numbers",
         {"rpt_sequenced_scans": "SELECT shipment_id, COUNT(*) AS sequenced_scans FROM shipment_events WHERE kind = 'scanned' GROUP BY shipment_id"}),
    ],
    "Irreducible: four protected reports, each with a trap.",
))

# App usage: DISTINCT, UNION DISTINCT and COUNT(DISTINCT) interplay.
add(case(
    ["mergeable_tables", "passthrough_chain"],
    {
        "stg_usage": "SELECT user_id, feature, units FROM usage_events",
        "int_feature_users": "SELECT DISTINCT user_id, feature FROM stg_usage WHERE user_id IS NOT NULL",
        "int_export_or_share_users": "SELECT user_id FROM int_feature_users WHERE feature = 'export' UNION DISTINCT SELECT user_id FROM int_feature_users WHERE feature = 'share'",
        "rpt_collaborators": "SELECT u.account_id, COUNT(*) AS collaborators FROM int_export_or_share_users AS x JOIN app_users AS u ON u.id = x.user_id GROUP BY u.account_id",
        "rpt_feature_reach": "SELECT feature, COUNT(*) AS users FROM int_feature_users GROUP BY feature",
    },
    ["rpt_collaborators", "rpt_feature_reach"],
    {
        "rpt_collaborators": "SELECT u.account_id, COUNT(DISTINCT u.id) AS collaborators FROM usage_events AS e JOIN app_users AS u ON u.id = e.user_id WHERE e.feature IN ('export', 'share') GROUP BY u.account_id",
        "rpt_feature_reach": "SELECT feature, COUNT(DISTINCT user_id) AS users FROM usage_events WHERE user_id IS NOT NULL GROUP BY feature",
    },
    [
        ("drops user_id IS NOT NULL because COUNT(DISTINCT) skips NULLs, but a feature used only anonymously then shows 0",
         {"rpt_feature_reach": "SELECT feature, COUNT(DISTINCT user_id) AS users FROM usage_events GROUP BY feature"}),
        ("counts collaborators with COUNT(*) after joining raw events",
         {"rpt_collaborators": "SELECT u.account_id, COUNT(*) AS collaborators FROM usage_events AS e JOIN app_users AS u ON u.id = e.user_id WHERE e.feature IN ('export', 'share') GROUP BY u.account_id"}),
        ("unions export and share users with UNION ALL",
         {"int_export_or_share_users": "SELECT user_id FROM int_feature_users WHERE feature = 'export' UNION ALL SELECT user_id FROM int_feature_users WHERE feature = 'share'"},
         "original"),
    ],
    "Distinct user sets collapse into COUNT(DISTINCT); every DISTINCT and NULL filter has a trap.",
))

# ================================================================== medium cases (6 to 11 tables)

# SaaS: latest subscription per account written twice (subquery and CTE).
add(case(
    ["duplicated_logic", "mergeable_tables", "passthrough_chain"],
    {
        "stg_subscriptions": "SELECT id AS subscription_id, account_id, plan, mrr, status, started_on FROM subscriptions",
        "int_latest_subscription": "SELECT * EXCEPT (rn) FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY account_id ORDER BY started_on DESC, subscription_id DESC) AS rn FROM stg_subscriptions) WHERE rn = 1",
        "int_account_current_plan": "WITH ranked AS (SELECT account_id, plan, mrr, ROW_NUMBER() OVER (PARTITION BY account_id ORDER BY started_on DESC, id DESC) AS rn FROM subscriptions) SELECT account_id, plan, mrr FROM ranked WHERE rn = 1",
        "dim_accounts": "SELECT a.id AS account_id, a.name, a.region, s.plan AS current_plan, s.mrr AS current_mrr FROM accounts AS a LEFT JOIN int_latest_subscription AS s ON s.account_id = a.id",
        "rpt_mrr_by_plan": "SELECT plan, SUM(mrr) AS mrr, COUNT(*) AS accounts FROM int_account_current_plan GROUP BY plan",
        "rpt_active_mrr": "SELECT SUM(mrr) AS active_mrr FROM int_latest_subscription WHERE status = 'active'",
    },
    ["dim_accounts", "rpt_mrr_by_plan", "rpt_active_mrr"],
    {
        "int_latest_subscription": "WITH ranked AS (SELECT account_id, plan, mrr, status, ROW_NUMBER() OVER (PARTITION BY account_id ORDER BY started_on DESC, id DESC) AS rn FROM subscriptions) SELECT account_id, plan, mrr, status FROM ranked WHERE rn = 1",
        "dim_accounts": KEEP,
        "rpt_mrr_by_plan": "SELECT plan, SUM(mrr) AS mrr, COUNT(*) AS accounts FROM int_latest_subscription GROUP BY plan",
        "rpt_active_mrr": KEEP,
    },
    [
        ("sums every active subscription instead of each account's latest one",
         {"rpt_active_mrr": "SELECT SUM(mrr) AS active_mrr FROM subscriptions WHERE status = 'active'"}),
        ("reads the plan mix from dim_accounts, which keeps accounts without subscriptions and drops orphan subscriptions",
         {"rpt_mrr_by_plan": "SELECT current_plan AS plan, SUM(current_mrr) AS mrr, COUNT(*) AS accounts FROM dim_accounts GROUP BY current_plan"}),
    ],
    "ROW_NUMBER is ordered on the start date and then the subscription key, so it is deterministic.",
))

# Logistics: wide shipment table with unused join, CASE and window columns; event counts fold in.
add(case(
    ["dead_tables", "mergeable_tables", "passthrough_chain", "unused_columns_joins"],
    {
        "stg_shipments": "SELECT id AS shipment_id, order_id, carrier_code, status AS shipment_status, weight_g FROM shipments",
        "stg_shipment_events": "SELECT id AS event_id, shipment_id, kind AS event_kind, seq FROM shipment_events",
        "stg_carriers": "SELECT code AS carrier_code, name AS carrier_name, is_active FROM carriers",
        "int_shipments_enriched": "SELECT s.shipment_id, s.order_id, s.shipment_status, s.weight_g, c.carrier_name, c.is_active AS carrier_is_active, CASE WHEN s.weight_g > 2 THEN 'heavy' ELSE 'light' END AS weight_band, ROW_NUMBER() OVER (PARTITION BY s.order_id ORDER BY s.shipment_id) AS shipment_number FROM stg_shipments AS s LEFT JOIN stg_carriers AS c ON c.carrier_code = s.carrier_code",
        "int_event_counts": "SELECT shipment_id, COUNT(*) AS events, COUNTIF(event_kind = 'scanned') AS scans FROM stg_shipment_events GROUP BY shipment_id",
        "fct_shipments": "SELECT s.shipment_id, s.order_id, s.shipment_status, s.weight_band, COALESCE(e.events, 0) AS events, COALESCE(e.scans, 0) AS scans FROM int_shipments_enriched AS s LEFT JOIN int_event_counts AS e ON e.shipment_id = s.shipment_id",
        "rpt_order_shipment_counts": "SELECT order_id, MAX(shipment_number) AS shipments FROM int_shipments_enriched GROUP BY order_id",
    },
    ["fct_shipments"],
    {
        "fct_shipments": "SELECT s.id AS shipment_id, s.order_id, s.status AS shipment_status, IF(s.weight_g > 2, 'heavy', 'light') AS weight_band, COUNT(e.id) AS events, SUM(IF(e.kind = 'scanned', 1, 0)) AS scans FROM shipments AS s LEFT JOIN shipment_events AS e ON e.shipment_id = s.id GROUP BY s.id, s.order_id, s.status, s.weight_g",
    },
    [
        ("counts events with COUNT(*) after the left join, so shipments without events get 1",
         {"fct_shipments": "SELECT s.id AS shipment_id, s.order_id, s.status AS shipment_status, IF(s.weight_g > 2, 'heavy', 'light') AS weight_band, COUNT(*) AS events, SUM(IF(e.kind = 'scanned', 1, 0)) AS scans FROM shipments AS s LEFT JOIN shipment_events AS e ON e.shipment_id = s.id GROUP BY s.id, s.order_id, s.status, s.weight_g"}),
    ],
    "The event aggregate folds into the fact table by grouping on the shipment key.",
))

# Finance: signed amounts, a CTE that repeats the typed-entries table, and a dead check.
add(case(
    ["cte_repeats_table", "dead_tables", "mergeable_tables", "passthrough_chain"],
    {
        "stg_gl_entries": "SELECT id AS entry_id, account_code, entity, CASE WHEN side = 'debit' THEN amount WHEN side = 'credit' THEN -amount END AS signed_amount, posted_on FROM gl_entries",
        "stg_gl_accounts": "SELECT code AS account_code, name AS account_name, account_type FROM gl_accounts",
        "int_entries_typed": "SELECT e.entry_id, e.entity, e.signed_amount, e.posted_on, a.account_type, a.account_name FROM stg_gl_entries AS e LEFT JOIN stg_gl_accounts AS a ON a.account_code = e.account_code",
        "rpt_trial_balance": "SELECT entity, account_type, SUM(signed_amount) AS balance FROM int_entries_typed GROUP BY entity, account_type",
        "rpt_income_statement": "WITH typed AS (SELECT e.entity, a.account_type, CASE WHEN e.side = 'debit' THEN e.amount WHEN e.side = 'credit' THEN -e.amount END AS signed_amount FROM gl_entries AS e LEFT JOIN gl_accounts AS a ON a.code = e.account_code) SELECT entity, SUM(CASE WHEN account_type = 'revenue' THEN -signed_amount ELSE 0 END) AS revenue, SUM(CASE WHEN account_type = 'expense' THEN signed_amount ELSE 0 END) AS expenses FROM typed GROUP BY entity",
        "rpt_entity_balance_usd": "SELECT t.entity, SUM(t.balance * f.rate) AS balance_usd FROM rpt_trial_balance AS t JOIN fx_rates AS f ON f.entity = t.entity GROUP BY t.entity",
        "tmp_unbalanced_entries": "SELECT entry_id FROM stg_gl_entries WHERE signed_amount IS NULL",
    },
    ["rpt_trial_balance", "rpt_income_statement", "rpt_entity_balance_usd"],
    {
        "int_entries_typed": "SELECT e.entity, a.account_type, CASE e.side WHEN 'debit' THEN e.amount WHEN 'credit' THEN -e.amount END AS signed_amount FROM gl_entries AS e LEFT JOIN gl_accounts AS a ON a.code = e.account_code",
        "rpt_trial_balance": KEEP,
        "rpt_income_statement": "SELECT entity, SUM(IF(account_type = 'revenue', -signed_amount, 0)) AS revenue, SUM(IF(account_type = 'expense', signed_amount, 0)) AS expenses FROM int_entries_typed GROUP BY entity",
        "rpt_entity_balance_usd": KEEP,
    },
    [
        ("signs amounts with IF(side = 'credit', ...), treating a NULL or unknown side as a debit",
         {"int_entries_typed": "SELECT e.entity, a.account_type, IF(e.side = 'credit', -e.amount, e.amount) AS signed_amount FROM gl_entries AS e LEFT JOIN gl_accounts AS a ON a.code = e.account_code"}),
    ],
    "The income statement's CTE repeats the typed-entries table.",
))

# Inventory: per-kind tables unioned into a ledger, merged into one signed sum.
add(case(
    ["dead_tables", "mergeable_tables", "passthrough_chain"],
    {
        "stg_stock_moves": "SELECT id AS move_id, sku, warehouse, qty, kind FROM stock_moves",
        "int_receipts": "SELECT sku, warehouse, qty FROM stg_stock_moves WHERE kind = 'receipt'",
        "int_shipments_out": "SELECT sku, warehouse, -qty AS qty FROM stg_stock_moves WHERE kind = 'shipment'",
        "int_adjustments": "SELECT sku, warehouse, qty FROM stg_stock_moves WHERE kind = 'adjustment' AND qty IS NOT NULL",
        "int_inventory_ledger": "SELECT * FROM int_receipts UNION ALL SELECT * FROM int_shipments_out UNION ALL SELECT * FROM int_adjustments",
        "fct_stock_on_hand": "SELECT l.sku, l.warehouse, w.region, SUM(l.qty) AS on_hand FROM int_inventory_ledger AS l LEFT JOIN warehouses AS w ON w.code = l.warehouse GROUP BY l.sku, l.warehouse, w.region",
        "rpt_stock_value": "SELECT s.sku, SUM(s.on_hand * k.unit_cost) AS stock_value FROM fct_stock_on_hand AS s JOIN skus AS k ON k.sku = s.sku GROUP BY s.sku",
        "rpt_receipts_by_sku": "SELECT sku, SUM(qty) AS received FROM int_receipts GROUP BY sku",
    },
    ["fct_stock_on_hand", "rpt_stock_value"],
    {
        "fct_stock_on_hand": "SELECT m.sku, m.warehouse, w.region, SUM(IF(m.kind = 'shipment', -m.qty, m.qty)) AS on_hand FROM stock_moves AS m LEFT JOIN warehouses AS w ON w.code = m.warehouse WHERE m.kind IN ('receipt', 'shipment') OR (m.kind = 'adjustment' AND m.qty IS NOT NULL) GROUP BY m.sku, m.warehouse, w.region",
        "rpt_stock_value": KEEP,
    },
    [
        ("drops qty IS NOT NULL on adjustments because SUM ignores NULLs, which adds empty sku/warehouse groups",
         {"fct_stock_on_hand": "SELECT m.sku, m.warehouse, w.region, SUM(IF(m.kind = 'shipment', -m.qty, m.qty)) AS on_hand FROM stock_moves AS m LEFT JOIN warehouses AS w ON w.code = m.warehouse WHERE m.kind IN ('receipt', 'shipment', 'adjustment') GROUP BY m.sku, m.warehouse, w.region"}),
    ],
    "Three per-kind tables and their union become one conditional sum.",
))

# Marketing: conversions through IN subqueries and a semi-join, rewritten as joins with DISTINCT counts.
add(case(
    ["dead_tables", "mergeable_tables", "passthrough_chain"],
    {
        "stg_sessions": "SELECT id AS session_id, visitor_id, campaign_id, landing_page, started_at FROM web_sessions",
        "stg_signups": "SELECT id AS signup_id, session_id, plan FROM signups",
        "stg_campaigns": "SELECT id AS campaign_id, name AS campaign_name, channel FROM campaigns",
        "int_sessions_with_campaign": "SELECT s.session_id, s.visitor_id, s.landing_page, c.campaign_name, c.channel FROM stg_sessions AS s LEFT JOIN stg_campaigns AS c ON c.campaign_id = s.campaign_id",
        "int_converting_sessions": "SELECT * FROM int_sessions_with_campaign WHERE session_id IN (SELECT session_id FROM stg_signups)",
        "rpt_channel_conversion": "SELECT s.channel, COUNT(*) AS sessions, COUNT(v.session_id) AS converted FROM int_sessions_with_campaign AS s LEFT JOIN int_converting_sessions AS v ON v.session_id = s.session_id GROUP BY s.channel",
        "rpt_landing_page_conversions": "SELECT landing_page, COUNT(DISTINCT visitor_id) AS converting_visitors FROM int_converting_sessions GROUP BY landing_page",
        "rpt_campaign_names": "SELECT DISTINCT campaign_name FROM int_sessions_with_campaign",
    },
    ["rpt_channel_conversion", "rpt_landing_page_conversions"],
    {
        "rpt_channel_conversion": "SELECT c.channel, COUNT(DISTINCT s.id) AS sessions, COUNT(DISTINCT g.session_id) AS converted FROM web_sessions AS s LEFT JOIN campaigns AS c ON c.id = s.campaign_id LEFT JOIN signups AS g ON g.session_id = s.id GROUP BY c.channel",
        "rpt_landing_page_conversions": "SELECT s.landing_page, COUNT(DISTINCT s.visitor_id) AS converting_visitors FROM web_sessions AS s JOIN signups AS g ON g.session_id = s.id GROUP BY s.landing_page",
    },
    [
        ("counts sessions with COUNT(*) after joining signups, so a session with two signups counts twice",
         {"rpt_channel_conversion": "SELECT c.channel, COUNT(*) AS sessions, COUNT(DISTINCT g.session_id) AS converted FROM web_sessions AS s LEFT JOIN campaigns AS c ON c.id = s.campaign_id LEFT JOIN signups AS g ON g.session_id = s.id GROUP BY c.channel"}),
        ("counts converted sessions without DISTINCT, which counts signups",
         {"rpt_channel_conversion": "SELECT c.channel, COUNT(DISTINCT s.id) AS sessions, COUNT(g.session_id) AS converted FROM web_sessions AS s LEFT JOIN campaigns AS c ON c.id = s.campaign_id LEFT JOIN signups AS g ON g.session_id = s.id GROUP BY c.channel"}),
    ],
    "The IN subquery becomes a join; the duplicates it brings are absorbed by COUNT(DISTINCT).",
))

# SaaS product usage: DISTINCT over a key, NULL-user filter, per-user then per-account rollups.
add(case(
    ["dead_tables", "mergeable_tables", "passthrough_chain", "redundant_filters", "unused_columns_joins"],
    {
        "stg_users": "SELECT DISTINCT id AS user_id, account_id, role, is_active FROM app_users",
        "stg_usage": "SELECT user_id, feature, units FROM usage_events WHERE user_id IS NOT NULL",
        "int_active_users": "SELECT * FROM stg_users WHERE is_active",
        "int_usage_by_user": "SELECT user_id, feature, SUM(units) AS units, COUNT(*) AS uses FROM stg_usage GROUP BY user_id, feature",
        "int_account_feature_usage": "SELECT u.account_id, x.feature, SUM(x.units) AS units, SUM(x.uses) AS uses FROM int_usage_by_user AS x JOIN int_active_users AS u ON u.user_id = x.user_id GROUP BY u.account_id, x.feature",
        "rpt_power_accounts": "SELECT account_id, SUM(uses) AS total_uses FROM int_account_feature_usage GROUP BY account_id HAVING SUM(uses) >= 3",
        "rpt_feature_adoption": "SELECT feature, COUNT(DISTINCT account_id) AS accounts FROM int_account_feature_usage GROUP BY feature",
        "rpt_export_heavy_users": "SELECT user_id, units FROM int_usage_by_user WHERE feature = 'export' AND units > 2",
        "tmp_inactive_users": "SELECT user_id FROM stg_users WHERE NOT is_active",
    },
    ["rpt_power_accounts", "rpt_feature_adoption", "rpt_export_heavy_users"],
    {
        "int_account_feature_usage": "SELECT u.account_id, e.feature, COUNT(*) AS uses FROM usage_events AS e JOIN app_users AS u ON u.id = e.user_id WHERE u.is_active GROUP BY u.account_id, e.feature",
        "rpt_power_accounts": KEEP,
        "rpt_feature_adoption": KEEP,
        "rpt_export_heavy_users": "SELECT user_id, SUM(units) AS units FROM usage_events WHERE feature = 'export' AND user_id IS NOT NULL GROUP BY user_id HAVING SUM(units) > 2",
    },
    [
        ("drops user_id IS NOT NULL in the export report, which then shows anonymous usage",
         {"rpt_export_heavy_users": "SELECT user_id, SUM(units) AS units FROM usage_events WHERE feature = 'export' GROUP BY user_id HAVING SUM(units) > 2"}),
        ("filters single events on units > 2 instead of each user's total",
         {"rpt_export_heavy_users": "SELECT user_id, units FROM usage_events WHERE feature = 'export' AND user_id IS NOT NULL AND units > 2"}),
    ],
    "The join to app_users drops NULL users by itself; the export report still needs the filter.",
))

# Support: bot filter must stay in the ON clause; COUNT of a left-joined key.
add(case(
    ["mergeable_tables", "passthrough_chain"],
    {
        "stg_tickets": "SELECT id AS ticket_id, requester_id, assignee_id, priority, status, channel FROM tickets",
        "stg_agents": "SELECT id AS agent_id, name AS agent_name, team, is_bot FROM agents",
        "stg_comments": "SELECT id AS comment_id, ticket_id, author_id, is_public FROM ticket_comments",
        "int_human_agents": "SELECT agent_id, agent_name, team FROM stg_agents WHERE is_bot = FALSE",
        "int_tickets_assigned": "SELECT t.*, a.agent_name, a.team FROM stg_tickets AS t LEFT JOIN int_human_agents AS a ON a.agent_id = t.assignee_id",
        "int_ticket_reply_counts": "SELECT ticket_id, COUNT(*) AS public_replies FROM stg_comments WHERE is_public GROUP BY ticket_id",
        "fct_tickets": "SELECT t.ticket_id, t.priority, t.status, t.team, COALESCE(r.public_replies, 0) AS public_replies FROM int_tickets_assigned AS t LEFT JOIN int_ticket_reply_counts AS r ON r.ticket_id = t.ticket_id",
        "rpt_team_backlog": "SELECT team, COUNT(*) AS open_tickets FROM int_tickets_assigned WHERE status = 'open' AND team IS NOT NULL GROUP BY team",
        "rpt_unanswered_urgent": "SELECT ticket_id FROM fct_tickets WHERE priority = 'urgent' AND public_replies = 0",
    },
    ["fct_tickets", "rpt_team_backlog", "rpt_unanswered_urgent"],
    {
        "fct_tickets": "SELECT t.id AS ticket_id, t.priority, t.status, a.team, COUNT(c.id) AS public_replies FROM tickets AS t LEFT JOIN agents AS a ON a.id = t.assignee_id AND a.is_bot = FALSE LEFT JOIN ticket_comments AS c ON c.ticket_id = t.id AND c.is_public GROUP BY t.id, t.priority, t.status, a.team",
        "rpt_team_backlog": "SELECT team, COUNT(*) AS open_tickets FROM fct_tickets WHERE status = 'open' AND team IS NOT NULL GROUP BY team",
        "rpt_unanswered_urgent": KEEP,
    },
    [
        ("moves the bot filter to WHERE, dropping unassigned tickets and those assigned to bots",
         {"fct_tickets": "SELECT t.id AS ticket_id, t.priority, t.status, a.team, COUNT(c.id) AS public_replies FROM tickets AS t LEFT JOIN agents AS a ON a.id = t.assignee_id LEFT JOIN ticket_comments AS c ON c.ticket_id = t.id AND c.is_public WHERE a.is_bot = FALSE GROUP BY t.id, t.priority, t.status, a.team"}),
        ("counts replies with COUNT(*) after the left join, so tickets without replies get 1",
         {"fct_tickets": "SELECT t.id AS ticket_id, t.priority, t.status, a.team, COUNT(*) AS public_replies FROM tickets AS t LEFT JOIN agents AS a ON a.id = t.assignee_id AND a.is_bot = FALSE LEFT JOIN ticket_comments AS c ON c.ticket_id = t.id AND c.is_public GROUP BY t.id, t.priority, t.status, a.team"}),
    ],
    "The agent filter and the reply count fold into the fact table's left joins.",
))

# E-commerce customer 360: ranking traps (filter before or after ROW_NUMBER, LOWER in the partition).
add(case(
    ["mergeable_tables", "passthrough_chain", "unused_columns_joins"],
    {
        "stg_customers": "SELECT id AS customer_id, LOWER(email) AS email, country, is_test FROM raw_customers",
        "stg_orders": "SELECT id AS order_id, customer_id, order_total, status, channel, created_on FROM raw_orders",
        "int_customers_deduped": "WITH ranked AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY email ORDER BY customer_id) AS rn FROM stg_customers WHERE NOT is_test) SELECT customer_id, email, country FROM ranked WHERE rn = 1",
        "int_completed_orders": "SELECT * FROM stg_orders WHERE status = 'completed'",
        "int_customer_order_stats": "SELECT customer_id, COUNT(*) AS orders, SUM(order_total) AS revenue, MIN(created_on) AS first_order_on, MAX(created_on) AS last_order_on FROM int_completed_orders GROUP BY customer_id",
        "int_order_ranks": "SELECT order_id, customer_id, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_id) AS order_seq, SUM(order_total) OVER (PARTITION BY customer_id ORDER BY order_id) AS running_revenue FROM int_completed_orders",
        "dim_customers": "SELECT c.customer_id, c.email, c.country, s.orders, s.revenue, s.first_order_on FROM int_customers_deduped AS c LEFT JOIN int_customer_order_stats AS s ON s.customer_id = c.customer_id",
        "rpt_repeat_rate": "SELECT country, COUNT(*) AS customers, COUNTIF(orders > 1) AS repeat_customers FROM dim_customers GROUP BY country",
        "rpt_second_orders": "SELECT o.customer_id, o.order_id FROM int_order_ranks AS o WHERE o.order_seq = 2",
        "rpt_revenue_by_channel": "SELECT channel, SUM(order_total) AS revenue FROM int_completed_orders GROUP BY channel",
    },
    ["dim_customers", "rpt_repeat_rate", "rpt_second_orders", "rpt_revenue_by_channel"],
    {
        "int_customers_deduped": "WITH ranked AS (SELECT id AS customer_id, LOWER(email) AS email, country, ROW_NUMBER() OVER (PARTITION BY LOWER(email) ORDER BY id) AS rn FROM raw_customers WHERE NOT is_test) SELECT customer_id, email, country FROM ranked WHERE rn = 1",
        "int_customer_order_stats": "SELECT customer_id, COUNT(*) AS orders, SUM(order_total) AS revenue, MIN(created_on) AS first_order_on FROM raw_orders WHERE status = 'completed' GROUP BY customer_id",
        "dim_customers": KEEP,
        "rpt_repeat_rate": KEEP,
        "rpt_second_orders": "WITH ranked AS (SELECT id AS order_id, customer_id, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY id) AS order_seq FROM raw_orders WHERE status = 'completed') SELECT customer_id, order_id FROM ranked WHERE order_seq = 2",
        "rpt_revenue_by_channel": "SELECT channel, SUM(order_total) AS revenue FROM raw_orders WHERE status = 'completed' GROUP BY channel",
    },
    [
        ("numbers all orders and keeps completed ones afterwards",
         {"rpt_second_orders": "WITH ranked AS (SELECT id AS order_id, customer_id, status, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY id) AS order_seq FROM raw_orders) SELECT customer_id, order_id FROM ranked WHERE order_seq = 2 AND status = 'completed'"}),
        ("dedups customers before dropping test accounts",
         {"int_customers_deduped": "WITH ranked AS (SELECT id AS customer_id, LOWER(email) AS email, country, is_test, ROW_NUMBER() OVER (PARTITION BY LOWER(email) ORDER BY id) AS rn FROM raw_customers) SELECT customer_id, email, country FROM ranked WHERE rn = 1 AND NOT is_test"}),
        ("partitions on the raw email, so case variants of one address are kept twice",
         {"int_customers_deduped": "WITH ranked AS (SELECT id AS customer_id, LOWER(email) AS email, country, ROW_NUMBER() OVER (PARTITION BY email ORDER BY id) AS rn FROM raw_customers WHERE NOT is_test) SELECT customer_id, email, country FROM ranked WHERE rn = 1"}),
    ],
    "Unused window columns go; every ROW_NUMBER is ordered on a key.",
))

# HR timesheets: duplicated weekly rollups, per-week HAVING, COUNT(DISTINCT).
add(case(
    ["dead_tables", "duplicated_logic", "mergeable_tables", "passthrough_chain"],
    {
        "stg_employees": "SELECT id AS employee_id, department_id, manager_id, salary, status, level FROM employees",
        "stg_departments": "SELECT id AS department_id, name AS department_name, cost_center FROM departments",
        "stg_timesheets": "SELECT employee_id, week, project, hours FROM timesheets",
        "int_active_employees": "SELECT * FROM stg_employees WHERE status IN ('active', 'leave')",
        "int_weekly_hours": "SELECT employee_id, week, SUM(hours) AS hours FROM stg_timesheets GROUP BY employee_id, week",
        "int_weekly_hours_v2": "SELECT t.employee_id, t.week, SUM(t.hours) AS hours FROM timesheets AS t GROUP BY 1, 2",
        "rpt_overtime": "SELECT w.employee_id, w.week, w.hours - 40 AS overtime_hours FROM int_weekly_hours AS w JOIN int_active_employees AS e ON e.employee_id = w.employee_id WHERE w.hours > 40",
        "rpt_department_hours": "SELECT d.department_name, SUM(w.hours) AS hours FROM int_weekly_hours_v2 AS w JOIN stg_employees AS e ON e.employee_id = w.employee_id LEFT JOIN stg_departments AS d ON d.department_id = e.department_id GROUP BY d.department_name",
        "rpt_project_staffing": "SELECT project, COUNT(DISTINCT employee_id) AS people FROM stg_timesheets WHERE hours > 0 GROUP BY project",
        "rpt_managers": "SELECT DISTINCT manager_id FROM stg_employees",
    },
    ["rpt_overtime", "rpt_department_hours", "rpt_project_staffing"],
    {
        "rpt_overtime": "SELECT t.employee_id, t.week, SUM(t.hours) - 40 AS overtime_hours FROM timesheets AS t JOIN employees AS e ON e.id = t.employee_id WHERE e.status IN ('active', 'leave') GROUP BY t.employee_id, t.week HAVING SUM(t.hours) > 40",
        "rpt_department_hours": "SELECT d.name AS department_name, SUM(t.hours) AS hours FROM timesheets AS t JOIN employees AS e ON e.id = t.employee_id LEFT JOIN departments AS d ON d.id = e.department_id GROUP BY d.name",
        "rpt_project_staffing": "SELECT project, COUNT(DISTINCT employee_id) AS people FROM timesheets WHERE hours > 0 GROUP BY project",
    },
    [
        ("checks overtime on single timesheet rows instead of weekly totals",
         {"rpt_overtime": "SELECT t.employee_id, t.week, t.hours - 40 AS overtime_hours FROM timesheets AS t JOIN employees AS e ON e.id = t.employee_id WHERE e.status IN ('active', 'leave') AND t.hours > 40"}),
        ("counts staffed people without DISTINCT",
         {"rpt_project_staffing": "SELECT project, COUNT(employee_id) AS people FROM timesheets WHERE hours > 0 GROUP BY project"}),
    ],
    "Two copies of the weekly rollup fold into their readers; the employee join is on the key.",
))

# Marketing: three ad platforms; staging filters make a later NOT NULL filter redundant; CTE repeats the union.
add(case(
    ["cte_repeats_table", "dead_tables", "mergeable_tables", "passthrough_chain", "redundant_filters"],
    {
        "stg_google_ads": "SELECT campaign_id, spend_date, spend, clicks, impressions FROM google_ads_daily WHERE spend >= 0",
        "stg_meta_ads": "SELECT campaign_id, spend_date, spend, clicks, impressions FROM meta_ads_daily WHERE spend >= 0",
        "stg_bing_ads": "SELECT campaign_id, spend_date, spend, clicks, impressions FROM bing_ads_daily WHERE spend >= 0",
        "int_ads_unioned": "SELECT 'google' AS source, * FROM stg_google_ads UNION ALL SELECT 'meta' AS source, * FROM stg_meta_ads UNION ALL SELECT 'bing' AS source, * FROM stg_bing_ads",
        "int_ads_positive": "SELECT * FROM int_ads_unioned WHERE spend IS NOT NULL",
        "stg_campaigns": "SELECT id AS campaign_id, name AS campaign_name, channel, budget FROM campaigns",
        "int_campaign_spend": "SELECT a.campaign_id, c.campaign_name, c.budget, SUM(a.spend) AS spend, SUM(a.clicks) AS clicks FROM int_ads_positive AS a LEFT JOIN stg_campaigns AS c ON c.campaign_id = a.campaign_id GROUP BY a.campaign_id, c.campaign_name, c.budget",
        "rpt_budget_pacing": "SELECT campaign_id, campaign_name, spend, budget, spend > budget AS over_budget FROM int_campaign_spend",
        "rpt_source_mix": "WITH unioned AS (SELECT 'google' AS source, spend FROM google_ads_daily WHERE spend >= 0 UNION ALL SELECT 'meta' AS source, spend FROM meta_ads_daily WHERE spend >= 0 UNION ALL SELECT 'bing' AS source, spend FROM bing_ads_daily WHERE spend >= 0) SELECT source, SUM(spend) AS spend FROM unioned GROUP BY source",
        "rpt_daily_clicks": "SELECT spend_date, SUM(clicks) AS clicks, SUM(impressions) AS impressions FROM int_ads_positive GROUP BY spend_date",
        "tmp_ctr": "SELECT campaign_id, SUM(clicks) AS clicks, SUM(impressions) AS impressions FROM int_ads_unioned GROUP BY campaign_id",
    },
    ["rpt_budget_pacing", "rpt_source_mix", "rpt_daily_clicks"],
    {
        "int_ads_unioned": "SELECT 'google' AS source, campaign_id, spend_date, spend, clicks, impressions FROM google_ads_daily WHERE spend >= 0 UNION ALL SELECT 'meta' AS source, campaign_id, spend_date, spend, clicks, impressions FROM meta_ads_daily WHERE spend >= 0 UNION ALL SELECT 'bing' AS source, campaign_id, spend_date, spend, clicks, impressions FROM bing_ads_daily WHERE spend >= 0",
        "rpt_budget_pacing": "SELECT a.campaign_id, c.name AS campaign_name, SUM(a.spend) AS spend, c.budget, SUM(a.spend) > c.budget AS over_budget FROM int_ads_unioned AS a LEFT JOIN campaigns AS c ON c.id = a.campaign_id GROUP BY a.campaign_id, c.name, c.budget",
        "rpt_source_mix": "SELECT source, SUM(spend) AS spend FROM int_ads_unioned GROUP BY source",
        "rpt_daily_clicks": "SELECT spend_date, SUM(clicks) AS clicks, SUM(impressions) AS impressions FROM int_ads_unioned GROUP BY spend_date",
    },
    [
        ("drops the spend >= 0 staging filters, keeping only the NOT NULL check they made redundant",
         {"int_ads_unioned": "SELECT 'google' AS source, campaign_id, spend_date, spend, clicks, impressions FROM google_ads_daily WHERE spend IS NOT NULL UNION ALL SELECT 'meta' AS source, campaign_id, spend_date, spend, clicks, impressions FROM meta_ads_daily WHERE spend IS NOT NULL UNION ALL SELECT 'bing' AS source, campaign_id, spend_date, spend, clicks, impressions FROM bing_ads_daily WHERE spend IS NOT NULL"}),
        ("unions the platforms with UNION DISTINCT, collapsing identical ad rows",
         {"int_ads_unioned": "SELECT 'google' AS source, campaign_id, spend_date, spend, clicks, impressions FROM google_ads_daily WHERE spend >= 0 UNION DISTINCT SELECT 'meta' AS source, campaign_id, spend_date, spend, clicks, impressions FROM meta_ads_daily WHERE spend >= 0 UNION DISTINCT SELECT 'bing' AS source, campaign_id, spend_date, spend, clicks, impressions FROM bing_ads_daily WHERE spend >= 0"}),
    ],
    "spend >= 0 implies spend IS NOT NULL; the source-mix CTE repeats the union table.",
))

# Order fulfilment: fold staging into the fact; NOT IN over a nullable column is the trap.
add(case(
    ["dead_tables", "mergeable_tables", "passthrough_chain"],
    {
        "stg_orders": "SELECT id AS order_id, customer_id, order_total, status AS order_status, channel FROM raw_orders",
        "stg_shipments": "SELECT id AS shipment_id, order_id, carrier_code, status AS shipment_status FROM shipments",
        "stg_carriers": "SELECT code AS carrier_code, name AS carrier_name, is_active FROM carriers",
        "stg_customers": "SELECT id AS customer_id, country FROM raw_customers",
        "int_order_shipments": "SELECT order_id, COUNT(*) AS shipments, COUNTIF(shipment_status = 'delivered') AS delivered, COUNTIF(shipment_status = 'lost') AS lost FROM stg_shipments GROUP BY order_id",
        "int_orders_fulfilment": "SELECT o.order_id, o.customer_id, o.order_status, o.order_total, COALESCE(s.shipments, 0) AS shipments, COALESCE(s.delivered, 0) AS delivered, COALESCE(s.lost, 0) AS lost, CASE WHEN s.shipments IS NULL THEN 'unshipped' WHEN s.delivered = s.shipments THEN 'delivered' WHEN s.lost > 0 THEN 'exception' ELSE 'in_progress' END AS fulfilment_state FROM stg_orders AS o LEFT JOIN int_order_shipments AS s ON s.order_id = o.order_id",
        "fct_orders_fulfilment": "SELECT f.*, c.country FROM int_orders_fulfilment AS f LEFT JOIN stg_customers AS c ON c.customer_id = f.customer_id",
        "rpt_fulfilment_by_country": "SELECT country, fulfilment_state, COUNT(*) AS orders FROM fct_orders_fulfilment WHERE order_status <> 'cancelled' GROUP BY country, fulfilment_state",
        "rpt_carrier_losses": "SELECT s.carrier_code, c.carrier_name, COUNT(*) AS lost_shipments FROM stg_shipments AS s LEFT JOIN stg_carriers AS c ON c.carrier_code = s.carrier_code WHERE s.shipment_status = 'lost' GROUP BY s.carrier_code, c.carrier_name",
        "rpt_unshipped_orders": "SELECT order_id, order_total FROM int_orders_fulfilment WHERE fulfilment_state = 'unshipped' AND order_status = 'completed'",
        "tmp_active_carriers": "SELECT carrier_code FROM stg_carriers WHERE is_active",
    },
    ["fct_orders_fulfilment", "rpt_fulfilment_by_country", "rpt_carrier_losses", "rpt_unshipped_orders"],
    {
        "int_order_shipments": "SELECT order_id, COUNT(*) AS shipments, COUNTIF(status = 'delivered') AS delivered, COUNTIF(status = 'lost') AS lost FROM shipments GROUP BY order_id",
        "fct_orders_fulfilment": "SELECT o.id AS order_id, o.customer_id, o.status AS order_status, o.order_total, COALESCE(s.shipments, 0) AS shipments, COALESCE(s.delivered, 0) AS delivered, COALESCE(s.lost, 0) AS lost, CASE WHEN s.shipments IS NULL THEN 'unshipped' WHEN s.delivered = s.shipments THEN 'delivered' WHEN s.lost > 0 THEN 'exception' ELSE 'in_progress' END AS fulfilment_state, c.country FROM raw_orders AS o LEFT JOIN int_order_shipments AS s ON s.order_id = o.id LEFT JOIN raw_customers AS c ON c.id = o.customer_id",
        "rpt_fulfilment_by_country": KEEP,
        "rpt_carrier_losses": "SELECT s.carrier_code, c.name AS carrier_name, COUNT(*) AS lost_shipments FROM shipments AS s LEFT JOIN carriers AS c ON c.code = s.carrier_code WHERE s.status = 'lost' GROUP BY s.carrier_code, c.name",
        "rpt_unshipped_orders": "SELECT order_id, order_total FROM fct_orders_fulfilment WHERE fulfilment_state = 'unshipped' AND order_status = 'completed'",
    },
    [
        ("finds unshipped orders with NOT IN over shipments, which returns nothing once a shipment has a NULL order_id",
         {"rpt_unshipped_orders": "SELECT id AS order_id, order_total FROM raw_orders WHERE status = 'completed' AND id NOT IN (SELECT order_id FROM shipments)"}),
        ("drops the unshipped branch of the CASE, trusting the COALESCEd counts",
         {"fct_orders_fulfilment": "SELECT o.id AS order_id, o.customer_id, o.status AS order_status, o.order_total, COALESCE(s.shipments, 0) AS shipments, COALESCE(s.delivered, 0) AS delivered, COALESCE(s.lost, 0) AS lost, CASE WHEN s.delivered = s.shipments THEN 'delivered' WHEN s.lost > 0 THEN 'exception' ELSE 'in_progress' END AS fulfilment_state, c.country FROM raw_orders AS o LEFT JOIN int_order_shipments AS s ON s.order_id = o.id LEFT JOIN raw_customers AS c ON c.id = o.customer_id"}),
    ],
    "The customer join is on the key, so the fact table can absorb the intermediate and the unshipped report can read it.",
))

# Finance: running balance by window equals a grouped sum at the last entry.
add(case(
    ["dead_tables", "mergeable_tables", "passthrough_chain", "unused_columns_joins"],
    {
        "stg_gl_entries": "SELECT id AS entry_id, account_code, entity, amount, side, posted_on FROM gl_entries",
        "int_signed": "SELECT entry_id, account_code, entity, posted_on, CASE WHEN side = 'credit' THEN -amount ELSE amount END AS signed_amount FROM stg_gl_entries",
        "int_running_balance": "SELECT entry_id, account_code, entity, posted_on, signed_amount, SUM(signed_amount) OVER (PARTITION BY entity, account_code ORDER BY entry_id) AS running_balance, ROW_NUMBER() OVER (PARTITION BY entity, account_code ORDER BY entry_id DESC) AS recency FROM int_signed",
        "rpt_closing_balances": "SELECT entity, account_code, running_balance AS closing_balance FROM int_running_balance WHERE recency = 1",
        "rpt_entity_net": "SELECT entity, COUNT(*) AS entries, SUM(signed_amount) AS net FROM int_running_balance GROUP BY entity",
        "tmp_negative_balances": "SELECT entity, account_code FROM int_running_balance WHERE running_balance < 0",
    },
    ["rpt_closing_balances", "rpt_entity_net"],
    {
        "rpt_closing_balances": "SELECT entity, account_code, SUM(IF(side = 'credit', -amount, amount)) AS closing_balance FROM gl_entries GROUP BY entity, account_code",
        "rpt_entity_net": "SELECT entity, COUNT(*) AS entries, SUM(IF(side = 'credit', -amount, amount)) AS net FROM gl_entries GROUP BY entity",
    },
    [
        ("signs with IF(side = 'debit', ...), turning a NULL side into a credit",
         {"rpt_closing_balances": "SELECT entity, account_code, SUM(IF(side = 'debit', amount, -amount)) AS closing_balance FROM gl_entries GROUP BY entity, account_code"}),
        ("takes the largest running balance as the closing balance",
         {"rpt_closing_balances": "SELECT entity, account_code, MAX(running_balance) AS closing_balance FROM int_running_balance GROUP BY entity, account_code",
          "int_running_balance": "SELECT entity, account_code, SUM(IF(side = 'credit', -amount, amount)) OVER (PARTITION BY entity, account_code ORDER BY id) AS running_balance FROM gl_entries"}),
    ],
    "The running sum at each partition's last entry (ordered on the key) is the partition's total.",
))

# E-commerce: EXISTS over a join chain rewritten as joins plus DISTINCT on the customer key.
add(case(
    ["mergeable_tables", "passthrough_chain"],
    {
        "stg_customers": "SELECT id AS customer_id, email, country, is_test FROM raw_customers",
        "stg_orders": "SELECT id AS order_id, customer_id, status FROM raw_orders",
        "stg_order_lines": "SELECT order_id, product_id, quantity FROM raw_order_lines",
        "stg_products": "SELECT id AS product_id, category FROM raw_products",
        "int_toy_orders": "SELECT DISTINCT l.order_id FROM stg_order_lines AS l JOIN stg_products AS p ON p.product_id = l.product_id WHERE p.category = 'toys'",
        "rpt_toy_buyers": "SELECT c.customer_id, c.country FROM stg_customers AS c WHERE EXISTS (SELECT 1 FROM stg_orders AS o JOIN int_toy_orders AS t ON t.order_id = o.order_id WHERE o.customer_id = c.customer_id AND o.status = 'completed')",
        "rpt_toy_buyers_by_country": "SELECT country, COUNT(*) AS buyers FROM rpt_toy_buyers GROUP BY country",
    },
    ["rpt_toy_buyers", "rpt_toy_buyers_by_country"],
    {
        "rpt_toy_buyers": "SELECT DISTINCT c.id AS customer_id, c.country FROM raw_customers AS c JOIN raw_orders AS o ON o.customer_id = c.id JOIN raw_order_lines AS l ON l.order_id = o.id JOIN raw_products AS p ON p.id = l.product_id WHERE o.status = 'completed' AND p.category = 'toys'",
        "rpt_toy_buyers_by_country": KEEP,
    },
    [
        ("turns the EXISTS into joins but forgets the DISTINCT, repeating a customer per toy line",
         {"rpt_toy_buyers": "SELECT c.id AS customer_id, c.country FROM raw_customers AS c JOIN raw_orders AS o ON o.customer_id = c.id JOIN raw_order_lines AS l ON l.order_id = o.id JOIN raw_products AS p ON p.id = l.product_id WHERE o.status = 'completed' AND p.category = 'toys'"}),
        ("counts buyers per country straight from order lines, counting lines rather than customers",
         {"rpt_toy_buyers_by_country": "SELECT c.country, COUNT(*) AS buyers FROM raw_customers AS c JOIN raw_orders AS o ON o.customer_id = c.id JOIN raw_order_lines AS l ON l.order_id = o.id JOIN raw_products AS p ON p.id = l.product_id WHERE o.status = 'completed' AND p.category = 'toys' GROUP BY c.country"}),
    ],
    "A correlated EXISTS becomes a join chain; DISTINCT is safe because the customer key is selected.",
    data={"raw_customers": [[1, "ann@shop.io", "US", False]],
          "raw_orders": [[1, 1, 5, "completed", "web", "2024-01-01"]],
          "raw_order_lines": [[1, 1, 1, 1, 1], [1, 2, 1, 2, 1]],
          "raw_products": [[1, "kite", "toys", True]]},
))

# ================================================================== large cases (12 to 20 tables)

# SaaS ARR mart: unused columns in rollups, a NOT NULL filter made redundant by COALESCE.
add(case(
    ["mergeable_tables", "passthrough_chain", "redundant_filters", "unused_columns_joins"],
    {
        "stg_accounts": "SELECT id AS account_id, name AS account_name, plan, region, is_deleted FROM accounts",
        "stg_subscriptions": "SELECT id AS subscription_id, account_id, plan, mrr, status, started_on FROM subscriptions",
        "stg_invoices": "SELECT id AS invoice_id, account_id, amount, status AS invoice_status, issued_on FROM invoices",
        "stg_users": "SELECT id AS user_id, account_id, role, is_active FROM app_users",
        "int_live_accounts": "SELECT * FROM stg_accounts WHERE is_deleted IS NOT TRUE",
        "int_active_subscriptions": "SELECT * FROM stg_subscriptions WHERE status IN ('active', 'trialing') AND mrr IS NOT NULL",
        "int_account_mrr": "SELECT account_id, SUM(mrr) AS mrr, COUNT(*) AS active_subscriptions FROM int_active_subscriptions GROUP BY account_id",
        "int_account_seats": "SELECT account_id, COUNT(*) AS seats, COUNTIF(role = 'admin') AS admins FROM stg_users WHERE is_active GROUP BY account_id",
        "int_account_invoices": "SELECT account_id, SUM(CASE WHEN invoice_status = 'paid' THEN amount ELSE 0 END) AS collected, SUM(CASE WHEN invoice_status = 'open' THEN amount ELSE 0 END) AS outstanding FROM stg_invoices GROUP BY account_id",
        "dim_accounts": "SELECT a.account_id, a.account_name, a.plan, a.region, COALESCE(m.mrr, 0) AS mrr, COALESCE(s.seats, 0) AS seats, i.outstanding FROM int_live_accounts AS a LEFT JOIN int_account_mrr AS m ON m.account_id = a.account_id LEFT JOIN int_account_seats AS s ON s.account_id = a.account_id LEFT JOIN int_account_invoices AS i ON i.account_id = a.account_id",
        "rpt_arr_by_region": "SELECT region, SUM(mrr) * 12 AS arr, COUNT(*) AS accounts FROM dim_accounts WHERE mrr > 0 GROUP BY region",
        "rpt_collections": "SELECT a.region, SUM(i.collected) AS collected FROM int_account_invoices AS i JOIN int_live_accounts AS a ON a.account_id = i.account_id GROUP BY a.region",
    },
    ["dim_accounts", "rpt_arr_by_region", "rpt_collections"],
    {
        "int_account_mrr": "SELECT account_id, SUM(mrr) AS mrr FROM subscriptions WHERE status IN ('active', 'trialing') GROUP BY account_id",
        "int_account_seats": "SELECT account_id, COUNT(*) AS seats FROM app_users WHERE is_active GROUP BY account_id",
        "int_account_invoices": "SELECT account_id, SUM(IF(status = 'paid', amount, 0)) AS collected, SUM(IF(status = 'open', amount, 0)) AS outstanding FROM invoices GROUP BY account_id",
        "dim_accounts": "SELECT a.id AS account_id, a.name AS account_name, a.plan, a.region, COALESCE(m.mrr, 0) AS mrr, COALESCE(s.seats, 0) AS seats, i.outstanding FROM accounts AS a LEFT JOIN int_account_mrr AS m ON m.account_id = a.id LEFT JOIN int_account_seats AS s ON s.account_id = a.id LEFT JOIN int_account_invoices AS i ON i.account_id = a.id WHERE a.is_deleted IS NOT TRUE",
        "rpt_arr_by_region": KEEP,
        "rpt_collections": "SELECT a.region, SUM(i.collected) AS collected FROM int_account_invoices AS i JOIN accounts AS a ON a.id = i.account_id WHERE a.is_deleted IS NOT TRUE GROUP BY a.region",
    },
    [
        ("filters deleted accounts with NOT is_deleted, dropping accounts whose flag is NULL",
         {"dim_accounts": "SELECT a.id AS account_id, a.name AS account_name, a.plan, a.region, COALESCE(m.mrr, 0) AS mrr, COALESCE(s.seats, 0) AS seats, i.outstanding FROM accounts AS a LEFT JOIN int_account_mrr AS m ON m.account_id = a.id LEFT JOIN int_account_seats AS s ON s.account_id = a.id LEFT JOIN int_account_invoices AS i ON i.account_id = a.id WHERE NOT a.is_deleted"}),
        ("keeps subscriptions that are not canceled instead of the active and trialing ones",
         {"int_account_mrr": "SELECT account_id, SUM(mrr) AS mrr FROM subscriptions WHERE status <> 'canceled' GROUP BY account_id"}),
    ],
    "mrr IS NOT NULL is redundant only because dim_accounts COALESCEs the sum to 0.",
))

# Support analytics: first solver by ROW_NUMBER, reopened tickets by EXISTS, CSAT averages.
add(case(
    ["dead_tables", "mergeable_tables", "passthrough_chain", "unused_columns_joins"],
    {
        "stg_tickets": "SELECT id AS ticket_id, requester_id, assignee_id, priority, status, channel FROM tickets",
        "stg_status_history": "SELECT id AS change_id, ticket_id, status AS new_status, agent_id FROM ticket_status_history",
        "stg_agents": "SELECT id AS agent_id, name AS agent_name, team, is_bot FROM agents",
        "stg_csat": "SELECT ticket_id, score FROM csat_responses WHERE score BETWEEN 1 AND 5",
        "int_first_solver": "SELECT ticket_id, agent_id FROM (SELECT ticket_id, agent_id, ROW_NUMBER() OVER (PARTITION BY ticket_id ORDER BY change_id) AS rn FROM stg_status_history WHERE new_status = 'solved') WHERE rn = 1",
        "int_reopened": "SELECT DISTINCT h.ticket_id FROM stg_status_history AS h WHERE h.new_status = 'open' AND EXISTS (SELECT 1 FROM stg_status_history AS s WHERE s.ticket_id = h.ticket_id AND s.new_status = 'solved' AND s.change_id < h.change_id)",
        "int_ticket_csat": "SELECT ticket_id, AVG(score) AS avg_score, COUNT(*) AS responses FROM stg_csat GROUP BY ticket_id",
        "fct_tickets": "SELECT t.ticket_id, t.priority, t.channel, f.agent_id AS first_solver_id, a.team AS solver_team, c.avg_score, r.ticket_id IS NOT NULL AS was_reopened FROM stg_tickets AS t LEFT JOIN int_first_solver AS f ON f.ticket_id = t.ticket_id LEFT JOIN stg_agents AS a ON a.agent_id = f.agent_id LEFT JOIN int_ticket_csat AS c ON c.ticket_id = t.ticket_id LEFT JOIN int_reopened AS r ON r.ticket_id = t.ticket_id",
        "rpt_team_csat": "SELECT solver_team, AVG(avg_score) AS avg_csat, COUNT(*) AS tickets FROM fct_tickets WHERE avg_score IS NOT NULL GROUP BY solver_team",
        "rpt_reopen_rate": "SELECT channel, COUNTIF(was_reopened) AS reopened, COUNT(*) AS tickets FROM fct_tickets GROUP BY channel",
        "rpt_bot_solves": "SELECT f.ticket_id FROM int_first_solver AS f JOIN stg_agents AS a ON a.agent_id = f.agent_id WHERE a.is_bot",
        "tmp_csat_raw": "SELECT * FROM stg_csat",
    },
    ["fct_tickets", "rpt_team_csat", "rpt_reopen_rate"],
    {
        "int_first_solver": "WITH solves AS (SELECT ticket_id, agent_id, ROW_NUMBER() OVER (PARTITION BY ticket_id ORDER BY id) AS rn FROM ticket_status_history WHERE status = 'solved') SELECT ticket_id, agent_id FROM solves WHERE rn = 1",
        "int_reopened": "SELECT DISTINCT h.ticket_id FROM ticket_status_history AS h JOIN ticket_status_history AS s ON s.ticket_id = h.ticket_id AND s.id < h.id WHERE h.status = 'open' AND s.status = 'solved'",
        "int_ticket_csat": "SELECT ticket_id, AVG(score) AS avg_score FROM csat_responses WHERE score BETWEEN 1 AND 5 GROUP BY ticket_id",
        "fct_tickets": "SELECT t.id AS ticket_id, t.priority, t.channel, f.agent_id AS first_solver_id, a.team AS solver_team, c.avg_score, r.ticket_id IS NOT NULL AS was_reopened FROM tickets AS t LEFT JOIN int_first_solver AS f ON f.ticket_id = t.id LEFT JOIN agents AS a ON a.id = f.agent_id LEFT JOIN int_ticket_csat AS c ON c.ticket_id = t.id LEFT JOIN int_reopened AS r ON r.ticket_id = t.id",
        "rpt_team_csat": KEEP,
        "rpt_reopen_rate": KEEP,
    },
    [
        ("ranks every status change and keeps the first one only if it is a solve",
         {"int_first_solver": "WITH solves AS (SELECT ticket_id, agent_id, status, ROW_NUMBER() OVER (PARTITION BY ticket_id ORDER BY id) AS rn FROM ticket_status_history) SELECT ticket_id, agent_id FROM solves WHERE rn = 1 AND status = 'solved'"}),
        ("drops the DISTINCT from the reopened self join, so tickets reopened twice repeat in the fact table",
         {"int_reopened": "SELECT h.ticket_id FROM ticket_status_history AS h JOIN ticket_status_history AS s ON s.ticket_id = h.ticket_id AND s.id < h.id WHERE h.status = 'open' AND s.status = 'solved'"}),
    ],
    "The correlated EXISTS becomes a self join under DISTINCT; the agent join is on the key.",
))

# E-commerce product analytics: a duplicated product rollup, fan-out traps on refunds.
add(case(
    ["dead_tables", "duplicated_logic", "mergeable_tables", "passthrough_chain"],
    {
        "stg_order_lines": "SELECT order_id, line_no, product_id, quantity, unit_price, quantity * unit_price AS line_revenue FROM raw_order_lines",
        "stg_products": "SELECT id AS product_id, name AS product_name, category, is_active FROM raw_products",
        "stg_orders": "SELECT id AS order_id, customer_id, status AS order_status, channel, created_on FROM raw_orders",
        "stg_refunds": "SELECT id AS refund_id, order_id, amount AS refund_amount, reason FROM raw_refunds",
        "int_completed_lines": "SELECT l.*, o.customer_id, o.channel FROM stg_order_lines AS l JOIN stg_orders AS o ON o.order_id = l.order_id WHERE o.order_status = 'completed'",
        "int_lines_with_product": "SELECT l.order_id, l.line_no, l.product_id, l.quantity, l.line_revenue, l.channel, p.product_name, p.category FROM int_completed_lines AS l LEFT JOIN stg_products AS p ON p.product_id = l.product_id",
        "int_product_sales": "SELECT product_id, product_name, category, SUM(quantity) AS units, SUM(line_revenue) AS revenue FROM int_lines_with_product GROUP BY product_id, product_name, category",
        "int_product_sales_copy": "SELECT l.product_id, p.name AS product_name, p.category, SUM(l.quantity) AS units, SUM(l.quantity * l.unit_price) AS revenue FROM raw_order_lines AS l JOIN raw_orders AS o ON o.id = l.order_id LEFT JOIN raw_products AS p ON p.id = l.product_id WHERE o.status = 'completed' GROUP BY l.product_id, p.name, p.category",
        "rpt_top_products_by_category": "SELECT category, product_id, revenue, RANK() OVER (PARTITION BY category ORDER BY revenue DESC) AS revenue_rank FROM int_product_sales",
        "rpt_category_units": "SELECT category, SUM(units) AS units FROM int_product_sales_copy GROUP BY category",
        "rpt_channel_mix": "SELECT channel, category, SUM(line_revenue) AS revenue FROM int_lines_with_product GROUP BY channel, category",
        "int_refund_totals": "SELECT order_id, SUM(refund_amount) AS refunded, COUNT(*) AS refunds FROM stg_refunds GROUP BY order_id",
        "rpt_refund_rate_by_channel": "SELECT o.channel, COUNT(*) AS orders, COUNTIF(r.order_id IS NOT NULL) AS refunded_orders FROM stg_orders AS o LEFT JOIN int_refund_totals AS r ON r.order_id = o.order_id WHERE o.order_status = 'completed' GROUP BY o.channel",
        "rpt_damaged_refunds": "SELECT order_id FROM stg_refunds WHERE reason = 'damaged'",
    },
    ["rpt_top_products_by_category", "rpt_category_units", "rpt_channel_mix", "rpt_refund_rate_by_channel"],
    {
        "int_lines_with_product": "SELECT o.channel, l.product_id, l.quantity, l.quantity * l.unit_price AS line_revenue, p.name AS product_name, p.category FROM raw_order_lines AS l JOIN raw_orders AS o ON o.id = l.order_id LEFT JOIN raw_products AS p ON p.id = l.product_id WHERE o.status = 'completed'",
        "int_product_sales": KEEP,
        "rpt_top_products_by_category": KEEP,
        "rpt_category_units": "SELECT category, SUM(quantity) AS units FROM int_lines_with_product GROUP BY category",
        "rpt_channel_mix": KEEP,
        "rpt_refund_rate_by_channel": "SELECT o.channel, COUNT(DISTINCT o.id) AS orders, COUNT(DISTINCT r.order_id) AS refunded_orders FROM raw_orders AS o LEFT JOIN raw_refunds AS r ON r.order_id = o.id WHERE o.status = 'completed' GROUP BY o.channel",
    },
    [
        ("counts orders with COUNT(*) after joining refunds, so an order with two refunds counts twice",
         {"rpt_refund_rate_by_channel": "SELECT o.channel, COUNT(*) AS orders, COUNT(DISTINCT r.order_id) AS refunded_orders FROM raw_orders AS o LEFT JOIN raw_refunds AS r ON r.order_id = o.id WHERE o.status = 'completed' GROUP BY o.channel"}),
        ("counts refunded orders without DISTINCT, which counts refunds",
         {"rpt_refund_rate_by_channel": "SELECT o.channel, COUNT(DISTINCT o.id) AS orders, COUNT(r.order_id) AS refunded_orders FROM raw_orders AS o LEFT JOIN raw_refunds AS r ON r.order_id = o.id WHERE o.status = 'completed' GROUP BY o.channel"}),
    ],
    "int_product_sales_copy repeats int_product_sales; the refund rollup folds into a distinct count.",
))

# HR org analytics: salary bands, DENSE_RANK top earners, span of control via a self join.
add(case(
    ["dead_tables", "mergeable_tables", "passthrough_chain", "unused_columns_joins"],
    {
        "stg_employees": "SELECT id AS employee_id, department_id, manager_id, salary, status, level FROM employees",
        "stg_departments": "SELECT id AS department_id, name AS department_name, cost_center FROM departments",
        "int_active_employees": "SELECT * FROM stg_employees WHERE status = 'active'",
        "int_employees_with_dept": "SELECT e.*, d.department_name, d.cost_center FROM int_active_employees AS e LEFT JOIN stg_departments AS d ON d.department_id = e.department_id",
        "int_salary_bands": "SELECT employee_id, department_name, salary, CASE WHEN salary IS NULL THEN 'unknown' WHEN salary >= 4 THEN 'high' WHEN salary >= 2 THEN 'mid' ELSE 'low' END AS salary_band, DENSE_RANK() OVER (PARTITION BY department_id ORDER BY salary DESC) AS salary_rank FROM int_employees_with_dept",
        "int_span_of_control": "SELECT manager_id, COUNT(*) AS direct_reports FROM int_active_employees WHERE manager_id IS NOT NULL GROUP BY manager_id",
        "int_managers": "SELECT e.employee_id, e.department_name, s.direct_reports FROM int_employees_with_dept AS e JOIN int_span_of_control AS s ON s.manager_id = e.employee_id",
        "rpt_band_distribution": "SELECT department_name, salary_band, COUNT(*) AS employees FROM int_salary_bands GROUP BY department_name, salary_band",
        "rpt_top_earners": "SELECT employee_id, department_name, salary FROM int_salary_bands WHERE salary_rank = 1",
        "rpt_wide_managers": "SELECT employee_id, direct_reports FROM int_managers WHERE direct_reports >= 3",
        "rpt_cost_center_headcount": "SELECT cost_center, COUNT(*) AS headcount FROM int_employees_with_dept GROUP BY cost_center",
        "tmp_level_counts": "SELECT level, COUNT(*) AS n FROM stg_employees GROUP BY level",
        "tmp_terminated": "SELECT employee_id FROM stg_employees WHERE status = 'terminated'",
    },
    ["rpt_band_distribution", "rpt_top_earners", "rpt_wide_managers", "rpt_cost_center_headcount"],
    {
        "int_employees_with_dept": "SELECT e.id AS employee_id, e.department_id, e.salary, d.name AS department_name, d.cost_center FROM employees AS e LEFT JOIN departments AS d ON d.id = e.department_id WHERE e.status = 'active'",
        "rpt_band_distribution": "SELECT department_name, CASE WHEN salary IS NULL THEN 'unknown' WHEN salary >= 4 THEN 'high' WHEN salary >= 2 THEN 'mid' ELSE 'low' END AS salary_band, COUNT(*) AS employees FROM int_employees_with_dept GROUP BY 1, 2",
        "rpt_top_earners": "WITH ranked AS (SELECT employee_id, department_name, salary, DENSE_RANK() OVER (PARTITION BY department_id ORDER BY salary DESC) AS salary_rank FROM int_employees_with_dept) SELECT employee_id, department_name, salary FROM ranked WHERE salary_rank = 1",
        "rpt_wide_managers": "SELECT m.id AS employee_id, COUNT(*) AS direct_reports FROM employees AS m JOIN employees AS r ON r.manager_id = m.id WHERE m.status = 'active' AND r.status = 'active' GROUP BY m.id HAVING COUNT(*) >= 3",
        "rpt_cost_center_headcount": KEEP,
    },
    [
        ("ranks salaries across all employees before keeping the active ones",
         {"rpt_top_earners": "WITH ranked AS (SELECT e.id AS employee_id, d.name AS department_name, e.salary, e.status, DENSE_RANK() OVER (PARTITION BY e.department_id ORDER BY e.salary DESC) AS salary_rank FROM employees AS e LEFT JOIN departments AS d ON d.id = e.department_id) SELECT employee_id, department_name, salary FROM ranked WHERE salary_rank = 1 AND status = 'active'"}),
        ("counts direct reports among all employees, not only active ones",
         {"rpt_wide_managers": "SELECT m.id AS employee_id, COUNT(*) AS direct_reports FROM employees AS m JOIN employees AS r ON r.manager_id = m.id WHERE m.status = 'active' GROUP BY m.id HAVING COUNT(*) >= 3"}),
        ("drops the 'unknown' band because NULL salaries seem to fall to ELSE",
         {"rpt_band_distribution": "SELECT department_name, CASE WHEN salary >= 4 THEN 'high' WHEN salary >= 2 THEN 'mid' ELSE 'low' END AS salary_band, COUNT(*) AS employees FROM int_employees_with_dept GROUP BY 1, 2"}),
    ],
    "Window and CASE columns move into the one report that uses each.",
))

# Finance consolidation: per-entity tables unioned, fx inner join, unused parent self join.
add(case(
    ["dead_tables", "mergeable_tables", "passthrough_chain", "unused_columns_joins"],
    {
        "stg_gl_entries": "SELECT id AS entry_id, account_code, entity, amount, side, posted_on FROM gl_entries WHERE amount IS NOT NULL",
        "stg_gl_accounts": "SELECT code AS account_code, name AS account_name, account_type, parent_code FROM gl_accounts",
        "stg_fx_rates": "SELECT entity, rate AS usd_rate FROM fx_rates",
        "int_entries_signed": "SELECT entry_id, account_code, entity, posted_on, IF(side = 'credit', -amount, amount) AS signed_amount FROM stg_gl_entries WHERE side IN ('debit', 'credit')",
        "int_entries_us": "SELECT * FROM int_entries_signed WHERE entity = 'us_inc'",
        "int_entries_uk": "SELECT * FROM int_entries_signed WHERE entity = 'uk_ltd'",
        "int_entries_consolidated": "SELECT * FROM int_entries_us UNION ALL SELECT * FROM int_entries_uk",
        "int_entries_usd": "SELECT c.entry_id, c.account_code, c.entity, c.posted_on, c.signed_amount * f.usd_rate AS amount_usd FROM int_entries_consolidated AS c JOIN stg_fx_rates AS f ON f.entity = c.entity",
        "int_entries_with_accounts": "SELECT u.*, a.account_name, a.account_type, p.account_name AS parent_name FROM int_entries_usd AS u LEFT JOIN stg_gl_accounts AS a ON a.account_code = u.account_code LEFT JOIN stg_gl_accounts AS p ON p.account_code = a.parent_code",
        "rpt_consolidated_pnl": "SELECT account_type, SUM(amount_usd) AS amount_usd FROM int_entries_with_accounts WHERE account_type IN ('revenue', 'expense') GROUP BY account_type",
        "rpt_monthly_revenue": "SELECT DATE_TRUNC(posted_on, MONTH) AS posting_month, -SUM(amount_usd) AS revenue_usd FROM int_entries_with_accounts WHERE account_type = 'revenue' GROUP BY posting_month",
        "rpt_entity_cash": "SELECT entity, SUM(signed_amount) AS cash_balance FROM int_entries_consolidated WHERE account_code = '1000' GROUP BY entity",
        "tmp_parent_rollup": "SELECT parent_name, SUM(amount_usd) AS amount_usd FROM int_entries_with_accounts GROUP BY parent_name",
        "tmp_fx_check": "SELECT entity FROM stg_fx_rates WHERE usd_rate IS NULL",
    },
    ["rpt_consolidated_pnl", "rpt_monthly_revenue", "rpt_entity_cash"],
    {
        "int_entries_usd": "SELECT e.posted_on, a.account_type, IF(e.side = 'credit', -e.amount, e.amount) * f.rate AS amount_usd FROM gl_entries AS e JOIN fx_rates AS f ON f.entity = e.entity LEFT JOIN gl_accounts AS a ON a.code = e.account_code WHERE e.amount IS NOT NULL AND e.side IN ('debit', 'credit') AND e.entity IN ('us_inc', 'uk_ltd')",
        "rpt_consolidated_pnl": "SELECT account_type, SUM(amount_usd) AS amount_usd FROM int_entries_usd WHERE account_type IN ('revenue', 'expense') GROUP BY account_type",
        "rpt_monthly_revenue": "SELECT DATE_TRUNC(posted_on, MONTH) AS posting_month, -SUM(amount_usd) AS revenue_usd FROM int_entries_usd WHERE account_type = 'revenue' GROUP BY posting_month",
        "rpt_entity_cash": "SELECT entity, SUM(IF(side = 'credit', -amount, amount)) AS cash_balance FROM gl_entries WHERE account_code = '1000' AND amount IS NOT NULL AND side IN ('debit', 'credit') AND entity IN ('us_inc', 'uk_ltd') GROUP BY entity",
    },
    [
        ("drops amount IS NOT NULL because SUM ignores NULLs, which adds account types with no amounts",
         {"int_entries_usd": "SELECT e.posted_on, a.account_type, IF(e.side = 'credit', -e.amount, e.amount) * f.rate AS amount_usd FROM gl_entries AS e JOIN fx_rates AS f ON f.entity = e.entity LEFT JOIN gl_accounts AS a ON a.code = e.account_code WHERE e.side IN ('debit', 'credit') AND e.entity IN ('us_inc', 'uk_ltd')"}),
        ("replaces the two entity branches with entity IS NOT NULL",
         {"rpt_entity_cash": "SELECT entity, SUM(IF(side = 'credit', -amount, amount)) AS cash_balance FROM gl_entries WHERE account_code = '1000' AND amount IS NOT NULL AND side IN ('debit', 'credit') AND entity IS NOT NULL GROUP BY entity"}),
        ("signs with IF(side = 'debit', ...) and drops the side filter",
         {"int_entries_usd": "SELECT e.posted_on, a.account_type, IF(e.side = 'debit', e.amount, -e.amount) * f.rate AS amount_usd FROM gl_entries AS e JOIN fx_rates AS f ON f.entity = e.entity LEFT JOIN gl_accounts AS a ON a.code = e.account_code WHERE e.amount IS NOT NULL AND e.entity IN ('us_inc', 'uk_ltd')"}),
    ],
    "The per-entity union is one IN filter; the fx join is inner and stays, the parent-account join goes.",
))

# Marketing funnel: first-touch attribution, signup revenue, fan-out trap on subscriptions.
add(case(
    ["dead_tables", "mergeable_tables", "passthrough_chain", "unused_columns_joins"],
    {
        "stg_sessions": "SELECT id AS session_id, visitor_id, campaign_id, landing_page, started_at FROM web_sessions",
        "stg_campaigns": "SELECT id AS campaign_id, name AS campaign_name, channel FROM campaigns",
        "stg_signups": "SELECT id AS signup_id, session_id, account_id, plan AS signup_plan FROM signups",
        "stg_subscriptions": "SELECT id AS subscription_id, account_id, plan, mrr, status FROM subscriptions",
        "int_first_touch": "WITH ranked AS (SELECT visitor_id, campaign_id, landing_page, ROW_NUMBER() OVER (PARTITION BY visitor_id ORDER BY started_at, session_id) AS rn FROM stg_sessions WHERE visitor_id IS NOT NULL) SELECT visitor_id, campaign_id AS first_campaign_id, landing_page AS first_landing_page FROM ranked WHERE rn = 1",
        "int_sessions_attributed": "SELECT s.session_id, s.visitor_id, s.campaign_id, f.first_campaign_id, s.landing_page FROM stg_sessions AS s LEFT JOIN int_first_touch AS f ON f.visitor_id = s.visitor_id",
        "int_signups_attributed": "SELECT g.signup_id, g.account_id, g.signup_plan, a.visitor_id, a.campaign_id AS last_campaign_id, a.first_campaign_id FROM stg_signups AS g LEFT JOIN int_sessions_attributed AS a ON a.session_id = g.session_id",
        "int_account_mrr": "SELECT account_id, SUM(mrr) AS mrr FROM stg_subscriptions WHERE status = 'active' GROUP BY account_id",
        "int_signup_revenue": "SELECT s.*, COALESCE(m.mrr, 0) AS mrr FROM int_signups_attributed AS s LEFT JOIN int_account_mrr AS m ON m.account_id = s.account_id",
        "rpt_first_touch_revenue": "SELECT c.channel, COUNT(*) AS signups, SUM(r.mrr) AS mrr FROM int_signup_revenue AS r LEFT JOIN stg_campaigns AS c ON c.campaign_id = r.first_campaign_id GROUP BY c.channel",
        "rpt_last_touch_revenue": "SELECT c.channel, COUNT(*) AS signups, SUM(r.mrr) AS mrr FROM int_signup_revenue AS r LEFT JOIN stg_campaigns AS c ON c.campaign_id = r.last_campaign_id GROUP BY c.channel",
        "rpt_landing_pages": "SELECT landing_page, COUNT(*) AS sessions, COUNT(DISTINCT visitor_id) AS visitors FROM int_sessions_attributed GROUP BY landing_page",
        "rpt_plan_mix": "SELECT signup_plan, COUNT(*) AS signups FROM int_signups_attributed GROUP BY signup_plan",
        "tmp_campaign_sessions": "SELECT campaign_id, COUNT(*) AS sessions FROM int_sessions_attributed GROUP BY campaign_id",
        "tmp_visitor_pages": "SELECT visitor_id, COUNT(DISTINCT landing_page) AS pages FROM stg_sessions GROUP BY visitor_id",
    },
    ["rpt_first_touch_revenue", "rpt_last_touch_revenue", "rpt_landing_pages", "rpt_plan_mix"],
    {
        "int_first_touch": "WITH ranked AS (SELECT visitor_id, campaign_id, ROW_NUMBER() OVER (PARTITION BY visitor_id ORDER BY started_at, id) AS rn FROM web_sessions WHERE visitor_id IS NOT NULL) SELECT visitor_id, campaign_id AS first_campaign_id FROM ranked WHERE rn = 1",
        "int_account_mrr": "SELECT account_id, SUM(mrr) AS mrr FROM subscriptions WHERE status = 'active' GROUP BY account_id",
        "int_signup_revenue": "SELECT s.campaign_id AS last_campaign_id, f.first_campaign_id, COALESCE(m.mrr, 0) AS mrr FROM signups AS g LEFT JOIN web_sessions AS s ON s.id = g.session_id LEFT JOIN int_first_touch AS f ON f.visitor_id = s.visitor_id LEFT JOIN int_account_mrr AS m ON m.account_id = g.account_id",
        "rpt_first_touch_revenue": "SELECT c.channel, COUNT(*) AS signups, SUM(r.mrr) AS mrr FROM int_signup_revenue AS r LEFT JOIN campaigns AS c ON c.id = r.first_campaign_id GROUP BY c.channel",
        "rpt_last_touch_revenue": "SELECT c.channel, COUNT(*) AS signups, SUM(r.mrr) AS mrr FROM int_signup_revenue AS r LEFT JOIN campaigns AS c ON c.id = r.last_campaign_id GROUP BY c.channel",
        "rpt_landing_pages": "SELECT landing_page, COUNT(*) AS sessions, COUNT(DISTINCT visitor_id) AS visitors FROM web_sessions GROUP BY landing_page",
        "rpt_plan_mix": "SELECT plan AS signup_plan, COUNT(*) AS signups FROM signups GROUP BY plan",
    },
    [
        ("takes each visitor's lowest campaign id instead of the campaign of the earliest session",
         {"int_first_touch": "SELECT visitor_id, MIN(campaign_id) AS first_campaign_id FROM web_sessions WHERE visitor_id IS NOT NULL GROUP BY visitor_id"}),
        ("joins active subscriptions directly, repeating a signup once per subscription",
         {"int_account_mrr": None,
          "int_signup_revenue": "SELECT s.campaign_id AS last_campaign_id, f.first_campaign_id, COALESCE(m.mrr, 0) AS mrr FROM signups AS g LEFT JOIN web_sessions AS s ON s.id = g.session_id LEFT JOIN int_first_touch AS f ON f.visitor_id = s.visitor_id LEFT JOIN subscriptions AS m ON m.account_id = g.account_id AND m.status = 'active'"}),
    ],
    "First touch is ordered on the session timestamp and then the session key; all lookups are on keys.",
))

# Warehouse operations: shared enriched moves, last scan per shipment, IS DISTINCT FROM.
add(case(
    ["dead_tables", "mergeable_tables", "passthrough_chain", "unused_columns_joins"],
    {
        "stg_stock_moves": "SELECT id AS move_id, sku, warehouse AS warehouse_code, qty, kind FROM stock_moves",
        "stg_warehouses": "SELECT code AS warehouse_code, region, is_3pl FROM warehouses",
        "stg_skus": "SELECT sku, category, unit_cost FROM skus",
        "stg_shipments": "SELECT id AS shipment_id, order_id, carrier_code, status, weight_g FROM shipments",
        "stg_shipment_events": "SELECT id AS event_id, shipment_id, kind AS event_kind, seq FROM shipment_events",
        "stg_carriers": "SELECT code AS carrier_code, name AS carrier_name, is_active FROM carriers",
        "int_moves_enriched": "SELECT m.move_id, m.sku, m.warehouse_code, m.qty, m.kind, w.region, w.is_3pl, k.category, k.unit_cost FROM stg_stock_moves AS m LEFT JOIN stg_warehouses AS w ON w.warehouse_code = m.warehouse_code LEFT JOIN stg_skus AS k ON k.sku = m.sku",
        "int_receipts": "SELECT * FROM int_moves_enriched WHERE kind = 'receipt'",
        "int_outbound": "SELECT * FROM int_moves_enriched WHERE kind = 'shipment'",
        "rpt_inbound_by_region": "SELECT region, SUM(qty) AS units_received, SUM(qty * unit_cost) AS value_received FROM int_receipts GROUP BY region",
        "rpt_outbound_by_category": "SELECT category, SUM(qty) AS units_shipped FROM int_outbound WHERE qty > 0 GROUP BY category",
        "rpt_3pl_share": "SELECT is_3pl, COUNT(*) AS moves FROM int_moves_enriched GROUP BY is_3pl",
        "int_last_scan": "SELECT shipment_id, event_kind AS last_event FROM (SELECT shipment_id, event_kind, ROW_NUMBER() OVER (PARTITION BY shipment_id ORDER BY seq DESC, event_id DESC) AS rn FROM stg_shipment_events) WHERE rn = 1",
        "fct_shipments": "SELECT s.shipment_id, s.order_id, s.status, c.carrier_name, l.last_event FROM stg_shipments AS s LEFT JOIN stg_carriers AS c ON c.carrier_code = s.carrier_code LEFT JOIN int_last_scan AS l ON l.shipment_id = s.shipment_id",
        "rpt_stuck_shipments": "SELECT shipment_id, carrier_name FROM fct_shipments WHERE status = 'in_transit' AND (last_event IS NULL OR last_event <> 'delivered')",
        "tmp_heavy_shipments": "SELECT shipment_id FROM stg_shipments WHERE weight_g > 3",
    },
    ["rpt_inbound_by_region", "rpt_outbound_by_category", "rpt_3pl_share", "fct_shipments", "rpt_stuck_shipments"],
    {
        "int_moves_enriched": "SELECT m.sku, m.qty, m.kind, w.region, w.is_3pl, k.category, k.unit_cost FROM stock_moves AS m LEFT JOIN warehouses AS w ON w.code = m.warehouse LEFT JOIN skus AS k ON k.sku = m.sku",
        "rpt_inbound_by_region": "SELECT region, SUM(qty) AS units_received, SUM(qty * unit_cost) AS value_received FROM int_moves_enriched WHERE kind = 'receipt' GROUP BY region",
        "rpt_outbound_by_category": "SELECT category, SUM(qty) AS units_shipped FROM int_moves_enriched WHERE kind = 'shipment' AND qty > 0 GROUP BY category",
        "rpt_3pl_share": KEEP,
        "int_last_scan": "WITH ranked AS (SELECT shipment_id, kind, ROW_NUMBER() OVER (PARTITION BY shipment_id ORDER BY seq DESC, id DESC) AS rn FROM shipment_events) SELECT shipment_id, kind AS last_event FROM ranked WHERE rn = 1",
        "fct_shipments": "SELECT s.id AS shipment_id, s.order_id, s.status, c.name AS carrier_name, l.last_event FROM shipments AS s LEFT JOIN carriers AS c ON c.code = s.carrier_code LEFT JOIN int_last_scan AS l ON l.shipment_id = s.id",
        "rpt_stuck_shipments": "SELECT shipment_id, carrier_name FROM fct_shipments WHERE status = 'in_transit' AND last_event IS DISTINCT FROM 'delivered'",
    },
    [
        ("drops last_event IS NULL, losing in-transit shipments that have no events yet",
         {"rpt_stuck_shipments": "SELECT shipment_id, carrier_name FROM fct_shipments WHERE status = 'in_transit' AND last_event <> 'delivered'"}),
        ("takes the alphabetically largest event kind as the last event",
         {"int_last_scan": "SELECT shipment_id, MAX(kind) AS last_event FROM shipment_events GROUP BY shipment_id"}),
    ],
    "Last event is ordered on seq and then the event key; the per-kind move tables are filters of one shared table.",
))

# SaaS account health: five rollups joined into one wide table and a CASE score.
add(case(
    ["dead_tables", "mergeable_tables", "passthrough_chain", "redundant_filters", "unused_columns_joins"],
    {
        "stg_accounts": "SELECT id AS account_id, name AS account_name, plan, region FROM accounts WHERE is_deleted IS NOT TRUE",
        "stg_subscriptions": "SELECT id AS subscription_id, account_id, mrr, status FROM subscriptions",
        "stg_users": "SELECT id AS user_id, account_id, role, is_active FROM app_users",
        "stg_usage": "SELECT user_id, feature, CAST(units AS INT64) AS units FROM usage_events",
        "stg_tickets": "SELECT id AS ticket_id, requester_id, priority, status FROM tickets",
        "stg_invoices": "SELECT id AS invoice_id, account_id, amount, status FROM invoices",
        "int_account_users": "SELECT account_id, COUNT(*) AS users, COUNTIF(is_active) AS active_users FROM stg_users GROUP BY account_id",
        "int_account_usage": "SELECT u.account_id, SUM(e.units) AS units, COUNT(DISTINCT e.feature) AS features_used FROM stg_usage AS e JOIN stg_users AS u ON u.user_id = e.user_id GROUP BY u.account_id",
        "int_account_tickets": "SELECT u.account_id, COUNT(*) AS tickets, COUNTIF(t.priority = 'urgent') AS urgent_tickets FROM stg_tickets AS t JOIN stg_users AS u ON u.user_id = t.requester_id WHERE t.status <> 'closed' GROUP BY u.account_id",
        "int_account_mrr": "SELECT account_id, SUM(mrr) AS mrr FROM stg_subscriptions WHERE status = 'active' GROUP BY account_id HAVING COUNT(*) > 0",
        "int_overdue": "SELECT DISTINCT account_id FROM stg_invoices WHERE status = 'open' AND amount > 0",
        "int_account_health": "SELECT a.account_id, a.account_name, a.plan, a.region, COALESCE(m.mrr, 0) AS mrr, COALESCE(us.active_users, 0) AS active_users, COALESCE(g.features_used, 0) AS features_used, COALESCE(t.urgent_tickets, 0) AS urgent_tickets, o.account_id IS NOT NULL AS has_overdue FROM stg_accounts AS a LEFT JOIN int_account_mrr AS m ON m.account_id = a.account_id LEFT JOIN int_account_users AS us ON us.account_id = a.account_id LEFT JOIN int_account_usage AS g ON g.account_id = a.account_id LEFT JOIN int_account_tickets AS t ON t.account_id = a.account_id LEFT JOIN int_overdue AS o ON o.account_id = a.account_id",
        "fct_account_health": "SELECT *, CASE WHEN has_overdue OR urgent_tickets > 1 THEN 'red' WHEN features_used < 2 OR active_users = 0 THEN 'amber' ELSE 'green' END AS health FROM int_account_health",
        "rpt_health_by_plan": "SELECT plan, health, COUNT(*) AS accounts, SUM(mrr) AS mrr FROM fct_account_health GROUP BY plan, health",
        "rpt_at_risk_mrr": "SELECT region, SUM(mrr) AS at_risk_mrr FROM fct_account_health WHERE health = 'red' GROUP BY region",
        "tmp_feature_popularity": "SELECT feature, SUM(units) AS units FROM stg_usage GROUP BY feature",
        "tmp_account_ticket_backlog": "SELECT account_id, tickets FROM int_account_tickets WHERE tickets > 5",
    },
    ["fct_account_health", "rpt_health_by_plan", "rpt_at_risk_mrr"],
    {
        "int_account_users": "SELECT account_id, COUNTIF(is_active) AS active_users FROM app_users GROUP BY account_id",
        "int_account_usage": "SELECT u.account_id, COUNT(DISTINCT e.feature) AS features_used FROM usage_events AS e JOIN app_users AS u ON u.id = e.user_id GROUP BY u.account_id",
        "int_account_tickets": "SELECT u.account_id, COUNTIF(t.priority = 'urgent') AS urgent_tickets FROM tickets AS t JOIN app_users AS u ON u.id = t.requester_id WHERE t.status <> 'closed' GROUP BY u.account_id",
        "int_account_mrr": "SELECT account_id, SUM(mrr) AS mrr FROM subscriptions WHERE status = 'active' GROUP BY account_id",
        "int_overdue": "SELECT DISTINCT account_id FROM invoices WHERE status = 'open' AND amount > 0",
        "fct_account_health": "SELECT a.id AS account_id, a.name AS account_name, a.plan, a.region, COALESCE(m.mrr, 0) AS mrr, COALESCE(us.active_users, 0) AS active_users, COALESCE(g.features_used, 0) AS features_used, COALESCE(t.urgent_tickets, 0) AS urgent_tickets, o.account_id IS NOT NULL AS has_overdue, CASE WHEN o.account_id IS NOT NULL OR COALESCE(t.urgent_tickets, 0) > 1 THEN 'red' WHEN COALESCE(g.features_used, 0) < 2 OR COALESCE(us.active_users, 0) = 0 THEN 'amber' ELSE 'green' END AS health FROM accounts AS a LEFT JOIN int_account_mrr AS m ON m.account_id = a.id LEFT JOIN int_account_users AS us ON us.account_id = a.id LEFT JOIN int_account_usage AS g ON g.account_id = a.id LEFT JOIN int_account_tickets AS t ON t.account_id = a.id LEFT JOIN int_overdue AS o ON o.account_id = a.id WHERE a.is_deleted IS NOT TRUE",
        "rpt_health_by_plan": KEEP,
        "rpt_at_risk_mrr": KEEP,
    },
    [
        ("drops the DISTINCT on overdue accounts, so an account with two open invoices repeats",
         {"int_overdue": "SELECT account_id FROM invoices WHERE status = 'open' AND amount > 0"}),
        ("filters deleted accounts with NOT is_deleted, dropping accounts whose flag is NULL",
         {"fct_account_health": "SELECT a.id AS account_id, a.name AS account_name, a.plan, a.region, COALESCE(m.mrr, 0) AS mrr, COALESCE(us.active_users, 0) AS active_users, COALESCE(g.features_used, 0) AS features_used, COALESCE(t.urgent_tickets, 0) AS urgent_tickets, o.account_id IS NOT NULL AS has_overdue, CASE WHEN o.account_id IS NOT NULL OR COALESCE(t.urgent_tickets, 0) > 1 THEN 'red' WHEN COALESCE(g.features_used, 0) < 2 OR COALESCE(us.active_users, 0) = 0 THEN 'amber' ELSE 'green' END AS health FROM accounts AS a LEFT JOIN int_account_mrr AS m ON m.account_id = a.id LEFT JOIN int_account_users AS us ON us.account_id = a.id LEFT JOIN int_account_usage AS g ON g.account_id = a.id LEFT JOIN int_account_tickets AS t ON t.account_id = a.id LEFT JOIN int_overdue AS o ON o.account_id = a.id WHERE NOT a.is_deleted"}),
    ],
    "HAVING COUNT(*) > 0 is always true for a group; the wide table and the health CASE merge.",
))

# E-commerce cohorts and LTV: IN subqueries become key joins, cohort tables fold into a CTE.
add(case(
    ["dead_tables", "mergeable_tables", "passthrough_chain", "unused_columns_joins"],
    {
        "stg_orders": "SELECT id AS order_id, customer_id, order_total, status, channel, created_on FROM raw_orders",
        "stg_customers": "SELECT id AS customer_id, LOWER(email) AS email, country, is_test FROM raw_customers",
        "stg_order_lines": "SELECT order_id, line_no, product_id, quantity, unit_price FROM raw_order_lines",
        "stg_products": "SELECT id AS product_id, name AS product_name, category FROM raw_products",
        "stg_refunds": "SELECT id AS refund_id, order_id, amount, reason FROM raw_refunds",
        "int_real_customers": "SELECT * FROM stg_customers WHERE is_test = FALSE OR is_test IS NULL",
        "int_valid_orders": "SELECT o.* FROM stg_orders AS o WHERE o.status = 'completed' AND o.customer_id IN (SELECT customer_id FROM int_real_customers)",
        "int_order_refunds": "SELECT order_id, SUM(amount) AS refunded FROM stg_refunds GROUP BY order_id",
        "int_orders_net": "SELECT o.order_id, o.customer_id, o.channel, o.created_on, o.order_total, COALESCE(r.refunded, 0) AS refunded, o.order_total - COALESCE(r.refunded, 0) AS net_total FROM int_valid_orders AS o LEFT JOIN int_order_refunds AS r ON r.order_id = o.order_id",
        "int_customer_first_order": "SELECT customer_id, MIN(created_on) AS first_order_on FROM int_valid_orders GROUP BY customer_id",
        "int_customer_cohorts": "SELECT customer_id, DATE_TRUNC(first_order_on, MONTH) AS cohort_month FROM int_customer_first_order",
        "int_orders_with_cohort": "SELECT n.*, c.cohort_month FROM int_orders_net AS n JOIN int_customer_cohorts AS c ON c.customer_id = n.customer_id",
        "rpt_cohort_revenue": "SELECT cohort_month, SUM(net_total) AS net_revenue, COUNT(DISTINCT customer_id) AS customers FROM int_orders_with_cohort GROUP BY cohort_month",
        "rpt_customer_ltv": "SELECT customer_id, SUM(net_total) AS ltv, COUNT(*) AS orders FROM int_orders_net GROUP BY customer_id",
        "rpt_category_revenue": "SELECT p.category, SUM(l.quantity * l.unit_price) AS gross_revenue FROM stg_order_lines AS l JOIN int_valid_orders AS o ON o.order_id = l.order_id LEFT JOIN stg_products AS p ON p.product_id = l.product_id GROUP BY p.category",
        "rpt_refund_reasons": "SELECT reason, COUNT(*) AS refunds, SUM(amount) AS refunded FROM stg_refunds WHERE order_id IN (SELECT order_id FROM int_valid_orders) GROUP BY reason",
        "tmp_country_customers": "SELECT country, COUNT(*) AS customers FROM int_real_customers GROUP BY country",
        "tmp_cohort_sizes": "SELECT cohort_month, COUNT(*) AS customers FROM int_customer_cohorts GROUP BY cohort_month",
    },
    ["rpt_cohort_revenue", "rpt_customer_ltv", "rpt_category_revenue", "rpt_refund_reasons"],
    {
        "int_valid_orders": "SELECT o.id AS order_id, o.customer_id, o.order_total, o.created_on FROM raw_orders AS o JOIN raw_customers AS c ON c.id = o.customer_id WHERE o.status = 'completed' AND c.is_test IS NOT TRUE",
        "int_order_refunds": "SELECT order_id, SUM(amount) AS refunded FROM raw_refunds GROUP BY order_id",
        "int_orders_net": "SELECT o.customer_id, o.order_total - COALESCE(r.refunded, 0) AS net_total FROM int_valid_orders AS o LEFT JOIN int_order_refunds AS r ON r.order_id = o.order_id",
        "rpt_cohort_revenue": "WITH cohorts AS (SELECT customer_id, DATE_TRUNC(MIN(created_on), MONTH) AS cohort_month FROM int_valid_orders GROUP BY customer_id) SELECT c.cohort_month, SUM(n.net_total) AS net_revenue, COUNT(DISTINCT n.customer_id) AS customers FROM int_orders_net AS n JOIN cohorts AS c ON c.customer_id = n.customer_id GROUP BY c.cohort_month",
        "rpt_customer_ltv": KEEP,
        "rpt_category_revenue": "SELECT p.category, SUM(l.quantity * l.unit_price) AS gross_revenue FROM raw_order_lines AS l JOIN int_valid_orders AS o ON o.order_id = l.order_id LEFT JOIN raw_products AS p ON p.id = l.product_id GROUP BY p.category",
        "rpt_refund_reasons": "SELECT r.reason, COUNT(*) AS refunds, SUM(r.amount) AS refunded FROM raw_refunds AS r JOIN int_valid_orders AS o ON o.order_id = r.order_id GROUP BY r.reason",
    },
    [
        ("filters test customers with is_test = FALSE, dropping customers whose flag is NULL",
         {"int_valid_orders": "SELECT o.id AS order_id, o.customer_id, o.order_total, o.created_on FROM raw_orders AS o JOIN raw_customers AS c ON c.id = o.customer_id WHERE o.status = 'completed' AND c.is_test = FALSE"}),
        ("computes cohorts from every order instead of valid orders only",
         {"rpt_cohort_revenue": "WITH cohorts AS (SELECT customer_id, DATE_TRUNC(MIN(created_on), MONTH) AS cohort_month FROM raw_orders GROUP BY customer_id) SELECT c.cohort_month, SUM(n.net_total) AS net_revenue, COUNT(DISTINCT n.customer_id) AS customers FROM int_orders_net AS n JOIN cohorts AS c ON c.customer_id = n.customer_id GROUP BY c.cohort_month"}),
    ],
    "IN subqueries over keyed tables become joins; the cohort chain becomes one CTE.",
))

# Retail legacy reports: copy-pasted CTEs, a duplicated line-revenue table, implied filters.
add(case(
    ["cte_repeats_table", "dead_tables", "duplicated_logic", "mergeable_tables", "passthrough_chain",
     "redundant_filters"],
    {
        "stg_orders": "SELECT id AS order_id, customer_id, order_total, status, channel, created_on FROM raw_orders",
        "stg_customers": "SELECT id AS customer_id, email, country, is_test FROM raw_customers",
        "stg_order_lines": "SELECT order_id, line_no, product_id, quantity, unit_price FROM raw_order_lines",
        "stg_products": "SELECT id AS product_id, name AS product_name, category, is_active FROM raw_products",
        "int_completed_orders": "SELECT * FROM stg_orders WHERE status = 'completed'",
        "int_completed_orders_web": "SELECT * FROM int_completed_orders WHERE channel = 'web' AND status IS NOT NULL",
        "int_completed_orders_app": "SELECT * FROM int_completed_orders WHERE channel = 'app' AND order_total IS NOT NULL",
        "int_daily_sales": "SELECT created_on, channel, SUM(order_total) AS sales, COUNT(*) AS orders FROM int_completed_orders GROUP BY created_on, channel",
        "rpt_daily_web_sales": "WITH web AS (SELECT * FROM stg_orders WHERE status = 'completed' AND channel = 'web') SELECT created_on, SUM(order_total) AS sales FROM web GROUP BY created_on",
        "rpt_daily_app_sales": "SELECT created_on, SUM(order_total) AS sales, COUNT(*) AS orders FROM int_completed_orders_app GROUP BY created_on",
        "rpt_channel_totals": "SELECT channel, SUM(sales) AS sales, SUM(orders) AS orders FROM int_daily_sales GROUP BY channel",
        "rpt_web_customers": "SELECT DISTINCT c.customer_id, c.country FROM int_completed_orders_web AS o JOIN stg_customers AS c ON c.customer_id = o.customer_id WHERE c.is_test IS NOT TRUE",
        "int_line_revenue": "SELECT l.order_id, l.product_id, l.quantity * l.unit_price AS revenue FROM stg_order_lines AS l WHERE l.order_id IN (SELECT order_id FROM int_completed_orders)",
        "int_line_revenue_legacy": "SELECT l.order_id, l.product_id, l.quantity * l.unit_price AS revenue FROM raw_order_lines AS l JOIN raw_orders AS o ON o.id = l.order_id WHERE o.status = 'completed'",
        "rpt_category_sales": "SELECT p.category, SUM(r.revenue) AS revenue FROM int_line_revenue AS r LEFT JOIN stg_products AS p ON p.product_id = r.product_id GROUP BY p.category",
        "rpt_active_product_sales": "WITH active AS (SELECT product_id, product_name FROM stg_products WHERE is_active) SELECT a.product_name, SUM(r.revenue) AS revenue FROM int_line_revenue_legacy AS r JOIN active AS a ON a.product_id = r.product_id GROUP BY a.product_name",
        "rpt_orders_without_lines": "SELECT o.order_id FROM int_completed_orders AS o LEFT JOIN stg_order_lines AS l ON l.order_id = o.order_id WHERE l.order_id IS NULL",
        "tmp_big_web_orders": "SELECT order_id FROM int_completed_orders_web WHERE order_total > 4",
        "tmp_inactive_products": "SELECT product_id FROM stg_products WHERE NOT is_active",
    },
    ["rpt_daily_web_sales", "rpt_daily_app_sales", "rpt_channel_totals", "rpt_web_customers", "rpt_category_sales",
     "rpt_active_product_sales", "rpt_orders_without_lines"],
    {
        "int_completed_orders": "SELECT id AS order_id, customer_id, order_total, channel, created_on FROM raw_orders WHERE status = 'completed'",
        "rpt_daily_web_sales": "SELECT created_on, SUM(order_total) AS sales FROM int_completed_orders WHERE channel = 'web' GROUP BY created_on",
        "rpt_daily_app_sales": "SELECT created_on, SUM(order_total) AS sales, COUNT(*) AS orders FROM int_completed_orders WHERE channel = 'app' AND order_total IS NOT NULL GROUP BY created_on",
        "rpt_channel_totals": "SELECT channel, SUM(order_total) AS sales, COUNT(*) AS orders FROM int_completed_orders GROUP BY channel",
        "rpt_web_customers": "SELECT DISTINCT c.id AS customer_id, c.country FROM int_completed_orders AS o JOIN raw_customers AS c ON c.id = o.customer_id WHERE o.channel = 'web' AND c.is_test IS NOT TRUE",
        "int_line_revenue": "SELECT l.product_id, l.quantity * l.unit_price AS revenue FROM raw_order_lines AS l JOIN int_completed_orders AS o ON o.order_id = l.order_id",
        "rpt_category_sales": "SELECT p.category, SUM(r.revenue) AS revenue FROM int_line_revenue AS r LEFT JOIN raw_products AS p ON p.id = r.product_id GROUP BY p.category",
        "rpt_active_product_sales": "SELECT p.name AS product_name, SUM(r.revenue) AS revenue FROM int_line_revenue AS r JOIN raw_products AS p ON p.id = r.product_id WHERE p.is_active GROUP BY p.name",
        "rpt_orders_without_lines": "SELECT o.order_id FROM int_completed_orders AS o LEFT JOIN raw_order_lines AS l ON l.order_id = o.order_id WHERE l.order_id IS NULL",
    },
    [
        ("drops order_total IS NOT NULL from the app report because SUM ignores NULLs, but COUNT(*) does not",
         {"rpt_daily_app_sales": "SELECT created_on, SUM(order_total) AS sales, COUNT(*) AS orders FROM int_completed_orders WHERE channel = 'app' GROUP BY created_on"}),
        ("drops the DISTINCT in the web customers report, repeating customers with several web orders",
         {"rpt_web_customers": "SELECT c.id AS customer_id, c.country FROM int_completed_orders AS o JOIN raw_customers AS c ON c.id = o.customer_id WHERE o.channel = 'web' AND c.is_test IS NOT TRUE"}),
    ],
    "One completed-orders table serves every report; the legacy line table duplicates the IN-subquery one.",
    data={"raw_customers": [[1, "ann@shop.io", "US", False]],
          "raw_orders": [[1, 1, 5, "completed", "web", "2024-01-01"], [2, 1, 3, "completed", "web", "2024-01-02"]]},
))

# Support platform: twenty tables, requester to account lookups, first public response, CSAT.
add(case(
    ["dead_tables", "mergeable_tables", "passthrough_chain", "unused_columns_joins"],
    {
        "stg_tickets": "SELECT id AS ticket_id, requester_id, assignee_id, priority, status, channel FROM tickets",
        "stg_status_history": "SELECT id AS change_id, ticket_id, status AS new_status, agent_id FROM ticket_status_history",
        "stg_comments": "SELECT id AS comment_id, ticket_id, author_id, is_public FROM ticket_comments",
        "stg_agents": "SELECT id AS agent_id, name AS agent_name, team, is_bot FROM agents",
        "stg_csat": "SELECT ticket_id, score FROM csat_responses",
        "stg_users": "SELECT id AS user_id, account_id, email FROM app_users",
        "stg_accounts": "SELECT id AS account_id, name AS account_name, plan, region FROM accounts",
        "int_ticket_requesters": "SELECT t.ticket_id, t.priority, t.status, t.channel, t.assignee_id, u.account_id FROM stg_tickets AS t LEFT JOIN stg_users AS u ON u.user_id = t.requester_id",
        "int_ticket_accounts": "SELECT r.*, a.plan, a.region FROM int_ticket_requesters AS r LEFT JOIN stg_accounts AS a ON a.account_id = r.account_id",
        "int_comment_stats": "SELECT ticket_id, COUNT(*) AS comments, COUNTIF(is_public) AS public_comments, COUNT(DISTINCT author_id) AS participants FROM stg_comments GROUP BY ticket_id",
        "int_first_response": "WITH public_comments AS (SELECT ticket_id, author_id, comment_id, ROW_NUMBER() OVER (PARTITION BY ticket_id ORDER BY comment_id) AS rn FROM stg_comments WHERE is_public) SELECT ticket_id, author_id AS first_responder_id FROM public_comments WHERE rn = 1",
        "int_solve_changes": "SELECT ticket_id, COUNT(*) AS solves FROM stg_status_history WHERE new_status = 'solved' GROUP BY ticket_id",
        "int_csat": "SELECT ticket_id, AVG(score) AS csat FROM stg_csat GROUP BY ticket_id",
        "fct_tickets": "SELECT t.ticket_id, t.priority, t.status, t.plan, t.region, COALESCE(c.public_comments, 0) AS public_comments, f.first_responder_id, ag.team AS first_responder_team, COALESCE(s.solves, 0) AS solves, q.csat FROM int_ticket_accounts AS t LEFT JOIN int_comment_stats AS c ON c.ticket_id = t.ticket_id LEFT JOIN int_first_response AS f ON f.ticket_id = t.ticket_id LEFT JOIN stg_agents AS ag ON ag.agent_id = f.first_responder_id LEFT JOIN int_solve_changes AS s ON s.ticket_id = t.ticket_id LEFT JOIN int_csat AS q ON q.ticket_id = t.ticket_id",
        "rpt_plan_support_load": "SELECT plan, COUNT(*) AS tickets, SUM(public_comments) AS public_comments FROM fct_tickets GROUP BY plan",
        "rpt_team_first_response": "SELECT first_responder_team, COUNT(*) AS tickets, AVG(csat) AS csat FROM fct_tickets WHERE first_responder_team IS NOT NULL GROUP BY first_responder_team",
        "rpt_reopen_candidates": "SELECT ticket_id, solves FROM fct_tickets WHERE solves > 1",
        "rpt_region_urgent": "SELECT region, COUNT(*) AS urgent_tickets FROM int_ticket_accounts WHERE priority = 'urgent' GROUP BY region",
        "tmp_bot_agents": "SELECT agent_id FROM stg_agents WHERE is_bot",
        "tmp_participants": "SELECT ticket_id FROM int_comment_stats WHERE participants > 3",
    },
    ["fct_tickets", "rpt_plan_support_load", "rpt_team_first_response", "rpt_reopen_candidates", "rpt_region_urgent"],
    {
        "int_comment_stats": "SELECT ticket_id, COUNTIF(is_public) AS public_comments FROM ticket_comments GROUP BY ticket_id",
        "int_first_response": "WITH public_comments AS (SELECT ticket_id, author_id, ROW_NUMBER() OVER (PARTITION BY ticket_id ORDER BY id) AS rn FROM ticket_comments WHERE is_public) SELECT ticket_id, author_id AS first_responder_id FROM public_comments WHERE rn = 1",
        "int_solve_changes": "SELECT ticket_id, COUNT(*) AS solves FROM ticket_status_history WHERE status = 'solved' GROUP BY ticket_id",
        "int_csat": "SELECT ticket_id, AVG(score) AS csat FROM csat_responses GROUP BY ticket_id",
        "fct_tickets": "SELECT t.id AS ticket_id, t.priority, t.status, a.plan, a.region, COALESCE(c.public_comments, 0) AS public_comments, f.first_responder_id, ag.team AS first_responder_team, COALESCE(s.solves, 0) AS solves, q.csat FROM tickets AS t LEFT JOIN app_users AS u ON u.id = t.requester_id LEFT JOIN accounts AS a ON a.id = u.account_id LEFT JOIN int_comment_stats AS c ON c.ticket_id = t.id LEFT JOIN int_first_response AS f ON f.ticket_id = t.id LEFT JOIN agents AS ag ON ag.id = f.first_responder_id LEFT JOIN int_solve_changes AS s ON s.ticket_id = t.id LEFT JOIN int_csat AS q ON q.ticket_id = t.id",
        "rpt_plan_support_load": KEEP,
        "rpt_team_first_response": KEEP,
        "rpt_reopen_candidates": KEEP,
        "rpt_region_urgent": "SELECT region, COUNT(*) AS urgent_tickets FROM fct_tickets WHERE priority = 'urgent' GROUP BY region",
    },
    [
        ("ranks every comment and keeps the first one only if it is public",
         {"int_first_response": "WITH public_comments AS (SELECT ticket_id, author_id, is_public, ROW_NUMBER() OVER (PARTITION BY ticket_id ORDER BY id) AS rn FROM ticket_comments) SELECT ticket_id, author_id AS first_responder_id FROM public_comments WHERE rn = 1 AND is_public"}),
        ("joins CSAT responses directly instead of each ticket's average, repeating tickets with several responses",
         {"int_csat": None,
          "fct_tickets": "SELECT t.id AS ticket_id, t.priority, t.status, a.plan, a.region, COALESCE(c.public_comments, 0) AS public_comments, f.first_responder_id, ag.team AS first_responder_team, COALESCE(s.solves, 0) AS solves, q.score AS csat FROM tickets AS t LEFT JOIN app_users AS u ON u.id = t.requester_id LEFT JOIN accounts AS a ON a.id = u.account_id LEFT JOIN int_comment_stats AS c ON c.ticket_id = t.id LEFT JOIN int_first_response AS f ON f.ticket_id = t.id LEFT JOIN agents AS ag ON ag.id = f.first_responder_id LEFT JOIN int_solve_changes AS s ON s.ticket_id = t.id LEFT JOIN csat_responses AS q ON q.ticket_id = t.id"}),
    ],
    "Requester and account lookups are on keys, so they move into the fact table and the urgent report reads it.",
))


# ------------------------------------------------------------------ build, verify, write


def build(number: int, raw: dict) -> dict:
    case_id = f"hw-{number:04d}"
    sqls = [*raw["tables"].values(), *raw["reference"]["tables"].values(),
            *(s for t in raw["traps"] for s in t["tables"].values())]
    used = set()
    for sql in sqls:
        used |= mc.reads(sql)
    return {
        "id": case_id,
        "source": "handwritten",
        "families": raw["families"],
        "split": mc.held_out_split(case_id),
        "dialect": "bigquery",
        "sources": {name: SOURCES[name] for name in SOURCES if name in used},
        "tables": raw["tables"],
        "protected": raw["protected"],
        "reference": {"tables": raw["reference"]["tables"]},
        "traps": [dict(t) for t in raw["traps"]],
        "_note": raw["note"],
        **({"data": raw["data"]} if raw.get("data") else {}),
    }


def _verify(args) -> dict | str:
    number, raw, databases, prove = args
    case = build(number, raw)
    note = case.pop("_note")
    try:
        out = gen.verify_case(case, databases, prove)
    except gen.GeneratorError as error:
        return str(error)
    except Exception as error:  # a broken query, say: report it with the case id
        return f"{case['id']}: {type(error).__name__}: {error}"[:500]
    out["source"] = "handwritten"
    out["note"] = (note + " " if note else "") + "Hand-written; reference and traps verified on DuckDB."
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--databases", type=int, default=gen.DATABASES)
    parser.add_argument("--no-prove", action="store_true")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--only", type=int, action="append", help="verify only this case number (no file written)")
    parser.add_argument("--out", type=Path, default=mc.CASES_DIR / "handwritten.jsonl")
    args = parser.parse_args(argv)
    started = time.time()
    numbers = args.only or list(range(1, len(CASES) + 1))
    work = [(n, CASES[n - 1], args.databases, not args.no_prove) for n in numbers]
    if args.jobs > 1 and len(work) > 1:
        from multiprocessing import Pool

        with Pool(args.jobs) as pool:
            cases = pool.map(_verify, work, chunksize=1)
    else:
        cases = [_verify(w) for w in work]
    failed = [c for c in cases if isinstance(c, str)]
    if failed:
        print("\n".join(failed), file=sys.stderr)
        print(f"{len(failed)} cases failed verification; nothing written", file=sys.stderr)
        return 1
    if args.only:
        for c in cases:
            print(c["id"], len(c["tables"]), c["original"]["complexity"]["score"], "->",
                  c["reference"]["complexity"]["score"], c["verification"]["proved"], file=sys.stderr)
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for c in cases:
            handle.write(json.dumps(c) + "\n")
    held = sum(c["split"] == "held_out" for c in cases)
    proved = sum(all(c["verification"]["proved"].values()) for c in cases if c["verification"]["proved"])
    sizes = sorted(len(c["tables"]) for c in cases)
    print(f"{len(cases)} cases ({held} held out), sizes {sizes[0]}-{sizes[-1]} "
          f"({sum(s >= 12 for s in sizes)} with 12 or more tables), {sum(len(c['traps']) for c in cases)} traps, "
          f"references proved in full for {proved}; {time.time() - started:.0f} s -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
