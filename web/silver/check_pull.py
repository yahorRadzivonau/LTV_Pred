"""Integrity checks for the pulled snapshot. Run after pull_web_raw.py.

Checks, per event table:
  - no event_id with MEANINGFUL payload differences (pending_webhooks ignored)
  - benign duplicate ids are allowed (webhook at-least-once delivery), just counted
  - max(created) is strictly before SNAPSHOT (compared in UTC, not local time)
Per dimension table:
  - non-empty

Note: duckdb returns TIMESTAMP in the machine's local timezone, so the snapshot
comparison converts to UTC explicitly. Never compare timestamps as strings.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import duckdb

# Resolve all project-relative paths from the repository root, regardless of
# the working directory configured in PyCharm or the shell.
ROOT = Path(__file__).resolve().parent.parent.parent
os.chdir(ROOT)

SNAPSHOT = datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)

EVENT_TABLES = ["stripe_events", "solidgate_events"]
DIM_TABLES = [
    "stripe_subscriptions",
    "web_conversions",
    "stripe_payments",
    "solidgate_old_transactions",
]

con = duckdb.connect()

for name in EVENT_TABLES:
    n, dup_ids, bad = con.execute(f"""
        WITH d AS (
            SELECT event_id FROM 'data/raw/{name}.parquet'
            GROUP BY event_id HAVING COUNT(*) > 1
        ),
        meaningful AS (
            SELECT e.event_id,
                   COUNT(DISTINCT regexp_replace(
                       e.data, '"pending_webhooks":[0-9]+', '')) AS variants
            FROM 'data/raw/{name}.parquet' e
            JOIN d USING (event_id)
            GROUP BY e.event_id
        )
        SELECT (SELECT COUNT(*) FROM 'data/raw/{name}.parquet'),
               (SELECT COUNT(*) FROM d),
               (SELECT COUNT(*) FROM meaningful WHERE variants > 1)
    """).fetchone()

    max_created = con.execute(
        f"SELECT MAX(created) FROM 'data/raw/{name}.parquet'"
    ).fetchone()[0]
    if max_created.tzinfo is None:  # naive -> treat as UTC
        max_created = max_created.replace(tzinfo=timezone.utc)
    max_utc = max_created.astimezone(timezone.utc)

    assert bad == 0, f"{name}: {bad} event_ids with MEANINGFUL payload diff"
    assert max_utc < SNAPSHOT, f"{name}: event at/after snapshot: {max_utc}"
    print(f"{name}: {n} rows, {dup_ids} benign dup ids, "
          f"max created {max_utc:%Y-%m-%d %H:%M:%S} UTC  OK")

for name in DIM_TABLES:
    n = con.execute(
        f"SELECT COUNT(*) FROM 'data/raw/{name}.parquet'"
    ).fetchone()[0]
    assert n > 0, f"{name}: empty"
    print(f"{name}: {n} rows  OK")

print("\nall checks passed")