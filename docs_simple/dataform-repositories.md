# Connecting a Dataform repository

[All simple guides](README.md) · [Full reference](../docs/dataform-repositories.md)

KumoSQL reads your Dataform project using git installed on your computer. Private repositories use the same credentials as your normal git commands.

## Connect through the app

1. Start `python -m kumosql ui`.
2. In **Settings → Local data folder**, choose a writable folder in your home directory.
3. In **Settings → Repositories**, enter your git URL and optionally a branch.
4. Activate the repository to use it on the graph, Cost, and Change reports pages.

A blank branch uses the remote's default. **Fetch latest** refreshes the saved copy. The app can keep several repositories, with one active at a time.

For a one-off load:

```sh
python -m kumosql ui --git https://github.com/owner/dataform-project.git --branch main
```

Replace the URL and branch with your own. Do not put a token, password or user name inside an https URL, or a password inside an ssh URL: those URLs are rejected, as is any URL with a query string. Use a Git credential helper for HTTPS, or an SSH key with `git@host:path` or `ssh://git@host/path`. Authentication must already work for git. If you use GitHub CLI for HTTPS authentication, `gh auth setup-git` lets git use that login; `gh auth login` alone is not enough.

## What gets stored

The local data folder holds settings, cached repository data, saved analyses, and logs. The app can show a saved project while it refreshes in the background. If a refresh fails, it can keep the cached copy and say why.

Choosing a home folder is especially helpful with Microsoft Store Python: Python and git can otherwise disagree about where AppData files live.

## Why a dependency might look surprising

Dataform's `${ref("name")}` resolves to the action or declaration with that name. Its own `schema` and `database` settings matter. KumoSQL reads declarations and project defaults rather than assuming every reference belongs to the default dataset.

When Google credentials are available, KumoSQL also reads Dataform workflow configurations. A model gets a production-schedule marker when an active configuration selects it, directly or through dependencies. Here, active production means a scheduled, enabled configuration using the release named `production`.

## What KumoSQL will not guess

KumoSQL reads your `.sqlx` files without running Dataform's JavaScript. When a value would need that JavaScript, it says it does not know instead of guessing.

For example, say one action has `type: dataform.projectConfig.vars.kind` and another has `type: "table"`, and both select `1 AS id`. If the variable is `incremental`, the first one keeps its old rows between runs, so the two are not the same table. KumoSQL marks the first one as an unknown type and will not call the pair equivalent. It still reads the first query for lineage.

Three more things it handles the way Dataform does:

- A `${ref("base")}` written inside a SQL comment is only text. It is not a dependency.
- Built-in assertions such as `rowConditions: ["status > 0"]`, and a table's `partitionBy` and `clusterBy`, use that table's columns. A column named there is never reported as unused, even when no query reads it. If the setting is computed by JavaScript and cannot be read, KumoSQL reports no unused columns for that table.
- A table with such settings and nothing reading it is still treated as a final output.

Limits: these checks come from small synthetic projects compared with Dataform's compiler, not from a benchmark. Computed table names, schema prefixes and projects the compiler would reject are not handled yet. The [full guide](../docs/dataform-repositories.md#what-the-static-reader-does-not-guess) lists them.

## If loading fails

Run:

```sh
python -m kumosql.ui --diagnose-repo https://github.com/owner/dataform-project.git
```

This reports the steps and environment details. Logs use placeholders such as `repo#1` and report counts, timing, error categories and positions rather than SQL, row values, variables or exception messages. Quoted secrets, JSON credential fields and private-key blocks are removed. Copy diagnostics includes only summarized log entries and leaves older or free-form entries out; settings values and unknown field names are withheld. Repository URLs and cached clone origins do not retain embedded credentials. `redaction-map.json` contains the real-name mappings and should stay on your computer. The full guide covers HTTPS fallback, cache behavior, schedules, and diagnostic codes.
