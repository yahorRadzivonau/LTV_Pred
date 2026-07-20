"""Pull web raw tables from BigQuery to local parquet, frozen at SNAPSHOT_TS.

Read-only: query (billed ~$0.003 once for event tables) + list_rows (free) for dims.
Rerunning with the same SNAPSHOT_TS reproduces the same dataset.
"""

import json
import pathlib

from google.cloud import bigquery

import pyarrow as pa
import pyarrow.parquet as pq

PROJECT = "web-payment-orchestration"
SNAPSHOT_TS = "2026-07-07T00:00:00Z"  # freeze: only events created before this UTC ts

# Resolve all project-relative paths from the repository root, regardless of
# the working directory configured in PyCharm or the shell.
ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "data" / "raw"
OUT.mkdir(parents=True, exist_ok=True)

# live webhook tables -> pulled via query with created < SNAPSHOT_TS (billed scan, once)
EVENT_TABLES = [
    "prod_web_events.stripe_events",
    "prod_web_events.solidgate_events",
]
# small, near-static dimension tables -> pulled via list_rows (free, not billed)
DIM_TABLES = [
    "prod_web_events.solidgate_old_transactions",
    "prod_silver_layer.stripe_subscriptions",
    "prod_silver_layer.web_conversions",
    "prod_silver_layer.stripe_payments",
]

client = bigquery.Client(project=PROJECT, location="EU")


def save(table: pa.Table, name: str) -> None:
    pq.write_table(table, OUT / f"{name}.parquet", compression="zstd")
    print(f"  saved {name}.parquet: {table.num_rows} rows")


def pull_event_table(full_name: str) -> None:
    name = full_name.split(".")[1]
    print(f"{name}: pulling snapshot < {SNAPSHOT_TS} ...")
    q = f"""
    SELECT event_id, event_type, created, TO_JSON_STRING(data) AS data, api_version
    FROM `{PROJECT}.{full_name}`
    WHERE created < TIMESTAMP('{SNAPSHOT_TS}')
    """
    job = client.query(q, job_config=bigquery.QueryJobConfig(use_query_cache=False))
    save(job.result().to_arrow(), name)


def pull_dim_table(full_name: str) -> None:
    name = full_name.split(".")[1]
    table = client.get_table(f"{PROJECT}.{full_name}")
    print(f"{name}: pulling via list_rows (~{table.num_rows} rows) ...")
    recs = []
    for row in client.list_rows(table, page_size=20_000):
        d = dict(row)
        for k, v in d.items():
            if isinstance(v, (dict, list)):  # JSON columns -> string
                d[k] = json.dumps(v)
        recs.append(d)
    save(pa.Table.from_pylist(recs), name)


if __name__ == "__main__":
    for t in EVENT_TABLES:
        pull_event_table(t)
    for t in DIM_TABLES:
        pull_dim_table(t)
    print("done")