"""
Per-person LTV (CAC unit = person, PERSON_KEY = lower(email)), built from the
appsflyer-data-411716 BQ pull, reusing the ALREADY-CALIBRATED per-sub MAP model
(2-parameter web h_base correction) via a ratio[k] = person_LTV[k] / sub_LTV[k]
scaling factor -- not refit from scratch.

Run: .venv/Scripts/python.exe web_person_level_ltv.py
"""
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SNAPSHOT_TS = pd.Timestamp("2026-07-07")
WHITELIST_FUNNELS = {
    "device-security-check-gate", "device-security-gate",
    "detect-security-warnigs-gate-now", "device-security", "device-security-check",
}
ORGANIC_SENTINELS = {None, "", "unknown", "none"}
RELIABILITY_N_THRESHOLD = 40
OUT_DIR = "reports/web_model/04_person_level"

dim = pd.read_parquet("data/raw/bq_appsflyer_person_dim_2026-07-11.parquet")
week = pd.read_parquet("data/raw/bq_appsflyer_person_week_2026-07-11.parquet")
dim["first_date"] = pd.to_datetime(dim["first_date"])
week["week_num"] = week["week_num"].astype(int)
week["week_amt"] = week["week_amt"].astype(float)

print(f"Loaded: {len(dim)} people (dimension), {len(week)} person-week revenue rows")

# ============================================================
# Filter: cohort_week (first_date) >= 2026-04-13, funnel whitelist + organic
# ============================================================
dim_scoped = dim[dim["first_date"] >= "2026-04-13"].copy()
print(f"\nAfter first_date>=2026-04-13: {len(dim_scoped)} / {len(dim)} people")

def funnel_class(f):
    if f in ORGANIC_SENTINELS:
        return "organic"
    if f in WHITELIST_FUNNELS:
        return "whitelist"
    return "other_funnel"

dim_scoped["funnel_class"] = dim_scoped["first_funnel"].apply(funnel_class)
print(dim_scoped["funnel_class"].value_counts().to_string())

kept = dim_scoped[dim_scoped["funnel_class"].isin(["whitelist", "organic"])].copy()
dropped = dim_scoped[dim_scoped["funnel_class"] == "other_funnel"]
print(f"\nKept (whitelist + organic): {len(kept)} people")
print(f"Dropped (named funnel NOT in whitelist): {len(dropped)} people")
print(f"  whitelist breakdown: {kept.loc[kept.funnel_class=='whitelist','first_funnel'].value_counts().to_dict()}")

# ============================================================
# Per-person cumulative LTV curve, pooled, by week_num
# ============================================================
kept_ids = set(kept["person_key"])
week_kept = week[week["person_key"].isin(kept_ids)].merge(
    kept[["person_key", "first_date"]], on="person_key", how="left"
)
week_kept["age_days"] = (SNAPSHOT_TS - week_kept["first_date"]).dt.days

n_people = len(kept)
n_payments = len(week_kept)
print(f"\nN people (final cohort): {n_people}")
print(f"N person-week revenue rows: {n_payments}, avg rows/person: {n_payments/n_people:.2f}")

max_k = int(week_kept["week_num"].max())
person_cum = week_kept.pivot_table(index="person_key", columns="week_num", values="week_amt", aggfunc="sum", fill_value=0.0)
person_cum = person_cum.reindex(columns=range(0, max_k + 1), fill_value=0.0).cumsum(axis=1)

age_by_person = kept.set_index("person_key")["first_date"].apply(lambda d: (SNAPSHOT_TS - d).days)

fact_rows = []
for k in range(0, max_k + 1):
    eligible = age_by_person[age_by_person >= (k + 1) * 7].index
    eligible = eligible.intersection(person_cum.index)
    n = len(eligible)
    if n == 0:
        continue
    avg = person_cum.loc[eligible, k].mean()
    fact_rows.append({"week": k, "n": n, "person_ltv": avg})
fact_person = pd.DataFrame(fact_rows).set_index("week")
print("\n=== Fact per-person LTV curve (pooled), by week ===")
print(fact_person.to_string(float_format=lambda x: f"{x:.2f}"))

