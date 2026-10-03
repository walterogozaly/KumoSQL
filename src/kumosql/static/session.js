"use strict";

// Loaded before any page scripts. Keep the secret on same-origin API requests,
// including callers that pass a Request rather than a URL and options.
(() => {
  const token = document.querySelector('meta[name="kumosql-session-token"]').content;
  const fetch = window.fetch.bind(window);
  window.fetch = (input, options) => {
    const url = new URL(input instanceof Request ? input.url : input, location.href);
    if (url.origin !== location.origin || !(url.pathname === "/api" || url.pathname.startsWith("/api/"))) {
      return fetch(input, options);
    }
    const headers = new Headers(options?.headers ?? (input instanceof Request ? input.headers : undefined));
    headers.set("X-KumoSQL-Session", token);
    return fetch(input, { ...options, headers });
  };
})();
