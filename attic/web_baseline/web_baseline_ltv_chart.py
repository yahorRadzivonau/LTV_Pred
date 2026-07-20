"""
Step 5b: turn the walk-forward survival prediction (frozen iOS MAP) into an LTV ($)
prediction. The iOS model only knows survival/hazard -- it has no pricing at all --
so $ has to come from web's own golden data (price_amount on PAID events).

LTV(w) = sum_{k=1..w} S(k) * ARPU(k)
  S(k)    = P(subscriber reached payment step k)  -- survival curve (fact or model)
  ARPU(k) = average price_amount actually paid at step k (empirical, from golden)

Run: .venv/Scripts/python.exe web_baseline_ltv_chart.py
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from models import common, map_model

HMAX = 52
WEEKS_FED = [1, 2, 4, 8]
FACT_CUTOFF = {"golden_all": 12, "golden_good": 4}
COLORS = {1: "#85C1E9", 2: "#3498DB", 4: "#2471A3", 8: "#1B4F72"}

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


def arpu_by_step(golden_path, se_path):
    """Empirical average $ paid at each step_k, joined from golden (has price_amount)
    onto se_training (has step_k), matched on subscription_id + exact event timestamp."""
    golden = pd.read_parquet(golden_path)
    paid = golden[golden["event_name"].isin(["trial_converted", "subscription_renewed"])].copy()
    paid["event_datetime"] = pd.to_datetime(paid["event_datetime"], utc=True)
    se = pd.read_parquet(se_path)
    merged = se.merge(
        paid[["subscription_id", "event_datetime", "price_amount"]],
        left_on=["sub_id", "pay_ts"], right_on=["subscription_id", "event_datetime"], how="left",
    )
    n_missing = merged["price_amount"].isna().sum()
    if n_missing:
        print(f"  WARNING: {n_missing} se_training rows had no price_amount match")
    arpu = merged.groupby("step_k")["price_amount"].mean()
    # ARPU is only observed up to whatever step_k a young cohort has actually reached;
    # beyond that, carry the last known value forward rather than silently treating it
    # as "no more revenue" (which would flatten the LTV curve to a fake plateau).
    arpu = arpu.reindex(range(1, HMAX + 1)).ffill()
    return arpu


fig, axes = plt.subplots(1, 2, figsize=(15, 7), sharey=True)

for ax, (golden_path, se_path, label) in zip(axes, [
    ("data/golden/golden_all.parquet", "data/golden/golden_all_se_training.parquet", "golden_all"),
    ("data/golden/golden_good.parquet", "data/golden/golden_good_se_training.parquet", "golden_good"),
]):
    mx = pd.read_parquet(se_path)
    mx["survived"] = mx["outcome"].isin(["renewed", "recovered"]).astype(int)
    mx["died"] = 1 - mx["survived"]

    fact_surv, mature, _ = common.direct_survival(mx)
    arpu = arpu_by_step(golden_path, se_path)
    print(f"[{label}] ARPU by step_k (first 10): {arpu.head(10).to_dict()}")

    def ltv_curve(surv_series, weeks_range):
        """cumulative $/subscriber = sum_{k<=w} S(k)*ARPU(k). S(k) here is fact/pred
        survival-to-step-k; the increment at k approximates P(paid at step k)*ARPU(k)
        (survival curves here are monotonic decreasing "reached step k" fractions, so
        we use S(k) directly as the per-step "made this payment" weight -- consistent
        with how ARPU(k) was computed, over subs that actually made it to step k)."""
        cum = 0.0
        out = {}
        for w in weeks_range:
            s = surv_series.get(w)
            a = arpu.get(w, np.nan)
            if s is not None and pd.notna(s) and pd.notna(a):
                cum += s * a
            out[w] = cum
        return out

    cutoff = FACT_CUTOFF[label]
    fact_ltv = ltv_curve(fact_surv, range(1, cutoff + 1))
    fact_x = list(fact_ltv.keys())
    fact_y = list(fact_ltv.values())
    ax.plot(fact_x, fact_y, color="black", linewidth=2.5, label="Fact LTV (empirical)", zorder=5)

    for n in WEEKS_FED:
        anchor_fact = fact_ltv.get(n)
        if anchor_fact is None:
            continue
        visible = visible_at_week_n(mx, n)
        if len(visible) == 0:
            continue
        pred_surv = map_model.predict(state_ios, visible, n)
        model_ltv = ltv_curve(pred_surv, range(1, HMAX + 1))
        model_at_n = model_ltv.get(n)
        if not model_at_n:
            continue
        scale = anchor_fact / model_at_n
        xs = list(range(n, HMAX + 1))
        ys = [model_ltv[w] * scale for w in xs]
        ax.plot(xs, ys, color=COLORS[n], linewidth=1.6, linestyle="--",
                 label=f"Model LTV, fed wk1-{n} (n_mature={mature.get(n)})", zorder=4)
        ax.scatter([n], [anchor_fact], color=COLORS[n], zorder=6, s=25)

    n_last = mature.get(cutoff, 0)
    ax.annotate(f"n_mature={n_last}\n(fact ends here)\n${fact_y[-1]:.2f}",
                xy=(cutoff, fact_y[-1]),
                xytext=(cutoff + 4, fact_y[-1] * 0.55),
                fontsize=9, fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="grey", alpha=0.9),
                arrowprops=dict(arrowstyle="->", color="grey", lw=1))
    ax.axvline(cutoff, color="grey", linestyle=":", linewidth=1)
    ax.set_title(label)
    ax.set_xlabel("Week of life")
    ax.set_xlim(0, HMAX)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left", fontsize=8)

axes[0].set_ylabel("Cumulative LTV per subscriber (USD)")
fig.suptitle("iOS MAP-driven LTV prediction applied to web, walk-forward (no web calibration) — 2026-07-07", fontsize=12)
fig.tight_layout(rect=[0, 0, 1, 0.95])

OUT = "reports/web_model/01_baseline/web_baseline_ltv_chart.png"
fig.savefig(OUT, dpi=160, bbox_inches="tight")
print(f"\nwrote {OUT}")
