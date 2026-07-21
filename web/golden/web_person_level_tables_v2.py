"""
FIX 2: cohort x utm_source / cohort x funnel tables, built on the refund-fixed
revenue (web_person_level_fix.py). Two EXPLICIT denominators per horizon:
  ltv_per_attributed[_ups] = revenue / n_attributed  (all acquired people, for CAC/LTV)
  ltv_per_payer[_ups]      = revenue / n_payers       (unit economics, payers only)
Growth SHAPE (relative extrapolation curve) is the identical, unmodified
reconstruction from web_person_level_tables.py -- not refit here.

Run: .venv/Scripts/python.exe web/golden/web_person_level_tables_v2.py
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
from ltv.config import (
    HMAX, H_EXT, RELIABILITY_N_THRESHOLD, TAPER_WIDTH, MIN_FIRST_PAYERS,
    MIN_MATURE_REBILL, LOW_N_CELL_THRESHOLD, HORIZONS_REPORT,
)
from core import common, map_model

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 25)

OUT_DIR = "reports/web_golden/05_person_level_clean"
HORIZONS = list(HORIZONS_REPORT)

# ================================================================ 1. relative growth SHAPE (unchanged reconstruction)
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

pooled_visible = pd.concat([c["visible"] for c in cohorts], ignore_index=True)
pooled_survival = map_model.predict(state_web, pooled_visible, k_max_reliable)
pooled_arpu = pd.Series(9.99, index=range(0, HMAX + 1))
shape = raw_map_ltv(pooled_survival, pooled_arpu)

last_incr = (shape.loc[HMAX] - shape.loc[HMAX - 8]) / 8
shape_ext = shape.reindex(range(0, H_EXT + 1))
for k in range(HMAX + 1, H_EXT + 1):
    shape_ext.loc[k] = shape_ext.loc[k - 1] + last_incr
shape = shape_ext


def shape_ratio(target_k, anchor_k):
    a = max(0, min(anchor_k, H_EXT))
    t = max(0, min(target_k, H_EXT))
    base = shape.loc[a] if a > 0 else 1e-9
    return shape.loc[t] / base if base > 0 else 1.0


# ================================================================ 2. fixed per-person data
pop = pd.read_parquet("data/raw/_tmp_pop_fixed.parquet")
cum_base = pd.read_parquet("data/raw/_tmp_cum_base_fixed.parquet")
cum_ups = pd.read_parquet("data/raw/_tmp_cum_ups_fixed.parquet")
MAX_OBS_WEEK = cum_base.shape[1] - 1
pop["cohort_date"] = (pop["first_date"] - pd.to_timedelta(pop["first_date"].dt.weekday, unit="D")).dt.normalize()


def cell_row(emails):
    emails = list(emails)
    sub = pop.set_index("email").loc[emails]
    n_attributed = len(sub)
    n_payers = int(sub["is_payer"].sum())
    ages = sub["age_weeks_now"]
    cell_age = int(ages.mean())

    current_ltv_all = sub["base_to_date"]
    current_ltv_ups_all = sub["base_to_date"] + sub["ups_to_date"]
    payer_mask = sub["is_payer"]

    row = {
        "n_attributed": n_attributed, "n_payers": n_payers, "avg_age_weeks": cell_age,
        "low_n": n_payers < LOW_N_CELL_THRESHOLD,
        "ltv_per_attributed_current": current_ltv_all.mean(),
        "ltv_per_attributed_current_ups": current_ltv_ups_all.mean(),
        "ltv_per_payer_current": current_ltv_all[payer_mask].mean() if n_payers else 0.0,
        "ltv_per_payer_current_ups": current_ltv_ups_all[payer_mask].mean() if n_payers else 0.0,
    }

    for N in HORIZONS:
        mature = ages[ages >= N].index
        n_mature = len(mature)
        if n_mature >= 1:
            colN = min(N, MAX_OBS_WEEK)
            fact_base = cum_base.loc[mature, colN]
            fact_ups = cum_ups.loc[mature, colN]
            fact_total_ups = fact_base + fact_ups
            mature_payers = [e for e in mature if pop.set_index("email").loc[e, "is_payer"]]
            n_mature_payers = len(mature_payers)

            row[f"ltv_per_attributed_{N}"] = fact_base.mean()
            row[f"ltv_per_attributed_{N}_ups"] = fact_total_ups.mean()
            row[f"ltv_per_payer_{N}"] = fact_base.loc[mature_payers].mean() if n_mature_payers else 0.0
            row[f"ltv_per_payer_{N}_ups"] = fact_total_ups.loc[mature_payers].mean() if n_mature_payers else 0.0
            row[f"ltv_{N}_source"] = "fact"
            row[f"ltv_{N}_n_mature"] = n_mature
        else:
            ratio = shape_ratio(N, max(cell_age, 1))
            row[f"ltv_per_attributed_{N}"] = row["ltv_per_attributed_current"] * ratio
            row[f"ltv_per_attributed_{N}_ups"] = row["ltv_per_attributed_current_ups"] * ratio
            row[f"ltv_per_payer_{N}"] = row["ltv_per_payer_current"] * ratio
            row[f"ltv_per_payer_{N}_ups"] = row["ltv_per_payer_current_ups"] * ratio
            row[f"ltv_{N}_source"] = "model"
            row[f"ltv_{N}_n_mature"] = 0
    return row


for group_col, out_name, label in [("utm_source", "table_A_cohort_utm_v2.csv", "Table A (cohort_date x utm_source)"),
                                     ("first_funnel", "table_B_cohort_funnel_v2.csv", "Table B (cohort_date x funnel)")]:
    rows = []
    for (cd, gv), sub in pop.groupby(["cohort_date", group_col]):
        r = cell_row(sub["email"].tolist())
        r["cohort_date"] = cd.date().isoformat()
        r[group_col] = gv
        rows.append(r)
    tbl = pd.DataFrame(rows)
    cols = ["cohort_date", group_col, "n_attributed", "n_payers", "avg_age_weeks", "low_n",
            "ltv_per_attributed_current", "ltv_per_attributed_current_ups",
            "ltv_per_payer_current", "ltv_per_payer_current_ups"]
    for N in HORIZONS:
        cols += [f"ltv_per_attributed_{N}", f"ltv_per_attributed_{N}_ups",
                 f"ltv_per_payer_{N}", f"ltv_per_payer_{N}_ups",
                 f"ltv_{N}_source", f"ltv_{N}_n_mature"]
    tbl = tbl[cols].sort_values(["cohort_date", group_col])
    path = f"{OUT_DIR}/{out_name}"
    tbl.to_csv(path, index=False)

    n_low = tbl["low_n"].sum()
    mature_cells = tbl[tbl["ltv_52_source"] == "fact"]
    lo, hi = (tbl["ltv_per_attributed_current_ups"].min(), tbl["ltv_per_attributed_current_ups"].max())
    print(f"\n=== {label} ===")
    print(f"rows(cells)={len(tbl)}, low_n (n_payers<{LOW_N_CELL_THRESHOLD}) cells={n_low} ({n_low/len(tbl):.1%})")
    print(f"ltv_per_attributed_current_ups range across ALL cells: ${lo:.2f} - ${hi:.2f}")
    print(f"wrote {path}")

print(f"\nColumn legend: ltv_per_attributed_* divides by n_attributed (ALL acquired people, incl. non-payers -- CAC/LTV unit).")
print(f"               ltv_per_payer_* divides by n_payers (only people with >=1 real charge -- unit economics).")
