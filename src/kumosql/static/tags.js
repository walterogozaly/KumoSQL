"use strict";

/* Tags (see kumosql/tags.py): labels on anything inside a dataset. Shared by the
   BigQuery explorer, the graph and Settings. Manual tags are added by hand; rule
   tags come from the "Tag rules" section and are recomputed by the server each
   time, so they cannot be removed here. Tags stay in KumoSQL: BigQuery is never changed. */

(() => {
  let snapshot = { objects: {}, aliases: {}, tags: [], rules: [], known_objects: 0 };
  let loaded = false;
  const listeners = new Set();

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

  async function request(method, url, payload) {
    const response = await fetch(url, {
      method, headers: { "Content-Type": "application/json" }, body: payload === undefined ? undefined : JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Tags request failed");
    return data;
  }

  function adopt(next) {
    snapshot = next;
    loaded = true;
    for (const listener of listeners) listener();
    return snapshot;
  }

  /** Read the tags now. Rule tags are recomputed from whatever is loaded at this moment. */
  async function load() {
    try {
      return adopt(await request("GET", "/api/tags"));
    } catch {
      return snapshot; // tags are optional: pages work without them
    }
  }

  const normalize = (key) => String(key).trim().toLowerCase();
  function entry(key) {
    const name = normalize(key);
    return snapshot.objects[name] || snapshot.objects[snapshot.aliases[name]] || { manual: [], rules: [] };
  }
  const tagsFor = (key) => entry(key);
  const hasTag = (key, tag) => {
    const slot = entry(key);
    return [...slot.manual, ...slot.rules].some((name) => name.toLowerCase() === String(tag).toLowerCase());
  };

  async function change(keys, add = [], remove = []) {
    return adopt(await request("PUT", "/api/tags", { keys, add, remove }));
  }

  /** Chips for one object: solid for tags added by hand, dashed for tags from a rule. */
  function chips(key, { max = 0 } = {}) {
    const slot = entry(key);
    const items = [
      ...slot.manual.map((name) => ({ name, rule: false })),
      ...slot.rules.filter((name) => !slot.manual.some((m) => m.toLowerCase() === name.toLowerCase())).map((name) => ({ name, rule: true })),
    ];
    const shown = max ? items.slice(0, max) : items;
    const wrap = h("span", { class: "tag-chips" }, ...shown.map((item) =>
      h("span", { class: `tag-chip${item.rule ? " is-rule" : ""}`, title: item.rule ? "From a tag rule" : "Added by hand", text: item.name })));
    if (items.length > shown.length) wrap.append(h("span", { class: "tag-chip is-more", title: items.slice(max).map((item) => item.name).join(", "), text: `+${items.length - shown.length}` }));
    return wrap;
  }

  /** Add and remove tags on the objects ``getKeys()`` returns (one, or several selected at once). */
  function editor(getKeys, { onStatus } = {}) {
    const datalistId = `tag-names-${Math.random().toString(36).slice(2, 8)}`;
    const list = h("span", { class: "tag-chips" });
    const input = h("input", { type: "text", class: "tag-input", list: datalistId, placeholder: "Add a tag", maxlength: "60", "aria-label": "Tag name" });
    const names = h("datalist", { id: datalistId });
    const add = h("button", { type: "submit", class: "toolbar-button", text: "Add" });
    const form = h("form", { class: "tag-add", autocomplete: "off" }, input, add);
    const root = h("div", { class: "tag-editor" }, list, form, names);

    const run = async (task, done) => {
      try {
        await task();
        onStatus?.(done);
      } catch (error) {
        onStatus?.(error.message, true);
      }
    };

    function draw() {
      const keys = getKeys();
      names.replaceChildren(...snapshot.tags.map((item) => new Option(item.tag)));
      const manual = new Map();
      const rules = new Set();
      for (const key of keys) {
        const slot = entry(key);
        for (const name of slot.manual) manual.set(name.toLowerCase(), { name, count: (manual.get(name.toLowerCase())?.count || 0) + 1 });
        for (const name of slot.rules) rules.add(name);
      }
      list.replaceChildren(
        ...[...manual.values()].map(({ name, count }) => h("span", { class: "tag-chip", title: "Added by hand" },
          name, count < keys.length ? h("small", { text: ` ${count}/${keys.length}` }) : null,
          h("button", {
            type: "button", class: "tag-remove", "aria-label": `Remove tag ${name}`, title: keys.length > 1 ? "Remove from every selected object" : "Remove",
            onclick: () => run(() => change(keys, [], [name]), `Removed “${name}”`), text: "×",
          }))),
        ...[...rules].filter((name) => !manual.has(name.toLowerCase())).map((name) =>
          h("span", { class: "tag-chip is-rule", title: "From a tag rule. Change it in Settings, Tag rules.", text: name })));
      if (!list.children.length) list.append(h("span", { class: "muted small", text: keys.length > 1 ? "No tags on these yet." : "No tags yet." }));
      add.disabled = !keys.length;
    }

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const name = input.value.trim();
      if (!name) return;
      run(async () => { await change(getKeys(), [name], []); input.value = ""; }, `Tagged “${name}”`);
    });
    let attached = false;
    const redraw = () => {
      if (root.isConnected) { attached = true; draw(); } else if (attached) listeners.delete(redraw); // page replaced it
    };
    listeners.add(redraw);
    draw();
    root.redraw = draw;
    return root;
  }

  /* ---------- Tag rules (the Settings section) ---------- */

  const FIELD_HELP = [
    ["project", "BigQuery project"], ["dataset", "dataset (also called schema)"], ["name", "table, view, function or procedure name"], ["full_name", "project.dataset.name"],
    ["type", "TABLE, VIEW, MATERIALIZED_VIEW, EXTERNAL, UDF, TABLE_FUNCTION, PROCEDURE…"], ["kind", "Dataform model kind"],
    ["source", "bigquery or dataform"], ["path", "Dataform file path"],
  ];
  const linkButton = (label, action) => h("button", { type: "button", class: "link-button", "data-action": action, text: label });

  function renderRules(body, { onStatus } = {}) {
    const kit = window.KumoScopes?.builder;
    if (!kit) {
      body.append(h("p", { class: "sp-lede", text: "Tag rules could not be loaded on this page." }));
      return;
    }
    const datalistId = "tag-rule-field-options";
    let rules = [];
    let editing = null;
    let tree = kit.newGroup();
    const list = h("ul", { class: "scope-list" });
    const empty = h("p", { class: "sp-row-hint", text: "No tag rules yet. Create one below." });
    const tag = h("input", { type: "text", maxlength: "60", placeholder: "Retired", required: "", "aria-label": "Tag" });
    const builder = h("div", { class: "rule-builder" });
    const preview = h("p", { class: "rule-preview", "aria-live": "polite" });
    const matches = h("p", { class: "sp-row-hint", "aria-live": "polite" });
    const datalist = h("datalist", { id: datalistId }, ...FIELD_HELP.map(([name, label]) => Object.assign(new Option(name), { label })));
    const saveButton = h("button", { type: "submit", class: "toolbar-button", text: "Save rule" });
    const previewButton = h("button", { type: "button", class: "toolbar-button", text: "Preview matches" });
    const cancel = h("button", { type: "button", class: "link-button", text: "Cancel", hidden: "" });
    const draw = () => kit.renderBuilder(builder, tree, preview, datalistId, () => (window.KumoScopes.list() || []).map((scope) => scope.name));
    draw();

    const status = () => snapshot.rules || [];
    const renderList = () => {
      list.replaceChildren();
      empty.hidden = rules.length > 0;
      rules.forEach((item, index) => {
        const row = status()[index];
        const summary = row?.error ? row.error : `${row ? `${row.matched} matched · ` : ""}${window.KumoScopes.describeRule(item.rule)}`;
        list.append(h("li", { class: "scope-item", "data-index": String(index) },
          h("span", { class: "tag-chip is-rule", text: item.tag }),
          h("div", {}, h("strong", { text: "Objects matching" }), h("small", { text: summary, title: summary, class: row?.error ? "is-error" : "" })),
          linkButton("Edit", "edit"), linkButton("Delete", "delete")));
      });
    };
    const reset = () => {
      editing = null;
      tag.value = "";
      tree = kit.newGroup();
      matches.textContent = "";
      cancel.hidden = true;
      saveButton.textContent = "Save rule";
      draw();
    };
    const persist = async (next) => {
      rules = await request("PUT", "/api/settings/tag_rules", next);
      await load();
      renderList();
    };

    list.addEventListener("click", async (event) => {
      const button = event.target.closest("button[data-action]");
      const index = Number(button?.closest(".scope-item")?.dataset.index);
      if (!button || Number.isNaN(index)) return;
      try {
        if (button.dataset.action === "edit") {
          editing = index;
          tag.value = rules[index].tag;
          let next = kit.ruleToTree(rules[index].rule);
          if (next.type !== "group") next = { type: "group", mode: "all", negate: false, children: [next] };
          tree = next;
          cancel.hidden = false;
          saveButton.textContent = "Update rule";
          draw();
          tag.focus();
        } else {
          await persist(rules.filter((_, i) => i !== index));
          if (editing === index) reset();
          onStatus?.("Deleted the rule");
        }
      } catch (error) {
        onStatus?.(error.message, true);
      }
    });

    const currentRule = () => ({ tag: tag.value.trim(), rule: kit.treeToRule(tree) });
    previewButton.addEventListener("click", async () => {
      try {
        const result = await request("POST", "/api/tag-rules/preview", currentRule());
        matches.textContent = `Would tag ${result.matched} of ${result.of} known objects${result.examples.length ? `, for example ${result.examples.slice(0, 5).join(", ")}` : ""}.`;
      } catch (error) {
        matches.textContent = error.message;
      }
    });
    const form = h("form", { class: "sp-scope-form", autocomplete: "off" },
      h("label", { class: "sp-scope-label" }, h("span", { text: "Tag" }), tag),
      h("div", { class: "rule-field" }, h("span", { text: "Objects matching" }), builder, datalist, preview,
        h("p", { class: "sp-row-hint", text: "Fields: " + FIELD_HELP.map(([name]) => name).join(", ") + ". Use “is one of” for a pasted list, a pattern such as RETIRED_* for names, or include a saved scope. Use “is returned by SQL query” to tag what a query lists (for example on full_name). “tag” is not available here: it is what a rule produces." }),
        matches),
      h("div", { class: "settings-actions" }, saveButton, previewButton, cancel));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const item = currentRule();
        const next = [...rules];
        if (editing === null) next.push(item); else next[editing] = item;
        await persist(next);
        reset();
        onStatus?.(`Saved the rule for “${item.tag}”`);
      } catch (error) {
        onStatus?.(error.message, true);
      }
    });
    cancel.addEventListener("click", reset);

    const inUse = h("p", { class: "tag-chips tags-in-use" });
    const drawInUse = () => {
      inUse.replaceChildren(...snapshot.tags.map((item) => h("span", { class: "tag-chip", text: `${item.tag} · ${item.count}` })));
      inUse.hidden = !snapshot.tags.length;
    };
    body.append(
      h("h3", { class: "sp-heading", text: "Tag rules" }),
      h("p", { class: "sp-lede", text: "A tag rule tags every matching table, view, function, procedure and Dataform model at once, for example everything in the RETIRED dataset gets “Retired”. Rules are checked again whenever the BigQuery catalog or a repository reloads. Tags are kept in KumoSQL only; nothing is written to BigQuery." }),
      h("div", { class: "sp-group" }, list, empty),
      h("h4", { class: "sp-subheading", text: "New rule" }),
      form,
      h("h4", { class: "sp-subheading", text: "Tags in use" }),
      inUse);
    renderList();
    Promise.all([window.KumoScopes.load(), request("GET", "/api/settings"), load()]).then(([, settings]) => {
      rules = settings.tag_rules || [];
      renderList();
      drawInUse();
    }).catch((error) => onStatus?.(error.message, true));
    let shown = false;
    const refresh = () => {
      if (body.isConnected) { shown = true; renderList(); drawInUse(); } else if (shown) listeners.delete(refresh);
    };
    listeners.add(refresh);
    drawInUse();
  }

  window.KumoTags = {
    load, change, tagsFor, hasTag, chips, editor, renderRules,
    names: () => snapshot.tags.map((item) => item.tag),
    isLoaded: () => loaded,
    onChange: (listener) => listeners.add(listener),
  };
})();
