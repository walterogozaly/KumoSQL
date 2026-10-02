"""Run KumoSQL's duplicate analyses on generated projects and score them (see ``dup_bench.py``)."""

from __future__ import annotations

from itertools import combinations
import time

import dup_bench as gen

DATABASES = 6


def _pairs(models) -> set[frozenset]:
    return {frozenset(p) for p in combinations(sorted(set(models)), 2)}


def _truth(project: gen.Project):
    """Model pairs by kind of truth: same query (A: textual rewrite, B: semantic rewrite) and same family."""

    textual, semantic, family = set(), set(), set()
    for sites in project.families.values():
        for a, b in combinations(sites, 2):
            pair = frozenset((a.model, b.model))
            if a.class_id == b.class_id or "decoy_upstream" not in (a.kind, b.kind):
                family.add(pair)
            if a.class_id == b.class_id:
                (semantic if "equiv_b" in (a.kind, b.kind) else textual).add(pair)
    return textual, semantic, family


def _pair_score(predicted: set, truth: set) -> dict:
    hit = predicted & truth
    return {
        "predicted": len(predicted),
        "truth": len(truth),
        "true": len(hit),
        "false": len(predicted) - len(hit),
        "precision": round(len(hit) / len(predicted), 4) if predicted else None,
        "recall": round(len(hit) / len(truth), 4) if truth else None,
    }


def _split(project: gen.Project, pairs: set, holdout: bool) -> set:
    flag = {s.model: s.holdout for s in project.sites}
    return {p for p in pairs if all(flag.get(m) is holdout for m in p)}


def _executed_agrees(databases, before: str, after: str, shared_sql: str) -> bool | None:
    """Run the refactor as it would ship: the shared SELECT as a table the edited model reads.

    ``False`` when the model returns different rows (or the shared SELECT cannot run on its own, which
    is a refactor that does not work); ``None`` when the checker could not run the model itself.
    """

    for con in databases:
        left = gen.rows_of(con, before)
        try:
            con.execute(f"CREATE OR REPLACE TEMP TABLE shared_model AS {gen.to_duckdb(shared_sql)}")
        except Exception:
            return False
        right = gen.rows_of(con, after)
        if left is None or right is None:
            return None
        if left != right:
            return False
    return True


