"""Render a change report as a code review check result and comment.

Input is the change report shape documented in docs/ui-roadmap.md
(``report {title, base, head, generated_at, changes[], diagnostics[]}``).
Output is ``ci {check_name, conclusion, summary}`` plus a markdown comment.

Conclusion policy is conservative. ``success`` needs positive evidence for
every change; anything unknown, unproven, incomplete or malformed is
``neutral`` and only a failed check (or unproven, when asked) is ``failure``.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

CHECK_NAME = "KumoSQL change report"
MARKER = "<!-- kumosql-change-report -->"
MAX_COMMENT_CHARS = 60000  # platform limit is 65536; leave headroom

_LABELS = ("proven", "planner_checked", "unproven", "failed", "unchanged")
_LABEL_TEXT = {
    "proven": "Proven", "planner_checked": "Planner checked", "unproven": "Unproven",
    "failed": "Failed", "unchanged": "Unchanged", "unknown": "Unknown",
}
_BASIS_TEXT = {"measured": "measured", "estimate": "estimate", "upper_bound": "upper bound"}


def _label(change: Any) -> str:
    if not isinstance(change, dict):
        return "unknown"
    ver = change.get("verification")
    label = ver.get("label") if isinstance(ver, dict) else None
    return label if label in _LABELS else "unknown"


def _changes(report: Any) -> list | None:
    changes = report.get("changes") if isinstance(report, dict) else None
    return changes if isinstance(changes, list) else None


def _diagnostics(report: Any) -> list:
    diags = report.get("diagnostics") if isinstance(report, dict) else None
    return [d for d in diags if isinstance(d, dict)] if isinstance(diags, list) else []


def _incomplete(change: Any) -> bool:
    cons = change.get("consumers") if isinstance(change, dict) else None
    return not (isinstance(cons, dict) and cons.get("complete") is True)


def conclude(report: Any, fail_on_unproven: bool = False) -> str:
    """Return ``success``, ``neutral`` or ``failure``."""
    changes = _changes(report)
    if changes is None:
        return "neutral"
    labels = [_label(c) for c in changes]
    if "failed" in labels or (fail_on_unproven and "unproven" in labels):
        return "failure"
    if _diagnostics(report):
        return "neutral"
    for change, label in zip(changes, labels):
        if label not in ("proven", "unchanged"):
            return "neutral"
        if label == "proven" and _incomplete(change):
            return "neutral"
    return "success"


def summarize(report: Any) -> str:
    changes = _changes(report)
    if changes is None:
        return "No readable change report; nothing was verified"
    labels = [_label(c) for c in changes]
    changed = [l for l in labels if l != "unchanged"]
    parts = [f"{len(changed)} changed model{'s' if len(changed) != 1 else ''}"]
    for key in ("proven", "planner_checked", "unproven", "failed", "unknown"):
        n = labels.count(key)
        if n:
            parts.append(f"{n} {_LABEL_TEXT[key].lower()}")
    n = len(_diagnostics(report))
    if n:
        parts.append(f"{n} diagnostic{'s' if n != 1 else ''}")
    return " · ".join(parts)


def build_check(report: Any, fail_on_unproven: bool = False) -> dict:
    return {"check_name": CHECK_NAME, "conclusion": conclude(report, fail_on_unproven),
            "summary": summarize(report)}


def _cell(text: Any) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def _money(cost: Any) -> str:
    if not isinstance(cost, dict):
        return "unavailable"
    basis = cost.get("basis")
    before, after = cost.get("before"), cost.get("after")
    if basis not in _BASIS_TEXT or not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
        return "unavailable"
    return f"{before:g} to {after:g} ({_BASIS_TEXT[basis]})"


def _consumers(change: Any) -> str:
    cons = change.get("consumers") if isinstance(change, dict) else None
    if not isinstance(cons, dict):
        return "Unknown"
    models = [str(m) for m in cons.get("models") or []]
    text = ", ".join(models) if models else "none found"
    return text if cons.get("complete") is True else f"{text} (list may be incomplete)"


MAX_OVERLAP_ROWS = 8
_KIND_TEXT = {"same_meaning": "Same meaning", "contains": "Contains", "partial": "Partial"}


def _overlap_check_text(checks: Any) -> str:
    parts = [f"{c.get('kind')}: {c.get('outcome')}" for c in checks if isinstance(c, dict)] if isinstance(checks, list) else []
    return ", ".join(parts) or "none"


def _overlap_role_text(role: Any) -> str:
    if not isinstance(role, dict) or not role.get("role"):
        return "unknown"
    text = f"{role.get('role')} ({role.get('confidence')} confidence)"
    evidence = role.get("evidence")
    details = [str(e.get("detail")) for e in evidence[:2] if isinstance(e, dict) and e.get("detail")] if isinstance(evidence, list) else []
    return text + (": " + "; ".join(details) if details else "")


def _overlap_lines(changes: list) -> list[str]:
    """The advisory "already done elsewhere" section. Never affects the conclusion."""

    entries = []
    for change in changes:
        section = change.get("overlaps") if isinstance(change, dict) else None
        if isinstance(section, dict):
            entries.append((str(change.get("model", "?")), section))
        elif section is not None:
            entries.append((str(change.get("model", "?")), {}))
    if not entries:
        return []
    lines = ["#### Already done elsewhere (advisory)", "",
             "Existing tables that may already provide the same attributes. This never blocks the check.", ""]
    for model, section in entries:
        summary = section.get("summary") if isinstance(section.get("summary"), str) else ""
        if section.get("status") == "unavailable" or not summary:
            summary = summary or "The comparison could not be completed; no tables were compared"
        lines.append(f"**`{_cell(model)}`**: {_cell(summary)}")
        matches = [m for m in section.get("matches") or [] if isinstance(m, dict)] if isinstance(section.get("matches"), list) else []
        if matches:
            lines += ["", "| Existing table | Match | Checks | Role | Note |", "| --- | --- | --- | --- | --- |"]
            for m in matches[:MAX_OVERLAP_ROWS]:
                notes = [n for n, on in (("retired in this change", m.get("retiring")),
                                         ("also new or edited in this change", m.get("in_this_change"))) if on]
                kind = _KIND_TEXT.get(m.get("kind"), str(m.get("kind", "?")))
                lines.append("| {} | {} ({}) | {} | {} | {} |".format(
                    _cell(m.get("table", "?")), kind, _cell(m.get("confidence", "?")),
                    _cell(_overlap_check_text(m.get("checks"))), _cell(_overlap_role_text(m.get("role"))),
                    _cell("; ".join(notes))))
            if len(matches) > MAX_OVERLAP_ROWS:
                lines.append(f"\n_{len(matches) - MAX_OVERLAP_ROWS} more not shown._")
        unknown = [u for u in section.get("unknown") or [] if isinstance(u, dict)] if isinstance(section.get("unknown"), list) else []
        if unknown:
            lines += ["", f"Could not be compared ({len(unknown)}): " + ", ".join(
                f"`{_cell(u.get('table', '?'))}`" for u in unknown[:MAX_OVERLAP_ROWS])
                + (" and more" if len(unknown) > MAX_OVERLAP_ROWS else "")]
        lines.append("")
    return lines


def _cap(text: str) -> str:
    if len(text) <= MAX_COMMENT_CHARS:
        return text
    note = "\n\n_Comment shortened; see the full report artifact._\n"
    return text[: MAX_COMMENT_CHARS - len(note)].rsplit("\n", 1)[0] + note


def render_comment(report: Any, fail_on_unproven: bool = False) -> str:
    check = build_check(report, fail_on_unproven)
    icon = {"success": "Passed", "neutral": "Needs attention", "failure": "Failed"}[check["conclusion"]]
    lines = [MARKER, f"### {CHECK_NAME}: {icon}", "", check["summary"], ""]
    changes = _changes(report)
    if changes is None:
        lines.append("The report was missing or unreadable, so no evidence is shown. "
                     "This is not a pass.")
        return _cap("\n".join(lines) + "\n")
    if report.get("base") or report.get("head"):
        lines += [f"Comparing `{_cell(report.get('base'))}` to `{_cell(report.get('head'))}`.", ""]
    shown = [c for c in changes if _label(c) != "unchanged"]
    if shown:
        lines += ["| Model | Evidence | Reason | Cost | Consumers |", "| --- | --- | --- | --- | --- |"]
        order = {k: i for i, k in enumerate(("failed", "unknown", "unproven", "planner_checked", "proven"))}
        for c in sorted(shown, key=lambda c: order.get(_label(c), 0)):
            d = c if isinstance(c, dict) else {}
            ver = d.get("verification")
            reason = ver.get("reason") if isinstance(ver, dict) else ""
            lines.append("| {} | {} | {} | {} | {} |".format(
                _cell(d.get("model", "?")), _LABEL_TEXT[_label(c)], _cell(reason),
                _cell(_money(d.get("cost"))), _cell(_consumers(c))))
        lines.append("")
    else:
        lines += ["No model definitions changed.", ""]
    lines += _overlap_lines(shown)
    unchanged = len(changes) - len(shown)
    if unchanged:
        lines += [f"{unchanged} model{'s' if unchanged != 1 else ''} unchanged.", ""]
    diags = _diagnostics(report)
    if diags:
        lines += ["#### Could not be analyzed", ""]
        lines += [f"- `{_cell(d.get('asset'))}`: {_cell(d.get('message'))}" for d in diags]
        lines.append("")
    if check["conclusion"] != "success":
        lines.append("Anything not proven is shown as such; missing evidence is never counted as a pass.")
    return _cap("\n".join(lines).rstrip() + "\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Render a change report as a review check and comment.")
    p.add_argument("report", help="Change report JSON file ('-' for stdin); the report or {report: ...}")
    p.add_argument("--comment-out", help="Write the markdown comment here")
    p.add_argument("--check-out", help="Write the check JSON here (default: stdout)")
    p.add_argument("--fail-on-unproven", action="store_true")
    p.add_argument("--exit-code", action="store_true", help="Exit 1 when the conclusion is failure")
    a = p.parse_args(argv)
    data = None
    try:
        if a.report == "-":
            data = json.loads(sys.stdin.read())
        else:
            with open(a.report, encoding="utf-8") as fh:
                data = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"could not read report: {exc}", file=sys.stderr)
    report = data.get("report", data) if isinstance(data, dict) else None
    check = build_check(report, a.fail_on_unproven)
    if a.comment_out:
        with open(a.comment_out, "w", encoding="utf-8") as fh:
            fh.write(render_comment(report, a.fail_on_unproven))
    out = json.dumps(check, indent=2)
    if a.check_out:
        with open(a.check_out, "w", encoding="utf-8") as fh:
            fh.write(out + "\n")
    else:
        print(out)
    return 1 if a.exit_code and check["conclusion"] == "failure" else 0


if __name__ == "__main__":
    raise SystemExit(main())
