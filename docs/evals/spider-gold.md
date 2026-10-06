# Spider gold queries as rewrite inputs

[Plain-language version](../../docs_simple/evals/spider-gold.md)

Spider 1.0 publishes SQLite gold queries for its dev and train questions. This eval deduplicates identical `(database, query)` pairs, translates each query to BigQuery and back to SQLite, then runs every KumoSQL rewrite rule, the canonical rewrite pipeline, and the proof-gated optimizer. Each rewrite is compared with the round-tripped input, so a translation difference is reported separately.

```bash
python tools/spider_gold_bench.py --set both --split dev --jobs 4
python tools/spider_gold_bench.py --set both --split held-out --jobs 4 --json held-out.json
python tools/spider_gold_bench.py --set both --write-results
python tools/spider_gold_bench.py --show wrong,caught,semantic
python tools/spider_gold_bench.py --check-sources
```

## Source and split

The source repository is [taoyds/spider](https://github.com/taoyds/spider), pinned at `b7b5b8c890cd30e35427348bb9eb8c6d1350ca7c`. The Apache-2.0 repository contains CC BY-SA 4.0 data. Data files are downloaded at runtime, SHA-256 checked, and never committed.

| File | SHA-256 |
|---|---|
| `tables.json` | `61bb20aa401f03164e2d7f3b16509b7b5f79cc9c943ca7bd159046df1159e2ed` |
| `dev.json` | `30d64a3fccde493226df79687aed9e4a1c0129525baf44f29c0573d914d758a4` |
| `train_spider.json` | `c43d0d72e59e1a9e1a60837da9bf70d5a6277226bdb7f634d544f380646f527a` |

The files contain 1,034 dev questions and 7,000 train questions, collapsing to 564 and 3,979 distinct `(database, query)` pairs. A database name is held out when SHA-1 of `spider-gold` plus that name falls in one fifth of the hash space; all queries for that database stay together. The default run reads only development databases. `--split held-out` is reserved for final scoring, and `--write-results` scores both splits.

## What is checked

Each query is tried against every registered rule individually, the canonical rule pipeline, and the proof-gated optimizer. A rewrite is trusted only when KumoSQL's gate accepts it. The benchmark checks outputs on 1,000 random SQLite databases that respect the listed primary and foreign keys; unordered queries also use targeted and bounded counterexample searches, with every found difference replayed in SQLite. Ordered results are compared as lists only when neither query has order ties; otherwise the comparison is tie-safe.

Spider's original SQLite databases are hosted on blocked services, so the checker builds databases from the pinned table names, types, listed keys, and foreign keys. The prover and optimizer receive column names and types but no keys or NOT NULL assumptions. `tables.json` lists only the first column of one composite key, so the generated key restriction is used for valid test databases, not as a proof premise.

The BigQuery round trip is also checked on 200 generated databases and reported separately as `translation`. A translated query that differs is not used as evidence against a KumoSQL rewrite; rewrites are compared with the round-tripped query.

## Results

The generated result row is [`spider-gold.json`](../../benchmarks/results/spider-gold.json). It records the distinct query count, trusted rewrites, observed differences, held-out results, source caveats, and run environment. A trusted rewrite that changes a result on a schema-valid database is wrong. A changed result from an untrusted rewrite is counted as caught by the proof gate. A timeout, crash, unsupported translation, or lack of an applied rewrite is never counted as a proof.

## Limits

- These are queries, not query pairs with published rewrite labels. The metric asks whether KumoSQL's accepted rewrites preserve the translated query on the generated schema-valid databases.
- Spider's real databases are unavailable in this environment. A finite generated-database check is evidence, not a proof; a changed output is replayed and shrunk before it counts as wrong.
- The generator enforces the listed primary and foreign keys, but the prover and optimizer do not assume them.
- SQLite-to-BigQuery-to-SQLite translation can itself change a query's result; that movement is reported separately and is not attributed to the rewrite.
