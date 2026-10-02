# Repository instructions for Claude

Keep documentation and README files up to date when your changes affect setup, usage, behavior, or developer workflows. Include those updates in the same pull request.

The README is the overview: the scoreboard, a short summary per feature, the CLI table and the work-laptop smoke test. Feature details live in `docs/` (for example `docs/ui.md`, `docs/provers.md`, `docs/pipeline-analysis.md`, `docs/rewrite-rules.md`, `docs/cost-and-change-reports.md`, `docs/dataform-repositories.md`, and one page per eval). Put new detail in the matching docs page, and change the README summary only when the one-line description changes. When a README merge conflicts over a section that moved to `docs/`, apply your change to the docs page.

When your work is complete, run the relevant tests and open a pull request. Enable auto-merge for that pull request using the repository's usual merge method, without waiting for a separate request to merge. Let GitHub merge it once required checks and reviews pass. If auto-merge cannot be enabled or a conflict blocks merging, report the blocker and leave the pull request open.

Benchmark scoreboard: the README's scoreboard is generated from `benchmarks/results/*.json`, one file per eval. Any change that moves an eval's score, or adds an eval, must update that eval's results file (score, size, date, caveats; keep "0 wrong" honest) and run `python tools/scoreboard.py` in the same pull request. Never edit the README table by hand. If the table conflicts when merging, keep either side and rerun the script. Mention the same numbers in the eval's own docs.
