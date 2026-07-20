"""Diff the payloads of duplicate event_ids where data differs."""

import duckdb
import json

con = duckdb.connect()

ids = [r[0] for r in con.execute("""
    SELECT event_id
    FROM 'data/raw/stripe_events.parquet'
    GROUP BY event_id
    HAVING COUNT(*) > 1 AND COUNT(DISTINCT data) > 1
""").fetchall()]
print("ids with differing payload:", ids, "\n")


def flat(d, prefix=""):
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(flat(v, f"{prefix}.{k}"))
    elif isinstance(d, list):
        for i, v in enumerate(d):
            out.update(flat(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = d
    return out


for eid in ids:
    rows = con.execute(
        "SELECT data FROM 'data/raw/stripe_events.parquet' WHERE event_id = ?",
        [eid],
    ).fetchall()
    versions = [flat(json.loads(r[0])) for r in rows]
    base, other = versions[0], versions[1]
    keys = set(base) | set(other)
    diffs = {k: (base.get(k), other.get(k)) for k in keys if base.get(k) != other.get(k)}
    print(f"=== {eid}: {len(rows)} rows, {len(diffs)} differing fields ===")
    for k, (a, b) in sorted(diffs.items())[:15]:
        print(f"  {k}:\n    v1={a}\n    v2={b}")
    print()