k_max_reliable_person = fact_person.index[fact_person["n"] >= RELIABILITY_N_THRESHOLD].max()
print(f"\nReliability boundary (person, N>={RELIABILITY_N_THRESHOLD}): week<= {k_max_reliable_person}")

# ============================================================
# Step 3: ratio[k] = person_LTV[k] / sub_LTV[k], reusing the EXISTING pooled
# per-sub fact curve (11-cohort $9.99/week pool, already computed/calibrated
# against, not re-derived here).
# ============================================================
from compare_map_to_local_sql_style_may_04_10 import (
    load_golden, filter_provider_and_app, subscription_start_table,
    local_paid_events, load_web_matrix, sql_style_summary,
    SNAPSHOT_TS as SUB_SNAPSHOT_TS, TARGET_STRIPE_PRICE_ID,
)

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

MIN_FIRST_PAYERS, MIN_MATURE_REBILL = 15, 3

def build_sub_cohort(cw):
    subs0 = pd.Index(starts_9_99.loc[starts_9_99["cohort_week"].eq(cw), "subscription_id"].unique())
    cp = paid_all[paid_all["subscription_id"].isin(subs0)]
    fp = pd.Index(cp.loc[cp["rebill_number"].eq(0), "subscription_id"].unique(), dtype="string")
    if len(fp) < MIN_FIRST_PAYERS:
        return None
    common_payers = fp.intersection(matrix_subs)
    age = int((SUB_SNAPSHOT_TS.tz_localize(None) - cw.tz_localize(None)).days // 7)
    mmr = max(0, age - 2)
    if mmr < MIN_MATURE_REBILL:
        return None
    fact = sql_style_summary(cp, common_payers, mmr)
    return {"cw": cw, "N": len(common_payers), "mmr": mmr, "fact": fact.set_index("rebill_number")}

cohort_weeks = sorted(starts_9_99["cohort_week"].unique())
sub_cohorts = [c for c in (build_sub_cohort(cw) for cw in cohort_weeks) if c is not None]

sub_ltv_rows = []
for r in range(0, max_k + 1):
    vals, weights = [], []
    for c in sub_cohorts:
        if r <= c["mmr"] and r in c["fact"].index:
            vals.append(c["fact"].loc[r, "payer_ltv"])
            weights.append(c["N"])
    if vals:
        sub_ltv_rows.append({"week": r, "sub_ltv": np.average(vals, weights=weights), "n_sub_cohorts": len(vals)})
sub_ltv = pd.DataFrame(sub_ltv_rows).set_index("week")

ratio_table = fact_person[["n", "person_ltv"]].join(sub_ltv[["sub_ltv"]], how="inner")
ratio_table["ratio"] = ratio_table["person_ltv"] / ratio_table["sub_ltv"]
print("\n=== ratio[k] = person_LTV[k] / sub_LTV[k] ===")
print(ratio_table.to_string(float_format=lambda x: f"{x:.3f}"))
print(f"\nRATIO DATA STAMP: sub-pool from compare_map_to_local_sql_style_may_04_10.py "
      f"(11 cohorts, $9.99/week, pulled 2026-07-09 web-payment-orchestration); "
      f"person data pulled 2026-07-11 appsflyer-data-411716. Both computed {pd.Timestamp.now().date() if False else '2026-07-11'}.")

ratio_table.to_csv(f"{OUT_DIR}/ratio_person_vs_sub.csv")

# ============================================================
# Step 4: apply ratio[k] to the EXISTING calibrated per-sub forecast (2-param
# web h_base correction, NOT refit here). Hold last reliable ratio flat beyond
# week 11 -- do not extrapolate ratio upward (would overstate).
# ============================================================
from models import common, map_model

HMAX = 52
TAPER_WIDTH = 3
RELIABILITY_N_THRESHOLD_SUB = 40

mx_ios = common.load_matrix()
apps_info_ios = common.select_apps(mx_ios)
state_ios = map_model.fit(mx_ios, apps_info_ios)

max_k_sub = max(c["mmr"] for c in sub_cohorts)
h_rows = []
for k in range(1, max_k_sub + 1):
    at_risk, died = 0, 0
    for c in sub_cohorts:
        if c["mmr"] < k:
            continue
        fact = c["fact"]
        prev = c["N"] if k == 1 else fact.loc[k - 1, "active_users"]
        cur = fact.loc[k, "active_users"] if k in fact.index else np.nan
        if pd.isna(cur):
            continue
        at_risk += prev
        died += (prev - cur)
    h = died / at_risk if at_risk else np.nan
    h_rows.append({"k": k, "N_at_risk": at_risk, "h_web_raw": h})
h_table = pd.DataFrame(h_rows).set_index("k")
h_table["h_ios"] = [state_ios["h_base"].get(k, np.nan) for k in h_table.index]
h_table["reliable"] = h_table["N_at_risk"] >= RELIABILITY_N_THRESHOLD_SUB
k_max_reliable_sub = h_table.index[h_table["reliable"]].max()

rel = h_table.loc[h_table["reliable"]].copy()
rel["log_ratio"] = np.log(rel["h_web_raw"] / rel["h_ios"])
X = np.vstack([np.ones(len(rel)), (rel.index - 1).values]).T
w = rel["N_at_risk"].values
alpha, beta = np.linalg.lstsq(X * np.sqrt(w)[:, None], rel["log_ratio"].values * np.sqrt(w), rcond=None)[0]

correction = pd.Series(1.0, index=range(1, HMAX + 1))
for k in range(1, k_max_reliable_sub + 1):
    correction[k] = np.exp(alpha + beta * (k - 1))
f_boundary = correction[k_max_reliable_sub]
for j, k in enumerate(range(k_max_reliable_sub + 1, k_max_reliable_sub + TAPER_WIDTH + 1), start=1):
    if k > HMAX:
        break
    frac = j / (TAPER_WIDTH + 1)
    correction[k] = f_boundary * (1 - frac) + 1.0 * frac
state_web = dict(state_ios)
state_web["h_base"] = state_ios["h_base"] * correction
print(f"\nReconstructed sub-level calibration: alpha={alpha:.4f}, beta={beta:.4f}, boundary k<={k_max_reliable_sub}")

pooled_visible = pd.concat([c_["fact"].assign(_dummy=1) for c_ in []], ignore_index=True) if False else None
# Need the actual se_training visible rows for the pooled sub cohorts to call predict(); rebuild via same helper as before.
from compare_map_to_local_sql_style_may_04_10 import visible_at_week_n

def build_sub_cohort_visible(cw):
    subs0 = pd.Index(starts_9_99.loc[starts_9_99["cohort_week"].eq(cw), "subscription_id"].unique())
    cp = paid_all[paid_all["subscription_id"].isin(subs0)]
    fp = pd.Index(cp.loc[cp["rebill_number"].eq(0), "subscription_id"].unique(), dtype="string")
    if len(fp) < MIN_FIRST_PAYERS:
        return None
    common_payers = fp.intersection(matrix_subs)
    age = int((SUB_SNAPSHOT_TS.tz_localize(None) - cw.tz_localize(None)).days // 7)
    mmr = max(0, age - 2)
    if mmr < MIN_MATURE_REBILL:
        return None
    cmx = web_all[web_all["sub_id"].isin(common_payers)].copy()
    vis = visible_at_week_n(cmx, mmr)
    if vis.empty:
        return None
    return vis

visibles = [v for v in (build_sub_cohort_visible(cw) for cw in cohort_weeks) if v is not None]
pooled_visible = pd.concat(visibles, ignore_index=True)
pooled_survival = map_model.predict(state_web, pooled_visible, k_max_reliable_sub)
pooled_arpu = pd.Series(9.99, index=range(0, HMAX + 1))

def raw_ltv(surv, arpu):
    cum = float(arpu.loc[0])
    out = {0: cum}
    for k in range(1, HMAX + 1):
        s = surv.get(k, np.nan)
        a = arpu.get(k, np.nan)
        if pd.notna(s) and pd.notna(a):
            cum += float(s) * float(a)
        out[k] = cum
    return pd.Series(out)

sub_forecast = raw_ltv(pooled_survival, pooled_arpu)

# ratio for scaling: use fitted ratio_table where available, hold LAST RELIABLE ratio flat beyond.
last_reliable_ratio_week = ratio_table.index[ratio_table["n"] >= RELIABILITY_N_THRESHOLD].max()
last_reliable_ratio = ratio_table.loc[last_reliable_ratio_week, "ratio"]
ratio_full = ratio_table["ratio"].reindex(range(0, HMAX + 1))
ratio_full = ratio_full.ffill()
ratio_full = ratio_full.fillna(last_reliable_ratio)
print(f"\nLast reliable ratio week={last_reliable_ratio_week}, value={last_reliable_ratio:.3f} "
      f"-- held flat for weeks {last_reliable_ratio_week+1}..{HMAX}")

person_forecast = sub_forecast * ratio_full
print("\n=== Person-level forecast (sub forecast x ratio), weeks 0,4,8,11,20,36,52 ===")
for k in [0, 4, 8, 11, 20, 36, 52]:
    print(f"  wk{k}: sub_forecast=${sub_forecast.get(k, float('nan')):.2f}, ratio={ratio_full.get(k, float('nan')):.3f}, "
          f"person_forecast=${person_forecast.get(k, float('nan')):.2f}")

# ============================================================
# Step 5: chart -- fact (solid, to observation limit) vs forecast (dashed, to wk52)
# ============================================================
fig, ax = plt.subplots(figsize=(12, 7.5))

fact_x = fact_person.index[fact_person.index <= k_max_reliable_person]
fact_y = fact_person.loc[fact_x, "person_ltv"]
ax.plot(fact_x, fact_y, color="black", linewidth=2.5, marker="o", label="Fact per-person LTV (pooled)", zorder=5)

LOO_BAND_PCT = 0.0843  # unchanged from the calibrated sub-level model's own LOO validation
model_x = list(range(0, HMAX + 1))
model_y = np.array([person_forecast.get(k, np.nan) for k in model_x])
upper = model_y * (1 + LOO_BAND_PCT)
lower = model_y * (1 - LOO_BAND_PCT)
ax.fill_between(model_x, lower, upper, color="#8E44AD", alpha=0.15, zorder=2,
                 label=f"±{LOO_BAND_PCT*100:.0f}% LOO spread (from sub-level model)")
ax.plot(model_x, model_y, color="#8E44AD", linewidth=2.0, linestyle="--",
        label="Calibrated forecast (per-sub model x ratio[k])", zorder=4)

ax.axvline(k_max_reliable_person, color="grey", linestyle=":", linewidth=1.2)
ymax = max(upper.max(), fact_y.max()) * 1.05
ax.set_ylim(0, ymax)
ax.text(k_max_reliable_person + 0.5, ymax * 0.05, f"дальше — экстраполяция, N<{RELIABILITY_N_THRESHOLD}",
        rotation=90, fontsize=9, color="grey", va="bottom")

final_val = model_y[-1]
ax.annotate(f"wk52: ${final_val:.2f}\nна человека, не на подписку\nratio person/sub датирован 2026-07-11\n"
            f"(±{LOO_BAND_PCT*100:.0f}%: ${lower[-1]:.2f}-${upper[-1]:.2f})",
            xy=(HMAX, final_val), xytext=(HMAX - 18, final_val * 0.55),
            fontsize=9, fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.35", fc="white", ec="#8E44AD", alpha=0.95),
            arrowprops=dict(arrowstyle="->", color="#8E44AD", lw=1))

ax.set_xlabel("Week since person's first payment")
ax.set_ylabel("Cumulative LTV per PERSON (USD)")
ax.set_xlim(0, HMAX)
ax.grid(True, alpha=0.3)
ax.legend(fontsize=9, loc="upper left")
ax.set_title(f"Per-person LTV forecast, invinci web (N={n_people} people, funnel-whitelisted + organic, start>=2026-04-13)\n"
             f"на человека, не на подписку; ratio person/sub датирован 2026-07-11")
fig.tight_layout()

OUT = f"{OUT_DIR}/person_ltv_forecast.png"
fig.savefig(OUT, dpi=160, bbox_inches="tight")
print(f"\nwrote {OUT}")
