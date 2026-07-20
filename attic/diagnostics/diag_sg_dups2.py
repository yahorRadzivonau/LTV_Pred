"""Are Solidgate duplicate-id rows different moments in time, or same-moment noise?"""

import json

import duckdb

con = duckdb.connect()

# 1) time spread inside each duplicated id
print(con.execute("""
    WITH d AS (
        SELECT event_id,
               COUNT(*) AS copies,
               COUNT(DISTINCT created) AS distinct_ts,
               date_diff('second', MIN(created), MAX(created)) AS span_sec
        FROM 'data/raw/solidgate_events.parquet'
        GROUP BY event_id HAVING COUNT(*) > 1
    )
    SELECT
        COUNT(*)                              AS dup_ids,
        COUNTIF(distinct_ts = 1)              AS same_second_only,
        COUNTIF(span_sec BETWEEN 1 AND 3600)  AS within_hour,
        COUNTIF(span_sec > 3600)              AS over_hour,
        MAX(span_sec)                         AS max_span_sec
    FROM d
""").fetchone())

# 2) for one long id: show the chain with a payload field that should evolve
rows = con.execute("""
    WITH d AS (
        SELECT event_id FROM 'data/raw/solidgate_events.parquet'
        GROUP BY event_id HAVING COUNT(*) > 4
        ORDER BY COUNT(*) DESC LIMIT 1
    )
    SELECT e.created, e.data
    FROM 'data/raw/solidgate_events.parquet' e
    JOIN d USING (event_id)
    ORDER BY e.created
""").fetchall()

print(f"\nlongest id: {len(rows)} rows")
for r in rows[:12]:
    d = json.loads(r[1])
    sub = d.get("subscription", {})
    # count orders + latest retry attempt inside payload
    invs = d.get("invoices", {}) or {}
    n_orders = sum(len((inv or {}).get("orders", {}) or {}) for inv in invs.values())
    print(f"  {r[0]} | status={sub.get('status'):11s} | orders_in_payload={n_orders}")