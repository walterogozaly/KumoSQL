"""``python -m kumosql advise``: local files in, ranked recommendations out; no network."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from kumosql import cli
from kumosql.__main__ import main as run_module
from kumosql.advice_report import amount, render

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
ROWS = 1_000_000
RAW, BASE, DAILY, RECENT, DIRECT, MART = (f"p.{d}.{t}" for d, t in (
    ("raw", "events"), ("core", "base"), ("core", "daily"), ("core", "recent"), ("core", "direct"), ("core", "mart")))

FILES = {
    "workflow_settings.yaml": "defaultProject: p\ndefaultDataset: core\n",
    "definitions/events.sqlx": 'config { type: "declaration", schema: "raw", name: "events" }\n',
    "definitions/base.sqlx": 'config { type: "table" }\nSELECT id, user_id, amount, kind FROM ${ref("raw", "events")} WHERE kind != \'test\'\n',
    "definitions/daily.sqlx": 'config { type: "view" }\nSELECT user_id, SUM(amount) AS total FROM ${ref("base")} GROUP BY user_id\n',
    "definitions/recent.sqlx": 'config { type: "view" }\nSELECT id, amount, CURRENT_DATE() AS d FROM ${ref("base")}\n',
    "definitions/direct.sqlx": 'config { type: "view" }\nSELECT user_id, amount FROM ${ref("raw", "events")}\n',
    "definitions/mart.sqlx": 'config { type: "table" }\nSELECT user_id, total FROM ${ref("daily")} WHERE total > 0\n',
}
SOURCE_SCHEMA = {RAW: {"id": "INT64", "user_id": "INT64", "amount": "FLOAT64", "kind": "STRING", "ts": "TIMESTAMP"}}


def at(day, hour):
    return (T0 + timedelta(days=day, hours=hour)).isoformat().replace("+00:00", "Z")


def job(job_id, when, refs, billed, slot, query="", destination=None, **extra):
    record = {"job_id": job_id, "creation_time": when, "referenced_tables": refs, "total_bytes_processed": billed,
              "total_bytes_billed": billed, "total_slot_ms": slot, "query": query, **extra}
    if destination:
        record["destination_table"] = destination
    return record


def history(days=14):
    jobs = []
    for day in range(days):
        jobs.append(job(f"b{day}", at(day, 6), [RAW], 30 * ROWS, 60_000, destination=BASE))
        jobs.append(job(f"m{day}", at(day, 6.2), [DAILY, BASE], 14.4 * ROWS, 20_000, destination=MART))
        for k in range(40):
            jobs.append(job(f"d{day}-{k}", at(day, 8 + k * 0.2), [DAILY, BASE], 14.4 * ROWS, 9_000,
                            f"SELECT user_id, total FROM `{DAILY}` WHERE user_id = 7", user_email=f"u{k % 5}@example.test"))
        for k in range(5):
            jobs.append(job(f"r{day}-{k}", at(day, 9 + k), [RECENT, BASE], 7.2 * ROWS, 2_000,
                            f"SELECT id, d FROM `{RECENT}`", user_email="a@example.test"))
        for k in range(3):
            jobs.append(job(f"x{day}-{k}", at(day, 10 + k), [DIRECT, RAW], 8 * ROWS, 3_000,
                            f"SELECT SUM(amount) FROM `{DIRECT}`", user_email="b@example.test"))
        jobs.append({"job_id": f"c{day}", "creation_time": at(day, 11), "cache_hit": True, "query": "SELECT 1",
                     "referenced_tables": [DAILY]})
    return jobs


def sizes():
    ints = {"id": ROWS * 8.0, "user_id": ROWS * 8.0, "amount": ROWS * 8.0}
    return {
        RAW: {"rows": ROWS, "columns": {**ints, "kind": ROWS * 6.0, "ts": ROWS * 8.0}},
        BASE: {"rows": ROWS * 0.9, "columns": {k: v * 0.9 for k, v in {**ints, "kind": ROWS * 6.0}.items()}},
        DAILY: {"rows": 10_000, "columns": {"user_id": 80_000.0, "total": 80_000.0}},
        MART: {"rows": 9_000, "columns": {"user_id": 72_000.0, "total": 72_000.0}},
    }


@pytest.fixture
def files(tmp_path):
    root = tmp_path / "project"
    for rel, text in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    paths = {"project": root, "jobs": tmp_path / "jobs.json", "sizes": tmp_path / "sizes.json",
             "schema": tmp_path / "schema.json", "out": tmp_path / "out.txt"}
    paths["jobs"].write_text(json.dumps(history()), encoding="utf-8")
    paths["sizes"].write_text(json.dumps(sizes()), encoding="utf-8")
    paths["schema"].write_text(json.dumps(SOURCE_SCHEMA), encoding="utf-8")
    return paths


def args(f, *more):
    return ["--project", str(f["project"]), "--jobs", str(f["jobs"]), "--sizes", str(f["sizes"]),
            "--source-schema", str(f["schema"]), *more]


def test_json_lists_proven_changes_apart_from_needs_proof(files, capsys):
    assert cli.advise_main(args(files, "--format", "json")) == 0
    out = json.loads(capsys.readouterr().out)
    recommended = {r["id"] for r in out["recommendations"]}
    assert "store:" + DAILY in recommended
    assert all(r["evidence"]["label"] == "proven" for r in out["recommendations"])
    pending = {r["id"]: r for r in out["needs_proof"]}
    assert pending and all(r["evidence"]["label"] in ("conditional", "unknown") for r in pending.values())
    assert any(r["id"] == "store:" + RECENT for r in out["changes_results"])
    # needs-proof and result-changing entries never reach the counted saving or the chosen set
    assert not (set(out["counted"]["chosen"]) & (set(pending) | {r["id"] for r in out["changes_results"]}))
    assert out["counted"]["saving_per_day"] == out["selection"]["saving_per_day"]
    assert out["counted"]["chosen"] == out["selection"]["chosen"] and out["counted"]["saving_per_day"] > 0
    assert "SELECT" not in json.dumps(out["workload"])  # query text stays out of the output


def test_text_report_separates_the_sections(files, capsys):
    assert cli.advise_main(args(files)) == 0
    text = capsys.readouterr().out
    assert "Counted saving (proven changes only)" in text
    recommended, _, rest = text.partition("Recommended: proven")[2].partition("Needs proof: NOT counted as savings")
    assert f"Store {DAILY} as a table" in recommended
    assert f"Store {DIRECT} as a table" in rest or f"Make {BASE} a view" in rest
    assert "NOT counted" in rest and "condition:" in rest
    assert f"Store {RECENT}" not in recommended  # reads CURRENT_DATE: would change results
    assert "Would change results: never recommended" in text
    assert "bytes billed" in text and "estimate" in text


def test_writes_only_the_output_file_and_prices_in_dollars(files, capsys):
    assert cli.advise_main(args(files, "--usd-per-tib", "6.25", "--usd-per-gib-month", "0.02", "-o", str(files["out"]))) == 0
    assert capsys.readouterr().out == ""
    text = files["out"].read_text(encoding="utf-8")
    assert "US dollars" in text and "$" in text


def test_schedules_file_can_turn_conditional_into_proven(files, capsys):
    schedules = files["jobs"].parent / "schedules.json"
    assert cli.advise_main(args(files, "--format", "json")) == 0
    before = {r["id"]: r for r in json.loads(capsys.readouterr().out)["recommendations"]}
    assert "nightly" not in before["store:" + DAILY]["evidence"]["reason"]
    schedules.write_text(json.dumps({m: ["nightly"] for m in (RAW, BASE, DAILY, MART)}), encoding="utf-8")
    assert cli.advise_main(args(files, "--schedules", str(schedules), "--format", "json")) == 0
    after = {r["id"]: r for r in json.loads(capsys.readouterr().out)["recommendations"]}
    assert "nightly" in after["store:" + DAILY]["evidence"]["reason"]  # the declared schedule is the proof
    schedules.write_text("[1]", encoding="utf-8")
    assert cli.advise_main(args(files, "--schedules", str(schedules))) == 2
    assert "schedules file" in capsys.readouterr().err


def test_without_sizes_it_says_the_estimates_are_weaker(files, capsys):
    argv = ["--project", str(files["project"]), "--jobs", str(files["jobs"]), "--source-schema", str(files["schema"])]
    assert cli.advise_main(argv) == 0
    assert "No --sizes file" in capsys.readouterr().out


@pytest.mark.parametrize("break_what, message", [
    ("jobs-missing", "error:"),
    ("jobs-empty", "no jobs"),
    ("sizes-bad", "error:"),
])
def test_bad_inputs_exit_2_without_a_traceback(files, capsys, break_what, message):
    if break_what == "jobs-missing":
        files["jobs"].unlink()
    elif break_what == "jobs-empty":
        files["jobs"].write_text("[]", encoding="utf-8")
    else:
        files["sizes"].write_text("{not json", encoding="utf-8")
    assert cli.advise_main(args(files)) == 2
    assert message in capsys.readouterr().err


def test_a_bad_price_is_a_usage_error(files):
    with pytest.raises(SystemExit) as stop:
        cli.advise_main(args(files, "--usd-per-tib", "-1"))
    assert stop.value.code == 2


def test_runs_through_python_dash_m_with_the_short_name(files, capsys):
    assert run_module(["advise", *args(files, "--format", "json")]) == 0
    assert "recommendations" in json.loads(capsys.readouterr().out)


def test_amount_formats_each_unit():
    assert amount(1536, "bytes_billed") == "1.5 KiB"
    assert amount(-2.5, "usd") == "-$2.50"
    assert amount(7_200_000, "slot_ms") == "2.00 slot-hours"
    assert amount(0.00031, "usd") == "$0.000310"
    assert amount(None, "usd") == "unknown"


def test_render_never_counts_needs_proof_entries():
    row = {"id": "store:x", "kind": "store_view", "title": "Store x as a table", "node": "x", "saving_per_day": 99.0,
           "refresh_per_day": 1.0, "refresh_cost": 1.0, "chosen": False,
           "evidence": {"label": "conditional", "reason": "reads a source", "conditions": ["x only changes before refresh"]}}
    advice = {"unit": "bytes_billed", "pricing": {"compute": "on_demand"}, "workload": {},
              "selection": {"saving_per_day": 0.0, "baseline_per_day": 10.0, "method": "exact", "combinations_evaluated": 1},
              "recommendations": [], "needs_proof": [row], "changes_results": [], "not_worth_it": [],
              "calibration": {}, "notes": []}
    text = render(advice)
    assert "Counted saving (proven changes only): 0 B per day" in text
    assert "would save 99 B per day if it held (estimate, NOT counted)" in text
    assert "condition: x only changes before refresh" in text
