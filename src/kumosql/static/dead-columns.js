"use strict";

(() => {
  const list = document.querySelector("#dc-list");
  const status = document.querySelector("#dc-status");

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function render(data) {
    list.replaceChildren();
    if (data.empty) {
      status.textContent = "";
      list.append(element("div", "dc-empty", "Load a project"));
      return;
    }
    const total = data.models.reduce((count, item) => count + item.columns.length, 0);
    status.textContent = `${total} columns · ${data.models.length} models${data.complete ? "" : " · Partial graph"}`;
    status.dataset.state = data.complete ? "complete" : "partial";
    for (const item of data.models) {
      const card = element("article", "dc-model");
      const heading = element("header", "dc-model-head");
      heading.append(element("code", "", item.model));
      heading.append(element("span", "", `${item.columns.length}`));
      const columns = element("div", "dc-columns");
      for (const column of item.columns) columns.append(element("code", "", column));
      card.append(heading, columns);
      list.append(card);
    }
    if (!data.models.length) list.append(element("div", "dc-empty", "No dead columns"));
  }

  async function load() {
    status.textContent = "Loading…";
    try {
      const response = await fetch("/api/dead-columns", { cache: "no-store" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      render(data);
    } catch (error) {
      status.textContent = error.message;
      status.className = "dc-status error";
      list.replaceChildren();
    }
  }

  load();
})();
