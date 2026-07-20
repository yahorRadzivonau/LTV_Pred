"""
Soft correction of iOS h_base toward web, in the reliable zone only (N_at_risk>=40,
pooled across 11 mature $9.99/week cohorts), via a 2-parameter smooth trend
multiplier on hazard (fit by weighted least squares on log(h_web_raw/h_ios)),
tapered back to pure iOS h_base beyond the reliability boundary (no seam jump).
hr in map_model.predict() is untouched -- K_SHRINK stays 800.

Run: .venv/Scripts/python.exe web_hbase_smooth_correction.py
"""
import numpy as np
import pandas as pd

from core.web_calibration import (
    load_golden, filter_provider_and_app, subscription_start_table,
    local_paid_events, load_web_matrix, sql_style_summary, visible_at_week_n,
    SNAPSHOT_TS, HMAX, TARGET_STRIPE_PRICE_ID,
)
from core import common, map_model
from core.map_model import MAP_LEVERS, _subs_with_levers, personal_multiplier, K_SHRINK

from ltv.config import (
    MIN_FIRST_PAYERS, MIN_MATURE_REBILL, RELIABILITY_N_THRESHOLD, TAPER_WIDTH,
)  # TAPER_WIDTH = weeks beyond boundary over which correction fades back to 1.0 (fixed, not fitted)

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
    }


cohort_weeks = sorted(starts_9_99["cohort_week"].unique())
cohorts = [c for c in (build_cohort_data(cw) for cw in cohort_weeks) if c is not None]
print(f"Pool: {len(cohorts)} mature cohorts")

# ============================================================
# 1. Reliable-zone boundary (identical pooled KM at-risk/died computation as before)
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
print(f"\nReliability boundary: weeks 1..{k_max_reliable} have N_at_risk>={RELIABILITY_N_THRESHOLD}")
print(h_table.loc[:k_max_reliable + 2].to_string(float_format=lambda x: f"{x:.4f}"))

# ============================================================
# 2. Smooth 2-parameter trend fit on log(h_web_raw/h_ios), reliable zone only,
#    weighted by N_at_risk. f(k) = exp(alpha + beta*(k-1)).
# ============================================================
rel = h_table.loc[h_table["reliable"]].copy()
rel["log_ratio"] = np.log(rel["h_web_raw"] / rel["h_ios"])
X = np.vstack([np.ones(len(rel)), (rel.index - 1).values]).T
w = rel["N_at_risk"].values
W = np.diag(w)
beta_hat = np.linalg.lstsq(X * np.sqrt(w)[:, None], rel["log_ratio"].values * np.sqrt(w), rcond=None)[0]
alpha, beta = beta_hat
print(f"\nFitted trend: log(f(k)) = {alpha:.4f} + {beta:.4f}*(k-1)  [2 parameters: alpha, beta]")
print(f"f(k) at k=1: {np.exp(alpha):.3f}, at k={k_max_reliable}: {np.exp(alpha + beta*(k_max_reliable-1)):.3f}")

# ============================================================
# 3. Build corrected h_base: trend correction inside reliable zone, linear taper
#    to 1.0 over TAPER_WIDTH weeks beyond boundary, pure iOS after that.
# ============================================================
correction = pd.Series(1.0, index=range(1, HMAX + 1))
for k in range(1, k_max_reliable + 1):
    correction[k] = np.exp(alpha + beta * (k - 1))
f_boundary = correction[k_max_reliable]
for j, k in enumerate(range(k_max_reliable + 1, k_max_reliable + TAPER_WIDTH + 1), start=1):
    if k > HMAX:
        break
    frac = j / (TAPER_WIDTH + 1)
    correction[k] = f_boundary * (1 - frac) + 1.0 * frac
# k > k_max_reliable+TAPER_WIDTH stays 1.0 (pure iOS)

h_base_corrected = state_ios["h_base"] * correction
state_web = dict(state_ios)
state_web["h_base"] = h_base_corrected

print(f"\nCorrection factor by week (1..{k_max_reliable + TAPER_WIDTH + 1}):")
print(correction.loc[1:k_max_reliable + TAPER_WIDTH + 1].to_string(float_format=lambda x: f"{x:.3f}"))
print(f"Beyond week {k_max_reliable + TAPER_WIDTH}: correction = 1.0 (pure iOS, untouched)")


