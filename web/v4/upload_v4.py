"""
Push the v3 table C into the existing BigQuery predictions table.

The table's 48-column schema is NOT changed -- v3 fills every one of them. The
legacy "without upsell" columns are computed (same curve, upsell share set to
zero), not zero-filled, so nothing in the table is a placeholder.

    .venv/Scripts/python.exe web/v4/upload_v4.py            # preview only, no write
    .venv/Scripts/python.exe web/v4/upload_v4.py --write    # actually writes

WRITE_TRUNCATE replaces the whole table. The current contents are v2 numbers
carrying the known per_attributed inflation, so replacing them is the point --
but it is not reversible, hence the explicit flag plus a yes/no prompt.

============================ SEMANTIC CHANGES UNDER UNCHANGED COLUMN NAMES

These three columns keep their name and change their meaning. Anything reading
them needs to know:

  n_attributed   was: v2's attributed population, whose definition drifted until
                      it silently contained only payers (the regression this
                      whole iteration fixes)
                 now: the per-trial population -- everyone who started a trial of
                      any kind, plus everyone who bought the base plan

  payer_rate     was: share with any revenue at all, trial-only people included
                 now: conversion to the $9.99/week plan, which is the number that
                      actually drives unit economics

  ltv_{N}_ups_projection_quality
                 was: 'fact' / 'rough_low_data' about the upsell projection
                 now: 'web_data' up to step 8, 'ios_tail_extrapolation' past it --
                      i.e. where the forecast stops being measurement. Weeks
                      26/52/104 are always on the borrowed side.

============================ COLUMN MAP

  cohort_date                      <- cohort_date
  funnel                           <- first_funnel
  utm_source                       <- utm_source
  n_attributed                     <- n_trial                     (see above)
  n_payers                         <- n_base_payer
  payer_rate                       <- conversion                  (see above)
  avg_age_weeks, low_n             <- same
  ltv_per_attributed_current       <- ltv_per_trial_current_base       (base only)
  ltv_per_attributed_current_ups   <- ltv_per_trial_current            (with upsell)
  ltv_per_payer_current            <- ltv_per_base_payer_current_base
  ltv_per_payer_current_ups        <- ltv_per_base_payer_current
  per horizon N in 4/12/26/52/104:
    ltv_per_attributed_{N}                 <- ltv_per_trial_{N}_base
    ltv_per_attributed_{N}_ups_projected   <- ltv_per_trial_{N}
    ltv_per_payer_{N}                      <- ltv_per_base_payer_{N}_base
    ltv_per_payer_{N}_ups_projected        <- ltv_per_base_payer_{N}
    ltv_{N}_source                         <- ltv_{N}_source        fact/model
    ltv_{N}_n_mature                       <- ltv_{N}_n_mature
    ltv_{N}_ups_projection_quality         <- ltv_{N}_evidence      (see above)
  snapshot_date                    <- today
"""
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from google.cloud import bigquery

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
os.chdir(ROOT)

from ltv_v4.config import OUT_DIR, BQ_PROJECT  # noqa: E402

TARGET = "appsflyer-data-411716.ad_hock_tables.ml_web_predictions"
HORIZONS = [4, 12, 26, 52, 104]
# Preferred clustering, used only as a reference to report against. The load
# always FOLLOWS whatever the live table is defined with -- see the note in
# main(). Clustering makes date filters cheaper; it does NOT sort SELECT *,
# which was verified the hard way on this very table.
CLUSTER_BY = ["cohort_date", "funnel"]
WRITE = "--write" in sys.argv


