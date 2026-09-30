"use strict";

/* Scopes: saved rules that decide what is "in" (see kumosql/scopes.py). Shared
   by every page. It provides the rule builder and scope manager used in the
   Settings panel, and the active-scope picker on the pages scopes apply to.
   The active scope is remembered in the browser and sent as ?scope=NAME. */

(() => {
  const ACTIVE_KEY = "kumosql-active-scope";
  const NO_VALUE_OPS = new Set(["is_null", "not_null"]);
  const LIST_OPS = new Set(["in", "not_in"]);
  const FALLBACK_OPERATORS = [
    ["in", "is one of"], ["not_in", "is not one of"], ["eq", "equals"], ["ne", "does not equal"],
    ["prefix", "starts with"], ["suffix", "ends with"], ["contains", "contains"], ["glob", "matches pattern"],
    ["regex", "matches regex"], ["gt", "is greater than"], ["gte", "is at least"], ["lt", "is less than"],
    ["lte", "is at most"], ["is_null", "is empty"], ["not_null", "is not empty"],
  ].map(([op, label]) => ({ op, label }));

  let scopes = [];
  let scopesLoaded = false;
  let fieldInfo = { fields: [], operators: [], loaded: null };
  const listeners = new Set();
  const listListeners = new Set();

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

  /* ---------- Storage ---------- */

  function getActive() {
    try {
      const name = localStorage.getItem(ACTIVE_KEY) || "";
      // Until the saved scopes are known, trust the stored name; the server rejects an unknown one.
      return !scopesLoaded || scopes.some((scope) => scope.name === name) ? name : "";
    } catch {
      return "";
    }
  }

  function setActive(name) {
    try {
      if (name) localStorage.setItem(ACTIVE_KEY, name); else localStorage.removeItem(ACTIVE_KEY);
    } catch {
      /* browser storage unavailable: the choice lasts until the page reloads */
    }
    for (const listener of listeners) listener(name || "");
  }

  async function load() {
    try {
      const response = await fetch("/api/settings");
      if (response.ok) {
        scopes = (await response.json()).scopes || [];
        scopesLoaded = true;
      }
    } catch {
      /* keep what we have */
    }
    return scopes;
  }

  async function loadFields() {
    try {
      const response = await fetch("/api/scope-fields");
      if (response.ok) fieldInfo = await response.json();
    } catch {
      /* suggestions are optional */
    }
    return fieldInfo;
  }

  async function save(next) {
    const response = await fetch("/api/settings/scopes", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(next),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not save scopes");
    scopes = data;
    scopesLoaded = true;
    for (const listener of listListeners) listener();
    let stored = "";
    try { stored = localStorage.getItem(ACTIVE_KEY) || ""; } catch { /* unavailable */ }
    if (stored && !scopes.some((scope) => scope.name === stored)) setActive("");
    return scopes;
  }

  /* ---------- Rule JSON <-> builder tree ---------- */

  const operators = () => (fieldInfo.operators.length ? fieldInfo.operators : FALLBACK_OPERATORS);
  const operatorLabel = (op) => operators().find((item) => item.op === op)?.label || op;
  const newCondition = () => ({ type: "cond", field: "", op: "in", value: "" });
  const newGroup = (mode = "all") => ({ type: "group", mode, negate: false, children: [newCondition()] });

  function treeToRule(node) {
    if (node.type === "cond") {
      const field = node.field.trim();
      if (!field) throw new Error("Pick a field for every condition");
      if (NO_VALUE_OPS.has(node.op)) return { field, op: node.op };
      const text = String(node.value).trim();
      if (!text) throw new Error(`Add a value for “${field}”`);
      return { field, op: node.op, value: LIST_OPS.has(node.op) ? text.split(",").map((v) => v.trim()).filter(Boolean) : text };
    }
    if (!node.children.length) throw new Error("A group needs at least one condition");
    const rules = node.children.map(treeToRule);
    const group = rules.length === 1 && !node.negate ? rules[0] : { [node.mode]: rules };
    return node.negate ? { not: group } : group;
  }

  function ruleToTree(rule) {
    if (rule.field !== undefined) {
      const value = Array.isArray(rule.value) ? rule.value.join(", ") : rule.value === undefined ? "" : String(rule.value);
      return { type: "cond", field: rule.field, op: rule.op, value };
    }
    if (rule.not !== undefined) {
      const inner = ruleToTree(rule.not);
      if (inner.type === "group" && !inner.negate) return { ...inner, negate: true };
      return { type: "group", mode: "all", negate: true, children: [inner] };
    }
    const mode = rule.all ? "all" : "any";
    return { type: "group", mode, negate: false, children: (rule.all || rule.any).map(ruleToTree) };
  }

  function describeRule(rule, top = true) {
    if (rule.field !== undefined) {
      const label = operatorLabel(rule.op);
      if (NO_VALUE_OPS.has(rule.op)) return `${rule.field} ${label}`;
      const value = Array.isArray(rule.value) ? `[${rule.value.join(", ")}]` : rule.value;
      return `${rule.field} ${label} ${value}`;
    }
    if (rule.not !== undefined) return `NOT ${describeRule(rule.not, false)}`;
    const [word, children] = rule.all ? ["AND", rule.all] : ["OR", rule.any];
    const text = children.map((child) => describeRule(child, false)).join(` ${word} `);
    return top || children.length === 1 ? text : `(${text})`;
  }

  function safeDescribe(scope) {
    try {
      return describeRule(scope.rule);
    } catch {
      return "";
    }
  }

  /* ---------- Rule builder ---------- */

  const linkButton = (label, action) => h("button", { type: "button", class: "link-button", "data-action": action, text: label });

  function renderBuilder(container, tree, preview, datalistId) {
    const redraw = () => {
      container.replaceChildren(renderGroup(tree, null));
      showPreview();
    };
    const showPreview = () => {
      try {
        preview.textContent = describeRule(treeToRule(tree));
        preview.classList.remove("invalid");
      } catch (error) {
        preview.textContent = error.message;
        preview.classList.add("invalid");
      }
    };

    const renderCondition = (node, parent) => {
      const field = h("input", { type: "text", list: datalistId, placeholder: "field", "aria-label": "Field", value: node.field });
      field.addEventListener("input", () => { node.field = field.value; showPreview(); });
      const op = h("select", { "aria-label": "Operator" });
      for (const item of operators()) op.append(new Option(item.label, item.op, false, item.op === node.op));
      const value = h("input", { type: "text", "aria-label": "Value", value: node.value });
      const syncValue = () => {
        value.hidden = NO_VALUE_OPS.has(node.op);
        value.placeholder = LIST_OPS.has(node.op) ? "a, b, c" : "value";
      };
      value.addEventListener("input", () => { node.value = value.value; showPreview(); });
      op.addEventListener("change", () => { node.op = op.value; syncValue(); showPreview(); });
      syncValue();
      const remove = linkButton("×", "remove-node");
      remove.classList.add("remove-node");
      remove.title = "Remove condition";
      remove.setAttribute("aria-label", "Remove condition");
      remove.addEventListener("click", () => { parent.children.splice(parent.children.indexOf(node), 1); redraw(); });
      return h("div", { class: "rule-condition" }, field, op, value, remove);
    };

    const renderGroup = (node, parent) => {
      const mode = h("select", { "aria-label": "Combine with" });
      mode.append(new Option("All of (AND)", "all", false, node.mode === "all"), new Option("Any of (OR)", "any", false, node.mode === "any"));
      mode.addEventListener("change", () => { node.mode = mode.value; showPreview(); });
      const negate = h("input", { type: "checkbox" });
      negate.checked = node.negate;
      negate.addEventListener("change", () => { node.negate = negate.checked; showPreview(); });
      const head = h("div", { class: "rule-group-head" }, mode, h("label", { class: "rule-negate" }, negate, " NOT"));
      if (parent) {
        const remove = linkButton("×", "remove-node");
        remove.classList.add("remove-node");
        remove.title = "Remove group";
        remove.setAttribute("aria-label", "Remove group");
        remove.addEventListener("click", () => { parent.children.splice(parent.children.indexOf(node), 1); redraw(); });
        head.append(remove);
      }
      const children = h("div", { class: "rule-children" },
        ...node.children.map((child) => (child.type === "cond" ? renderCondition(child, node) : renderGroup(child, node))));
      const addCondition = linkButton("+ Condition", "add-condition");
      addCondition.addEventListener("click", () => { node.children.push(newCondition()); redraw(); });
      const addGroup = linkButton("+ Group", "add-group");
      addGroup.addEventListener("click", () => { node.children.push(newGroup("any")); redraw(); });
      return h("div", { class: "rule-group" }, head, children, h("div", { class: "rule-actions" }, addCondition, addGroup));
    };

    redraw();
    return redraw;
  }

  /* ---------- Scope manager (the Settings section) ---------- */

  function renderManager(body, { onStatus } = {}) {
    let tree = newGroup();
    let editing = null;
    const datalistId = "scope-field-options";
    const list = h("ul", { class: "scope-list" });
    const name = h("input", { type: "text", name: "name", maxlength: "80", placeholder: "My Team", required: "", "aria-label": "Scope name" });
    const builder = h("div", { class: "rule-builder" });
    const preview = h("p", { class: "rule-preview", "aria-live": "polite" });
    const datalist = h("datalist", { id: datalistId });
    const hint = h("p", { class: "sp-row-hint" });
    const saveButton = h("button", { type: "submit", class: "toolbar-button", text: "Save scope" });
    const cancel = h("button", { type: "button", class: "link-button", text: "Cancel", hidden: "" });
    const empty = h("p", { class: "sp-row-hint", text: "No scopes yet. Create one below." });
    let redraw = renderBuilder(builder, tree, preview, datalistId);

    const fillFields = () => {
      datalist.replaceChildren(...fieldInfo.fields.map((info) => {
        const option = new Option(info.name);
        option.label = `${info.name} · ${info.source}${info.examples.length ? ` · e.g. ${info.examples.slice(0, 2).join(", ")}` : ""}`;
        return option;
      }));
      hint.textContent = fieldInfo.fields.length
        ? `${fieldInfo.fields.length} fields found${fieldInfo.loaded ? ` in ${fieldInfo.loaded}` : ""}. You can also type any field name.`
        : "Type any field name. Load a project on the Query graph page to see the fields it has.";
    };

    const setTree = (next) => {
      tree = next;
      redraw = renderBuilder(builder, tree, preview, datalistId);
    };
    const reset = () => {
      editing = null;
      name.value = "";
      setTree(newGroup());
      cancel.hidden = true;
      saveButton.textContent = "Save scope";
    };

    const renderList = () => {
      list.replaceChildren();
      empty.hidden = scopes.length > 0;
      const active = getActive();
      for (const scope of scopes) {
        const isActive = scope.name === active;
        const item = h("li", { class: "scope-item", "data-name": scope.name },
          h("div", {}, h("strong", { text: scope.name }), h("small", { text: safeDescribe(scope), title: safeDescribe(scope) })),
          linkButton(isActive ? "Active ✓" : "Use", "use"), linkButton("Edit", "edit"), linkButton("Delete", "delete"));
        if (isActive) item.classList.add("is-active");
        list.append(item);
      }
    };

    list.addEventListener("click", async (event) => {
      const button = event.target.closest("button[data-action]");
      const scope = scopes.find((item) => item.name === button?.closest(".scope-item")?.dataset.name);
      if (!scope) return;
      const action = button.dataset.action;
      try {
        if (action === "use") {
          setActive(getActive() === scope.name ? "" : scope.name);
          renderList();
          onStatus?.(getActive() ? `“${scope.name}” is the active scope` : "No active scope");
        } else if (action === "edit") {
          editing = scope.name;
          name.value = scope.name;
          let next = ruleToTree(scope.rule);
          if (next.type === "cond") next = { type: "group", mode: "all", negate: false, children: [next] };
          setTree(next);
          cancel.hidden = false;
          saveButton.textContent = "Update scope";
          name.focus();
        } else {
          await save_(scopes.filter((item) => item !== scope));
          if (editing === scope.name) reset();
          onStatus?.(`Deleted “${scope.name}”`);
        }
      } catch (error) {
        onStatus?.(error.message, true);
      }
    });

    const save_ = async (next) => {
      await save(next);
      renderList();
    };

    const form = h("form", { class: "sp-scope-form", autocomplete: "off" },
      h("label", { class: "sp-scope-label" }, h("span", { text: "Name" }), name),
      h("div", { class: "rule-field" }, h("span", { text: "Include when" }), builder, datalist, preview, hint),
      h("div", { class: "settings-actions" }, saveButton, cancel));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const scopeName = name.value.trim();
        const scope = { name: scopeName, rule: treeToRule(tree) };
        const key = (editing ?? scopeName).toLowerCase();
        if (scopes.some((item) => item.name.toLowerCase() === scopeName.toLowerCase() && item.name.toLowerCase() !== key)) {
          throw new Error(`A scope named “${scopeName}” already exists`);
        }
        const next = [...scopes];
        const index = next.findIndex((item) => item.name.toLowerCase() === key);
        if (index < 0) next.push(scope); else next[index] = scope;
        await save_(next);
        reset();
        onStatus?.(`Saved “${scopeName}”`);
      } catch (error) {
        onStatus?.(error.message, true);
      }
    });
    cancel.addEventListener("click", reset);

    body.append(
      h("h3", { class: "sp-heading", text: "Scopes" }),
      h("p", { class: "sp-lede", text: "A scope is a saved rule that decides what is “in”: combine conditions on any field (a job’s submitter, a project, a table’s columns) with AND, OR and NOT. Choose the active scope on the graph and report pages to limit what they show." }),
      h("div", { class: "sp-group" }, list, empty),
      h("h4", { class: "sp-subheading", text: "New scope" }),
      form);
    renderList();
    Promise.all([load(), loadFields()]).then(() => { renderList(); fillFields(); redraw(); });
    fillFields();
  }

  /* ---------- Active-scope picker ---------- */

  /** Fill ``select`` with the saved scopes and keep it in step with the active one. */
  async function mountPicker(select, { onChange } = {}) {
    await load();
    const draw = () => {
      select.replaceChildren(new Option("No scope (everything)", ""));
      for (const scope of scopes) select.append(new Option(scope.name, scope.name, false, scope.name === getActive()));
      select.value = getActive();
    };
    draw();
    listListeners.add(draw);
    select.addEventListener("change", () => {
      setActive(select.value);
      onChange?.(select.value);
    });
    return draw;
  }

  window.KumoScopes = {
    load, loadFields, save, getActive, setActive, renderManager, mountPicker, describeRule,
    list: () => scopes,
    describe: safeDescribe,
    onActiveChange: (listener) => listeners.add(listener),
    /** Query string parameters for the pages' data requests. */
    params: () => (getActive() ? { scope: getActive() } : {}),
  };
})();
