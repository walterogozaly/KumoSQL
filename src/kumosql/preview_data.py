"""Sample payloads for UI views whose backends are still on the roadmap.

The query graph, cost and change report pages in the browser UI read
``/api/graph``, ``/api/cost`` and ``/api/changes``. Until the roadmap issues
that produce this data land, ``ui.py`` serves the functions below. Every
payload carries ``"preview": True`` and the issues it stands in for, and the
pages show a banner while that flag is set.

To wire a view up, return the same shape from real code and drop the
``preview`` key (or set it to False). ``docs/ui-roadmap.md`` lists each field
and the issue that owns it. All names here are generic samples; no real
project, table or query appears.
"""

from __future__ import annotations

from copy import deepcopy

# Evidence labels shared by the workspace, change reports and proposals (#15).
EVIDENCE_LABELS = ("proven", "planner_checked", "unproven", "failed", "unchanged")

_WINDOW = {"start": "2026-09-01T00:00:00Z", "end": "2026-09-30T00:00:00Z"}


def _node(node_id, kind, source, columns=(), **extra):
    dataset, _, name = node_id.partition(".")
    return {"id": node_id, "dataset": dataset, "name": name, "kind": kind,
            "source": source, "columns": list(columns), **extra}


def _edge(upstream, downstream, source, confidence, last_seen=None, observed_count=0):
    return {"from": upstream, "to": downstream, "source": source, "confidence": confidence,
            "last_seen": last_seen, "observed_count": observed_count}


def _col(node, column, sources, transform):
    return {"node": node, "column": column,
            "sources": [{"node": n, "column": c} for n, c in sources], "transform": transform}


