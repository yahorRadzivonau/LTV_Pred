"""
STEP 3: per-person LTV tables by (cohort_date, utm_source) and (cohort_date, funnel),
using ONLY real captured revenue (web_person_level_revenue.py's base/ups split).
Reuses the ALREADY-CALIBRATED per-sub MAP model (2-param smooth h_base correction,
same constants as web_hbase_smooth_correction.py / web_boss_charts_ab.py) purely as a
RELATIVE growth shape to extrapolate each cell's own fact anchor forward. No new
tuning, no new ratio -- just the accepted shape applied to clean per-person facts.

Run: .venv/Scripts/python.exe web/golden/web_person_level_tables.py
"""
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Resolve all project-relative paths from the repository root, regardless of
# the working directory configured in PyCharm or the shell.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from core.web_calibration import (
    load_golden, filter_provider_and_app, subscription_start_table,
    local_paid_events, load_web_matrix, sql_style_summary, visible_at_week_n,
    raw_map_ltv, SNAPSHOT_TS, TARGET_STRIPE_PRICE_ID,
)
from core import common, map_model
from core.common import HMAX
from ltv.config import (
    RELIABILITY_N_THRESHOLD, MIN_FIRST_PAYERS, MIN_MATURE_REBILL, TAPER_WIDTH,
    LOW_N_CELL_THRESHOLD, H_EXT,
)

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 25)

OUT_DIR = "reports/web_model/05_person_level_clean"

# ================================================================ 1. relative growth SHAPE
# (identical reconstruction as web_boss_charts_ab.py -- not refit, just reused)
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
    return {"cohort_week": cw, "N": len(common_payers), "max_mature_rebill": max_mature_rebill,
            "fact": common_fact.set_index("rebill_number"), "visible": visible}


cohort_weeks = sorted(starts_9_99["cohort_week"].unique())
cohorts = [c for c in (build_cohort_data(cw) for cw in cohort_weeks) if c is not None]

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
print(f"Reconstructed calibration: alpha={alpha:.4f}, beta={beta:.4f}, k_max_reliable={k_max_reliable} "
      f"(identical to web_boss_charts_ab.py)")

pooled_visible = pd.concat([c["visible"] for c in cohorts], ignore_index=True)
pooled_survival = map_model.predict(state_web, pooled_visible, k_max_reliable)
pooled_arpu = pd.Series(9.99, index=range(0, HMAX + 1))
shape = raw_map_ltv(pooled_survival, pooled_arpu)  # cumulative $/sub, index 0..HMAX

# extend 52 -> 104: hold the average weekly increment of the last 8 reliable-ish weeks constant (flat tail, wide corridor, per spec)
last_incr = (shape.loc[HMAX] - shape.loc[HMAX - 8]) / 8
shape_ext = shape.reindex(range(0, H_EXT + 1))
for k in range(HMAX + 1, H_EXT + 1):
    shape_ext.loc[k] = shape_ext.loc[k - 1] + last_incr
shape = shape_ext
print(f"Shape (relative growth curve) built: wk4={shape.loc[4]:.2f} wk12={shape.loc[12]:.2f} "
      f"wk26={shape.loc[26]:.2f} wk52={shape.loc[52]:.2f} wk104(extrapolated)={shape.loc[104]:.2f}")


def shape_ratio(target_k, anchor_k):
    a = max(0, min(anchor_k, H_EXT))
    t = max(0, min(target_k, H_EXT))
    base = shape.loc[a] if a > 0 else 1e-9
    return shape.loc[t] / base if base > 0 else 1.0


# ================================================================ 2. clean per-person revenue + attribution
pop = pd.read_parquet("data/raw/_tmp_person_pop_5406.parquet").rename(columns={"person_key": "email"})
rev = pd.read_parquet("data/raw/_tmp_revenue_events_with_email.parquet")
rev = rev.dropna(subset=["email"])

w = pd.read_parquet("data/raw/web_conversions.parquet")
w["email"] = w["email"].str.lower()
w["event_date"] = pd.to_datetime(w["event_date"], utc=True, errors="coerce")
w = w[w["email"].isin(set(pop["email"]))].sort_values("event_date")
utm_map = w.groupby("email").first().reset_index()[["email", "utm_source"]]

