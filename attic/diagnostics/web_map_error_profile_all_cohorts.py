"""
Step 1: error profile of raw iOS-MAP LTV prediction vs fact, across ALL mature
$9.99/week weekly cohorts (not just 2026-05-04..05-10). Diagnostic only -- no
calibration here.

Run: .venv/Scripts/python.exe web_map_error_profile_all_cohorts.py
"""
import numpy as np
import pandas as pd

from compare_map_to_local_sql_style_may_04_10 import (
    load_golden, filter_provider_and_app, subscription_start_table,
    local_paid_events, load_web_matrix, sql_style_summary, visible_at_week_n,
    raw_map_ltv, SNAPSHOT_TS, HMAX, TARGET_STRIPE_PRICE_ID,
)
from models import common, map_model

MIN_FIRST_PAYERS = 15  # below this, per-rebill % is too noisy to read a trend from
MIN_MATURE_REBILL = 3  # need a few mature points to see a trend at all

golden = load_golden()
filtered, _ = filter_provider_and_app(golden)
starts = subscription_start_table(filtered)
starts["cohort_week"] = (
    starts["subscription_start_ts"] - pd.to_timedelta(starts["subscription_start_ts"].dt.weekday, unit="D")
).dt.normalize()

target_subs = pd.Index(filtered.loc[filtered["price_id"].eq(TARGET_STRIPE_PRICE_ID), "subscription_id"].unique())
starts_9_99 = starts[starts["subscription_id"].isin(target_subs)].copy()

paid_all = local_paid_events(filtered, target_subs)  # exact $9.99 payments only, all cohorts at once
web_all = load_web_matrix()
matrix_subs = pd.Index(web_all["sub_id"].dropna().unique(), dtype="string")

mx_ios = common.load_matrix()
apps_info_ios = common.select_apps(mx_ios)
state = map_model.fit(mx_ios, apps_info_ios)

cohort_weeks = sorted(starts_9_99["cohort_week"].unique())
summary_rows = []
err_rows = []

for cw in cohort_weeks:
    cohort_start_subs = pd.Index(starts_9_99.loc[starts_9_99["cohort_week"].eq(cw), "subscription_id"].unique())
    cohort_paid = paid_all[paid_all["subscription_id"].isin(cohort_start_subs)]
    first_payers = pd.Index(cohort_paid.loc[cohort_paid["rebill_number"].eq(0), "subscription_id"].unique(), dtype="string")
    if len(first_payers) < MIN_FIRST_PAYERS:
        summary_rows.append({"cohort_week": cw.date(), "n_start_subs": len(cohort_start_subs),
                              "n_first_payers": len(first_payers), "max_mature_rebill": None, "skipped": "N too small"})
        continue

    common_payers = first_payers.intersection(matrix_subs)
    cohort_age_weeks = int((SNAPSHOT_TS.tz_localize(None) - cw.tz_localize(None)).days // 7)
    max_mature_rebill = max(0, cohort_age_weeks - 2)
    if max_mature_rebill < MIN_MATURE_REBILL:
        summary_rows.append({"cohort_week": cw.date(), "n_start_subs": len(cohort_start_subs),
                              "n_first_payers": len(first_payers), "max_mature_rebill": max_mature_rebill,
                              "skipped": "not mature enough"})
        continue

    common_fact = sql_style_summary(cohort_paid, common_payers, max_mature_rebill)
    cohort_mx = web_all[web_all["sub_id"].isin(common_payers)].copy()
    weeks = max_mature_rebill
    visible = visible_at_week_n(cohort_mx, weeks)
    if visible.empty:
        summary_rows.append({"cohort_week": cw.date(), "n_start_subs": len(cohort_start_subs),
                              "n_first_payers": len(first_payers), "max_mature_rebill": max_mature_rebill,
                              "skipped": "no visible se_training rows"})
        continue
    pred_survival = map_model.predict(state, visible, weeks)
    arpu_by_rebill = common_fact.set_index("rebill_number")["arpu_at_payment"].reindex(range(0, HMAX + 1)).ffill()
    pred_ltv = raw_map_ltv(pred_survival, arpu_by_rebill)

    summary_rows.append({"cohort_week": cw.date(), "n_start_subs": len(cohort_start_subs),
                          "n_first_payers": len(common_payers), "max_mature_rebill": max_mature_rebill, "skipped": None})

    for r in range(0, max_mature_rebill + 1):
        fact_row = common_fact.loc[common_fact["rebill_number"].eq(r)]
        if fact_row.empty:
            continue
        fact_v = float(fact_row["payer_ltv"].iloc[0])
        map_v = float(pred_ltv.loc[r])
        signed_pct = (map_v - fact_v) / fact_v * 100 if fact_v else np.nan
        err_rows.append({"cohort_week": cw.date(), "rebill": r, "fact_$": fact_v, "map_$": map_v, "signed_err_%": signed_pct})

summary_df = pd.DataFrame(summary_rows)
err_df = pd.DataFrame(err_rows)

print("=== Cohort summary (N, maturity, included/skipped) ===")
print(summary_df.to_string(index=False))

print("\n=== err% by cohort_week x rebill (mature cohorts only) ===")
pivot = err_df.pivot(index="cohort_week", columns="rebill", values="signed_err_%")
print(pivot.to_string(float_format=lambda x: f"{x:.1f}" if pd.notna(x) else ""))

included = summary_df[summary_df["skipped"].isna()]
print(f"\nCohorts included in profile: {len(included)}")
print(f"N first-payers per cohort: min={included['n_first_payers'].min()}, "
      f"max={included['n_first_payers'].max()}, median={included['n_first_payers'].median():.0f}")

# Stability check: err% at each common rebill horizon, across cohorts -- print
# spread (std, min, max) so "stable vs scattered" is a measured fact.
print("\n=== Stability of signed_err_% at each rebill, across cohorts (fact, not judged) ===")
stab = err_df.groupby("rebill")["signed_err_%"].agg(["count", "mean", "std", "min", "max"])
print(stab.to_string(float_format=lambda x: f"{x:.2f}"))
