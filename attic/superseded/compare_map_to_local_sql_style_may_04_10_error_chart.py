"""
Fact (SQL-style, real payments) vs raw (unanchored) MAP prediction on the exact
same 92 common-cohort subscribers (2026-05-04..05-10, $9.99/week, first payers).
Raw MAP (not scaled/anchored to fact) is used so the mature-range table shows the
model's genuine error, not a forced match at one point.

Run: .venv/Scripts/python.exe compare_map_to_local_sql_style_may_04_10_error_chart.py
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from compare_map_to_local_sql_style_may_04_10 import (
    load_golden, filter_provider_and_app, subscription_start_table,
    choose_price_cohort, local_paid_events, load_web_matrix, sql_style_summary,
    visible_at_week_n, raw_map_ltv,
    COHORT_START, COHORT_END_EXCLUSIVE, COHORT_WEEK, SNAPSHOT_TS, HMAX,
)
from models import common, map_model

golden = load_golden()
filtered, _ = filter_provider_and_app(golden)
starts = subscription_start_table(filtered)
cohort_starts = starts[
    starts["subscription_start_ts"].ge(COHORT_START) & starts["subscription_start_ts"].lt(COHORT_END_EXCLUSIVE)
].copy()
cohort_start_subs = pd.Index(cohort_starts["subscription_id"].unique())
price_subs, _, _ = choose_price_cohort(filtered, cohort_start_subs)
paid = local_paid_events(filtered, price_subs)
first_payers = pd.Index(paid.loc[paid["rebill_number"].eq(0), "subscription_id"].unique(), dtype="string")

web_all = load_web_matrix()
matrix_subs = pd.Index(web_all["sub_id"].dropna().unique(), dtype="string")
common_payers = first_payers.intersection(matrix_subs)
print(f"Common cohort: {len(common_payers)} subs (verified 100% overlap in prior step)")

cohort_age_weeks = int((SNAPSHOT_TS.tz_localize(None) - COHORT_WEEK).days // 7)
max_mature_rebill = max(0, cohort_age_weeks - 2)
print(f"cohort_age_weeks={cohort_age_weeks}, max_mature_rebill={max_mature_rebill}")

common_fact = sql_style_summary(paid, common_payers, max_mature_rebill)

# Sanity: reproduce colleague's exact anchors before trusting anything downstream.
fact0 = common_fact.loc[common_fact["rebill_number"].eq(0), "payer_ltv"].iloc[0]
fact7 = common_fact.loc[common_fact["rebill_number"].eq(7), "payer_ltv"].iloc[0]
print(f"\nAnchor check: rebill0 fact=${fact0:.2f} (colleague: $9.99), rebill7 fact=${fact7:.2f} (colleague: $55.16)")
if abs(fact0 - 9.99) > 0.01 or abs(fact7 - 55.16) > 0.01:
    raise SystemExit("STOP: fact anchors do not match colleague's numbers -- payment definition mismatch, not proceeding.")
print("Anchors match exactly -- proceeding.")

# Raw (unanchored) MAP prediction, fed the cohort's own max mature visible history.
mx_ios = common.load_matrix()
apps_info_ios = common.select_apps(mx_ios)
state = map_model.fit(mx_ios, apps_info_ios)

cohort_mx = web_all[web_all["sub_id"].isin(common_payers)].copy()
weeks = max_mature_rebill
visible = visible_at_week_n(cohort_mx, weeks)
pred_survival = map_model.predict(state, visible, weeks)

arpu_by_rebill = common_fact.set_index("rebill_number")["arpu_at_payment"]
arpu_by_rebill = arpu_by_rebill.reindex(range(0, HMAX + 1)).ffill()
pred_ltv = raw_map_ltv(pred_survival, arpu_by_rebill)

# Error table on the mature range only.
rows = []
for r in range(0, max_mature_rebill + 1):
    fact_v = common_fact.loc[common_fact["rebill_number"].eq(r), "payer_ltv"].iloc[0]
    map_v = float(pred_ltv.loc[r])
    signed_err = map_v - fact_v
    signed_pct = signed_err / fact_v * 100 if fact_v else np.nan
    rows.append({"rebill": r, "fact_$": fact_v, "map_$": map_v,
                 "signed_err_$": signed_err, "signed_err_%": signed_pct})
err_table = pd.DataFrame(rows)
print("\n=== Error table, mature range (rebill 0.." + str(max_mature_rebill) + ") ===")
print(err_table.to_string(index=False, float_format=lambda x: f"{x:.2f}"))

# Plot.
fig, ax = plt.subplots(figsize=(11, 7.5))
fact_plot = common_fact[common_fact["rebill_number"].le(max_mature_rebill)]
ax.plot(fact_plot["rebill_number"], fact_plot["payer_ltv"], color="black", linewidth=2.5,
        marker="o", label="Fact LTV (real payments)", zorder=5)

map_x = list(range(0, HMAX + 1))
map_y = [float(pred_ltv.loc[r]) for r in map_x]
ax.plot(map_x, map_y, color="#C0392B", linewidth=1.8, linestyle="--",
        label=f"MAP prediction (raw, fed {weeks} weeks)", zorder=4)

ax.axvline(max_mature_rebill, color="grey", linestyle=":", linewidth=1.2)
ax.text(max_mature_rebill + 0.5, ax.get_ylim()[1] * 0.05,
        "дальше — экстраполяция, не факт", rotation=90, fontsize=9, color="grey", va="bottom")

ax.set_xlabel("Rebill number (0 = first payment)")
ax.set_ylabel("Cumulative LTV per subscriber (USD)")
ax.set_xlim(0, HMAX)
ax.grid(True, alpha=0.3)
ax.legend(fontsize=9, loc="upper left")
ax.set_title(f"Cohort 2026-05-04..05-10, $9.99/week, N={len(common_payers)} common subscribers\n"
             f"Fact vs raw MAP prediction (no anchoring/scaling)")
fig.tight_layout()

OUT = "reports/web_model/02_map_vs_sql_cohort/map_to_local_sql_style_may_04_10_error_chart.png"
fig.savefig(OUT, dpi=160, bbox_inches="tight")
print(f"\nwrote {OUT}")
