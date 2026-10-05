"""Plain-text and JSON rendering of the materialization advisor's answer.

The rules of the page, kept here so every output follows them:

* only ``proven`` changes are recommended, and only they add to the counted saving;
* "needs proof" (a condition must hold) and "changes results" entries are listed
  apart, with their reasons, and are never part of any total;
* every figure says whether it was measured or estimated, and in which unit.
"""

from __future__ import annotations

from typing import Mapping

_BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def _bytes(value: float) -> str:
    sign = "-" if value < 0 else ""
    number = abs(float(value))
    for unit in _BYTE_UNITS:
        if number < 1024 or unit == _BYTE_UNITS[-1]:
            return f"{sign}{number:.0f} {unit}" if unit == "B" else f"{sign}{number:.1f} {unit}"
        number /= 1024
    return f"{sign}{number:.1f} {_BYTE_UNITS[-1]}"


def amount(value: float | None, unit: str) -> str:
    """``value`` in the advice's unit: dollars, bytes billed or slot time."""

    if value is None:
        return "unknown"
    if unit == "usd":
        digits = 2 if abs(value) >= 0.01 or value == 0 else 6
        return f"{'-' if value < 0 else ''}${abs(value):,.{digits}f}"
    if unit == "slot_ms":
        hours = value / 3_600_000
        return f"{hours:,.2f} slot-hours" if abs(hours) >= 0.01 else f"{value:,.0f} slot-ms"
    return _bytes(value)


def counted(advice: Mapping) -> dict:
    """The saving that counts: the best set of proven changes, and nothing else."""

    selection = advice["selection"]
    return {
        "saving_per_day": selection["saving_per_day"],
        "baseline_per_day": selection["baseline_per_day"],
        "chosen": list(selection["chosen"]),
        "basis": "estimate",
        "excludes": "needs_proof and changes_results entries",
    }


def to_json(advice: Mapping) -> dict:
    return {**advice, "counted": counted(advice)}


def _what(row: Mapping, unit: str) -> str:
    refresh = f"refresh {row['refresh_per_day']:.2g}/day at {amount(row['refresh_cost'], unit)}"
    bases = [f"refresh {row.get('refresh_basis', 'unknown')}", f"size {row.get('storage_basis', 'unknown')}"]
    return f"{refresh} ({', '.join(bases)})"


def render(advice: Mapping, *, title: str = "") -> str:
    unit = advice["unit"]
    lines = ["Materialization advice" + (f" for {title}" if title else "")]
    pricing = advice["pricing"]
    unit_text = {"usd": "US dollars", "bytes_billed": "bytes billed", "slot_ms": "slot time"}.get(unit, unit)
    lines.append(f"unit: {unit_text} per day ({pricing['compute']}); savings are estimates scaled from measured jobs")
    window = advice["workload"].get("window") or {}
    if window.get("start"):
        lines.append(f"history: {window['start']} to {window['end']} ({window['days']:.1f} days)")
    excluded = advice["workload"].get("excluded") or {}
    if excluded:
        lines.append("left out of the history: " + ", ".join(f"{n} {why.replace('_', ' ')}" for why, n in sorted(excluded.items())))
    if advice["workload"].get("unplaced_jobs"):
        lines.append(f"jobs that read no model of this project: {advice['workload']['unplaced_jobs']}")
    selection = advice["selection"]
    lines.append("")
    lines.append(
        f"Counted saving (proven changes only): {amount(selection['saving_per_day'], unit)} per day "
        f"of {amount(selection['baseline_per_day'], unit)} measured/estimated baseline "
        f"({selection['method']} search, {selection['combinations_evaluated']} sets evaluated)"
    )
    if selection.get("upper_bound_per_day") is not None:
        lines.append(f"  upper bound on any set: {amount(selection['upper_bound_per_day'], unit)} per day")

    recommended = advice["recommendations"]
    lines += ["", f"Recommended: proven to keep results ({len(recommended)})"]
    for index, row in enumerate(recommended, 1):
        lines.append(f"  {index}. {row['title']}")
        status = "in the best set" if row["chosen"] else "saves alone, but not in the best set"
        lines.append(f"     saves {amount(row['saving_per_day'], unit)} per day (estimate), {status}")
        lines.append(f"     {_what(row, unit)}")
        lines.append(f"     proof: {row['evidence']['reason']}")
    if not recommended:
        lines.append("  none")

    needs = advice["needs_proof"]
    lines += ["", f"Needs proof: NOT counted as savings ({len(needs)})"]
    for row in needs:
        lines.append(f"  - {row['title']}")
        saving = row["saving_per_day"] or 0
        effect = f"would save {amount(saving, unit)}" if saving > 0 else f"would cost {amount(-saving, unit)} more"
        lines.append(f"    {effect} per day if it held (estimate, NOT counted)")
        lines.append(f"    why not proven: {row['evidence']['reason']}")
        for condition in row["evidence"].get("conditions", []):
            lines.append(f"    condition: {condition}")
    if not needs:
        lines.append("  none")

    changes = advice["changes_results"]
    if changes:
        lines += ["", f"Would change results: never recommended ({len(changes)})"]
        for row in changes:
            lines.append(f"  - {row['title']}: {row['evidence']['reason']}")

    worthless = advice["not_worth_it"]
    if worthless:
        lines += ["", f"Proven, but no saving ({len(worthless)})"]
        for row in worthless:
            lines.append(f"  - {row['title']}: would cost {amount(-(row['saving_per_day'] or 0), unit)} more per day (estimate)")

    check = advice["calibration"].get("bytes_estimate_vs_measured") or {}
    if check.get("jobs"):
        lines += ["", f"Estimate check: bytes estimated from column sizes against {check['jobs']} measured job templates, "
                      f"q-error p50 {check['q_error_p50']}, p90 {check['q_error_p90']}, max {check['q_error_max']}"]
    for note in advice.get("notes", []):
        lines.append(f"note: {note}")
    return "\n".join(lines)