def build_payload() -> pd.DataFrame:
    src = sorted(Path(OUT_DIR).glob("table_C_v3_*.csv"))[-1]
    t = pd.read_csv(src)
    print(f"source: {src}  ({len(t)} rows)")

    out = pd.DataFrame({
        "cohort_date": pd.to_datetime(t["cohort_date"]).dt.date,
        "funnel": t["first_funnel"].astype(str),
        "utm_source": t["utm_source"].astype(str),
        "n_attributed": t["n_trial"].astype(int),
        "n_payers": t["n_base_payer"].astype(int),
        "payer_rate": t["conversion"].astype(float),
        "avg_age_weeks": t["avg_age_weeks"].astype(int),
        "low_n": t["low_n"].astype(bool),
        "ltv_per_attributed_current": t["ltv_per_trial_current_base"].astype(float),
        "ltv_per_attributed_current_ups": t["ltv_per_trial_current"].astype(float),
        "ltv_per_payer_current": t["ltv_per_base_payer_current_base"].astype(float),
        "ltv_per_payer_current_ups": t["ltv_per_base_payer_current"].astype(float),
    })

    for N in HORIZONS:
        out[f"ltv_per_attributed_{N}"] = t[f"ltv_per_trial_{N}_base"].astype(float)
        out[f"ltv_per_attributed_{N}_ups_projected"] = t[f"ltv_per_trial_{N}"].astype(float)
        out[f"ltv_per_payer_{N}"] = t[f"ltv_per_base_payer_{N}_base"].astype(float)
        out[f"ltv_per_payer_{N}_ups_projected"] = t[f"ltv_per_base_payer_{N}"].astype(float)
        out[f"ltv_{N}_source"] = t[f"ltv_{N}_source"].astype(str)
        out[f"ltv_{N}_n_mature"] = t[f"ltv_{N}_n_mature"].astype(int)
        out[f"ltv_{N}_ups_projection_quality"] = t[f"ltv_{N}_evidence"].astype(str)

    out["snapshot_date"] = date.today()
    return out


def main():
    client = bigquery.Client(project=BQ_PROJECT)
    table = client.get_table(TARGET)
    schema_names = [f.name for f in table.schema]

    payload = build_payload()

    missing = [c for c in schema_names if c not in payload.columns]
    extra = [c for c in payload.columns if c not in schema_names]
    print(f"\ntarget: {TARGET}")
    print(f"  currently: {table.num_rows} rows, modified {table.modified}")
    print(f"  schema columns: {len(schema_names)} | payload columns: {len(payload.columns)}")
    print(f"  schema columns NOT filled: {missing or 'none'}")
    print(f"  payload columns NOT in schema: {extra or 'none'}")
    if missing or extra:
        raise SystemExit("STOP: payload does not match the table schema exactly.")

    payload = payload[schema_names]

    print("\n--- preview: 3 rows, key columns ---")
    cols = ["cohort_date", "funnel", "utm_source", "n_attributed", "n_payers", "payer_rate",
            "ltv_per_payer_52", "ltv_per_payer_52_ups_projected", "ltv_52_source",
            "ltv_52_n_mature", "ltv_52_ups_projection_quality"]
    print(payload[cols].head(3).to_string(index=False))

    print("\n--- totals ---")
    print(f"  rows {len(payload)} | attributed {int(payload['n_attributed'].sum())} "
          f"| payers {int(payload['n_payers'].sum())}")
    for N in HORIZONS:
        q = payload[f"ltv_{N}_ups_projection_quality"].value_counts().to_dict()
        s = payload[f"ltv_{N}_source"].value_counts().to_dict()
        print(f"  wk{N:3d}: source={s} evidence={q}")

    if not WRITE:
        print("\nPREVIEW ONLY -- nothing written. Re-run with --write to upload.")
        return

    print(f"\n!!! WRITE_TRUNCATE replaces all {table.num_rows} existing rows (v2 numbers). "
          f"This is not reversible.")
    if input("Type 'yes' to proceed: ").strip().lower() != "yes":
        raise SystemExit("Cancelled.")

    # MATCH the table's existing clustering rather than imposing CLUSTER_BY.
    # A load job whose spec differs from the table's is rejected outright
    # ("Incompatible table partitioning specification"), and the direction of the
    # mismatch does not matter -- adding clustering to an unclustered table fails
    # exactly like dropping it would. Passing through whatever the table already
    # has keeps this script working no matter how the table was last defined, and
    # leaves the clustering decision where it belongs: in terraform.
    existing = list(table.clustering_fields) if table.clustering_fields else None
    if existing != CLUSTER_BY:
        print(f"  note: table clusters on {existing}, script default is {CLUSTER_BY} — "
              f"following the table, not the default")
    job = client.load_table_from_dataframe(
        payload, TARGET,
        job_config=bigquery.LoadJobConfig(
            write_disposition="WRITE_TRUNCATE",
            schema=table.schema,
            clustering_fields=existing,
        ),
    )
    job.result()
    after = client.get_table(TARGET)
    print(f"done: {after.num_rows} rows, modified {after.modified}")


if __name__ == "__main__":
    main()