def run_project(project: gen.Project) -> dict:
    from kumosql import load_compiled_graph
    from kumosql.repeated_work import find_repeated_work
    from kumosql.shared_logic import propose_shared_logic, refactor_sql

    timings: dict[str, float] = {}

    def timed(name, fn):
        start = time.perf_counter()
        value = fn()
        timings[name] = round(time.perf_counter() - start, 3)
        return value

    pipeline = timed("load", lambda: load_compiled_graph(project.graph))
    groups = timed("exact", lambda: pipeline.duplicate_selects())
    clusters = timed("near", lambda: pipeline.near_duplicate_selects())
    repeated = timed("repeated_work", lambda: find_repeated_work(pipeline))
    proposals = timed("refactors", lambda: propose_shared_logic(pipeline, verify=True, verify_limit=10**6))

    where = {s.model: (s.location, s.placement) for s in project.sites}

    def at_site(model, location, strict: bool = True) -> bool:
        """Only findings at the place the copy lives count; a wrapper SELECT around it is not a copy."""

        site = where.get(model)
        # A match on the whole query holds the copy too: its WITH clause or derived table is part of it.
        if site is None:
            return False
        return location in (site[0], "query") if not strict else location == site[0]

    exact_pred = set()
    for g in groups:
        exact_pred |= _pairs(o.model for o in g.occurrences if at_site(o.model, o.location, strict=False))
    near_pred = set()
    for c in clusters:
        near_pred |= _pairs(m for v in c.variants for m, loc in v.occurrences if at_site(m, loc))
    repeat_pred = set()
    for item in repeated:
        if item.kind in ("identical_logic", "similar_logic"):
            repeat_pred |= _pairs(r.node for r in item.repeats if at_site(r.node, r.where))
    databases = [gen.make_database(project.tables, 100 + i) for i in range(DATABASES)]
    labels = gen.check_labels(project, databases)
    skip = set(labels["decoy_agree"])  # decoys the data could not tell from the original: unscored
    textual, semantic, family = (
        {p for p in part if not p & skip} for part in _truth(project)
    )
    same_class = textual | semantic
    exact_pred, near_pred, repeat_pred = ({p for p in pred if not p & skip} for pred in (exact_pred, near_pred, repeat_pred))

    result: dict = {"label_check": {"copies_compared": labels["checked"], "equal_but_different_rows": len(labels["equal_disagree"]),
                                    "decoys_not_told_apart": len(labels["decoy_agree"])},
                    "models": project.models, "families": len(project.families), "sites": len(project.sites), "seconds": timings}
    for name, flag in (("dev", False), ("held_out", True)):
        sel = lambda s: _split(project, s, flag)  # noqa: E731
        result[name] = {
            # a pair predicted to be the same query that is not (decoys and near variants grouped as copies)
            "exact_vs_semantic": _pair_score(sel(exact_pred), sel(same_class)),
            "exact_recall_textual": round(len(sel(exact_pred) & sel(textual)) / len(sel(textual)), 4) if sel(textual) else None,
            "exact_recall_semantic": round(len(sel(exact_pred) & sel(semantic)) / max(1, len(sel(semantic))), 4) if sel(semantic) else None,
            "exact_wrong": len(sel(exact_pred) - sel(same_class)),
            "similar": _pair_score(sel(exact_pred | near_pred), sel(family)),
            "near_only": _pair_score(sel(near_pred - exact_pred), sel(family - textual)),
            "repeated_work": _pair_score(sel(repeat_pred), sel(family)),
        }

    # refactors: judged by proof (the product's own readiness) and by execution on random databases
    kind_of = {s.model: s for s in project.sites}
    summary = {"proposals": len(proposals), "ready": 0, "proof_ready_unsafe": 0, "executed_agree": 0,
               "executed_disagree": 0, "executed_unknown": 0, "not_applicable": 0, "ready_but_disagree": []}
    covered: set[int] = set()
    for proposal in proposals:
        verdicts = []
        for model_key in {m for m, _ in proposal.sites}:
            model = pipeline.models[model_key]
            after = model.sql
            for site in [s for s in proposal.sites if s[0] == model_key]:
                after = refactor_sql(proposal, after, site, shared_model="shared_model") if after else None
            verdicts.append(None if after is None else _executed_agrees(databases, model.sql, after, proposal.shared_sql))
        if any(v is False for v in verdicts):
            summary["executed_disagree"] += 1
        elif any(v is None for v in verdicts):
            summary["executed_unknown"] += 1
        else:
            summary["executed_agree"] += 1
        if proposal.ready:
            summary["ready"] += 1
            for model_key in {m for m, _ in proposal.sites}:
                site = kind_of.get(model_key)
                if site is not None:
                    covered.add(site.family)
            if any(v is False for v in verdicts):
                summary["proof_ready_unsafe"] += 1
                summary["ready_but_disagree"].append(proposal.id)
    # usefulness: families with at least two truly equivalent copies that a ready proposal covers
    eligible = {f for f, sites in project.families.items() if len({s.model for s in sites if s.class_id == sites[0].class_id}) >= 2}
    ready_models = {m for p in proposals if p.ready for m, _ in p.sites}
    useful = {f for f in eligible if sum(1 for s in project.families[f] if s.class_id == project.families[f][0].class_id and s.model in ready_models) >= 2}
    summary["families_eligible"] = len(eligible)
    summary["families_with_verified_refactor"] = len(useful)
    result["refactors"] = summary
    return result


def run_sizes(sizes: list[int], seed: int) -> dict:
    return {"seed": seed, "runs": [run_project(gen.generate(n, seed)) for n in sizes]}


def format_report(report: dict) -> str:
    lines = []
    for run in report["runs"]:
        lines.append(f"== {run['models']} models, {run['families']} families, {run['sites']} copies  ({run['seconds']})")
        for split in ("dev", "held_out"):
            r = run[split]
            lines.append(
                f"  [{split}] exact: wrong {r['exact_wrong']}, textual recall {r['exact_recall_textual']}, "
                f"semantic recall {r['exact_recall_semantic']}; similar P/R {r['similar']['precision']}/{r['similar']['recall']}; "
                f"near-only P/R {r['near_only']['precision']}/{r['near_only']['recall']}; "
                f"repeated-work P/R {r['repeated_work']['precision']}/{r['repeated_work']['recall']}"
            )
        f = run["refactors"]
        lines.append(
            f"  refactors: {f['proposals']} proposed, {f['ready']} ready (proof), {f['proof_ready_unsafe']} unsafe-ready; "
            f"executed agree/disagree/unknown {f['executed_agree']}/{f['executed_disagree']}/{f['executed_unknown']}; "
            f"families with a verified refactor {f['families_with_verified_refactor']}/{f['families_eligible']}"
        )
    return "\n".join(lines)