_GRAPH = {
    "issues": [23, 24, 25, 26, 27, 28, 29],
    "window": _WINDOW,
    # #29: coverage and sampled accuracy, anonymized aggregates only.
    "coverage": {
        "assets_total": 14, "assets_analyzed": 12,
        "statements_total": 1840, "statements_matched": 1712,
        "sampled_impact_accuracy": 0.93, "sample_size": 60,
        "complete": False,
    },
    # #23: one identity per asset/table. kind: source | model | view | observed | unmatched.
    "nodes": [
        _node("raw.orders", "source", "observed", ["order_id", "customer_id", "amount", "currency", "status", "created_at"]),
        _node("raw.customers", "source", "observed", ["customer_id", "region", "signup_date", "email"]),
        _node("raw.payments", "source", "observed", ["payment_id", "order_id", "amount", "paid_at"]),
        _node("raw.fx_rates", "source", "observed", ["currency", "rate", "rate_date"]),
        _node("staging.stg_orders", "model", "both", ["order_id", "customer_id", "amount_usd", "status", "order_date"]),
        _node("staging.stg_customers", "model", "both", ["customer_id", "region", "signup_date"]),
        _node("staging.stg_payments", "model", "declared", ["order_id", "paid_amount", "paid_at"]),
        _node("marts.fct_orders", "model", "both", ["order_id", "customer_id", "amount_usd", "paid_amount", "order_date"]),
        _node("marts.dim_customers", "model", "both", ["customer_id", "region", "first_order_date"]),
        _node("marts.daily_revenue", "model", "both", ["order_date", "region", "revenue_usd"]),
        _node("marts.customer_ltv", "model", "declared", ["customer_id", "lifetime_value_usd"]),
        _node("reporting.exec_dashboard", "observed", "observed", ["order_date", "revenue_usd"],
              note="Scheduled query seen in job history; no compiled model."),
        _node("reporting.finance_extract", "unmatched", "observed", [],
              note="Referenced by jobs but could not be read."),
        _node("marts.legacy_rollup", "model", "declared", [],
              note="Could not be parsed; its reads are unknown."),
    ],
    # #24: every edge says declared / observed / both / parsed, with confidence and last seen.
    "edges": [
        _edge("raw.orders", "staging.stg_orders", "both", "high", "2026-09-29T06:00:00Z", 30),
        _edge("raw.fx_rates", "staging.stg_orders", "both", "high", "2026-09-29T06:00:00Z", 30),
        _edge("raw.customers", "staging.stg_customers", "both", "high", "2026-09-29T06:00:00Z", 30),
        _edge("raw.payments", "staging.stg_payments", "declared", "medium"),
        _edge("staging.stg_orders", "marts.fct_orders", "both", "high", "2026-09-29T06:10:00Z", 30),
        _edge("staging.stg_payments", "marts.fct_orders", "both", "high", "2026-09-29T06:10:00Z", 30),
        _edge("staging.stg_customers", "marts.dim_customers", "both", "high", "2026-09-29T06:10:00Z", 30),
        _edge("staging.stg_orders", "marts.dim_customers", "parsed", "low"),
        _edge("marts.fct_orders", "marts.daily_revenue", "both", "high", "2026-09-29T06:20:00Z", 30),
        _edge("marts.dim_customers", "marts.daily_revenue", "both", "high", "2026-09-29T06:20:00Z", 30),
        _edge("marts.fct_orders", "marts.customer_ltv", "declared", "medium"),
        _edge("marts.daily_revenue", "reporting.exec_dashboard", "observed", "medium", "2026-09-29T07:00:00Z", 29),
        _edge("marts.fct_orders", "reporting.finance_extract", "observed", "low", "2026-09-22T09:00:00Z", 4),
        _edge("staging.stg_orders", "marts.legacy_rollup", "declared", "low"),
    ],
    # #27: output column -> source columns, one hop per entry.
    "column_lineage": [
        _col("staging.stg_orders", "amount_usd", [("raw.orders", "amount"), ("raw.fx_rates", "rate")], "amount × rate"),
        _col("staging.stg_orders", "order_date", [("raw.orders", "created_at")], "DATE(created_at)"),
        _col("staging.stg_orders", "customer_id", [("raw.orders", "customer_id")], "renamed"),
        _col("staging.stg_orders", "order_id", [("raw.orders", "order_id")], "passthrough"),
        _col("staging.stg_orders", "status", [("raw.orders", "status")], "passthrough"),
        _col("staging.stg_customers", "customer_id", [("raw.customers", "customer_id")], "passthrough"),
        _col("staging.stg_customers", "region", [("raw.customers", "region")], "UPPER(region)"),
        _col("staging.stg_customers", "signup_date", [("raw.customers", "signup_date")], "passthrough"),
        _col("staging.stg_payments", "paid_amount", [("raw.payments", "amount")], "SUM(amount)"),
        _col("staging.stg_payments", "order_id", [("raw.payments", "order_id")], "group key"),
        _col("marts.fct_orders", "amount_usd", [("staging.stg_orders", "amount_usd")], "passthrough"),
        _col("marts.fct_orders", "paid_amount", [("staging.stg_payments", "paid_amount")], "LEFT JOIN"),
        _col("marts.fct_orders", "order_date", [("staging.stg_orders", "order_date")], "passthrough"),
        _col("marts.fct_orders", "customer_id", [("staging.stg_orders", "customer_id")], "passthrough"),
        _col("marts.fct_orders", "order_id", [("staging.stg_orders", "order_id")], "passthrough"),
        _col("marts.dim_customers", "customer_id", [("staging.stg_customers", "customer_id")], "passthrough"),
        _col("marts.dim_customers", "region", [("staging.stg_customers", "region")], "passthrough"),
        _col("marts.dim_customers", "first_order_date", [("staging.stg_orders", "order_date")], "MIN(order_date)"),
        _col("marts.daily_revenue", "revenue_usd", [("marts.fct_orders", "amount_usd")], "SUM(amount_usd)"),
        _col("marts.daily_revenue", "order_date", [("marts.fct_orders", "order_date")], "group key"),
        _col("marts.daily_revenue", "region", [("marts.dim_customers", "region")], "JOIN on customer_id"),
        _col("marts.customer_ltv", "lifetime_value_usd", [("marts.fct_orders", "amount_usd")], "SUM(amount_usd)"),
        _col("marts.customer_ltv", "customer_id", [("marts.fct_orders", "customer_id")], "group key"),
        _col("reporting.exec_dashboard", "revenue_usd", [("marts.daily_revenue", "revenue_usd")], "observed read"),
        _col("reporting.exec_dashboard", "order_date", [("marts.daily_revenue", "order_date")], "observed read"),
    ],
    # #28: never let a partial graph look complete.
    "gaps": [
        {"asset": "marts.legacy_rollup", "kind": "parse_error",
         "message": "Statement could not be parsed; downstream reads from it are unknown."},
        {"asset": "reporting.finance_extract", "kind": "inaccessible",
         "message": "Seen in job history but its definition could not be read."},
        {"asset": "raw.orders_backup", "kind": "unmatched_reference",
         "message": "Referenced by 3 jobs; no matching asset identity."},
        {"asset": "ad hoc queries", "kind": "unattributed_reads",
         "message": "128 statements read graph tables without a destination asset."},
    ],
}