pop = pop.merge(utm_map, on="email", how="left")
pop["utm_source"] = pop["utm_source"].fillna("(missing)")
pop["first_funnel"] = pop["first_funnel"].fillna("unknown").replace("", "unknown")
pop["first_date"] = pd.to_datetime(pop["first_date"], utc=True)
pop["age_weeks_now"] = ((SNAPSHOT_TS - pop["first_date"]).dt.days // 7).clip(lower=0)

rev_pop = rev[rev["email"].isin(set(pop["email"]))].merge(
    pop[["email", "first_date"]], on="email", how="left"
)
rev_pop["week_of_life"] = ((rev_pop["event_datetime"].dt.tz_convert("UTC") - rev_pop["first_date"]).dt.days // 7).clip(lower=0)

print(f"\nPopulation: {len(pop)} people (matches the previously-verified 5406)")
print(f"Revenue rows matched into population: {len(rev_pop)}, distinct payers: {rev_pop['email'].nunique()}")

# per-person-week base/ups revenue -> cumulative
pw = rev_pop.groupby(["email", "week_of_life"]).agg(base=("base_amt", "sum"), ups=("ups_amt", "sum")).reset_index()


def person_cum_at(email_weeks_df, max_week):
    """cumulative base/ups per email up to (and including) max_week, vectorised via pivot."""
    piv_base = email_weeks_df.pivot_table(index="email", columns="week_of_life", values="base", aggfunc="sum", fill_value=0.0)
    piv_ups = email_weeks_df.pivot_table(index="email", columns="week_of_life", values="ups", aggfunc="sum", fill_value=0.0)
    piv_base = piv_base.reindex(columns=range(0, max_week + 1), fill_value=0.0).cumsum(axis=1)
    piv_ups = piv_ups.reindex(columns=range(0, max_week + 1), fill_value=0.0).cumsum(axis=1)
    return piv_base, piv_ups


MAX_OBS_WEEK = int(pop["age_weeks_now"].max())
cum_base, cum_ups = person_cum_at(pw, MAX_OBS_WEEK)
cum_base = cum_base.reindex(pop["email"], fill_value=0.0)
cum_ups = cum_ups.reindex(pop["email"], fill_value=0.0)
cum_base.index = pop["email"].values
cum_ups.index = pop["email"].values

pop = pop.set_index("email")
pop["base_to_date"] = [cum_base.loc[e, min(int(pop.loc[e, "age_weeks_now"]), MAX_OBS_WEEK)] for e in pop.index]
pop["ups_to_date"] = [cum_ups.loc[e, min(int(pop.loc[e, "age_weeks_now"]), MAX_OBS_WEEK)] for e in pop.index]
pop = pop.reset_index()

total_rev_check = pop["base_to_date"].sum() + pop["ups_to_date"].sum()
print(f"Total clean revenue attributed to the 5406 population: ${total_rev_check:,.2f} "
      f"(base=${pop['base_to_date'].sum():,.2f}, ups=${pop['ups_to_date'].sum():,.2f})")
print(f"Clean LTV / person = ${total_rev_check/len(pop):.2f}  (n_people={len(pop)})")

pop.to_parquet("data/raw/_tmp_pop_final.parquet")
cum_base.to_parquet("data/raw/_tmp_cum_base.parquet")
cum_ups.to_parquet("data/raw/_tmp_cum_ups.parquet")

# ================================================================ 3. cohort x source / cohort x funnel tables
HORIZONS = [4, 12, 26, 52, 104]


def cell_ltv_row(emails, group_key_vals):
    emails = list(emails)
    n = len(emails)
    ages = pop.set_index("email").loc[emails, "age_weeks_now"]
    base_now = pop.set_index("email").loc[emails, "base_to_date"]
    ups_now = pop.set_index("email").loc[emails, "ups_to_date"]
    current_ltv = base_now.mean()
    current_ltv_ups = (base_now + ups_now).mean()
    cell_age = int(ages.mean())

    row = {"n_people": n, "avg_age_weeks": cell_age,
           "current_ltv": current_ltv, "current_ltv_ups": current_ltv_ups,
           "low_n": n < LOW_N_CELL_THRESHOLD}

    for N in HORIZONS:
        mature = ages[ages >= N].index
        n_mature = len(mature)
        if n_mature >= 1:
            colN = min(N, MAX_OBS_WEEK)
            fact_base = cum_base.loc[mature, colN].mean()
            fact_ups = cum_ups.loc[mature, colN].mean()
            row[f"ltv_{N}"] = fact_base
            row[f"ltv_{N}_ups"] = fact_base + fact_ups
            row[f"ltv_{N}_source"] = "fact"
            row[f"ltv_{N}_n_mature"] = n_mature
        else:
            ratio = shape_ratio(N, max(cell_age, 1))
            row[f"ltv_{N}"] = current_ltv * ratio
            row[f"ltv_{N}_ups"] = current_ltv_ups * ratio
            row[f"ltv_{N}_source"] = "model"
            row[f"ltv_{N}_n_mature"] = 0
    return row


pop["cohort_date"] = (pop["first_date"] - pd.to_timedelta(pop["first_date"].dt.weekday, unit="D")).dt.normalize()

for group_col, out_name, label in [("utm_source", "table_A_cohort_utm.csv", "Table A (cohort_date x utm_source)"),
                                     ("first_funnel", "table_B_cohort_funnel.csv", "Table B (cohort_date x funnel)")]:
    rows = []
    for (cd, gv), sub in pop.groupby(["cohort_date", group_col]):
        r = cell_ltv_row(sub["email"].tolist(), (cd, gv))
        r["cohort_date"] = cd.date().isoformat()
        r[group_col] = gv
        rows.append(r)
    tbl = pd.DataFrame(rows)
    cols = ["cohort_date", group_col, "n_people", "avg_age_weeks", "low_n",
            "current_ltv", "current_ltv_ups"]
    for N in HORIZONS:
        cols += [f"ltv_{N}", f"ltv_{N}_ups", f"ltv_{N}_source", f"ltv_{N}_n_mature"]
    tbl = tbl[cols].sort_values(["cohort_date", group_col])
    path = f"{OUT_DIR}/{out_name}"
    tbl.to_csv(path, index=False)
    n_low = tbl["low_n"].sum()
    print(f"\n=== {label} ===")
    print(f"rows(cells)={len(tbl)}, low_n(<{LOW_N_CELL_THRESHOLD} people) cells={n_low} ({n_low/len(tbl):.1%})")
    print(f"wrote {path}")
    print(tbl.head(8).to_string(index=False))
