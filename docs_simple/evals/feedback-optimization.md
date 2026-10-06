# Feedback-driven SQL optimization

The source publishes ten query rewrites whose results matched on its generated TPC-H data. KumoSQL checks whether it can prove each rewrite and searches for a database that makes the two queries return different rows.

The source is GPL-3.0. Its SQL is downloaded from a pinned commit at test time and checked by SHA-256; it is not copied into KumoSQL. The download includes the SQL and result summaries, but not the TPC-H database or parameter files. KumoSQL represents each parameter with a scalar value from a synthetic typed table so it can prove and search without choosing fixed parameter values. The finite source checks and missing data limit what can be reproduced. All pairs were inspected during setup, so this run is marked tuned on test.
