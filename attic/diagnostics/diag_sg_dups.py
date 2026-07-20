"""Diagnose duplicate event_id semantics in solidgate_events snapshot."""

import json

import duckdb

con = duckdb.connect()

# 1) scale: how many rows per duplicated id, and which event_types
print(con.execute("""
    WITH d AS (
        SELECT event_id, COUNT(*) AS copies
        FROM 'data/raw/solidgate_events.parquet'
        GROUP BY event_id HAVING COUNT(*) > 1
    )
    SELECT MIN(copies), MAX(copies), AVG(copies), COUNT(*) AS dup_ids FROM d
""").fetchone())

print(con.execute("""
    WITH d AS (
        SELECT event_id FROM 'data/raw/solidgate_events.parquet'
        GROUP BY event_id HAVING COUNT(*) > 1
    )
    SELECT e.event_type, COUNT(*) AS n
    FROM 'data/raw/solidgate_events.parquet' e
    JOIN d USING (event_id)
    GROUP BY e.event_type ORDER BY n DESC
""").fetchall())

# 2) is event_id actually the subscription id or order id?
rows = con.execute("""
    WITH d AS (
        SELECT event_id FROM 'data/raw/solidgate_events.parquet'
        GROUP BY event_id HAVING COUNT(*) > 1
        LIMIT 1
    )
    SELECT e.event_id, e.event_type, e.created, e.data
    FROM 'data/raw/solidgate_events.parquet' e
    JOIN d USING (event_id)
    ORDER BY e.created
""").fetchall()

eid = rows[0][0]
sub_id = json.loads(rows[0][3]).get("subscription", {}).get("id")
print(f"\nexample event_id: {eid}")
print(f"subscription.id in payload: {sub_id}")
print(f"event_id == subscription.id?  {eid == sub_id}")

print(f"\nchain of {len(rows)} rows for this id:")
for r in rows:
    d = json.loads(r[3])
    sub = d.get("subscription", {})
    print(f"  {r[2]} | {r[1]:22s} | status={sub.get('status')} "
          f"| cancel={sub.get('cancel_message')}")