"use strict";

/* Project picker for the BigQuery tab. Nothing is listed until at least one project is
   chosen. The chosen ids are saved on the server; the list of every project the
   credentials hold a role on is only fetched when you ask to browse it. Used by the
   BigQuery page and by the BigQuery section of Settings. */

(() => {
  const h = (tag, attrs = {}, ...children) => {
    const element = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === "class") element.className = value;
      else if (key === "text") element.textContent = value;
      else if (key.startsWith("on")) element.addEventListener(key.slice(2), value);
      else if (value !== false && value != null) element.setAttribute(key, value === true ? "" : value);
    }
    element.append(...children);
    return element;
  };

  async function request(url, options) {
    const response = await fetch(url, options);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Request failed");
    return payload;
  }

  const loadSelection = async () => (await request("/api/catalog/selection")).projects;

  async function saveSelection(projects) {
    const saved = await request("/api/catalog/selection", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ projects }),
    });
    window.dispatchEvent(new CustomEvent("kumosql:bq-projects", { detail: saved.projects }));
    return saved.projects;
  }

  function render(container, { onStatus = () => {} } = {}) {
    let chosen = [];
    let available = null;
    const selectedList = h("ul", { class: "bq-chosen", "aria-label": "Chosen projects" });
    const idInput = h("input", { type: "text", class: "sp-input", placeholder: "my-project-id", spellcheck: "false", "aria-label": "Project ID" });
    const search = h("input", { type: "search", class: "sp-input", placeholder: "Search your projects", "aria-label": "Search projects", hidden: true });
    const results = h("ul", { class: "bq-results", "aria-label": "Accessible projects" });
    const browseButton = h("button", { type: "button", class: "toolbar-button", text: "Browse my projects" });

    async function change(next, doneText) {
      onStatus("Checking access…");
      try {
        chosen = await saveSelection(next);
        onStatus(doneText);
      } catch (error) {
        onStatus(error.message, true);
      }
      draw();
    }

    function draw() {
      selectedList.replaceChildren();
      if (!chosen.length) selectedList.append(h("li", { class: "empty-state", text: "No projects chosen yet. Nothing is loaded from BigQuery until you add one." }));
      for (const id of chosen) {
        selectedList.append(h("li", {},
          h("span", { text: id }),
          h("button", { type: "button", class: "link-button", text: "Remove", "aria-label": `Remove ${id}`,
            onclick: () => change(chosen.filter((item) => item !== id), `Removed ${id}`) })));
      }
      results.replaceChildren();
      if (available) {
        const text = search.value.trim().toLowerCase();
        const matches = available.filter((item) => !text || `${item.id} ${item.name}`.toLowerCase().includes(text)).slice(0, 200);
        if (!matches.length) results.append(h("li", { class: "empty-state", text: "No matching projects." }));
        for (const item of matches) {
          const picked = chosen.includes(item.id);
          results.append(h("li", {},
            h("span", { text: item.name !== item.id ? `${item.name} · ${item.id}` : item.id }),
            h("button", { type: "button", class: "link-button", text: picked ? "Chosen" : "Add", disabled: picked,
              onclick: () => change([...chosen, item.id], `Added ${item.id}`) })));
        }
      }
    }

    const add = () => {
      const id = idInput.value.trim();
      if (!id) return;
      if (chosen.includes(id)) { onStatus(`${id} is already chosen`); return; }
      idInput.value = "";
      change([...chosen, id], `Added ${id}`);
    };
    idInput.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); add(); } });
    search.addEventListener("input", draw);
    browseButton.addEventListener("click", async () => {
      browseButton.disabled = true;
      onStatus("Listing your projects…");
      try {
        available = (await request("/api/catalog/projects")).data;
        search.hidden = false;
        search.focus();
        onStatus(`${available.length} projects found. Add the ones you want.`);
        draw();
      } catch (error) {
        onStatus(error.message, true);
      } finally {
        browseButton.disabled = false;
      }
    });

    container.append(
      h("p", { class: "sp-lede", text: "Choose the BigQuery projects to show in the BigQuery tab. Only these are listed, and datasets load only for them." }),
      selectedList,
      h("div", { class: "sp-inline" }, idInput, h("button", { type: "button", class: "toolbar-button", text: "Add by ID", onclick: add }), browseButton),
      search,
      results,
    );
    loadSelection().then((projects) => { chosen = projects; draw(); }).catch((error) => onStatus(error.message, true));
    draw();
  }

  // Billing project and query cache lifetime. Other features that run SQL read these
  // from GET /api/catalog/settings; until a billing project is set they report that one is needed.
  function renderBilling(container, { onStatus = () => {} } = {}) {
    const input = h("input", { type: "text", class: "sp-input", list: "bq-billing-options", placeholder: "my-billing-project", spellcheck: "false", "aria-label": "Billing project" });
    const options = h("datalist", { id: "bq-billing-options" });
    const hours = h("input", { type: "number", class: "sp-input sp-number", min: "0", max: "8760", step: "1", "aria-label": "Query cache lifetime in hours" });
    const note = h("p", { class: "sp-row-hint" });

    const put = async (body) => {
      onStatus("Saving…");
      try {
        const saved = await request("/api/catalog/settings", {
          method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
        });
        input.value = saved.billingProject;
        hours.value = saved.queryCacheHours;
        note.textContent = saved.billingProject ? "" : "No billing project set: anything that runs queries will ask for one.";
        onStatus("Saved");
        window.dispatchEvent(new CustomEvent("kumosql:bq-settings", { detail: saved }));
      } catch (error) {
        onStatus(error.message, true);
      }
    };
    const save = h("button", { type: "button", class: "toolbar-button", text: "Set billing project", onclick: () => put({ billingProject: input.value }) });
    const load = h("button", { type: "button", class: "toolbar-button", text: "Browse my projects", onclick: async () => {
      load.disabled = true;
      onStatus("Listing your projects…");
      try {
        const projects = (await request("/api/catalog/projects")).data;
        options.replaceChildren(...projects.map((item) => h("option", { value: item.id, label: item.name })));
        onStatus(`${projects.length} projects found. Pick one from the box.`);
        input.focus();
      } catch (error) {
        onStatus(error.message, true);
      } finally {
        load.disabled = false;
      }
    } });
    input.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); put({ billingProject: input.value }); } });
    hours.addEventListener("change", () => put({ queryCacheHours: Number(hours.value) }));

    container.append(
      h("h4", { class: "sp-heading", text: "Billing project" }),
      h("p", { class: "sp-lede", text: "Queries KumoSQL runs for you (such as scope rules that use SQL) run and are billed in this project. It needs permission to create BigQuery jobs." }),
      h("div", { class: "sp-inline" }, input, options, save, load),
      note,
      h("h4", { class: "sp-heading", text: "Query cache" }),
      h("p", { class: "sp-lede", text: "Hours to reuse the result of a query KumoSQL ran before asking BigQuery again. Default 48." }),
      h("div", { class: "sp-inline" }, hours, h("span", { text: "hours" })),
    );
    request("/api/catalog/settings").then((saved) => {
      input.value = saved.billingProject;
      hours.value = saved.queryCacheHours;
      note.textContent = saved.billingProject ? "" : "No billing project set: anything that runs queries will ask for one.";
    }).catch((error) => onStatus(error.message, true));
  }

  window.KumoBqProjects = { render, renderBilling, loadSelection };
})();
