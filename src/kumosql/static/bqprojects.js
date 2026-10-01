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

  window.KumoBqProjects = { render, loadSelection };
})();
