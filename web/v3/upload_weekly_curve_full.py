"""
Load the full-breakdown weekly-curve CSV into ad_hock_tables.ml_web_weekly_curve_full.

    .venv/Scripts/python.exe web/v3/upload_weekly_curve_full.py           # preview, no write
    .venv/Scripts/python.exe web/v3/upload_weekly_curve_full.py --write   # writes

SAFETY -- this script can only ever touch ONE table (same doctrine as
web/v3/upload_weekly_curve.py, which is why each table gets its own uploader
instead of one script with a table argument):

  * TARGET is a literal constant. Nothing derives it, nothing overrides it,
    there is no CLI argument for it.
  * _assert_safe_target() re-checks the name before every API call and refuses
    anything that is not exactly ml_web_weekly_curve_full, with an extra
    explicit refusal on any name containing "prediction".
  * No delete, drop or recreate path exists in this file. The only write is a
    WRITE_TRUNCATE load, which replaces this table's rows and nothing else.
  * The payload schema is compared to the live schema field by field and the
    job aborts on any mismatch rather than letting BigQuery coerce.
"""
import os
import sys
from pathlib import Path

import pandas as pd
from google.cloud import bigquery

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
os.chdir(ROOT)

from ltv_v3.config import OUT_DIR, BQ_PROJECT  # noqa: E402

TARGET = "appsflyer-data-411716.ad_hock_tables.ml_web_weekly_curve_full"
ALLOWED_TABLE = "ml_web_weekly_curve_full"
CSV_GLOB = "weekly_curve_full_v3_*.csv"
CLUSTER_BY = ["cohort_date", "funnel", "campaign_name"]
WRITE = "--write" in sys.argv

DIM_COLS = ["cohort_date", "funnel", "utm_source", "country", "campaign_name", "ad_name"]

EXPECTED = {
    "cohort_date": "DATE", "funnel": "STRING", "utm_source": "STRING",
    "country": "STRING", "campaign_name": "STRING", "ad_name": "STRING",
    "week": "INTEGER", "n_base_payer": "INTEGER", "sample_grade": "STRING",
    "rebills_base": "FLOAT", "rebills_ups": "FLOAT",
    "rebills_base_cum": "FLOAT", "rebills_ups_cum": "FLOAT",
    "active_share": "FLOAT", "revenue_week": "FLOAT", "revenue_cum": "FLOAT",
    "source": "STRING", "evidence": "STRING",
}


def _assert_safe_target(name: str) -> None:
    """Refuse anything that is not the full-breakdown weekly-curve table."""
    table = name.rsplit(".", 1)[-1]
    if "prediction" in name.lower():
        raise SystemExit(f"REFUSING: '{name}' looks like the predictions table. This script only writes {ALLOWED_TABLE}.")
    if table != ALLOWED_TABLE:
        raise SystemExit(f"REFUSING: target must be exactly {ALLOWED_TABLE}, got '{table}'.")


def build_payload() -> pd.DataFrame:
    src = sorted(Path(OUT_DIR).glob(CSV_GLOB))[-1]
    # keep_default_na=False: dimension values that look like NA-sentinels must
    # survive the round-trip verbatim (the geo table's 'NA' = Namibia was the
    # live example). The CSV holds no true NaN -- every dimension is
    # fallback-filled at build time.
    df = pd.read_csv(src, keep_default_na=False, na_values=[])
    print(f"source: {src}  ({len(df)} rows, {len(df.columns)} cols)")

    df["cohort_date"] = pd.to_datetime(df["cohort_date"]).dt.date
    for c, t in EXPECTED.items():
        if t == "INTEGER":
            df[c] = df[c].astype(int)
        elif t == "FLOAT":
            df[c] = df[c].astype(float)
        elif t == "STRING":
            df[c] = df[c].astype(str)
    return df[list(EXPECTED)]


def main():
    _assert_safe_target(TARGET)
    client = bigquery.Client(project=BQ_PROJECT)
    table = client.get_table(TARGET)
    _assert_safe_target(table.full_table_id.replace(":", "."))

    payload = build_payload()
    live = {f.name: (f.field_type, f.mode) for f in table.schema}

    missing = [c for c in live if c not in payload.columns]
    extra = [c for c in payload.columns if c not in live]
    wrong = [(c, live[c][0], t) for c, t in EXPECTED.items() if c in live and live[c][0] != t]

    print(f"\ntarget: {TARGET}")
    print(f"  currently: {table.num_rows} rows | clustering {table.clustering_fields}")
    print(f"  schema {len(live)} cols | payload {len(payload.columns)} cols")
    print(f"  missing {missing or 'none'} | extra {extra or 'none'} | type mismatch {wrong or 'none'}")
    if missing or extra or wrong:
        raise SystemExit("STOP: schema mismatch, nothing written.")
    if table.clustering_fields and list(table.clustering_fields) != CLUSTER_BY:
        raise SystemExit(f"STOP: table clusters on {table.clustering_fields}, script expects {CLUSTER_BY}.")

    print("\n--- preview ---")
    print(payload.head(3).to_string(index=False))
    print(f"\n  cells {payload.groupby(DIM_COLS).ngroups} "
          f"| weeks {payload['week'].min()}..{payload['week'].max()}")
    print(f"  source: {payload['source'].value_counts().to_dict()}")
    print(f"  sample_grade: {payload[payload['week'] == 0]['sample_grade'].value_counts().to_dict()}")

    if not WRITE:
        print("\nPREVIEW ONLY -- nothing written. Re-run with --write.")
        return

    print(f"\nWRITE_TRUNCATE into {ALLOWED_TABLE} ({table.num_rows} existing rows). "
          f"No other table is touched.")
    if input("Type 'yes' to proceed: ").strip().lower() != "yes":
        raise SystemExit("Cancelled.")

    _assert_safe_target(TARGET)
    job = client.load_table_from_dataframe(
        payload, TARGET,
        job_config=bigquery.LoadJobConfig(
            write_disposition="WRITE_TRUNCATE",
            schema=table.schema,
            clustering_fields=CLUSTER_BY,
        ),
    )
    job.result()
    after = client.get_table(TARGET)
    print(f"done: {after.num_rows} rows | clustering {after.clustering_fields}")


if __name__ == "__main__":
    main()
