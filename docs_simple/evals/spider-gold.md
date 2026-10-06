# Spider gold query rewrites

Spider publishes thousands of gold SQLite queries. This eval checks whether KumoSQL's accepted rewrites keep those queries' results the same.

The files are downloaded from a pinned Spider revision and checked against their saved hashes. The data is not checked into KumoSQL. Identical `(database, query)` pairs are counted once. One database in five is held out by a stable hash, so queries for the same database stay in one split.

The checker translates each query from SQLite to BigQuery and back, then runs KumoSQL's individual rewrite rules, the normal rewrite pipeline and the proof-gated optimizer. It tests changed queries on generated SQLite databases that satisfy Spider's listed keys. The prover and optimizer are not given those keys. Ordered results are compared only when ties cannot change the answer.

Spider's real databases are hosted on services unavailable here. A generated database can demonstrate a difference, but matching results on generated examples do not prove equivalence. The BigQuery translation check is reported separately from rewrite results.

The exact counts, run date and environment are in [`spider-gold.json`](../../benchmarks/results/spider-gold.json). A trusted rewrite that changes results is counted as wrong; an untrusted rewrite that changes results shows the safety gate caught it. Held-out databases are scored once after the development run is settled.