def predict_with_state(cohort, state):
    return map_model.predict(state, cohort["visible"], cohort["weeks"])


def compute_hr(state, app_rows, weeks):
    """Mirrors map_model.predict()'s internal hr computation exactly, for
    inspection only -- does not modify or call into the model's own hr logic."""
    h_base = state["h_base"]
    combos = personal_multiplier(state, app_rows)
    subs = _subs_with_levers(state, app_rows)
    subs = subs.merge(combos[MAP_LEVERS + ["mult"]], on=MAP_LEVERS, how="left")
    sub_mult = subs.set_index("sub_id")["mult"]
    obs = app_rows[app_rows["weeks_obs"] >= app_rows["step_k"] + 1].copy()
    obs = obs[obs["step_k"] <= weeks]
    n_seen = len(obs)
    if n_seen > 0:
        obs["mult"] = obs["sub_id"].map(sub_mult)
        obs["exp_die"] = (h_base.reindex(obs["step_k"]).values * obs["mult"].values)
        obs["exp_die"] = obs["exp_die"].clip(0.001, 0.999)
        g = obs.groupby("step_k").agg(o=("died", "mean"), e=("exp_die", "mean"), n=("died", "size"))
        g = g[(g["n"] >= 20) & (g["e"] > 0)]
        log_hr = np.average(np.log(np.clip(g["o"] / g["e"], 0.2, 5.0)), weights=g["n"]) if len(g) else 0.0
    else:
        log_hr = 0.0
    return np.exp(log_hr * (n_seen / (n_seen + K_SHRINK)))


# ============================================================
# hr check: before vs after correction
# ============================================================
print("\n=== hr per cohort: iOS h_base vs corrected h_base (must stay shrunk near 1) ===")
hr_rows = []
for c in cohorts:
    hr_ios = compute_hr(state_ios, c["visible"], c["weeks"])
    hr_web = compute_hr(state_web, c["visible"], c["weeks"])
    hr_rows.append({"cohort_week": c["cohort_week"].date(), "hr_with_ios_hbase": hr_ios, "hr_with_corrected_hbase": hr_web})
