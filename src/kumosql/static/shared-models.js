"use strict";

/* Shared models: pick a CTE repeated across Dataform models, generate the patch that moves it into one
   shared model and updates every copy, and show each edited model's prover result (kumosql/shared_models.py). */

(() => {
  const $ = (id) => document.getElementById(id);
  const status = $("sm-status");
  const list = $("sm-list");
  const detail = $("sm-detail");
  const filter = $("sm-filter");
  let groups = [];
  let selected = null;

  const { LABELS, h, call, tag, short, checksTable, list: bullets, assumptions, diffCard } = window.KumoPatch;

  function say(message, error = false) {
    status.textContent = message;
    status.classList.toggle("error", error);
  }

  const models = (group) => [...new Set(group.sites.map((site) => short(site.model)))];

  function renderList() {
    list.replaceChildren();
    const text = filter.value.trim().toLowerCase();
    const shown = groups.filter((group) => !text || group.sites.some((site) => `${site.model} ${site.cte}`.toLowerCase().includes(text)));
    if (!shown.length) {
      list.append(h("div", { class: "sm-empty", text: groups.length ? "No match" : "No repeated CTEs" }));
      return;
    }
    for (const group of shown) {
      list.append(h("button", {
        type: "button", class: "sm-group", "aria-current": String(group === selected),
        "data-blocked": !group.extractable, onclick: () => select(group),
      },
      h("strong", { text: group.suggested_name }),
      h("span", { text: `${group.sites.length} copies · ${models(group).join(", ")}` })));
    }
  }

  function select(group) {
    selected = group;
    renderList();
    const name = h("input", { type: "text", value: group.suggested_name, "aria-label": "New model name", spellcheck: "false" });
    const kind = h("select", { "aria-label": "New model type" },
      h("option", { value: "view", text: "View" }), h("option", { value: "table", text: "Table" }));
    const button = h("button", { type: "button", class: "primary-button", text: "Generate patch", disabled: !group.extractable });
    const results = h("div");
    button.addEventListener("click", () => generate(group, name.value.trim(), kind.value, button, results));
    detail.replaceChildren(
      h("div", { class: "sm-card" },
        h("h2", { text: `${group.sites.length} copies` }),
        h("pre", { text: group.sql }),
        h("ul", { class: "sm-sites" }, ...group.sites.map((site) =>
          h("li", { text: `${site.cte} in ${site.path}${site.problem ? ` (${site.problem})` : ""}` }))),
        group.others.length ? h("ul", { class: "sm-sites" }, ...group.others.map((other) =>
          h("li", { text: `${other.location} in ${short(other.model)}` }))) : null,
        group.problem ? h("p", { class: "sm-problem", text: group.problem }) : null,
        h("div", { class: "sm-form" }, h("label", {}, "Name ", name), h("label", {}, "Type ", kind), button)),
      results);
  }

  function renderPatch(patch, box) {
    box.replaceChildren(
      h("div", { class: "sm-card" },
        h("div", { class: "sm-verdict" }, h("h2", { text: patch.new_file }), tag(patch.verdict)),
        bullets(patch.diagnostics),
        checksTable(patch.checks, { edited: "Edited", downstream: "Reads an edited model" }),
        assumptions(patch.assumptions)),
      diffCard(patch.diff, patch.changed_files.length, `${patch.name}.diff`, say));
  }

  async function generate(group, name, kind, button, box) {
    button.disabled = true;
    say("Proving…");
    try {
      const patch = await call("POST", "/api/shared-models/patch", { id: group.id, name: name || null, kind });
      renderPatch(patch, box);
      say(LABELS[patch.verdict] || patch.verdict);
    } catch (error) {
      say(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  async function load() {
    say("Loading…");
    try {
      const data = await call("GET", "/api/shared-models");
      groups = data.groups;
      renderList();
      if (!data.loaded) say("Load a project first");
      else if (!data.files_available) say("Reload the project to edit its files", true);
      else say(data.label);
      if (groups.length) select(groups.find((group) => group.extractable) || groups[0]);
    } catch (error) {
      say(error.message, true);
    }
  }

  filter.addEventListener("input", renderList);
  load();
})();