_COST = {
    "issues": [30, 31, 32, 33, 34, 35],
    "window": _WINDOW,
    "currency": "USD",
    # #30: measured cost joined to nodes; what cannot be attributed stays visible.
    "totals": {"measured": 4820.0, "attributed": 4310.0, "unattributed": 510.0},
    "nodes": [
        {"node": "marts.fct_orders", "measured": 1480.0, "runs": 30, "bytes_processed": 96.2e12},
        {"node": "staging.stg_orders", "measured": 1120.0, "runs": 30, "bytes_processed": 71.5e12},
        {"node": "marts.daily_revenue", "measured": 690.0, "runs": 30, "bytes_processed": 44.1e12},
        {"node": "marts.customer_ltv", "measured": 540.0, "runs": 30, "bytes_processed": 34.6e12},
        {"node": "reporting.exec_dashboard", "measured": 310.0, "runs": 29, "bytes_processed": 19.8e12},
        {"node": "marts.dim_customers", "measured": 170.0, "runs": 30, "bytes_processed": 10.9e12},
    ],
    # #31-#33: ranked opportunities in the first-recommendation format.
    "opportunities": [
        {
            "id": "opp-1", "rank": 1,
            "title": "Currency conversion repeated in three models",
            "savings": {"value": 610.0, "basis": "estimate", "range": [420.0, 610.0]},
            "measured_cost": 1830.0, "frequency": "daily", "downstream_reach": 6,
            "repeats": [
                {"node": "staging.stg_orders", "where": "amount × rate join"},
                {"node": "marts.customer_ltv", "where": "same fx join, recomputed"},
                {"node": "reporting.exec_dashboard", "where": "same fx join, recomputed"},
            ],
            "consumers": ["marts.fct_orders", "marts.daily_revenue", "marts.customer_ltv",
                          "reporting.exec_dashboard"],
            "proposed_change": "Read amount_usd from staging.stg_orders instead of recomputing the fx join.",
            "rule": "reuse_upstream_column",
            "verification": {"required": "proven", "plan": [
                "Structural proof per changed consumer",
                "Synthetic-result check with a fixed seed",
                "Planner check for output schema",
            ]},
            "status": "open",
        },
        {
            "id": "opp-2", "rank": 2,
            "title": "Full scan of raw.orders where a date filter is possible",
            "savings": {"value": 380.0, "basis": "upper_bound"},
            "measured_cost": 1120.0, "frequency": "daily", "downstream_reach": 8,
            "repeats": [{"node": "staging.stg_orders", "where": "no partition filter"}],
            "consumers": ["marts.fct_orders", "marts.dim_customers", "marts.daily_revenue"],
            "proposed_change": "Filter on created_at partitions before the join.",
            "rule": "partition_filter_pushdown",
            "verification": {"required": "proven", "plan": ["SMT proof of the predicate change", "Planner check"]},
            "status": "open",
        },
        {
            "id": "opp-3", "rank": 3,
            "title": "Identical CTE in two dashboard queries",
            "savings": {"value": 95.0, "basis": "measured"},
            "measured_cost": 310.0, "frequency": "hourly", "downstream_reach": 1,
            "repeats": [{"node": "reporting.exec_dashboard", "where": "two identical CTEs"}],
            "consumers": ["reporting.exec_dashboard"],
            "proposed_change": "Deduplicate the CTE.",
            "rule": "deduplicate_ctes",
            "verification": {"required": "proven", "plan": ["Structural proof"]},
            "status": "validated",
        },
    ],
    # #34: each cost rule ships alone with its conditions and requirements.
    "rules": [
        {"id": "deduplicate_ctes", "name": "Deduplicate CTEs", "state": "shipped",
         "safe_when": "CTE bodies are structurally identical.", "requires": "proven",
         "outcome": "Measured on accepted changes"},
        {"id": "reuse_upstream_column", "name": "Reuse upstream column", "state": "planned",
         "safe_when": "Upstream column lineage matches the recomputed expression.", "requires": "proven",
         "outcome": "Not yet measured"},
        {"id": "partition_filter_pushdown", "name": "Partition filter pushdown", "state": "planned",
         "safe_when": "Every consumer filters on the partition column.", "requires": "proven",
         "outcome": "Not yet measured"},
    ],
    # #35: success is validated savings from accepted changes.
    "validated": {"accepted_changes": 1, "validated_savings": 95.0, "pending_estimates": 990.0},
}

