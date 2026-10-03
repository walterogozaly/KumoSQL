# Continuing someone else's work

[All simple guides](README.md) · [Full reference](../docs/handoff.md)

Work on KumoSQL is split into workstreams. Each one has a GitHub issue labelled `workstream` that says what the goal is, how far it got, which branches and pull requests hold it, and what is left. Anyone can pick one up from the issue alone. The [workstream index](https://github.com/walterogozaly/KumoSQL/issues/528) lists them in priority order.

For example, an issue might say a fix is finished on a branch but one eval comparison is still missing. You would claim it with a comment, run that comparison from a fresh clone, open the pull request, and comment on the issue with what you did.

The short version of the rules:

- Comments on an issue are newer than its body.
- Run only the tests aimed at your change and the evals it could move, never the whole suite.
- A wrong proof or a wrong refutation is never acceptable; "not proven" is always allowed.
- Never look at held-out evaluation cases.
- Don't merge your own pull request; the maintainers merge after a full run.

The issue is a hand-off note, not a guarantee: check its claims on current `master` before relying on them.
