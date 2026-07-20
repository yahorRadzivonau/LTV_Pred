"""
Path 2 check: does cohort composition (geo/trial_type/channel mix) explain the
2-4x spread in signed_err% across the 11 mature $9.99/week cohorts, or not?
One measurement, not an investigation.

Run: .venv/Scripts/python.exe web_cohort_composition_vs_error.py
"""
import numpy as np
import pandas as pd

from compare_map_to_local_sql_style_may_04_10 import (
    load_golden, filter_provider_and_app, subscription_start_table,
    local_paid_events, load_web_matrix, sql_style_summary, visible_at_week_n,
    raw_map_ltv, SNAPSHOT_TS, HMAX, TARGET_STRIPE_PRICE_ID,
)
from models import common, map_model

MIN_FIRST_PAYERS = 15
MIN_MATURE_REBILL = 3

golden = load_golden()
filtered, _ = filter_provider_and_app(golden)
starts = subscription_start_table(filtered)
starts["cohort_week"] = (
    starts["subscription_start_ts"] - pd.to_timedelta(starts["subscription_start_ts"].dt.weekday, unit="D")
).dt.normalize()

target_subs = pd.Index(filtered.loc[filtered["price_id"].eq(TARGET_STRIPE_PRICE_ID), "subscription_id"].unique())
starts_9_99 = starts[starts["subscription_id"].isin(target_subs)].copy()
paid_all = local_paid_events(filtered, target_subs)
web_all = load_web_matrix()
matrix_subs = pd.Index(web_all["sub_id"].dropna().unique(), dtype="string")

mx_ios = common.load_matrix()
apps_info_ios = common.select_apps(mx_ios)
state = map_model.fit(mx_ios, apps_info_ios)

# first row per subscription for composition fields (same "first event" convention as se_training)
first_row = (
    filtered.sort_values("event_datetime")
    .groupby("subscription_id")
    .first()[["country_code", "trial_type", "media_source"]]
)

cohort_weeks = sorted(starts_9_99["cohort_week"].unique())
rows = []

for cw in cohort_weeks:
    cohort_start_subs = pd.Index(starts_9_99.loc[starts_9_99["cohort_week"].eq(cw), "subscription_id"].unique())
    cohort_paid = paid_all[paid_all["subscription_id"].isin(cohort_start_subs)]
    first_payers = pd.Index(cohort_paid.loc[cohort_paid["rebill_number"].eq(0), "subscription_id"].unique(), dtype="string")
    if len(first_payers) < MIN_FIRST_PAYERS:
        continue
    common_payers = first_payers.intersection(matrix_subs)
    cohort_age_weeks = int((SNAPSHOT_TS.tz_localize(None) - cw.tz_localize(None)).days // 7)
    max_mature_rebill = max(0, cohort_age_weeks - 2)
    if max_mature_rebill < MIN_MATURE_REBILL:
        continue

    common_fact = sql_style_summary(cohort_paid, common_payers, max_mature_rebill)
    cohort_mx = web_all[web_all["sub_id"].isin(common_payers)].copy()
    visible = visible_at_week_n(cohort_mx, max_mature_rebill)
    if visible.empty:
        continue
    pred_survival = map_model.predict(state, visible, max_mature_rebill)
    arpu_by_rebill = common_fact.set_index("rebill_number")["arpu_at_payment"].reindex(range(0, HMAX + 1)).ffill()
    pred_ltv = raw_map_ltv(pred_survival, arpu_by_rebill)

    fact_last = float(common_fact.loc[common_fact["rebill_number"].eq(max_mature_rebill), "payer_ltv"].iloc[0])
    map_last = float(pred_ltv.loc[max_mature_rebill])
    final_err_pct = (map_last - fact_last) / fact_last * 100

    comp = first_row.loc[first_row.index.isin(common_payers)]
    n = len(comp)
    geo_counts = comp["country_code"].value_counts(normalize=True)
    us_share = geo_counts.get("US", 0.0) * 100
    top3_geo = geo_counts.head(3)
    top3_geo_str = ", ".join(f"{g}={v*100:.0f}%" for g, v in top3_geo.items())

    trial_counts = comp["trial_type"].value_counts(normalize=True)
    paid_trial_share = trial_counts.get("paid", 0.0) * 100
    free_trial_share = trial_counts.get("free", 0.0) * 100

    media_counts = comp["media_source"].value_counts(normalize=True)
    top2_media = media_counts.head(2)
    top_channel_share = float(top2_media.iloc[0]) * 100 if len(top2_media) else 0.0
    top2_media_str = ", ".join(f"{m}={v*100:.0f}%" for m, v in top2_media.items())

    rows.append({
        "cohort_week": cw.date(), "N": n, "final_rebill": max_mature_rebill, "final_err_%": final_err_pct,
        "US_%": us_share, "top3_geo": top3_geo_str,
        "paid_trial_%": paid_trial_share, "free_trial_%": free_trial_share,
        "top_channel_%": top_channel_share, "top2_media": top2_media_str,
    })

df = pd.DataFrame(rows).sort_values("final_err_%")
print("=== Composition x error, 11 cohorts, sorted worst -> best ===")
print(df.to_string(index=False, float_format=lambda x: f"{x:.1f}"))

print("\n=== Directional correlation with final_err_% (11 points) ===")
for col in ["US_%", "paid_trial_%", "top_channel_%"]:
    r = np.corrcoef(df["final_err_%"], df[col])[0, 1]
    print(f"  corr(final_err_%, {col}) = {r:+.3f}")
