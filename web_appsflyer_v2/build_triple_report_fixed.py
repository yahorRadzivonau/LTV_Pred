"""
Triple breakdown: cohort_date x first_funnel x utm_source, on the appsflyer-
source pipeline (ltv_v2). Reads ONLY already-built local artifacts
(data/raw/_tmp_v2_pop_final.parquet, _tmp_v2_cum_base.parquet,
_tmp_v2_cum_ups.parquet, written by web_appsflyer_v2/build_tables.py) plus the
local golden/iOS files needed to reconstruct the growth shape
(load_golden(), common.load_matrix() -- both local parquet reads, no BigQuery).

NO BigQuery calls anywhere in this script. Purely local read -> compute ->
write to reports/. The upload step (schema + cast_columns + write_to_bigquery)
lives in web_appsflyer_v2/upload_triple_report_to_bq.py as inert code -- this
script does not import or call it.

Two variants per horizon only (per the ask): base, and base+ups_projected --
no ups_factonly here (unlike table_A/table_B).

Run: .venv/Scripts/python.exe web_appsflyer_v2/build_triple_report.py
"""
import os
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

# Resolve all project-relative paths from the repository root, regardless of
# the working directory configured in PyCharm or the shell.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from compare_map_to_local_sql_style_may_04_10 import (
    load_golden, filter_provider_and_app, subscription_start_table,
    local_paid_events, load_web_matrix, sql_style_summary, visible_at_week_n,
    raw_map_ltv, SNAPSHOT_TS, TARGET_STRIPE_PRICE_ID,
)
from models import common, map_model
from models.common import HMAX
from ltv.config import (
    RELIABILITY_N_THRESHOLD, MIN_FIRST_PAYERS, MIN_MATURE_REBILL, TAPER_WIDTH,
    LOW_N_CELL_THRESHOLD, H_EXT, HORIZONS_REPORT,
)
from ltv_v2.config import MONTH_STEP

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 30)

HORIZONS = HORIZONS_REPORT
OUT_DIR = "reports/web_appsflyer_v2"
SNAPSHOT_DATE = date(2026, 7, 20)  # true DATE value; matches the pop/revenue build's SNAPSHOT_NOW
MIN_COHORT_AGE_WEEKS = 2  # 2026-07-20 rule change: drop cohorts younger than this entirely --
# not flagged low_n, OUT of the sample. Upsell/rebills haven't had a chance to fire yet for a
# 0-1 week old cohort, so any LTV number for them is noise, not signal.

POP_PATH = "data/raw/_tmp_v2_pop_final.parquet"
CUM_BASE_PATH = "data/raw/_tmp_v2_cum_base.parquet"
CUM_UPS_PATH = "data/raw/_tmp_v2_cum_ups.parquet"

# ================================================================ 1. relative growth SHAPE (identical reconstruction, unchanged math -- local files only)
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
cohorts_list = [c for c in (build_cohort_data(cw) for cw in cohort_weeks) if c is not None]

max_k = max(c["max_mature_rebill"] for c in cohorts_list)
rows = []
for k in range(1, max_k + 1):
    at_risk, died = 0, 0
    for c in cohorts_list:
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

pooled_visible = pd.concat([c["visible"] for c in cohorts_list], ignore_index=True)
pooled_survival = map_model.predict(state_web, pooled_visible, k_max_reliable)
pooled_arpu = pd.Series(9.99, index=range(0, HMAX + 1))
shape = raw_map_ltv(pooled_survival, pooled_arpu)

last_incr = (shape.loc[HMAX] - shape.loc[HMAX - 8]) / 8
shape_ext = shape.reindex(range(0, H_EXT + 1))
for k in range(HMAX + 1, H_EXT + 1):
    shape_ext.loc[k] = shape_ext.loc[k - 1] + last_incr
shape = shape_ext
print(f"Growth shape reconstructed (unchanged calibration): alpha={alpha:.4f}, beta={beta:.4f}, "
      f"k_max_reliable={k_max_reliable}")


def shape_ratio(target_k, anchor_k):
    a = max(0, min(anchor_k, H_EXT))
    t = max(0, min(target_k, H_EXT))
    base = shape.loc[a] if a > 0 else 1e-9
    return shape.loc[t] / base if base > 0 else 1.0


# ================================================================ 1b. UPS monthly-cadence shape (same fix as build_tables.py)
implied_weekly_survival = shape.diff() / 9.99
implied_weekly_survival.loc[0] = 1.0

