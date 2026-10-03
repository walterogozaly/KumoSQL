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

Replace the URL and branch with your own. Authentication must already work for git. If you use GitHub CLI for HTTPS authentication, `gh auth setup-git` lets git use that login; `gh auth login` alone is not enough.

## What gets stored

The local data folder holds settings, cached repository data, saved analyses, and logs. The app can show a saved project while it refreshes in the background. If a refresh fails, it can keep the cached copy and say why.

Choosing a home folder is especially helpful with Microsoft Store Python: Python and git can otherwise disagree about where AppData files live.

## Why a dependency might look surprising

Dataform's `${ref("name")}` resolves to the action or declaration with that name. Its own `schema` and `database` settings matter. KumoSQL reads declarations and project defaults rather than assuming every reference belongs to the default dataset.

When Google credentials are available, KumoSQL also reads Dataform workflow configurations. A model gets a production-schedule marker when an active configuration selects it, directly or through dependencies. Here, active production means a scheduled, enabled configuration using the release named `production`.

## If loading fails

Run:

```sh
python -m kumosql.ui --diagnose-repo https://github.com/owner/dataform-project.git
```

This reports the steps and environment details. Logs replace known names with placeholders such as `repo#1`; check the report before sharing it. `redaction-map.json` contains the real-name mappings and should stay on your computer. The full guide covers HTTPS fallback, cache behavior, schedules, and diagnostic codes.