_CHANGES = {
    "issues": [22, 36, 37, 38, 39, 40, 41, 42],
    # #36: one report per proposed change set.
    "report": {
        "title": "Use converted amounts from staging",
        "base": "main", "head": "reuse-amount-usd",
        "generated_at": "2026-09-29T18:00:00Z",
        "changes": [
            {"model": "marts.customer_ltv", "kind": "modified",
             "verification": {"label": "proven", "reason": "Structural proof after normalizing the join",
                              "checks": [{"kind": "structural_proof", "outcome": "passed"},
                                         {"kind": "planner", "outcome": "passed", "detail": "Output schema matches"}]},
             "cost": {"basis": "estimate", "before": 540.0, "after": 310.0},
             "consumers": {"models": [], "complete": True}},
            {"model": "reporting.exec_dashboard", "kind": "modified",
             "verification": {"label": "planner_checked", "reason": "Plans and schemas match; no proof yet",
                              "checks": [{"kind": "structural_proof", "outcome": "inconclusive",
                                          "detail": "Window function order differs"},
                                         {"kind": "planner", "outcome": "passed"},
                                         {"kind": "synthetic_results", "outcome": "inconclusive",
                                          "detail": "Baseline differs from itself across runs"}]},
             "cost": {"basis": "estimate", "before": 310.0, "after": 190.0},
             "consumers": {"models": [], "complete": False}},
            {"model": "marts.daily_revenue", "kind": "unchanged",
             "verification": {"label": "unchanged", "reason": "Text identical", "checks": []},
             "cost": {"basis": "unavailable"},
             "consumers": {"models": ["reporting.exec_dashboard"], "complete": True}},
            {"model": "staging.stg_orders", "kind": "modified",
             "verification": {"label": "unproven", "reason": "Predicate change outside the SMT fragment",
                              "checks": [{"kind": "smt", "outcome": "unsupported", "detail": "Uses a UDF"}]},
             "cost": {"basis": "upper_bound", "before": 1120.0, "after": 740.0},
             "consumers": {"models": ["marts.fct_orders", "marts.dim_customers", "marts.legacy_rollup"],
                           "complete": False}},
        ],
        # #39: one unreadable asset is a diagnostic, not a lost report.
        "diagnostics": [
            {"asset": "marts.legacy_rollup", "message": "Could not be parsed; impact on it is unknown."},
            {"asset": "reporting.finance_extract", "message": "Definition not accessible; skipped."},
        ],
    },
    # #22: share of changed outputs with useful evidence; proof and planner kept apart.
    "evidence_coverage": {"changed": 212, "proven": 131, "planner_checked": 38, "unproven": 36, "failed": 7},
    # #37: what the CI check posts on a review request.
    "ci": {
        "check_name": "KumoSQL change report", "conclusion": "neutral",
        "summary": "3 changed models · 1 proven · 1 planner checked · 1 unproven · 2 diagnostics",
    },
    # #38: further query sources, enabled once identities reconcile.
    "sources": [
        {"name": "Dataform repository", "kind": "compiled models", "state": "connected", "matched": 0.97},
        {"name": "BigQuery job history", "kind": "observed reads", "state": "connected", "matched": 0.91},
        {"name": "Scheduled queries", "kind": "observed definitions", "state": "planned", "matched": None},
        {"name": "BI tool queries", "kind": "observed reads", "state": "planned", "matched": None},
    ],
    # #40-#42: graph-wide proposals are ready only when every consumer has a result.
    "proposals": [
        {"id": "prop-1", "kind": "shared_logic", "title": "Extract shared fx conversion into one model",
         "cost_rationale": "Removes two recomputations (estimate).",
         "consumers": [
             {"node": "marts.fct_orders", "label": "proven"},
             {"node": "marts.customer_ltv", "label": "proven"},
             {"node": "reporting.exec_dashboard", "label": "planner_checked"},
             {"node": "reporting.finance_extract", "label": "unknown"},
         ]},
        {"id": "prop-2", "kind": "upstream_filter", "title": "Push the paid-status filter into staging",
         "cost_rationale": "Every consumer filters on status = paid (upper bound).",
         "consumers": [
             {"node": "marts.fct_orders", "label": "proven"},
             {"node": "marts.daily_revenue", "label": "proven"},
             {"node": "marts.customer_ltv", "label": "proven"},
         ]},
    ],
}


