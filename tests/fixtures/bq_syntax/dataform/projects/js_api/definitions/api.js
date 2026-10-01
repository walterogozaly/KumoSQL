declare({ schema: "kumosql_messy", name: "raw_users" });

publish("users_copy", { type: "table", tags: ["js"] }).query(
  ctx => `SELECT id FROM ${ctx.ref("raw_users")}`
);

publish("users_view_js").config({ type: "view" }).query(
  ctx => `SELECT id FROM ${ctx.ref("users_copy")}`
);

operate("js_op", ctx => [`DELETE FROM ${ctx.ref("users_copy")} WHERE FALSE`]);

assert("js_assert").query(ctx => `SELECT id FROM ${ctx.ref("users_copy")} WHERE id IS NULL`);

["a", "b"].forEach(suffix =>
  publish(`generated_${suffix}`).query(ctx => `SELECT '${suffix}' AS k FROM ${ctx.ref("raw_users")}`)
);
