"""
LTV prediction for the June 1-26 2026 start cohort specifically (invinci, golden_all
base: non-QA, non-fraud). Same method as web_baseline_ltv_chart.py: frozen iOS MAP
survival curve (no web calibration) x empirical ARPU by payment step, walk-forward
vintages anchored at the cohort's own fact.

Run: .venv/Scripts/python.exe web_baseline_june_cohort_chart.py
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from models import common, map_model

HMAX = 52
WEEKS_FED = [1, 2, 4]  # cohort max weeks_obs ~5, wk8 not feedable
FACT_CUTOFF = 4  # n_mature: wk1=1682, wk2=1068, wk3=447, wk4=204, wk5=2 (unusable)
COLORS = {1: "#85C1E9", 2: "#3498DB", 4: "#1B4F72"}
COHORT_START, COHORT_END = "2026-06-01", "2026-06-26"

golden = pd.read_parquet("data/golden/golden_all.parquet")
cohort_subs = golden.loc[
    (golden["install_date"] >= COHORT_START) & (golden["install_date"] <= COHORT_END),
    "subscription_id",
].dropna().unique()
print(f"Cohort {COHORT_START}..{COHORT_END}: {len(cohort_subs)} subscriptions")

se_all = pd.read_parquet("data/golden/golden_all_se_training.parquet")
mx = se_all[se_all["sub_id"].isin(cohort_subs)].copy()
mx["survived"] = mx["outcome"].isin(["renewed", "recovered"]).astype(int)
mx["died"] = 1 - mx["survived"]
print(f"se_training rows for cohort: {len(mx)}, distinct sub_id: {mx['sub_id'].nunique()}")

# ARPU by step_k, empirical, from this cohort's own PAID events (golden has price_amount)
paid = golden[
    (golden["subscription_id"].isin(cohort_subs)) &
    (golden["event_name"].isin(["trial_converted", "subscription_renewed"]))
].copy()
paid["event_datetime"] = pd.to_datetime(paid["event_datetime"], utc=True)
merged = mx.merge(
    paid[["subscription_id", "event_datetime", "price_amount"]],
    left_on=["sub_id", "pay_ts"], right_on=["subscription_id", "event_datetime"], how="left",
)
arpu = merged.groupby("step_k")["price_amount"].mean()
arpu = arpu.reindex(range(1, HMAX + 1)).ffill()
print(f"ARPU by step (1-6): {arpu.head(6).to_dict()}")

mx_ios = common.load_matrix()
apps_info_ios = common.select_apps(mx_ios)
state_ios = map_model.fit(mx_ios, apps_info_ios)


def visible_at_week_n(mx, n):
    first_pay = mx.groupby("sub_id")["pay_ts"].transform("min")
    age_weeks = (mx["pay_ts"] - first_pay).dt.total_seconds() / (7 * 86400)
    eligible_subs = mx.loc[mx["weeks_obs"] >= n, "sub_id"].unique()
    visible = mx[(mx["sub_id"].isin(eligible_subs)) & (age_weeks < n)].copy()
    visible["weeks_obs"] = n
    return visible


def ltv_curve(surv_series, weeks_range):
    cum = 0.0
    out = {}
    for w in weeks_range:
        s = surv_series.get(w)
        a = arpu.get(w, np.nan)
        if s is not None and pd.notna(s) and pd.notna(a):
            cum += s * a
        out[w] = cum
    return out


fact_surv, mature, _ = common.direct_survival(mx)
fact_ltv = ltv_curve(fact_surv, range(1, FACT_CUTOFF + 1))

fig, ax = plt.subplots(figsize=(10, 7))
fact_x = list(fact_ltv.keys())
fact_y = list(fact_ltv.values())
ax.plot(fact_x, fact_y, color="black", linewidth=2.5, label="Fact LTV (empirical)", zorder=5)

final_preds = {}
for n in WEEKS_FED:
    anchor_fact = fact_ltv.get(n)
    if anchor_fact is None:
        continue
    visible = visible_at_week_n(mx, n)
    if len(visible) == 0:
        print(f"  wk{n}: 0 eligible subs, skipped")
        continue
    pred_surv = map_model.predict(state_ios, visible, n)
    model_ltv = ltv_curve(pred_surv, range(1, HMAX + 1))
    model_at_n = model_ltv.get(n)
    if not model_at_n:
        continue
    scale = anchor_fact / model_at_n
    xs = list(range(n, HMAX + 1))
    ys = [model_ltv[w] * scale for w in xs]
    ax.plot(xs, ys, color=COLORS[n], linewidth=1.8, linestyle="--",
             label=f"Model LTV, fed wk1-{n} (n_mature={mature.get(n)})", zorder=4)
    ax.scatter([n], [anchor_fact], color=COLORS[n], zorder=6, s=30)
    final_preds[n] = ys[-1]
    print(f"  wk{n} vintage: predicted LTV @ wk52 = ${ys[-1]:.2f}")

n_last = mature.get(FACT_CUTOFF, 0)
ax.annotate(f"n_mature={n_last}\n(fact ends here)\n${fact_y[-1]:.2f}",
            xy=(FACT_CUTOFF, fact_y[-1]),
            xytext=(FACT_CUTOFF + 4, fact_y[-1] * 0.6),
            fontsize=9, fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="grey", alpha=0.9),
            arrowprops=dict(arrowstyle="->", color="grey", lw=1))
ax.axvline(FACT_CUTOFF, color="grey", linestyle=":", linewidth=1)
ax.set_xlabel("Week of life")
ax.set_ylabel("Cumulative LTV per subscriber (USD)")
ax.set_xlim(0, HMAX)
ax.grid(True, alpha=0.3)
ax.legend(loc="upper left", fontsize=9)
ax.set_title(f"invinci web, cohort started {COHORT_START}..{COHORT_END} (N={len(cohort_subs)})\n"
             f"iOS MAP-driven LTV prediction, no web calibration — snapshot 2026-07-07", fontsize=11)
fig.tight_layout()

OUT = "reports/web_model/01_baseline/web_baseline_june_cohort_ltv_chart.png"
fig.savefig(OUT, dpi=160, bbox_inches="tight")
print(f"\nwrote {OUT}")
print(f"\nPredicted LTV @ wk52 range across vintages: ${min(final_preds.values()):.2f} - ${max(final_preds.values()):.2f}")
