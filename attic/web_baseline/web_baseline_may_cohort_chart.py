"""
LTV prediction for the May 1-31 2026 start cohort specifically (invinci, golden_all
base: non-QA, non-fraud). Same method as web_baseline_june_cohort_chart.py: frozen
iOS MAP survival curve (no web calibration) x empirical ARPU by payment step,
walk-forward vintages anchored at the cohort's own fact.

The cohort is defined by install_date in May 2026. A diagnostic CSV is also written
with empirical survival, death probability, mature sample size, ARPU and cumulative
fact LTV by payment week.

Run: .venv/Scripts/python.exe web_baseline_may_cohort_chart.py
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from models import common, map_model

HMAX = 52
WEEKS_FED = [1, 2, 4, 8]
FACT_CUTOFF = 8
COLORS = {1: "#85C1E9", 2: "#3498DB", 4: "#1B4F72", 8: "#512E5F"}
COHORT_START, COHORT_END = "2026-05-01", "2026-05-31"
SNAPSHOT_DATE = "2026-07-07"


golden = pd.read_parquet("data/golden/golden_all.parquet")

# "Came in May" follows the same cohort definition as the June script: install_date.
cohort_subs = golden.loc[
    (golden["install_date"] >= COHORT_START) &
    (golden["install_date"] <= COHORT_END),
    "subscription_id",
].dropna().unique()

if len(cohort_subs) == 0:
    raise RuntimeError(
        f"No subscriptions found for install_date {COHORT_START}..{COHORT_END}"
    )

print(f"Cohort {COHORT_START}..{COHORT_END}: {len(cohort_subs)} subscriptions")

se_all = pd.read_parquet("data/golden/golden_all_se_training.parquet")
mx = se_all[se_all["sub_id"].isin(cohort_subs)].copy()

if mx.empty:
    raise RuntimeError("No se_training rows found for the May cohort")

mx["survived"] = mx["outcome"].isin(["renewed", "recovered"]).astype(int)
mx["died"] = 1 - mx["survived"]
print(f"se_training rows for cohort: {len(mx)}, distinct sub_id: {mx['sub_id'].nunique()}")

# ARPU by step_k, empirical, from this cohort's own PAID events.
# This preserves the current methodology: both 9.99 and 11.99 are reflected through
# the observed mean price_amount at each payment step.
paid = golden[
    (golden["subscription_id"].isin(cohort_subs)) &
    (golden["event_name"].isin(["trial_converted", "subscription_renewed"]))
].copy()
paid["event_datetime"] = pd.to_datetime(paid["event_datetime"], utc=True)

merged = mx.merge(
    paid[["subscription_id", "event_datetime", "price_amount"]],
    left_on=["sub_id", "pay_ts"],
    right_on=["subscription_id", "event_datetime"],
    how="left",
)

arpu = merged.groupby("step_k")["price_amount"].mean()
arpu = arpu.reindex(range(1, HMAX + 1)).ffill()
print(f"ARPU by step (1-10): {arpu.head(10).to_dict()}")

# Frozen iOS MAP model, unchanged from the June script.
mx_ios = common.load_matrix()
apps_info_ios = common.select_apps(mx_ios)
state_ios = map_model.fit(mx_ios, apps_info_ios)


def visible_at_week_n(matrix: pd.DataFrame, n: int) -> pd.DataFrame:
    first_pay = matrix.groupby("sub_id")["pay_ts"].transform("min")
    age_weeks = (matrix["pay_ts"] - first_pay).dt.total_seconds() / (7 * 86400)
    eligible_subs = matrix.loc[matrix["weeks_obs"] >= n, "sub_id"].unique()
    visible = matrix[
        matrix["sub_id"].isin(eligible_subs) & (age_weeks < n)
    ].copy()
    visible["weeks_obs"] = n
    return visible


def ltv_curve(surv_series: pd.Series, weeks_range) -> dict[int, float]:
    cum = 0.0
    out = {}
    for week in weeks_range:
        survival = surv_series.get(week)
        step_arpu = arpu.get(week, np.nan)
        if survival is not None and pd.notna(survival) and pd.notna(step_arpu):
            cum += survival * step_arpu
        out[week] = cum
    return out


# Empirical survival of the May cohort: this is the observed "dying" curve used to
# anchor the model vintages.
fact_surv, mature, _ = common.direct_survival(mx)

available_fact_weeks = [
    week for week in range(1, FACT_CUTOFF + 1)
    if pd.notna(fact_surv.get(week)) and mature.get(week, 0) > 0
]
if not available_fact_weeks:
    raise RuntimeError("No mature empirical survival weeks found for the May cohort")

actual_fact_cutoff = max(available_fact_weeks)
if actual_fact_cutoff < FACT_CUTOFF:
    print(
        f"Warning: requested fact cutoff wk{FACT_CUTOFF}, but data is only available "
        f"through wk{actual_fact_cutoff}"
    )

fact_ltv = ltv_curve(fact_surv, range(1, actual_fact_cutoff + 1))

# Save an auditable week-level table without changing the prediction methodology.
diagnostic_rows = []
for week in range(1, actual_fact_cutoff + 1):
    survival = fact_surv.get(week, np.nan)
    diagnostic_rows.append({
        "week": week,
        "n_mature": mature.get(week, 0),
        "empirical_survival_probability": survival,
        "empirical_death_probability": 1.0 - survival if pd.notna(survival) else np.nan,
        "empirical_arpu": arpu.get(week, np.nan),
        "cumulative_fact_ltv": fact_ltv.get(week, np.nan),
    })

diagnostic = pd.DataFrame(diagnostic_rows)
DIAGNOSTIC_OUT = "reports/web_baseline_may_cohort_survival.csv"
diagnostic.to_csv(DIAGNOSTIC_OUT, index=False)
print("\nMay cohort empirical survival / death table:")
print(diagnostic.to_string(index=False))
print(f"\nwrote {DIAGNOSTIC_OUT}")

fig, ax = plt.subplots(figsize=(10, 7))
fact_x = list(fact_ltv.keys())
fact_y = list(fact_ltv.values())
ax.plot(
    fact_x,
    fact_y,
    color="black",
    linewidth=2.5,
    label="Fact LTV (empirical)",
    zorder=5,
)

final_preds = {}
for n in WEEKS_FED:
    if n > actual_fact_cutoff:
        print(f"  wk{n}: no empirical anchor yet, skipped")
        continue

    anchor_fact = fact_ltv.get(n)
    if anchor_fact is None or pd.isna(anchor_fact):
        print(f"  wk{n}: missing empirical anchor, skipped")
        continue

    visible = visible_at_week_n(mx, n)
    if visible.empty:
        print(f"  wk{n}: 0 eligible subscriptions, skipped")
        continue

    pred_surv = map_model.predict(state_ios, visible, n)
    model_ltv = ltv_curve(pred_surv, range(1, HMAX + 1))
    model_at_n = model_ltv.get(n)
    if model_at_n is None or pd.isna(model_at_n) or model_at_n == 0:
        print(f"  wk{n}: invalid model anchor, skipped")
        continue

    scale = anchor_fact / model_at_n
    xs = list(range(n, HMAX + 1))
    ys = [model_ltv[week] * scale for week in xs]

    ax.plot(
        xs,
        ys,
        color=COLORS[n],
        linewidth=1.8,
        linestyle="--",
        label=f"Model LTV, fed wk1-{n} (n_mature={mature.get(n)})",
        zorder=4,
    )
    ax.scatter([n], [anchor_fact], color=COLORS[n], zorder=6, s=30)
    final_preds[n] = ys[-1]
    print(f"  wk{n} vintage: predicted LTV @ wk52 = ${ys[-1]:.2f}")

n_last = mature.get(actual_fact_cutoff, 0)
ax.annotate(
    f"n_mature={n_last}\n(fact ends here)\n${fact_y[-1]:.2f}",
    xy=(actual_fact_cutoff, fact_y[-1]),
    xytext=(actual_fact_cutoff + 4, fact_y[-1] * 0.6),
    fontsize=9,
    fontweight="bold",
    bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="grey", alpha=0.9),
    arrowprops=dict(arrowstyle="->", color="grey", lw=1),
)
ax.axvline(actual_fact_cutoff, color="grey", linestyle=":", linewidth=1)
ax.set_xlabel("Week of life")
ax.set_ylabel("Cumulative LTV per subscriber (USD)")
ax.set_xlim(0, HMAX)
ax.grid(True, alpha=0.3)
ax.legend(loc="upper left", fontsize=9)
ax.set_title(
    f"invinci web, cohort installed {COHORT_START}..{COHORT_END} "
    f"(N={len(cohort_subs)})\n"
    f"iOS MAP-driven LTV prediction, no web calibration — snapshot {SNAPSHOT_DATE}",
    fontsize=11,
)
fig.tight_layout()

OUT = "reports/web_baseline_may_cohort_ltv_chart.png"
fig.savefig(OUT, dpi=160, bbox_inches="tight")
print(f"\nwrote {OUT}")

if final_preds:
    print(
        "\nPredicted LTV @ wk52 range across vintages: "
        f"${min(final_preds.values()):.2f} - ${max(final_preds.values()):.2f}"
    )
else:
    print("\nNo valid wk52 predictions were produced")
