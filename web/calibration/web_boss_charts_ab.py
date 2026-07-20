"""
Two boss-facing charts using the calibrated (2-parameter smooth h_base
correction) MAP model: A = accuracy on the reference cohort, B = app-level
forecast with an honest +-8% (LOO) uncertainty band. No further tuning here.

Run: .venv/Scripts/python.exe web/calibration/web_boss_charts_ab.py
"""
import os
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# Resolve all project-relative paths from the repository root, regardless of
# the working directory configured in PyCharm or the shell.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web" / "golden"))
os.chdir(ROOT)

from core.web_calibration import (
    load_golden, filter_provider_and_app, subscription_start_table,
    local_paid_events, load_web_matrix, sql_style_summary, visible_at_week_n,
    raw_map_ltv, SNAPSHOT_TS, HMAX, TARGET_STRIPE_PRICE_ID,
)
from core import common, map_model

from ltv.config import (
    MIN_FIRST_PAYERS, MIN_MATURE_REBILL, RELIABILITY_N_THRESHOLD, TAPER_WIDTH, LOO_BAND_PCT,
)
OUT_DIR = "reports/web_model/03_hazard_calibration"

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
state_ios = map_model.fit(mx_ios, apps_info_ios)


def build_cohort_data(cw):
    cohort_start_subs = pd.Index(starts_9_99.loc[starts_9_99["cohort_week"].eq(cw), "subscription_id"].unique())
    cohort_paid = paid_all[paid_all["subscription_id"].isin(cohort_start_subs)]
    first_payers = pd.Index(cohort_paid.loc[cohort_paid["rebill_number"].eq(0), "subscription_id"].unique(), dtype="string")
    if len(first_payers) < MIN_FIRST_PAYERS:
        return None
    common_payers = first_payers.intersection(matrix_subs)
    cohort_age_weeks = int((SNAPSHOT_TS.tz_localize(None) - cw.tz_localize(None)).days // 7)
    max_mature_rebill = max(0, cohort_age_weeks - 2)
    if max_mature_rebill < MIN_MATURE_REBILL:
        return None
    common_fact = sql_style_summary(cohort_paid, common_payers, max_mature_rebill)
    cohort_mx = web_all[web_all["sub_id"].isin(common_payers)].copy()
    visible = visible_at_week_n(cohort_mx, max_mature_rebill)
    if visible.empty:
        return None
    return {
        "cohort_week": cw, "N": len(common_payers), "max_mature_rebill": max_mature_rebill,
        "fact": common_fact.set_index("rebill_number"), "visible": visible, "weeks": max_mature_rebill,
        "common_payers": common_payers,
    }


cohort_weeks = sorted(starts_9_99["cohort_week"].unique())
cohorts = [c for c in (build_cohort_data(cw) for cw in cohort_weeks) if c is not None]
print(f"Pool: {len(cohorts)} mature cohorts")

# ============================================================
# Rebuild the 2-parameter corrected h_base (identical method/params as the
# calibration step -- not refit here, just reconstructed).
# ============================================================
max_k = max(c["max_mature_rebill"] for c in cohorts)
rows = []
for k in range(1, max_k + 1):
    at_risk, died = 0, 0
    for c in cohorts:
        if c["max_mature_rebill"] < k:
            continue
        fact = c["fact"]
        prev = c["N"] if k == 1 else fact.loc[k - 1, "active_users"]
        cur = fact.loc[k, "active_users"] if k in fact.index else np.nan
        if pd.isna(cur):
            continue
        at_risk += prev
        died += (prev - cur)
    h = died / at_risk if at_risk else np.nan
    rows.append({"k": k, "N_at_risk": at_risk, "h_web_raw": h})
h_table = pd.DataFrame(rows).set_index("k")
h_table["h_ios"] = [state_ios["h_base"].get(k, np.nan) for k in h_table.index]
h_table["reliable"] = h_table["N_at_risk"] >= RELIABILITY_N_THRESHOLD
k_max_reliable = h_table.index[h_table["reliable"]].max()

rel = h_table.loc[h_table["reliable"]].copy()
rel["log_ratio"] = np.log(rel["h_web_raw"] / rel["h_ios"])
X = np.vstack([np.ones(len(rel)), (rel.index - 1).values]).T
w = rel["N_at_risk"].values
alpha, beta = np.linalg.lstsq(X * np.sqrt(w)[:, None], rel["log_ratio"].values * np.sqrt(w), rcond=None)[0]

correction = pd.Series(1.0, index=range(1, HMAX + 1))
for k in range(1, k_max_reliable + 1):
    correction[k] = np.exp(alpha + beta * (k - 1))
f_boundary = correction[k_max_reliable]
for j, k in enumerate(range(k_max_reliable + 1, k_max_reliable + TAPER_WIDTH + 1), start=1):
    if k > HMAX:
        break
    frac = j / (TAPER_WIDTH + 1)
    correction[k] = f_boundary * (1 - frac) + 1.0 * frac

state_web = dict(state_ios)
state_web["h_base"] = state_ios["h_base"] * correction
print(f"Reconstructed correction: alpha={alpha:.4f}, beta={beta:.4f}, reliability boundary k<={k_max_reliable}")

# ============================================================
# Chart A: reference cohort 2026-05-04, fact vs calibrated model, raw (no anchor)
# ============================================================
ref = next(c for c in cohorts if c["cohort_week"] == pd.Timestamp("2026-05-04", tz="UTC"))
pred_survival = map_model.predict(state_web, ref["visible"], ref["weeks"])
arpu_by_rebill = ref["fact"]["arpu_at_payment"].reindex(range(0, HMAX + 1)).ffill()
pred_ltv = raw_map_ltv(pred_survival, arpu_by_rebill)

fig, ax = plt.subplots(figsize=(11, 7.5))
fact_plot = ref["fact"].loc[ref["fact"].index <= ref["max_mature_rebill"]]
ax.plot(fact_plot.index, fact_plot["payer_ltv"], color="black", linewidth=2.5, marker="o",
        label="Fact LTV (real payments)", zorder=5)
map_x = list(range(0, ref["max_mature_rebill"] + 1))
map_y = [float(pred_ltv.loc[r]) for r in map_x]
ax.plot(map_x, map_y, color="#2471A3", linewidth=2.0, linestyle="--",
        label="Calibrated MAP (web h_base correction)", zorder=4)

err_final = (map_y[-1] - fact_plot["payer_ltv"].iloc[-1]) / fact_plot["payer_ltv"].iloc[-1] * 100
ax.text(0.02, 0.96, "После веб-калибровки h_base: средняя ошибка +2.6%, наклон плоский\n"
                     f"(эта когорта, rebill {ref['max_mature_rebill']}: {err_final:+.1f}%)",
        transform=ax.transAxes, fontsize=10, va="top",
        bbox=dict(boxstyle="round,pad=0.4", fc="#eaf2f8", ec="#2471A3"))
ax.set_xlabel("Rebill number (0 = first payment)")
ax.set_ylabel("Cumulative LTV per subscriber (USD)")
ax.set_xlim(0, ref["max_mature_rebill"])
ax.grid(True, alpha=0.3)
ax.legend(fontsize=9, loc="lower right")
ax.set_title(f"Cohort 2026-05-04..05-10, $9.99/week, N={ref['N']}\nFact vs calibrated MAP prediction")
fig.tight_layout()
OUT_A = f"{OUT_DIR}/boss_A_accuracy.png"
fig.savefig(OUT_A, dpi=160, bbox_inches="tight")
plt.close(fig)
print(f"wrote {OUT_A}")

# ============================================================
# Chart B: app-level (pooled) forecast, fact on reliable zone, model dashed to
# 52 with +-8% LOO band, boundary line at k_max_reliable.
# ============================================================
pooled_visible = pd.concat([c["visible"] for c in cohorts], ignore_index=True)
pooled_survival = map_model.predict(state_web, pooled_visible, k_max_reliable)

# pooled fact LTV by rebill: N-weighted mean across cohorts mature enough at each r
pooled_fact_rows = []
for r in range(0, k_max_reliable + 1):
    vals, weights = [], []
    for c in cohorts:
        if r <= c["max_mature_rebill"] and r in c["fact"].index:
            vals.append(c["fact"].loc[r, "payer_ltv"])
            weights.append(c["N"])
    if vals:
        pooled_fact_rows.append({"rebill": r, "payer_ltv": np.average(vals, weights=weights)})
pooled_fact = pd.DataFrame(pooled_fact_rows).set_index("rebill")

pooled_arpu = pd.Series(9.99, index=range(0, HMAX + 1))  # fixed $9.99/week plan, confirmed exact
pooled_pred_ltv = raw_map_ltv(pooled_survival, pooled_arpu)

fig, ax = plt.subplots(figsize=(12, 7.5))
ax.plot(pooled_fact.index, pooled_fact["payer_ltv"], color="black", linewidth=2.5, marker="o",
        label="Fact LTV (pooled, N-weighted, reliable zone)", zorder=5)

model_x = list(range(0, HMAX + 1))
model_y = np.array([float(pooled_pred_ltv.loc[r]) for r in model_x])
upper = model_y * (1 + LOO_BAND_PCT)
lower = model_y * (1 - LOO_BAND_PCT)
ax.fill_between(model_x, lower, upper, color="#2471A3", alpha=0.15, zorder=2,
                 label=f"±{LOO_BAND_PCT*100:.0f}% LOO cross-cohort spread")
ax.plot(model_x, model_y, color="#2471A3", linewidth=2.0, linestyle="--",
        label="Calibrated MAP (app-level, pooled)", zorder=4)

ax.axvline(k_max_reliable, color="grey", linestyle=":", linewidth=1.2)
ax.text(k_max_reliable + 0.5, ax.get_ylim()[1] if ax.get_ylim()[1] else 5, "", alpha=0)  # placeholder to size axes first
ymax_guess = max(upper.max(), pooled_fact["payer_ltv"].max()) * 1.05
ax.set_ylim(0, ymax_guess)
ax.text(k_max_reliable + 0.5, ymax_guess * 0.05,
        f"дальше — экстраполяция, N_at_risk<{RELIABILITY_N_THRESHOLD}",
        rotation=90, fontsize=9, color="grey", va="bottom")

final_val = model_y[-1]
final_lo, final_hi = lower[-1], upper[-1]
ax.annotate(f"wk52: ${final_val:.2f}\nориентир, ±{LOO_BAND_PCT*100:.0f}% по когортам\n"
            f"(${final_lo:.2f}-${final_hi:.2f}), wk52 не наблюдаем",
            xy=(HMAX, final_val), xytext=(HMAX - 16, final_val * 0.55),
            fontsize=9, fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.35", fc="white", ec="#2471A3", alpha=0.95),
            arrowprops=dict(arrowstyle="->", color="#2471A3", lw=1))

ax.set_xlabel("Rebill number / week of life (0 = first payment)")
ax.set_ylabel("Cumulative LTV per subscriber (USD)")
ax.set_xlim(0, HMAX)
ax.grid(True, alpha=0.3)
ax.legend(fontsize=9, loc="upper left")
ax.set_title(f"App-level LTV forecast, $9.99/week, invinci web (pooled {len(cohorts)} cohorts)\n"
             f"Calibrated MAP (2-param web h_base correction), honest cross-cohort uncertainty shown")
fig.tight_layout()
OUT_B = f"{OUT_DIR}/boss_B_forecast.png"
fig.savefig(OUT_B, dpi=160, bbox_inches="tight")
plt.close(fig)
print(f"wrote {OUT_B}")
