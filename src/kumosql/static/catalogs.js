"use strict";

/* Catalogs: saved rules for what a team owns (see kumosql/catalogs.py). They use the same rule
   builder as scopes and tag rules. The active catalogs are kept on the server. */

(() => {
  const h = (tag, attrs = {}, ...children) => {
    const element = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === "class") element.className = value;
      else if (key === "text") element.textContent = value;
      else if (key.startsWith("on")) element.addEventListener(key.slice(2), value);
      else if (value !== false && value != null) element.setAttribute(key, value === true ? "" : value);
    }
    element.append(...children.filter(Boolean));
    return element;
  };
  const linkButton = (label, action) => h("button", { type: "button", class: "link-button", "data-action": action, text: label });

  async function request(method, url, payload) {
    const response = await fetch(url, {
      method, headers: { "Content-Type": "application/json" }, body: payload === undefined ? undefined : JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Request failed");
    return data;
  }

  const FIELDS = [
    ["project", "BigQuery project"], ["dataset", "dataset"], ["name", "table, view, function or procedure name"], ["full_name", "project.dataset.name"],
    ["type", "TABLE, VIEW, UDF, PROCEDURE…"], ["kind", "Dataform model kind"], ["source", "bigquery or dataform"], ["path", "Dataform file path"], ["tag", "tags"],
  ];

  function renderManager(body, { onStatus } = {}) {
    const kit = window.KumoScopes?.builder;
    if (!kit) {
      body.append(h("p", { class: "sp-lede", text: "Catalogs could not be loaded on this page." }));
      return;
    }
    const datalistId = "catalog-field-options";
    let info = { catalogs: [], active: [] };
    let editing = null;
    let tree = kit.newGroup();
    const list = h("ul", { class: "scope-list" });
    const name = h("input", { type: "text", maxlength: "80", placeholder: "My team", required: "", "aria-label": "Catalog name" });
    const builder = h("div", { class: "rule-builder" });
    const preview = h("p", { class: "rule-preview", "aria-live": "polite" });
    const matches = h("p", { class: "sp-row-hint", "aria-live": "polite" });
    const datalist = h("datalist", { id: datalistId }, ...FIELDS.map(([field, label]) => Object.assign(new Option(field), { label })));
    const saveButton = h("button", { type: "submit", class: "toolbar-button", text: "Save catalog" });
    const previewButton = h("button", { type: "button", class: "toolbar-button", text: "Preview" });
    const cancel = h("button", { type: "button", class: "link-button", text: "Cancel", hidden: "" });
    const draw = () => kit.renderBuilder(builder, tree, preview, datalistId, () => (window.KumoScopes.list() || []).map((scope) => scope.name));
    draw();

    const saved = () => info.catalogs.filter((item) => !item.builtin).map(({ name: n, rule }) => ({ name: n, rule }));
    const renderList = () => {
      list.replaceChildren(...info.catalogs.map((item) => {
        const summary = item.error || `${item.matched} objects · ${item.description}`;
        const row = h("li", { class: `scope-item${item.active ? " is-active" : ""}`, "data-name": item.name },
          h("div", {}, h("strong", { text: item.name }), h("small", { text: summary, title: summary, class: item.error ? "is-error" : "" })),
          linkButton(item.active ? "Active ✓" : "Use", "use"));
        if (!item.builtin) row.append(linkButton("Edit", "edit"), linkButton("Delete", "delete"));
        return row;
      }));
    };
    const load = async () => {
      info = await request("GET", "/api/catalogs");
      renderList();
    };
    const reset = () => {
      editing = null;
      name.value = "";
      tree = kit.newGroup();
      matches.textContent = "";
      cancel.hidden = true;
      saveButton.textContent = "Save catalog";
      draw();
    };
    const changed = () => document.dispatchEvent(new CustomEvent("kumosql:catalogs-changed"));
    const persist = async (next) => {
      await request("PUT", "/api/settings/catalogs", next);
      await load();
      changed();
    };

    list.addEventListener("click", async (event) => {
      const button = event.target.closest("button[data-action]");
      const item = info.catalogs.find((entry) => entry.name === button?.closest(".scope-item")?.dataset.name);
      if (!item) return;
      try {
        if (button.dataset.action === "use") {
          const names = new Set(info.active);
          if (names.has(item.name)) names.delete(item.name); else names.add(item.name);
          await request("POST", "/api/catalogs/active", { active: info.catalogs.map((c) => c.name).filter((n) => names.has(n)) });
          await load();
          changed();
          onStatus?.(`Active: ${info.active.join(", ")}`);
        } else if (button.dataset.action === "edit") {
          editing = item.name;
          name.value = item.name;
          let next = kit.ruleToTree(item.rule);
          if (next.type !== "group") next = { type: "group", mode: "all", negate: false, children: [next] };
          tree = next;
          cancel.hidden = false;
          saveButton.textContent = "Update catalog";
          draw();
          name.focus();
        } else {
          await persist(saved().filter((entry) => entry.name !== item.name));
          if (editing === item.name) reset();
          onStatus?.(`Deleted “${item.name}”`);
        }
      } catch (error) {
        onStatus?.(error.message, true);
      }
    });

    const current = () => ({ name: name.value.trim(), rule: kit.treeToRule(tree) });
    previewButton.addEventListener("click", async () => {
      try {
        const result = await request("POST", "/api/catalogs/preview", current());
        matches.textContent = `Owns ${result.matched} of ${result.of} known objects${result.examples.length ? `, for example ${result.examples.slice(0, 5).join(", ")}` : ""}.`;
      } catch (error) {
        matches.textContent = error.message;
      }
    });
    const form = h("form", { class: "sp-scope-form", autocomplete: "off" },
      h("label", { class: "sp-scope-label" }, h("span", { text: "Name" }), name),
      h("div", { class: "rule-field" }, h("span", { text: "Owns objects matching" }), builder, datalist, preview, matches),
      h("div", { class: "settings-actions" }, saveButton, previewButton, cancel));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const item = current();
        const next = saved();
        const index = next.findIndex((entry) => entry.name.toLowerCase() === (editing ?? item.name).toLowerCase());
        if (index < 0) next.push(item); else next[index] = item;
        await persist(next);
        reset();
        onStatus?.(`Saved “${item.name}”`);
      } catch (error) {
        onStatus?.(error.message, true);
      }
    });
    cancel.addEventListener("click", reset);

    body.append(
      h("h3", { class: "sp-heading", text: "Catalogs" }),
      h("div", { class: "sp-group" }, list),
      h("h4", { class: "sp-subheading", text: "New catalog" }),
      form);
    window.KumoScopes.load().then(draw).catch(() => {});
    load().catch((error) => onStatus?.(error.message, true));
  }

  window.KumoCatalogs = { renderManager };
})();
