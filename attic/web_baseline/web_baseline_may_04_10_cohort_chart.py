"""
LTV prediction for the May 4-10 2026 start cohort specifically (invinci, golden_all
base: non-QA, non-fraud). Same method as web_baseline_june_cohort_chart.py: frozen
iOS MAP survival curve (no web calibration) x empirical ARPU by payment step,
walk-forward vintages anchored at the cohort's own fact.

Additional outputs:
1. Payment funnel by subscription week: trial/cohort base, paid at step 1, 2, 3...
2. Distribution by observed week: among mature subscriptions, how many paid 0, 1,
   2... times up to that week.
3. Exact empirical LTV by week and exact predicted wk52 LTV for every model vintage.

Run: .venv/Scripts/python.exe web_baseline_may_04_10_cohort_chart.py
"""
from pathlib import Path

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
COHORT_START, COHORT_END = "2026-05-04", "2026-05-10"
SNAPSHOT_DATE = "2026-07-07"
REPORTS_DIR = Path("reports")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


golden = pd.read_parquet("data/golden/golden_all.parquet")

# "Came in May 4-10" follows the same cohort definition as the June script: install_date.
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
    raise RuntimeError("No se_training rows found for the May 4-10 cohort")

mx["pay_ts"] = pd.to_datetime(mx["pay_ts"], utc=True)
mx["survived"] = mx["outcome"].isin(["renewed", "recovered"]).astype(int)
mx["died"] = 1 - mx["survived"]
analysis_subs = mx["sub_id"].dropna().unique()
trial_base = len(analysis_subs)
print(f"se_training rows for cohort: {len(mx)}, distinct sub_id / trial base: {trial_base}")

# ARPU by step_k, empirical, from this cohort's own PAID events.
# This preserves the current methodology: both 9.99 and 11.99 are reflected through
# the observed mean price_amount at each payment step.
paid = golden[
    (golden["subscription_id"].isin(cohort_subs)) &
    (golden["event_name"].isin(["trial_converted", "subscription_renewed"]))
].copy()
paid["event_datetime"] = pd.to_datetime(paid["event_datetime"], utc=True)

# Protect counts from exact duplicate event rows.
paid = paid.drop_duplicates(
    subset=["subscription_id", "event_datetime", "event_name", "price_amount"]
)

merged = mx.merge(
    paid[["subscription_id", "event_datetime", "price_amount"]],
    left_on=["sub_id", "pay_ts"],
    right_on=["subscription_id", "event_datetime"],
    how="left",
)

# One matched paid row per subscription/payment step for auditable counts.
matched_paid = (
    merged.loc[merged["price_amount"].notna(), ["sub_id", "step_k", "price_amount"]]
    .drop_duplicates(subset=["sub_id", "step_k"])
)

arpu = matched_paid.groupby("step_k")["price_amount"].mean()
arpu = arpu.reindex(range(1, HMAX + 1)).ffill()
print(f"ARPU by step (1-10): {arpu.head(10).to_dict()}")

# Total number of successful paid events per subscription as of the snapshot.
payments_per_sub_snapshot = (
    paid.groupby("subscription_id").size().reindex(analysis_subs, fill_value=0)
)

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


def mature_subscription_ids(matrix: pd.DataFrame, week: int) -> pd.Index:
    return pd.Index(matrix.loc[matrix["weeks_obs"] >= week, "sub_id"].dropna().unique())


# Empirical survival of the May 4-10 cohort: the observed "dying" curve used to anchor
# the model vintages.
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

# -----------------------------------------------------------------------------
# 1) Week/payment-step funnel with exact counts and exact fact LTV.
# -----------------------------------------------------------------------------
funnel_rows = [{
    "payment_step": 0,
    "stage": "trial/cohort base",
    "n_mature": trial_base,
    "paid_at_this_step": trial_base,
    "paid_at_least_k_times_snapshot": trial_base,
    "paid_exactly_k_times_snapshot": int((payments_per_sub_snapshot == 0).sum()),
    "observed_paid_share_among_mature": 1.0,
    "empirical_survival_probability": 1.0,
    "empirical_arpu": 0.0,
    "incremental_fact_ltv": 0.0,
    "cumulative_fact_ltv": 0.0,
}]

