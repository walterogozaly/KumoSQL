# Testing ORDER BY and LIMIT rewrites

[All simple guides](README.md) · [Full reference](../docs/rule-fuzz-limits.md)

KumoSQL has a targeted fuzzer for the rewrites that remove unused sort orders, remove repeated sort keys, merge
top-k limits, and move a limit out of a plain projection. It also has near-miss queries that make sure a rewrite
keeps an order or limit when that detail can change which rows are returned.

Run it with:

```bash
python tools/rule_fuzz.py run --corpus target:limits --seed 496 --count 240 --jobs 3 --out limits.json
python tools/rule_fuzz.py report limits.json
```

The report shows which rewrites fired and how many DuckDB checks agreed. It is evidence for the queries and
databases tried; it does not prove every possible LIMIT query is safe. The full reference describes the target's
cases and its limits.
