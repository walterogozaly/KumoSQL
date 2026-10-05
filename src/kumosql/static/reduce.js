"use strict";

/* Reduce: tick the outputs to keep, run the reduction of the loaded Dataform project in the background and
   show the proved patch (kumosql/reduction_app.py, kumosql/project_reduction.py). */

(() => {
  const $ = (id) => document.getElementById(id);
  const status = $("rd-status");
  const list = $("rd-list");
  const filter = $("rd-filter");
  const results = $("rd-results");
  const run = $("rd-run");
  const count = $("rd-count");
  const { LABELS, h, call, tag, short, checksTable, list: bullets, assumptions, diffCard } = window.KumoPatch;
  const ROLES = { kept: "Kept output", checked: "Table with assertions" };
  const POLL_MS = 1000;
  let actions = [];
  let ready = false;
  let busy = false;
  const kept = new Set();

  function say(message, error = false) {
    status.textContent = message;
    status.classList.toggle("error", error);
  }

  const sync = () => {
    count.textContent = kept.size ? `${kept.size} kept` : "";
    run.disabled = busy || !ready || !kept.size;
  };

  function renderList() {
    list.replaceChildren();
    const text = filter.value.trim().toLowerCase();
    const shown = visible(text);
    if (!shown.length) {
      list.append(h("div", { class: "rd-empty", text: actions.length ? "No match" : "No actions" }));
      return;
    }
    for (const action of shown) {
      const box = h("input", { type: "checkbox", "aria-label": `Keep ${action.name}`, checked: kept.has(action.key) });
      const row = h("label", { class: "rd-row", "data-kept": String(kept.has(action.key)), title: action.path || action.key },
        box,
        h("span", { class: "rd-name" }, action.name, action.schema ? h("small", { text: action.schema }) : null),
        h("span", { class: "rd-kind", text: action.kind }));
      box.addEventListener("change", () => {
        if (box.checked) kept.add(action.key); else kept.delete(action.key);
        row.dataset.kept = String(box.checked);
        sync();
      });
      list.append(row);
    }
  }

  const visible = (text) => actions.filter((action) => !text
    || `${action.key} ${action.path} ${action.kind} ${action.tags.join(" ")}`.toLowerCase().includes(text));

  const number = (value) => (Number.isInteger(value) ? String(value) : value.toFixed(2));
  const stat = (label, before, after) => h("div", { class: "rd-stat" },
    h("span", { text: label }), h("strong", { text: `${number(before)} → ${number(after)}` }));

  function group(title, rows) {
    if (!rows.length) return null;
    return h("details", { class: "rd-group", open: true },
      h("summary", { text: `${title} (${rows.length})` }),
      h("ul", { class: "rd-items" }, ...rows.map(([name, why]) => h("li", {},
        h("code", { text: name }), h("span", { class: "rd-why", text: why || "" })))));
  }

  function renderResult(data) {
    const rows = {
      removed: data.removed.map((item) => [short(item.model), `${item.kind}: ${item.why}`]),
      assertions: data.dropped_assertions.map((item) => [short(item.model), item.why]),
      changed: data.changed.map((key) => [short(key), ""]),
      added: data.added.map((item) => [short(item.model), item.path]),
      fixed: data.fixed.map((item) => [short(item.model), item.why]),
      rejected: data.rejected_moves.map((item) => [item.move, item.why]),
    };
    results.replaceChildren(
      h("div", { class: "sm-card" },
        h("div", { class: "sm-verdict" }, h("h2", { text: "Result" }), tag(data.verdict)),
        h("div", { class: "rd-stats" },
          stat("Actions", data.actions.before, data.actions.after),
          stat("Complexity", data.score.before, data.score.after)),
        bullets(data.diagnostics),
        checksTable(data.checks, ROLES),
        assumptions(data.assumptions),
        group("Removed", rows.removed),
        group("Assertions dropped", rows.assertions),
        group("Changed", rows.changed),
        group("Added", rows.added),
        group("Kept as written", rows.fixed),
        group("Rejected moves", rows.rejected)),
      data.diff ? diffCard(data.diff, data.changed_files.length, "reduced-project.diff", say) : null);
  }

  async function follow(first) {
    let job = first;
    busy = true;
    sync();
    try {
      while (job.state === "running") {
        say(`Running… ${job.elapsed ?? 0}s${job.line ? ` · ${job.line}` : ""}`);
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
        job = await call("GET", "/api/reduce/status");
      }
      if (job.state === "done") {
        renderResult(job.result);
        say(LABELS[job.result.verdict] || job.result.verdict);
      } else if (job.state === "error") {
        say(job.error, true);
      }
    } catch (error) {
      say(error.message, true);
    } finally {
      busy = false;
      sync();
    }
  }

  async function start() {
    busy = true;
    sync();
    say("Starting…");
    try {
      const job = await call("POST", "/api/reduce/run", {
        keep: [...kept],
        drop_only: $("rd-drop-only").checked,
        keep_assertions: $("rd-keep-assertions").checked,
        strict: $("rd-strict").checked,
        table_type: $("rd-table-type").value,
      });
      await follow(job);
    } catch (error) {
      busy = false;
      sync();
      say(error.message, true);
    }
  }

  async function load() {
    say("Loading…");
    try {
      const data = await call("GET", "/api/reduce");
      actions = data.actions;
      renderList();
      if (!data.loaded) say("Load a project first");
      else if (!data.files_available) say("Reload the project to edit its files", true);
      else say(data.label);
      ready = data.loaded && data.files_available;
      sync();
      const job = await call("GET", "/api/reduce/status");
      const known = new Set(actions.map((action) => action.key));
      if (ready && job.state !== "idle" && (job.keep || []).every((key) => known.has(key))) {
        for (const key of job.keep || []) kept.add(key);
        renderList();
        await follow(job);
      }
    } catch (error) {
      say(error.message, true);
    }
  }

  filter.addEventListener("input", renderList);
  $("rd-all").addEventListener("click", () => {
    for (const action of visible(filter.value.trim().toLowerCase())) kept.add(action.key);
    renderList();
    sync();
  });
  $("rd-none").addEventListener("click", () => {
    kept.clear();
    renderList();
    sync();
  });
  run.addEventListener("click", start);
  load();
})();
