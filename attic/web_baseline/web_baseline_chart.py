"""
Step 5 (revised): walk-forward LTV(survival) prediction (frozen iOS MAP, no web
calibration) vs empirical fact, weeks 0-52, invinci -- two subplots (golden_all,
golden_good). Multiple dashed prediction curves, one per "weeks of visible data
fed to the model" (N=1,2,4,8), each anchored exactly at the fact point for week N
(scaled so there's no gap between fact and model at the anchor).

Run: .venv/Scripts/python.exe web_baseline_chart.py
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
COLORS = {1: "#F1948A", 2: "#E74C3C", 4: "#B03A2E", 8: "#78281F"}

mx_ios = common.load_matrix()
apps_info_ios = common.select_apps(mx_ios)
state_ios = map_model.fit(mx_ios, apps_info_ios)


def visible_at_week_n(mx, n):
    """Rows visible if we'd only observed each subscriber for their first n weeks
    of life, restricted to subscribers who have actually lived >= n weeks by now
    (so we're not fabricating maturity). weeks_obs is forced to n to simulate the
    n-week-old vantage point for the model's own censoring/hr logic."""
    first_pay = mx.groupby("sub_id")["pay_ts"].transform("min")
    age_weeks = (mx["pay_ts"] - first_pay).dt.total_seconds() / (7 * 86400)
    eligible_subs = mx.loc[mx["weeks_obs"] >= n, "sub_id"].unique()
    visible = mx[(mx["sub_id"].isin(eligible_subs)) & (age_weeks < n)].copy()
    visible["weeks_obs"] = n
    return visible


fig, axes = plt.subplots(1, 2, figsize=(15, 7), sharey=True)

for ax, (path, label) in zip(axes, [
    ("data/golden/golden_all_se_training.parquet", "golden_all"),
    ("data/golden/golden_good_se_training.parquet", "golden_good"),
]):
    mx = pd.read_parquet(path)
    mx["survived"] = mx["outcome"].isin(["renewed", "recovered"]).astype(int)
    mx["died"] = 1 - mx["survived"]

    fact, mature, _ = common.direct_survival(mx)
    cutoff = FACT_CUTOFF[label]
    fact_x = list(range(1, cutoff + 1))
    fact_y = [fact.get(k) for k in fact_x]
    ax.plot(fact_x, fact_y, color="black", linewidth=2.5, label="Fact (empirical)", zorder=5)

    for n in WEEKS_FED:
        anchor_fact = fact.get(n)
        if anchor_fact is None or pd.isna(anchor_fact):
            continue  # not enough fact history to anchor this vintage
        visible = visible_at_week_n(mx, n)
        if len(visible) == 0:
            continue
        pred = map_model.predict(state_ios, visible, n)
        pred_at_n = pred.get(n)
        if pred_at_n is None or pred_at_n <= 0:
            continue
        scale = anchor_fact / pred_at_n
        xs = list(range(n, HMAX + 1))
        ys = [pred.get(w) * scale for w in xs]
        ax.plot(xs, ys, color=COLORS[n], linewidth=1.6, linestyle="--",
                 label=f"Model, fed wk1-{n} (n_mature={mature.get(n)})", zorder=4)
        ax.scatter([n], [anchor_fact], color=COLORS[n], zorder=6, s=25)

    n_last = mature.get(cutoff, 0)
    ax.annotate(f"n_mature={n_last}\n(fact ends here)",
                xy=(cutoff, fact.get(cutoff)),
                xytext=(cutoff + 4, min(0.95, fact.get(cutoff) + 0.15)),
                fontsize=9, fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="grey", alpha=0.9),
                arrowprops=dict(arrowstyle="->", color="grey", lw=1))
    ax.axvline(cutoff, color="grey", linestyle=":", linewidth=1)
    ax.set_title(label)
    ax.set_xlabel("Week of life")
    ax.set_xlim(0, HMAX)
    ax.set_ylim(0, 1)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right", fontsize=8)

axes[0].set_ylabel("Survival (retention)")
fig.suptitle("iOS MAP baseline applied to web, walk-forward (no web calibration) — 2026-07-07", fontsize=13)
fig.tight_layout(rect=[0, 0, 1, 0.95])

OUT = "reports/web_model/01_baseline/web_baseline_chart.png"
fig.savefig(OUT, dpi=160, bbox_inches="tight")
print(f"wrote {OUT}")
