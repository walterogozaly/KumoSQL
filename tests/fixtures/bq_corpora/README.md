# Open-source BigQuery projects

Real Dataform projects and BigQuery SQL for `tools/bq_corpus_bench.py` ([docs](../../../docs/evals/bq-real-corpora.md)).

`sources.json` lists each project's repository, pinned commit, licence and the paths kept. Each folder holds that
project's own `LICENSE`. Refresh with `python tools/fetch_bq_corpora.py`. The files are copied unchanged; the only
change is that the `basedosdados` files are renamed after their folders. A project can be one folder of a larger repository (`security-analytics` is `dataform/`; `bqutils-views` and `bqutils-datavault` are two folders of bigquery-utils), with the repository's licence copied in.