def _preview(payload: dict) -> dict:
    return {"preview": True, **deepcopy(payload)}


def graph() -> dict:
    """Query graph payload (#23-#29)."""

    return _preview(_GRAPH)


def cost() -> dict:
    """Cost intelligence payload (#30-#35)."""

    return _preview(_COST)


def changes() -> dict:
    """Change report, CI, sources and proposals payload (#22, #36-#42)."""

    payload = _preview(_CHANGES)
    for proposal in payload["proposals"]:
        # #42: ready only when every affected consumer has a verification result.
        proposal["ready"] = all(item["label"] in ("proven", "unchanged") for item in proposal["consumers"])
    return payload


def impact(node, column, kind):
    """Sample blast radius of one column change, in the shape of ``ChangeImpact.to_json``.

    Mirrors what ``Pipeline.assess_change`` returns for a real project so the
    page renders the server's answer either way. Readers seen only in job
    history are listed under ``observed``, not ``affected``.
    """

    graph = _GRAPH
    lineage = [item for item in graph["column_lineage"] if item["transform"] != "observed read"]
    gaps = {gap["asset"] for gap in graph["gaps"]}
    traced = {item["node"] for item in lineage}
    down = {}
    observed_down = {}
    for edge in graph["edges"]:
        target = observed_down if edge["source"] == "observed" else down
        target.setdefault(edge["from"], []).append(edge)

    affected = {}
    queue = [(node, column, 1, None)]
    seen = set()
    while queue:
        table, col, depth, _ = queue.pop(0)
        if (table, col) in seen:
            continue
        seen.add((table, col))
        for item in lineage:
            if not any(src["node"] == table and src["column"] == col for src in item["sources"]):
                continue
            if kind == "change_expression":
                effect = "values_change"
            else:
                effect = "breaks" if depth == 1 else "indirect"
            old = affected.get(item["node"])
            cols = set(old["columns"]) if old else set()
            cols.add(col)
            if old is None or depth < old["depth"]:
                affected[item["node"]] = {"model": item["node"], "effect": effect, "via": "output_column",
                                          "depth": depth, "columns": sorted(cols)}
            queue.append((item["node"], item["column"], depth + 1, table))

    readers = {}
    frontier = [node]
    while frontier:
        following = []
        for name in frontier:
            for edge in down.get(name, ()):
                if edge["to"] not in readers and edge["to"] != node:
                    readers[edge["to"]] = edge
                    following.append(edge["to"])
        frontier = following
    unknown = [
        {"model": name, "reason": "unparsed_model" if name in gaps else "column_use_not_traced"}
        for name in sorted(readers)
        if name not in affected and (name not in traced or name in gaps)
    ]

    observed = {}
    frontier = [(node, 0)] + [(m["model"], m["depth"]) for m in affected.values()]
    while frontier:
        name, depth = frontier.pop(0)
        for edge in observed_down.get(name, ()):
            target = edge["to"]
            if target == node or target in affected or target in observed:
                continue
            observed[target] = {
                "model": target, "effect": "may_change" if kind == "change_expression" else "may_break",
                "via": name, "depth": depth + 1, "last_seen": edge["last_seen"],
                "confidence": edge["confidence"], "observed_count": edge["observed_count"], "source": "observed",
            }
            frontier.append((target, depth + 1))

    return {
        "kind": kind, "target": f"{node}.{column}", "target_known": True,
        "affected": sorted(affected.values(), key=lambda a: (a["depth"], a["model"])),
        "unknown": unknown,
        "observed": sorted(observed.values(), key=lambda o: (o["depth"], o["model"])),
        "observed_checked": True, "terminal": False, "complete": False,
        "incomplete_reasons": ["unknown_readers"] if unknown else [],
        "scope": None, "out_of_scope": 0,
        "safe_to_delete": "unknown",
        "safe_to_delete_note": "Not claimed. Readers outside the analysed pipeline are not visible.",
        "source": {"kind": "sample", "label": "Sample data"},
    }
