"use strict";

/* Data sources (Settings → Data sources): saved queries whose result columns become
   fields in scopes and tag rules, and whose names are "Applies to" choices of scopes. */

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

  async function request(url, options) {
    const response = await fetch(url, options);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Request failed");
    return payload;
  }
  const json = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

  function summary(status) {
    if (!status) return "Not populated";
    const parts = [`${status.rows.toLocaleString()} rows`, `${status.columns.length} columns`];
    parts.push(`fetched ${new Date(status.fetched_at * 1000).toLocaleString()}`);
    if (status.stale) parts.push("stale");
    return parts.join(" · ");
  }

  function render(body, { onStatus = () => {} } = {}) {
    let info = { types: [], sources: [], default_cache_hours: 48 };
    const list = h("ul", { class: "scope-list" });
    const empty = h("p", { class: "sp-row-hint", text: "No data sources yet." });
    const add = h("button", { type: "button", class: "toolbar-button", text: "New data source" });

    const reload = async () => {
      info = await request("/api/data-sources");
      draw();
    };

    const persist = async (sources) => {
      await request("/api/settings/data_sources", json("PUT", sources.map(({ id, name, type, query, cache_hours }) => ({ id, name, type, query, cache_hours }))));
      await reload();
      window.KumoScopes?.loadFields?.();
    };

    const populate = async (id, refresh) => {
      onStatus("Running the query…");
      try {
        const status = await request("/api/data-sources/populate", json("POST", { id, refresh }));
        onStatus(status.error ? status.error : `${status.rows.toLocaleString()} rows loaded`, Boolean(status.error));
      } catch (error) {
        onStatus(error.message, true);
      }
      await reload();
      window.KumoScopes?.loadFields?.();
    };

    function draw() {
      list.replaceChildren();
      empty.hidden = info.sources.length > 0;
      for (const source of info.sources) {
        const status = source.status;
        list.append(h("li", { class: "scope-item", "data-id": source.id },
          h("div", {},
            h("strong", { text: source.name }),
            h("small", { text: summary(status), title: summary(status) }),
            status?.columns?.length ? h("span", { class: "scope-applies" }, ...status.columns.slice(0, 8).map((name) => h("span", { class: "domain-chip", text: name }))) : null),
          h("button", { type: "button", class: "link-button", "data-action": "populate", text: status ? "Refresh" : "Populate" }),
          h("button", { type: "button", class: "link-button", "data-action": "edit", text: "Edit" }),
          h("button", { type: "button", class: "link-button", "data-action": "delete", text: "Delete" })));
      }
    }

    list.addEventListener("click", async (event) => {
      const button = event.target.closest("button[data-action]");
      const source = info.sources.find((item) => item.id === button?.closest(".scope-item")?.dataset.id);
      if (!source) return;
      try {
        if (button.dataset.action === "populate") await populate(source.id, true);
        else if (button.dataset.action === "edit") openDialog(source);
        else {
          await persist(info.sources.filter((item) => item.id !== source.id));
          onStatus(`Deleted “${source.name}”`);
        }
      } catch (error) {
        onStatus(error.message, true);
      }
    });

    function openDialog(source) {
      const dialog = h("dialog", { class: "ds-dialog", "aria-label": source ? "Edit data source" : "New data source" });
      const name = h("input", { type: "text", class: "sp-input", maxlength: "80", value: source?.name || "", "aria-label": "Name", placeholder: "Name" });
      const type = h("select", { class: "sp-input", "aria-label": "Source" }, ...info.types.map((item) => new Option(item.label, item.key, false, item.key === (source?.type || "bigquery_sql"))));
      const query = h("textarea", { class: "sp-input ds-query", rows: "6", spellcheck: "false", "aria-label": "Query", placeholder: "SELECT * FROM project.dataset.table" });
      query.value = source?.query || "";
      const hours = h("input", { type: "number", class: "sp-input sp-number", min: "0", step: "1", "aria-label": "Cache lifetime in hours" });
      hours.value = source?.cache_hours ?? info.default_cache_hours;
      const message = h("p", { class: "sp-row-hint", role: "status" });
      const buttons = {
        close: h("button", { type: "button", class: "link-button", text: "Close" }),
        save: h("button", { type: "button", class: "toolbar-button", text: "Save" }),
        populate: h("button", { type: "button", class: "toolbar-button", text: "Save and Populate" }),
      };

      const save = async (andPopulate) => {
        try {
          const cache = Number(hours.value);
          const entry = {
            id: source?.id, name: name.value.trim(), type: type.value, query: query.value.trim(),
            cache_hours: hours.value !== "" && cache !== info.default_cache_hours ? cache : null,
          };
          const others = info.sources.filter((item) => item.id !== source?.id);
          const sources = source ? info.sources.map((item) => (item.id === source.id ? entry : item)) : [...others, entry];
          await persist(sources);
          dialog.close();
          if (andPopulate) {
            const saved = info.sources.find((item) => item.name === entry.name);
            if (saved) await populate(saved.id, false);
          }
        } catch (error) {
          message.textContent = error.message;
          message.classList.add("is-error");
        }
      };
      buttons.close.addEventListener("click", () => dialog.close());
      buttons.save.addEventListener("click", () => save(false));
      buttons.populate.addEventListener("click", () => save(true));
      dialog.addEventListener("close", () => dialog.remove());
      dialog.append(
        h("h4", { class: "sp-heading", text: source ? "Edit data source" : "New data source" }),
        h("label", { class: "ds-field" }, h("span", { text: "Name" }), name),
        h("label", { class: "ds-field" }, h("span", { text: "Source" }), type),
        h("label", { class: "ds-field" }, h("span", { text: "Query" }), query),
        h("label", { class: "ds-field ds-inline" }, h("span", { text: "Cache" }), hours, h("span", { text: "hours" })),
        message,
        h("div", { class: "settings-actions" }, buttons.close, buttons.save, buttons.populate));
      document.body.append(dialog);
      dialog.showModal();
      name.focus();
    }

    add.addEventListener("click", () => openDialog(null));
    body.append(
      h("h3", { class: "sp-heading", text: "Data sources" }),
      h("div", { class: "sp-group" }, list, empty),
      h("div", { class: "settings-actions" }, add));
    reload().catch((error) => onStatus(error.message, true));
  }

  window.KumoDataSources = { render };
})();