for week in range(1, actual_fact_cutoff + 1):
    mature_ids = mature_subscription_ids(mx, week)
    paid_step_ids = pd.Index(
        matched_paid.loc[matched_paid["step_k"] == week, "sub_id"].unique()
    )
    paid_at_step = len(mature_ids.intersection(paid_step_ids))
    n_mature = len(mature_ids)
    survival = fact_surv.get(week, np.nan)
    step_arpu = arpu.get(week, np.nan)
    incremental_ltv = (
        survival * step_arpu
        if pd.notna(survival) and pd.notna(step_arpu)
        else np.nan
    )

    funnel_rows.append({
        "payment_step": week,
        "stage": f"paid payment {week}",
        "n_mature": n_mature,
        "paid_at_this_step": paid_at_step,
        "paid_at_least_k_times_snapshot": int((payments_per_sub_snapshot >= week).sum()),
        "paid_exactly_k_times_snapshot": int((payments_per_sub_snapshot == week).sum()),
        "observed_paid_share_among_mature": (
            paid_at_step / n_mature if n_mature else np.nan
        ),
        "empirical_survival_probability": survival,
        "empirical_arpu": step_arpu,
        "incremental_fact_ltv": incremental_ltv,
        "cumulative_fact_ltv": fact_ltv.get(week, np.nan),
    })

funnel = pd.DataFrame(funnel_rows)
FUNNEL_OUT = REPORTS_DIR / "web_baseline_may_04_10_cohort_payment_funnel_ltv.csv"
funnel.to_csv(FUNNEL_OUT, index=False)

