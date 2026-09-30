"use strict";

/* Evidence vocabulary shared by the workspace and the roadmap views (#15, #16).
   Every changed result carries exactly one label; checks list what supports it.
   Proof and planner checks are separate kinds and never merge into one line. */

(() => {
  const LABELS = {
    proven: {
      title: "Proven",
      tone: "ok",
      meaning: "A proof shows the new SQL returns the same results as the original.",
    },
    planner_checked: {
      title: "Planner checked",
      tone: "info",
      meaning: "BigQuery planned the new SQL and its output schema matches. This is not proof that results are equal.",
    },
    unproven: {
      title: "Unproven",
      tone: "warn",
      meaning: "No check could show the results are equal. Review the diff before using it.",
    },
    failed: {
      title: "Failed",
      tone: "bad",
      meaning: "The rewrite could not run safely, so no output is offered.",
    },
    unchanged: {
      title: "Unchanged",
      tone: "neutral",
      meaning: "The rule made no change to this SQL.",
    },
    unknown: {
      title: "Unknown",
      tone: "idle",
      meaning: "Evidence is incomplete, so KumoSQL cannot say whether this consumer is affected safely.",
    },
  };

  const CHECKS = {
    structural_proof: { title: "Structural proof", group: "proof", issue: 19 },
    smt: { title: "SMT proof", group: "proof", issue: 20 },
    synthetic_results: { title: "Synthetic results", group: "results", issue: 21 },
    planner: { title: "Planner (dry run)", group: "planner", issue: 16 },
    idempotence: { title: "Idempotence", group: "stability", issue: 18 },
    source_spans: { title: "Unchanged text kept", group: "stability", issue: 17 },
  };

  const OUTCOMES = {
    passed: { title: "passed", tone: "ok" },
    failed: { title: "failed", tone: "bad" },
    inconclusive: { title: "inconclusive", tone: "warn" },
    unsupported: { title: "unsupported", tone: "warn" },
    not_run: { title: "not run", tone: "idle" },
  };

  /** Accept "planner checked", "planner-checked" or "planner_checked". */
  function normalize(label) {
    const key = String(label || "").trim().toLowerCase().replace(/[\s-]+/g, "_");
    return LABELS[key] ? key : "unknown";
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /** A pill naming one evidence label, with its meaning as the tooltip. */
  function pill(label) {
    const key = normalize(label);
    const meta = LABELS[key];
    const node = el("span", `ev-pill ev-${meta.tone}`, meta.title);
    node.title = meta.meaning;
    node.dataset.label = key;
    return node;
  }

  /** A small chip for one supporting check. */
  function checkChip(check) {
    const meta = CHECKS[check.kind] || { title: check.kind };
    const outcome = OUTCOMES[check.outcome] || { title: check.outcome || "", tone: "idle" };
    const node = el("span", `ev-check ev-${outcome.tone}`);
    node.append(el("span", "ev-check-name", meta.title), el("span", "ev-check-outcome", outcome.title));
    node.title = check.detail ? `${meta.title}: ${check.detail}` : meta.title;
    return node;
  }

  /** Definition list of the five result labels, for legends. */
  function legend(keys = ["proven", "planner_checked", "unproven", "failed", "unchanged"]) {
    const list = el("dl", "ev-legend");
    for (const key of keys) {
      const row = el("div", "ev-legend-row");
      const term = el("dt");
      term.append(pill(key));
      row.append(term, el("dd", "", LABELS[key].meaning));
      list.append(row);
    }
    return list;
  }

  window.KumoEvidence = { LABELS, CHECKS, OUTCOMES, normalize, pill, checkChip, legend };
})();