hr_df = pd.DataFrame(hr_rows)
print(hr_df.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
print(f"\nmean hr (iOS h_base) = {hr_df['hr_with_ios_hbase'].mean():.4f}, "
      f"mean hr (corrected h_base) = {hr_df['hr_with_corrected_hbase'].mean():.4f}")

# ============================================================
# 4. Residual by rebill, pooled, AFTER correction
# ============================================================
residual_rows = []
for c in cohorts:
    pred = predict_with_state(c, state_web)
    fact = c["fact"]
    for r in range(1, c["max_mature_rebill"] + 1):
        if r not in fact.index:
            continue
        fact_surv = fact.loc[r, "rebill_percent"]
        pred_surv = pred.get(r, np.nan)
        if pd.isna(pred_surv) or fact_surv == 0:
            continue
        residual_rows.append({"rebill": r, "signed_err_%": (pred_surv - fact_surv) / fact_surv * 100})
residual_df = pd.DataFrame(residual_rows)
pooled_by_rebill = residual_df.groupby("rebill")["signed_err_%"].agg(["count", "mean", "std"])
print("\n=== Residual err% by rebill, pooled, AFTER smooth h_base correction ===")
print(pooled_by_rebill.to_string(float_format=lambda x: f"{x:.2f}"))

reliable_residuals = pooled_by_rebill[pooled_by_rebill.index <= k_max_reliable]
unreliable_residuals = pooled_by_rebill[pooled_by_rebill.index > k_max_reliable]
slope_reliable = np.polyfit(reliable_residuals.index, reliable_residuals["mean"], 1)[0] if len(reliable_residuals) > 1 else np.nan
print(f"\nReliable zone (k<={k_max_reliable}) mean residual = {reliable_residuals['mean'].mean():.2f}%, "
      f"slope = {slope_reliable:+.3f} pp/rebill")
print(f"Tail (k>{k_max_reliable}) mean residual = {unreliable_residuals['mean'].mean():.2f}% (not fitted, informational)")
print(f"\nComparison -- reliable-zone slope: raw MAP ~ -1.5..-2 pp/rebill (grew from -4% to -14%), "
      f"scalar-c = +1.039, web_h_base(pointwise) = -0.393, THIS (smooth 2-param) = {slope_reliable:+.3f}")

# ============================================================
# 5. LOO
# ============================================================
def fit_correction(cohorts_subset):
    max_k_sub = max(c["max_mature_rebill"] for c in cohorts_subset)
    tbl = {}
    for k in range(1, max_k_sub + 1):
        at_risk, died = 0, 0
        for c in cohorts_subset:
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
        tbl[k] = (h, at_risk)
    tbl_df = pd.DataFrame({k: {"h_web_raw": h, "N_at_risk": n} for k, (h, n) in tbl.items()}).T
    tbl_df["h_ios"] = [state_ios["h_base"].get(k, np.nan) for k in tbl_df.index]
    tbl_df["reliable"] = tbl_df["N_at_risk"] >= RELIABILITY_N_THRESHOLD
    if not tbl_df["reliable"].any():
        return pd.Series(1.0, index=range(1, HMAX + 1))
    k_max_rel = tbl_df.index[tbl_df["reliable"]].max()
    rel_sub = tbl_df.loc[tbl_df["reliable"]].copy()
    rel_sub["log_ratio"] = np.log(rel_sub["h_web_raw"] / rel_sub["h_ios"])
    Xs = np.vstack([np.ones(len(rel_sub)), (rel_sub.index - 1)]).T
    ws = rel_sub["N_at_risk"].values
    a_b = np.linalg.lstsq(Xs * np.sqrt(ws)[:, None], rel_sub["log_ratio"].values * np.sqrt(ws), rcond=None)[0]
    a0, b0 = a_b
    corr = pd.Series(1.0, index=range(1, HMAX + 1))
    for k in range(1, int(k_max_rel) + 1):
        corr[k] = np.exp(a0 + b0 * (k - 1))
    f_b = corr[int(k_max_rel)]
    for j, k in enumerate(range(int(k_max_rel) + 1, int(k_max_rel) + TAPER_WIDTH + 1), start=1):
        if k > HMAX:
            break
        frac = j / (TAPER_WIDTH + 1)
        corr[k] = f_b * (1 - frac) + 1.0 * frac
    return corr


print("\n=== LOO: correction fit on 10 cohorts, evaluated on held-out ===")
loo_rows = []
for i, held_out in enumerate(cohorts):
    train_set = cohorts[:i] + cohorts[i + 1:]
    corr_loo = fit_correction(train_set)
    state_loo = dict(state_ios)
    state_loo["h_base"] = state_ios["h_base"] * corr_loo
    pred = predict_with_state(held_out, state_loo)
    fact = held_out["fact"]
    errs = []
    for r in range(1, held_out["max_mature_rebill"] + 1):
        if r not in fact.index:
            continue
        fact_surv = fact.loc[r, "rebill_percent"]
        pred_surv = pred.get(r, np.nan)
        if pd.isna(pred_surv) or fact_surv == 0:
            continue
        errs.append((pred_surv - fact_surv) / fact_surv * 100)
    mean_abs_err = np.mean(np.abs(errs)) if errs else np.nan
    mean_signed_err = np.mean(errs) if errs else np.nan
    loo_rows.append({
        "held_out_cohort": held_out["cohort_week"].date(), "held_out_max_mature_rebill": held_out["max_mature_rebill"],
        "held_out_N": held_out["N"], "mean_signed_err_%": mean_signed_err, "mean_abs_err_%": mean_abs_err,
    })
loo_df = pd.DataFrame(loo_rows)
print(loo_df.to_string(index=False, float_format=lambda x: f"{x:.2f}"))
print(f"\nLOO mean |err%| (smooth 2-param correction) = {loo_df['mean_abs_err_%'].mean():.2f}% "
      f"(scalar-c LOO = 8.67%, pointwise web_h_base LOO = 8.49%)")

print("\n=== Overfit control ===")
print("Number of fitted parameters: 2 (alpha, beta) -- NOT 8 (one per week).")
print(f"Taper width beyond boundary: {TAPER_WIDTH} weeks, fixed (not fitted).")