print("\nMay 4-10 cohort payment funnel and exact fact LTV:")
print(funnel.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
print(f"\nwrote {FUNNEL_OUT}")

# -----------------------------------------------------------------------------
# 2) For each observed week, distribution of mature subscriptions by number of
#    successful payments accumulated through that week.
#
# Example: at observed_week=4 the columns paid_0_times..paid_4_times sum to
# n_mature. This prevents young subscriptions from being treated as churned.
# -----------------------------------------------------------------------------
distribution_rows = []
for observed_week in range(1, actual_fact_cutoff + 1):
    mature_ids = mature_subscription_ids(mx, observed_week)
    payments_through_week = (
        matched_paid.loc[
            (matched_paid["sub_id"].isin(mature_ids)) &
            (matched_paid["step_k"] <= observed_week)
        ]
        .groupby("sub_id")["step_k"]
        .nunique()
        .reindex(mature_ids, fill_value=0)
        .astype(int)
    )

    row = {
        "observed_week": observed_week,
        "n_mature": len(mature_ids),
        "fact_ltv": fact_ltv.get(observed_week, np.nan),
    }
    for payment_count in range(0, actual_fact_cutoff + 1):
        row[f"paid_{payment_count}_times"] = int(
            (payments_through_week == payment_count).sum()
        ) if payment_count <= observed_week else 0
    distribution_rows.append(row)

payment_distribution = pd.DataFrame(distribution_rows)
DISTRIBUTION_OUT = REPORTS_DIR / "web_baseline_may_04_10_cohort_payment_distribution_by_week.csv"
payment_distribution.to_csv(DISTRIBUTION_OUT, index=False)

print("\nPayment-count distribution among mature subscriptions at every week:")
print(payment_distribution.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
print(f"\nwrote {DISTRIBUTION_OUT}")

# Preserve the earlier diagnostic output, now with exact payer counts and LTV pieces.
diagnostic = funnel.loc[funnel["payment_step"] > 0, [
    "payment_step",
    "n_mature",
    "paid_at_this_step",
    "observed_paid_share_among_mature",
    "empirical_survival_probability",
    "empirical_arpu",
    "incremental_fact_ltv",
    "cumulative_fact_ltv",
]].rename(columns={"payment_step": "week"})
diagnostic["empirical_death_probability"] = 1.0 - diagnostic[
    "empirical_survival_probability"
]

DIAGNOSTIC_OUT = REPORTS_DIR / "web_baseline_may_04_10_cohort_survival.csv"
diagnostic.to_csv(DIAGNOSTIC_OUT, index=False)
print(f"\nwrote {DIAGNOSTIC_OUT}")

# -----------------------------------------------------------------------------
# 3) LTV chart with concrete values written at every empirical point and at each
#    wk52 model endpoint.
# -----------------------------------------------------------------------------
fig, ax = plt.subplots(figsize=(12, 8))
fact_x = list(fact_ltv.keys())
fact_y = list(fact_ltv.values())
ax.plot(
    fact_x,
    fact_y,
    color="black",
    linewidth=2.5,
    marker="o",
    markersize=4,
    label="Fact LTV (empirical)",
    zorder=5,
)

# Exact empirical LTV labels for every available week.
for week, value in fact_ltv.items():
    ax.annotate(
        f"wk{week}: ${value:.2f}",
        xy=(week, value),
        xytext=(4, 7),
        textcoords="offset points",
        fontsize=8,
        color="black",
        zorder=7,
    )

final_preds = {}
prediction_rows = []
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
    wk52_ltv = ys[-1]

    ax.plot(
        xs,
        ys,
        color=COLORS[n],
        linewidth=1.8,
        linestyle="--",
        label=(
            f"Model fed wk1-{n}: anchor ${anchor_fact:.2f}, "
            f"wk52 ${wk52_ltv:.2f} (n={mature.get(n)})"
        ),
        zorder=4,
    )
    ax.scatter([n], [anchor_fact], color=COLORS[n], zorder=6, s=35)
    ax.scatter([HMAX], [wk52_ltv], color=COLORS[n], zorder=6, s=35)
    ax.annotate(
        f"wk{n} → wk52\n${wk52_ltv:.2f}",
        xy=(HMAX, wk52_ltv),
        xytext=(-4, 0),
        textcoords="offset points",
        ha="right",
        va="center",
        fontsize=8,
        fontweight="bold",
        color=COLORS[n],
    )

    final_preds[n] = wk52_ltv
    prediction_rows.append({
        "vintage_anchor_week": n,
        "n_mature": mature.get(n, 0),
        "anchor_fact_ltv": anchor_fact,
        "raw_model_ltv_at_anchor": model_at_n,
        "anchor_scale": scale,
        "predicted_ltv_wk52": wk52_ltv,
    })
    print(
        f"  wk{n} vintage: fact anchor=${anchor_fact:.2f}; "
        f"predicted LTV @ wk52=${wk52_ltv:.2f}"
    )

predictions = pd.DataFrame(prediction_rows)
PREDICTIONS_OUT = REPORTS_DIR / "web_baseline_may_04_10_cohort_ltv_predictions.csv"
predictions.to_csv(PREDICTIONS_OUT, index=False)
print("\nExact LTV prediction numbers:")
if not predictions.empty:
    print(predictions.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
print(f"\nwrote {PREDICTIONS_OUT}")

n_last = mature.get(actual_fact_cutoff, 0)
ax.axvline(actual_fact_cutoff, color="grey", linestyle=":", linewidth=1)
ax.set_xlabel("Week of life")
ax.set_ylabel("Cumulative LTV per subscriber (USD)")
ax.set_xlim(0, HMAX + 1)
ax.grid(True, alpha=0.3)
ax.legend(loc="upper left", fontsize=8)
ax.set_title(
    f"invinci web, cohort installed {COHORT_START}..{COHORT_END} "
    f"(trial/base N={trial_base})\n"
    f"Fact through wk{actual_fact_cutoff} (n_mature={n_last}); "
    f"iOS MAP-driven prediction — snapshot {SNAPSHOT_DATE}",
    fontsize=11,
)
fig.tight_layout()

OUT = REPORTS_DIR / "web_baseline_may_04_10_cohort_ltv_chart.png"
fig.savefig(OUT, dpi=160, bbox_inches="tight")
print(f"\nwrote {OUT}")

if final_preds:
    print(
        "\nPredicted LTV @ wk52 range across vintages: "
        f"${min(final_preds.values()):.2f} - ${max(final_preds.values()):.2f}"
    )
else:
    print("\nNo valid wk52 predictions were produced")
