"""Diagnose duplicate event_id rows in stripe_events snapshot."""

import duckdb

con = duckdb.connect()

# how many ids, and are payloads identical within each id?
full, payload_diff = con.execute("""
    WITH d AS (
        SELECT event_id
        FROM 'data/raw/stripe_events.parquet'
        GROUP BY event_id
        HAVING COUNT(*) > 1
    )
    SELECT
        (SELECT COUNT(*) FROM d) AS dup_ids,
        (SELECT COUNT(*) FROM (
            SELECT e.event_id
            FROM 'data/raw/stripe_events.parquet' e
            JOIN d USING (event_id)
            GROUP BY e.event_id
            HAVING COUNT(DISTINCT e.data) > 1
                OR COUNT(DISTINCT e.event_type) > 1
                OR COUNT(DISTINCT e.created) > 1
        )) AS ids_with_differing_payload
""").fetchone()
print(f"duplicate ids: {full}, of which payload differs: {payload_diff}")

# show a couple of examples either way
rows = con.execute("""
    WITH d AS (
        SELECT event_id FROM 'data/raw/stripe_events.parquet'
        GROUP BY event_id HAVING COUNT(*) > 1
        LIMIT 3
    )
    SELECT e.event_id, e.event_type, e.created,
           substr(e.data, 1, 80) AS data_head
    FROM 'data/raw/stripe_events.parquet' e
    JOIN d USING (event_id)
    ORDER BY e.event_id, e.created
""").fetchall()
for r in rows:
    print(r)