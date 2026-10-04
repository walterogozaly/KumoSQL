"""Work-in-progress sweep: every SQL string in tests/fixtures/<dir> read under a dialect through kumosql.parse_check.

Usage: python tools/parse_check_sweep.py spider2:bigquery googlesql:bigquery qed:postgres  (SHOW=n prints n disagreements).
Held-out files and rows (new/held_out/split markers) are skipped and never printed.
"""
import gzip, json, sys, re, collections, glob, os, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src"))
from kumosql import parse_check as pc

ROOT = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "tests" / "fixtures")
SQLISH = re.compile(r"^\s*\(*\s*(SELECT|WITH)\b", re.I)

HELD = [0]
def held_out(obj):
    return isinstance(obj, dict) and (obj.get("new") is True or obj.get("held_out") is True or obj.get("heldout") is True
        or str(obj.get("split", "")).lower().replace("-", "_") in ("held_out", "heldout", "test"))

def strings(obj):
    if held_out(obj):
        HELD[0] += 1
        return
    if isinstance(obj, str):
        if SQLISH.match(obj) and len(obj) < 50000:
            yield obj
    elif isinstance(obj, dict):
        for v in obj.values(): yield from strings(v)
    elif isinstance(obj, list):
        for v in obj: yield from strings(v)

def load(path):
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt", encoding="utf-8") as f:
        text = f.read()
    if path.endswith(".sql"):
        yield text; return
    if ".jsonl" in path:
        for line in text.splitlines():
            if line.strip():
                try: yield from strings(json.loads(line))
                except Exception: pass
        return
    try: yield from strings(json.loads(text))
    except Exception: return

def sweep(dirname, dialect, show=8):
    files = [p for p in glob.glob(f"{ROOT}/{dirname}/**/*", recursive=True) if os.path.isfile(p) and re.search(r"\.(json|jsonl|json\.gz|jsonl\.gz|sql)$", p)
             and not re.search(r"held|test_split", p, re.I)]
    seen = set(); stats = collections.Counter(); notes = collections.Counter(); dis = []
    t0 = time.time()
    for f in files:
        for s in load(f):
            if s in seen: continue
            seen.add(s)
            r = pc.check_query(s, dialect)
            stats[r.status] += 1
            if r.status == "unchecked": notes[re.sub(r"'[^']*'|\"[^\"]*\"|\d+", "_", r.note)[:70]] += 1
            if r.status == "disagree": dis.append((s, r.reasons))
    print(f"== {dirname} [{dialect}] {dict(stats)} in {time.time()-t0:.1f}s")
    for n, c in notes.most_common(12): print(f"   unchecked {c:5d}  {n}")
    for s, rs in dis[:show]:
        print("   DIS:", " ".join(s.split())[:300]); 
        for r in rs[:3]: print("        ", r[:250])
    return dis

if __name__ == "__main__":
    for arg in sys.argv[1:]:
        d, dialect = arg.split(":")
        sweep(d, dialect, show=int(os.environ.get("SHOW", 8)))
