"""
Load the weekly curve CSV into ad_hock_tables.ml_web_weekly_curve.

    .venv/Scripts/python.exe web/v3/upload_weekly_curve.py           # preview, no write
    .venv/Scripts/python.exe web/v3/upload_weekly_curve.py --write   # writes

SAFETY -- this script can only ever touch ONE table.

  * TARGET is a literal constant. Nothing derives it, nothing overrides it, there
    is no CLI argument for it.
  * _assert_safe_target() re-checks the name before every API call and refuses
    anything that is not exactly ml_web_weekly_curve, with an extra explicit
    refusal on any name containing "prediction" so ml_web_predictions cannot be
    reached even by a typo.
  * There is NO delete, drop or recreate path anywhere in this file. The only
    write is a WRITE_TRUNCATE load, which replaces this table's rows and touches
    nothing else in the dataset.
  * The payload schema is compared to the live schema field by field and the job
    aborts on any mismatch rather than letting BigQuery coerce.

KNOWN GAPS IN THE DATA BEING LOADED (owner's call: ship now, fix after)

  1. rebills_ups / rebills_ups_cum / revenue_week / revenue_cum understate the
     upsell by ~7.5%. The model applies the share of base payers who EVER took an
     upsell (21.7%), while the rate measured among people actually alive at a
     checkpoint is ~50% (49.6% at week 2, 49.7% at week 6, n=1204/344). The
     lifetime share is diluted by everyone who churned before week 2 and never
     had the chance.
  2. The observed part counts PAYMENTS, not people -- 0.92% of person-weeks carry
     two base payments, so fact sits ~0.9% above the model's people-based scale
     and there is a small step at the handover. The fix is already in
     build_weekly_curve.py but this CSV predates a rebuild.

  rebills_base and active_share are unaffected by (1); (2) moves them by <1%.
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

TARGET = "appsflyer-data-411716.ad_hock_tables.ml_web_weekly_curve"
ALLOWED_TABLE = "ml_web_weekly_curve"
CLUSTER_BY = ["cohort_date", "funnel"]
WRITE = "--write" in sys.argv

EXPECTED = {
    "cohort_date": "DATE", "funnel": "STRING", "utm_source": "STRING",
    "week": "INTEGER", "n_base_payer": "INTEGER",
    "rebills_base": "FLOAT", "rebills_ups": "FLOAT",
    "rebills_base_cum": "FLOAT", "rebills_ups_cum": "FLOAT",
    "active_share": "FLOAT", "revenue_week": "FLOAT", "revenue_cum": "FLOAT",
    "source": "STRING", "evidence": "STRING",
}


def _assert_safe_target(name: str) -> None:
    """Refuse anything that is not the weekly-curve table. Called before every API call."""
    table = name.rsplit(".", 1)[-1]
    if "prediction" in name.lower():
        raise SystemExit(f"REFUSING: '{name}' looks like the predictions table. This script only writes {ALLOWED_TABLE}.")
    if table != ALLOWED_TABLE:
        raise SystemExit(f"REFUSING: target must be exactly {ALLOWED_TABLE}, got '{table}'.")


def build_payload() -> pd.DataFrame:
    src = sorted(Path(OUT_DIR).glob("weekly_curve_v3_*.csv"))[-1]
    df = pd.read_csv(src)
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
    print(f"\n  cells {payload.groupby(['cohort_date','funnel','utm_source']).ngroups} "
          f"| weeks {payload['week'].min()}..{payload['week'].max()}")
    print(f"  source: {payload['source'].value_counts().to_dict()}")

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