_month_ks = list(range(0, H_EXT + 1, MONTH_STEP))
_ups_cum = {0: 1.0}
_running = 1.0
for _m in _month_ks[1:]:
    _running += implied_weekly_survival.loc[_m]
    _ups_cum[_m] = _running
ups_cycles_cum = pd.Series(_ups_cum)


def _floor_month(k):
    return max(0, (int(k) // MONTH_STEP) * MONTH_STEP)


def ups_shape_ratio(target_k, anchor_k):
    t = min(_floor_month(target_k), H_EXT)
    a = min(_floor_month(max(anchor_k, 1)), H_EXT)
    base = ups_cycles_cum.get(a, ups_cycles_cum[0])
    return ups_cycles_cum.get(t, ups_cycles_cum[H_EXT]) / base if base > 0 else 1.0


# ================================================================ 2. load already-built v2 population + cumulative revenue (LOCAL parquet, no BigQuery)
pop = pd.read_parquet(POP_PATH)
cum_base = pd.read_parquet(CUM_BASE_PATH)
cum_ups = pd.read_parquet(CUM_UPS_PATH)
MAX_OBS_WEEK = cum_base.shape[1] - 1

print(f"\nLoaded population: {len(pop)} people ({POP_PATH})")
print(f"per-person-week cumulative arrays: weeks 0..{MAX_OBS_WEEK}")

n_cells_before_maturity = pop.groupby(["cohort_date", "first_funnel", "utm_source"]).ngroups
n_people_before_maturity = len(pop)
pop = pop[pop["age_weeks_now"] >= MIN_COHORT_AGE_WEEKS].reset_index(drop=True)
n_people_after_maturity = len(pop)
n_cells_after_maturity = pop.groupby(["cohort_date", "first_funnel", "utm_source"]).ngroups
print(f"\nMaturity filter: age_weeks_now >= {MIN_COHORT_AGE_WEEKS} weeks (cohorts younger than this dropped entirely, not low_n)")
print(f"  people: {n_people_before_maturity} -> {n_people_after_maturity} "
      f"(dropped {n_people_before_maturity - n_people_after_maturity} immature)")
print(f"  cells:  {n_cells_before_maturity} -> {n_cells_after_maturity} "
      f"(dropped {n_cells_before_maturity - n_cells_after_maturity} cells that were 100% immature people)")

pop_indexed = pop.set_index("email")


def cell_row(emails):
    emails = list(emails)
    sub = pop_indexed.loc[emails]
    n_attributed = len(sub)
    n_payers = int(sub["is_payer"].sum())
    ages = sub["age_weeks_now"]
    cell_age = int(ages.mean())
    payer_mask = sub["is_payer"]

    base_now = sub["base_net"]
    total_now = sub["total_net"]

    row = {
        "n_attributed": n_attributed, "n_payers": n_payers,
        "payer_rate": (n_payers / n_attributed) if n_attributed else 0.0,
        "avg_age_weeks": cell_age,
        "low_n": n_payers < LOW_N_CELL_THRESHOLD,
        "ltv_per_attributed_current": base_now.mean(),
        "ltv_per_attributed_current_ups": total_now.mean(),
        "ltv_per_payer_current": base_now[payer_mask].mean() if n_payers else 0.0,
        "ltv_per_payer_current_ups": total_now[payer_mask].mean() if n_payers else 0.0,
    }

    for N in HORIZONS:
        mature = ages[ages >= N].index
        n_mature = len(mature)
        if n_mature >= 1:
            colN = min(N, MAX_OBS_WEEK)
            fact_base = cum_base.loc[mature, colN]
            fact_total = fact_base + cum_ups.loc[mature, colN]
            mature_payers = [e for e in mature if pop_indexed.loc[e, "is_payer"]]
            n_mature_payers = len(mature_payers)

            row[f"ltv_per_attributed_{N}"] = fact_base.mean()
            row[f"ltv_per_payer_{N}"] = fact_base.loc[mature_payers].mean() if n_mature_payers else 0.0
            row[f"ltv_per_attributed_{N}_ups_projected"] = fact_total.mean()
            row[f"ltv_per_payer_{N}_ups_projected"] = fact_total.loc[mature_payers].mean() if n_mature_payers else 0.0
            row[f"ltv_{N}_source"] = "fact"
            row[f"ltv_{N}_n_mature"] = n_mature
            row[f"ltv_{N}_ups_projection_quality"] = "fact"
        else:
            ratio = shape_ratio(N, max(cell_age, 1))
            attributed_base_N = row["ltv_per_attributed_current"] * ratio
            payer_base_N = row["ltv_per_payer_current"] * ratio
            row[f"ltv_per_attributed_{N}"] = attributed_base_N
            row[f"ltv_per_payer_{N}"] = payer_base_N
            row[f"ltv_{N}_source"] = "model"
            row[f"ltv_{N}_n_mature"] = 0

            ups_only_attributed_now = row["ltv_per_attributed_current_ups"] - row["ltv_per_attributed_current"]
            ups_only_payer_now = row["ltv_per_payer_current_ups"] - row["ltv_per_payer_current"]
            ups_ratio = ups_shape_ratio(N, max(cell_age, 1))
            row[f"ltv_per_attributed_{N}_ups_projected"] = attributed_base_N + ups_only_attributed_now * ups_ratio
            row[f"ltv_per_payer_{N}_ups_projected"] = payer_base_N + ups_only_payer_now * ups_ratio
            row[f"ltv_{N}_ups_projection_quality"] = "rough_low_data"
    return row


# ================================================================ 3. triple breakdown: cohort_date x first_funnel x utm_source
rows = []
for (cd, funnel, utm), sub in pop.groupby(["cohort_date", "first_funnel", "utm_source"]):
    r = cell_row(sub["email"].tolist())
    r["cohort_date"] = cd.date()
    r["funnel"] = funnel
    r["utm_source"] = utm
    rows.append(r)

tbl = pd.DataFrame(rows)
tbl["snapshot_date"] = SNAPSHOT_DATE

# Keep both fields as Python datetime.date objects. With the pyarrow parquet
# engine this is written as Arrow date32, so BigQuery receives DATE rather than
# STRING or TIMESTAMP. The CSV representation remains YYYY-MM-DD.
for date_col in ("cohort_date", "snapshot_date"):
    tbl[date_col] = pd.to_datetime(tbl[date_col], errors="raise").dt.date

cols = ["cohort_date", "funnel", "utm_source", "n_attributed", "n_payers", "payer_rate", "avg_age_weeks", "low_n",
        "ltv_per_attributed_current", "ltv_per_attributed_current_ups",
        "ltv_per_payer_current", "ltv_per_payer_current_ups"]
for N in HORIZONS:
    cols += [f"ltv_per_attributed_{N}", f"ltv_per_attributed_{N}_ups_projected",
             f"ltv_per_payer_{N}", f"ltv_per_payer_{N}_ups_projected",
             f"ltv_{N}_source", f"ltv_{N}_n_mature", f"ltv_{N}_ups_projection_quality"]
cols += ["snapshot_date"]
tbl = tbl[cols].sort_values(["cohort_date", "funnel", "utm_source"]).reset_index(drop=True)

n_cells = len(tbl)
n_low = int(tbl["low_n"].sum())
n_tiny = int(((tbl["n_payers"] >= 1) & (tbl["n_payers"] <= 2)).sum())
print(f"\n=== Triple report (cohort_date x first_funnel x utm_source) ===")
print(f"n_attributed total = {tbl['n_attributed'].sum()} "
      f"(sanity: should be {n_people_after_maturity} = 5406 minus "
      f"{n_people_before_maturity - n_people_after_maturity} dropped by the maturity filter)")
print(f"N cells = {n_cells} (before maturity filter: {n_cells_before_maturity})")
print(f"low_n (n_payers<{LOW_N_CELL_THRESHOLD}) cells = {n_low} ({n_low/n_cells:.1%})")
print(f"n_payers in [1,2] cells = {n_tiny} ({n_tiny/n_cells:.1%})")
print(f"snapshot_date = {SNAPSHOT_DATE} (informational field -- run date, matches the pop/revenue "
      f"build's own SNAPSHOT_NOW age-reference date; not a version)")

csv_path = f"{OUT_DIR}/table_C_cohort_funnel_utm_appsflyer.csv"
parquet_path = f"{OUT_DIR}/table_C_cohort_funnel_utm_appsflyer.parquet"
tbl.to_csv(csv_path, index=False)
tbl.to_parquet(parquet_path, index=False)
print(f"\nwrote {csv_path}")
print(f"wrote {parquet_path}")
print(f"\n{tbl.head(10).to_string(index=False)}")