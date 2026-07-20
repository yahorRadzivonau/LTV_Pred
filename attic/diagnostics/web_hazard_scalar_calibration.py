"""
Step 2b-3: build a real per-week empirical web_h_base (KM-style pooled hazard
across the 11 mature $9.99/week cohorts), replacing the flat scalar (which
failed the slope check: residual drifted +1.04pp/rebill). Geo/channel/trial
multipliers are untouched -- they still sit on top of this new h_base.

Run: .venv/Scripts/python.exe web_hazard_scalar_calibration.py
"""
import numpy as np
import pandas as pd

from compare_map_to_local_sql_style_may_04_10 import (
    load_golden, filter_provider_and_app, subscription_start_table,
    local_paid_events, load_web_matrix, sql_style_summary, visible_at_week_n,
    SNAPSHOT_TS, HMAX, TARGET_STRIPE_PRICE_ID,
)
from models import common, map_model

MIN_FIRST_PAYERS = 15
MIN_MATURE_REBILL = 3
RELIABILITY_N_THRESHOLD = 40  # below this at-risk N, don't trust the raw weekly hazard

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
# Step 2b.1: pooled KM-style weekly hazard, h_web[k] = died[k] / at_risk[k]
# at_risk[k] = active_users at rebill k-1 (or N first-payers for k=1), summed
# across cohorts mature enough to have observed BOTH k-1 and k (no censoring bias).
# ============================================================
max_k = max(c["max_mature_rebill"] for c in cohorts)
rows = []
for k in range(1, max_k + 1):
    at_risk = 0
    died = 0
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
    rows.append({"k": k, "N_at_risk": at_risk, "died": died, "h_web_raw": h})

h_table = pd.DataFrame(rows).set_index("k")
h_table["h_ios"] = [state_ios["h_base"].get(k, np.nan) for k in h_table.index]
h_table["reliable"] = h_table["N_at_risk"] >= RELIABILITY_N_THRESHOLD

print("\n=== h_web (raw, pooled KM) vs h_ios, by week k ===")
print(h_table.to_string(float_format=lambda x: f"{x:.4f}"))

first_unreliable_k = h_table.index[~h_table["reliable"]].min() if (~h_table["reliable"]).any() else None
print(f"\nFirst unreliable week (N_at_risk < {RELIABILITY_N_THRESHOLD}): k={first_unreliable_k}")

# Choice (explicit, stated): hold the last reliable hazard value flat beyond the
# reliability threshold AND beyond observed range (k > max_k), rather than smoothing.
last_reliable_h = h_table.loc[h_table["reliable"], "h_web_raw"].iloc[-1]
h_web_full = h_table["h_web_raw"].copy()
h_web_full[~h_table["reliable"]] = last_reliable_h
h_web_full = h_web_full.reindex(range(1, HMAX + 1))
h_web_full = h_web_full.ffill().fillna(last_reliable_h)
print(f"\nHeld-flat value beyond reliability threshold / observed range: h={last_reliable_h:.4f} "
      f"(last reliable week's own value, weeks {first_unreliable_k}..{HMAX} held flat -- NOT smoothed, explicit choice)")

s_web = (1 - h_web_full).cumprod()
print("\nS_web (survival), weeks 1-15 sample:")
print(s_web.head(15).to_string(float_format=lambda x: f"{x:.4f}"))

state_web = dict(state_ios)
state_web["h_base"] = h_web_full


def predict_with_state(cohort, state):
    return map_model.predict(state, cohort["visible"], cohort["weeks"])


# ============================================================
# Step 3.1: residual by rebill, pooled, with web_h_base
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
        residual_rows.append({"rebill": r, "signed_err_%": (pred_surv - fact_surv) / fact_surv * 100,
                               "n_at_risk": h_table.loc[r, "N_at_risk"] if r in h_table.index else np.nan})

residual_df = pd.DataFrame(residual_rows)
pooled_by_rebill = residual_df.groupby("rebill").agg(count=("signed_err_%", "count"), mean=("signed_err_%", "mean"),
                                                        std=("signed_err_%", "std"))
print("\n=== Residual err% by rebill, pooled, AFTER web_h_base ===")
print(pooled_by_rebill.to_string(float_format=lambda x: f"{x:.2f}"))

reliable_ks = h_table.index[h_table["reliable"]]
reliable_residuals = pooled_by_rebill[pooled_by_rebill.index.isin(reliable_ks)]
unreliable_residuals = pooled_by_rebill[~pooled_by_rebill.index.isin(reliable_ks)]
print(f"\nReliable zone (N_at_risk>={RELIABILITY_N_THRESHOLD}, k<={reliable_ks.max()}): "
      f"mean residual = {reliable_residuals['mean'].mean():.2f}%")
if len(unreliable_residuals):
    print(f"Unreliable/extrapolated tail (k>{reliable_ks.max()}): mean residual = {unreliable_residuals['mean'].mean():.2f}%")

slope_all = np.polyfit(pooled_by_rebill.index, pooled_by_rebill["mean"], 1)[0]
slope_reliable = np.polyfit(reliable_residuals.index, reliable_residuals["mean"], 1)[0] if len(reliable_residuals) > 1 else np.nan
print(f"\nSlope (all rebills) = {slope_all:+.3f} pp/rebill")
print(f"Slope (reliable zone only) = {slope_reliable:+.3f} pp/rebill")
print(f"(scalar-c slope for comparison: +1.039 pp/rebill)")

# ============================================================
# Step 3.2: LOO with held-out maturity depth
# ============================================================
def fit_h_web(cohorts_subset):
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
    reliable_h = [h for k, (h, n) in tbl.items() if n >= RELIABILITY_N_THRESHOLD]
    last_h = reliable_h[-1] if reliable_h else list(tbl.values())[-1][0]
    h_series = pd.Series({k: (h if n >= RELIABILITY_N_THRESHOLD else last_h) for k, (h, n) in tbl.items()})
    h_series = h_series.reindex(range(1, HMAX + 1)).ffill().fillna(last_h)
    return h_series


print("\n=== LOO: web_h_base fit on 10 cohorts, evaluated on held-out ===")
loo_rows = []
for i, held_out in enumerate(cohorts):
    train_set = cohorts[:i] + cohorts[i + 1:]
    h_loo = fit_h_web(train_set)
    state_loo = dict(state_ios)
    state_loo["h_base"] = h_loo
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
print(f"\nLOO mean |err%| (web_h_base) = {loo_df['mean_abs_err_%'].mean():.2f}% "
      f"(scalar-c LOO was 8.67%)")

print("\n=== Pre-declaration check ===")
print(f"1. Slope flat (|slope| < ~0.3pp/rebill)? all-rebills slope={slope_all:+.3f}, "
      f"reliable-zone-only slope={slope_reliable:+.3f} "
      f"-> {'PASS' if abs(slope_reliable) < 0.3 else 'FAIL'} on reliable zone")
print(f"2. Residual near zero on reliable zone (N_at_risk>={RELIABILITY_N_THRESHOLD})? "
      f"mean={reliable_residuals['mean'].mean():.2f}% -> "
      f"{'PASS' if abs(reliable_residuals['mean'].mean()) < 3 else 'FAIL'}")
print(f"3. Tail (unreliable zone) may drift -- expected, not fitted there: "
      f"mean={unreliable_residuals['mean'].mean():.2f}% (informational only)" if len(unreliable_residuals) else "3. No unreliable-zone rebills observed in this pool")
