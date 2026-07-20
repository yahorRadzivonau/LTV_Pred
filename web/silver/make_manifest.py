"""Write the snapshot manifest. Run after check_pull.py passes.

The manifest (committed to the repo) + SNAPSHOT_TS in pull_web_raw.py make the
local dataset reproducible; the parquet files themselves stay out of git.
"""

import datetime
import json
import os
from pathlib import Path

import duckdb

# Resolve all project-relative paths from the repository root, regardless of
# the working directory configured in PyCharm or the shell.
ROOT = Path(__file__).resolve().parent.parent.parent
os.chdir(ROOT)

SNAPSHOT_TS = "2026-07-07T00:00:00Z"

TABLES = [
    "stripe_events",
    "solidgate_events",
    "stripe_subscriptions",
    "web_conversions",
    "stripe_payments",
    "solidgate_old_transactions",
]

con = duckdb.connect()

manifest = {
    "snapshot_ts": SNAPSHOT_TS,
    "pulled_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "tables": {},
}
for name in TABLES:
    n = con.execute(
        f"SELECT COUNT(*) FROM 'data/raw/{name}.parquet'"
    ).fetchone()[0]
    manifest["tables"][name] = n

with open("data/raw/SNAPSHOT_MANIFEST.json", "w") as f:
    json.dump(manifest, f, indent=2)

print(json.dumps(manifest, indent=2))