import sys, json, hashlib, re
sys.path.insert(0, "/home/claude/KumoSQL/src")
from kumosql import view_candidates as vc
from kumosql.joinorder.bench import stats_ceb
repo = "/root/.kumosql-bench/End-to-End-CardEst-Benchmark"
ddl = open(repo + "/datasets/stats_simplified/stats.sql").read()
schema = {}
for m in re.finditer(r"CREATE TABLE (\w+) \((.*?)\);", ddl, re.S):
    cols = [l.strip().split()[0].lower() for l in m.group(2).split(",\n") if l.strip()]
    schema[m.group(1).lower()] = cols
print(schema)
w = stats_ceb.workload(repo)
qs = {f"q{i:03d}": sql.rstrip(";") for i, (c, sql) in enumerate(w)}
def held(name): return int(hashlib.sha256(("split:"+name).encode()).hexdigest(), 16) % 4 == 0
dev = {k: v for k, v in qs.items() if not held(k)}
print(len(dev), "dev")
cands = vc.mine(dev, schema)
print(len(cands), "candidates")
for c in cands[:40]:
    print(len(c.queries), c.shape.tables, c.shape.edges)
