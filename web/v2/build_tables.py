"""
Per-person LTV tables (cohort_date x utm_source, cohort_date x funnel) on the
appsflyer-source pipeline (ltv_v2). Isolated from the golden pipeline: money
comes ONLY from ltv_v2.revenue (appsflyer captured events + refund haircut).

Population/attribution (email, first_date, first_funnel, utm_source) is
attribution-only metadata sourced from appsflyer itself (person_dim + local
web_conversions attribution snapshot) -- reused as-is from ltv.cohorts /
web_person_level_tables_v2.py's enrichment step, since it carries no golden
money and reusing it does not create a dependency on the golden revenue path.

Growth SHAPE (relative extrapolation curve) is the SAME unmodified calibrated
reconstruction used throughout the project (2-param smooth h_base correction,
alpha/beta refit live, K_SHRINK untouched) -- structure is new, math is not.

Four variants per horizon: {base, base+ups} x {per_attributed, per_payer}.

Run: .venv/Scripts/python.exe web/v2/build_tables.py
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
sys.path.insert(0, str(ROOT / "web" / "golden"))
os.chdir(ROOT)

from core.web_calibration import (
    load_golden, filter_provider_and_app, subscription_start_table,
    local_paid_events, load_web_matrix, sql_style_summary, visible_at_week_n,
    raw_map_ltv, fit_web_ios_calibration, SNAPSHOT_TS, TARGET_STRIPE_PRICE_ID,
)
from core import common, map_model
from core.common import HMAX
from ltv import cohorts
from ltv.config import (
    RELIABILITY_N_THRESHOLD, MIN_FIRST_PAYERS, MIN_MATURE_REBILL, TAPER_WIDTH,
    LOW_N_CELL_THRESHOLD, H_EXT, HORIZONS_REPORT,
)
from ltv_v2 import revenue as R2
from ltv_v2.config import WINDOW_START, MONTH_STEP, SNAPSHOT_DATE, OUT_DIR

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 30)

HORIZONS = HORIZONS_REPORT

# ================================================================ 1. relative growth SHAPE (identical reconstruction, unchanged math)
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

alpha, beta, k_max_reliable = fit_web_ios_calibration(cohorts_list, state_ios["h_base"], RELIABILITY_N_THRESHOLD)

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
      f"k_max_reliable={k_max_reliable}, wk52={shape.loc[52]:.2f}, wk104(extrap)={shape.loc[104]:.2f}")


def shape_ratio(target_k, anchor_k):
    a = max(0, min(anchor_k, H_EXT))
    t = max(0, min(target_k, H_EXT))
    base = shape.loc[a] if a > 0 else 1e-9
    return shape.loc[t] / base if base > 0 else 1.0


# ================================================================ 1b. UPS monthly-cadence shape (fix for the weekly-stretch "+21%" bug)
# ups (upsale_converted, $11.99) fires on a ~30-day cadence, not weekly like base
# ($9.99). shape_ratio() above compounds ONE retention step per week -- applying
# it to ups implicitly assumes ~4x more charging opportunities than actually
# occur. Fix: reuse the SAME retention curve (same subscriber population, same
# underlying attrition process -- retention doesn't depend on what you charge),
# but sample it only at monthly checkpoints (week 4, 8, 12, ...), matching ups's
# real cadence, instead of every week. (MONTH_STEP itself now lives in
# ltv_v2/config.py, shared with build_triple_report_fixed.py.)

implied_weekly_survival = shape.diff() / 9.99
implied_weekly_survival.loc[0] = 1.0  # cycle 0 (first payment) always happened -- same convention as raw_map_ltv

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


print(f"UPS monthly-cadence cycle curve (same retention curve, sampled every {MONTH_STEP} weeks): "
      f"cycles@wk12={ups_cycles_cum[12]:.3f}, cycles@wk52={ups_cycles_cum[52]:.3f}, cycles@wk104={ups_cycles_cum[104]:.3f}")
print("sanity -- monthly ratio must be LOWER than the old weekly-stretch ratio (equal would mean the fix did nothing):")
for _N, _age in [(52, 12), (104, 12)]:
    _rm, _rw = ups_shape_ratio(_N, _age), shape_ratio(_N, _age)
    print(f"  N={_N:3d}, age={_age}: monthly_ratio={_rm:.4f}  weekly_ratio(OLD BUG)={_rw:.4f}  "
          f"-> old bug would have inflated by {_rw/_rm:.3f}x")


# ================================================================ 2. population + attribution (appsflyer-native, no golden dependency)
pop = cohorts.build_population_5406().rename(columns={"person_key": "email"})
pop["first_date"] = pd.to_datetime(pop["first_date"]).dt.tz_localize("UTC")

w = pd.read_parquet("data/raw/web_conversions.parquet")
w["email"] = w["email"].str.lower()
w["event_date"] = pd.to_datetime(w["event_date"], utc=True, errors="coerce")
w = w[w["email"].isin(set(pop["email"]))].sort_values("event_date")
utm_map = w.groupby("email").first().reset_index()[["email", "utm_source"]]

pop = pop.merge(utm_map, on="email", how="left")
pop["utm_source"] = pop["utm_source"].fillna("(missing)")
pop["first_funnel"] = pop["first_funnel"].fillna("unknown").replace("", "unknown")
SNAPSHOT_NOW = pd.Timestamp(SNAPSHOT_DATE, tz="UTC")  # appsflyer is live; use today, not golden's frozen boundary
pop["age_weeks_now"] = ((SNAPSHOT_NOW - pop["first_date"]).dt.days // 7).clip(lower=0)
pop["cohort_date"] = (pop["first_date"] - pd.to_timedelta(pop["first_date"].dt.weekday, unit="D")).dt.normalize()

print(f"\nPopulation (appsflyer-native, same as golden pipeline's attribution): {len(pop)} people")

# ================================================================ 3. appsflyer revenue: totals + per-week cumulative
events = R2.load_raw_events()
totals = R2.per_person_revenue(events)  # all-time (no window_end) -- "current" fact
pop = pop.merge(totals[["email", "base_net", "ups_net"]], on="email", how="left")
pop[["base_net", "ups_net"]] = pop[["base_net", "ups_net"]].fillna(0.0)
pop["total_net"] = pop["base_net"] + pop["ups_net"]
pop["is_payer"] = pop["total_net"] > 0

n_payers = int(pop["is_payer"].sum())
print(f"n_payers (appsflyer, all-time net>0): {n_payers} of {len(pop)}")
print(f"total appsflyer net revenue on this population: ${pop['total_net'].sum():,.2f} "
      f"(base=${pop['base_net'].sum():,.2f}, ups=${pop['ups_net'].sum():,.2f})")

cum_base, cum_ups = R2.per_person_week_cumulative(pop[["email", "first_date"]], events)
MAX_OBS_WEEK = cum_base.shape[1] - 1
print(f"per-person-week cumulative arrays built, weeks 0..{MAX_OBS_WEEK}")

pop.to_parquet("data/raw/_tmp_v2_pop_final.parquet")
cum_base.to_parquet("data/raw/_tmp_v2_cum_base.parquet")
cum_ups.to_parquet("data/raw/_tmp_v2_cum_ups.parquet")

# ================================================================ 4. cohort x source / cohort x funnel tables


def cell_row(emails):
    emails = list(emails)
    sub = pop.set_index("email").loc[emails]
    n_attributed = len(sub)
    n_payers = int(sub["is_payer"].sum())
    ages = sub["age_weeks_now"]
    cell_age = int(ages.mean())
    payer_mask = sub["is_payer"]

    base_now = sub["base_net"]
    total_now = sub["total_net"]

    row = {
        "n_attributed": n_attributed, "n_payers": n_payers, "avg_age_weeks": cell_age,
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
            # real observed data at this checkpoint -- factonly and projected coincide exactly
            colN = min(N, MAX_OBS_WEEK)
            fact_base = cum_base.loc[mature, colN]
            fact_ups = cum_ups.loc[mature, colN]
            fact_total = fact_base + fact_ups
            mature_payers = [e for e in mature if pop.set_index("email").loc[e, "is_payer"]]
            n_mature_payers = len(mature_payers)

            attributed_base_N = fact_base.mean()
            payer_base_N = fact_base.loc[mature_payers].mean() if n_mature_payers else 0.0
            attributed_ups_N = fact_total.mean()
            payer_ups_N = fact_total.loc[mature_payers].mean() if n_mature_payers else 0.0

            row[f"ltv_per_attributed_{N}"] = attributed_base_N
            row[f"ltv_per_payer_{N}"] = payer_base_N
            row[f"ltv_per_attributed_{N}_ups_factonly"] = attributed_ups_N
            row[f"ltv_per_payer_{N}_ups_factonly"] = payer_ups_N
            row[f"ltv_per_attributed_{N}_ups_projected"] = attributed_ups_N
            row[f"ltv_per_payer_{N}_ups_projected"] = payer_ups_N
            row[f"ltv_{N}_source"] = "fact"
            row[f"ltv_{N}_n_mature"] = n_mature
            row[f"ltv_{N}_ups_projection_quality"] = "fact"
        else:
            # base: unchanged weekly ratio (correct cadence for a $9.99/week stream)
            ratio = shape_ratio(N, max(cell_age, 1))
            attributed_base_N = row["ltv_per_attributed_current"] * ratio
            payer_base_N = row["ltv_per_payer_current"] * ratio
            row[f"ltv_per_attributed_{N}"] = attributed_base_N
            row[f"ltv_per_payer_{N}"] = payer_base_N
            row[f"ltv_{N}_source"] = "model"
            row[f"ltv_{N}_n_mature"] = 0

            # ups_factonly: base keeps growing, ups FROZEN at today's observed value (no growth assumed -- a conservative floor)
            ups_only_attributed_now = row["ltv_per_attributed_current_ups"] - row["ltv_per_attributed_current"]
            ups_only_payer_now = row["ltv_per_payer_current_ups"] - row["ltv_per_payer_current"]
            row[f"ltv_per_attributed_{N}_ups_factonly"] = attributed_base_N + ups_only_attributed_now
            row[f"ltv_per_payer_{N}_ups_factonly"] = payer_base_N + ups_only_payer_now

            # ups_projected: base via weekly model (unchanged); ups via MONTHLY-cadence ratio -- the fix
            ups_ratio = ups_shape_ratio(N, max(cell_age, 1))
            row[f"ltv_per_attributed_{N}_ups_projected"] = attributed_base_N + ups_only_attributed_now * ups_ratio
            row[f"ltv_per_payer_{N}_ups_projected"] = payer_base_N + ups_only_payer_now * ups_ratio
            row[f"ltv_{N}_ups_projection_quality"] = "rough_low_data"
    return row


for group_col, out_name, label in [("utm_source", "table_A_cohort_utm_appsflyer.csv", "Table A (cohort_date x utm_source)"),
                                     ("first_funnel", "table_B_cohort_funnel_appsflyer.csv", "Table B (cohort_date x funnel)")]:
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
        cols += [f"ltv_per_attributed_{N}",
                 f"ltv_per_attributed_{N}_ups_factonly", f"ltv_per_attributed_{N}_ups_projected",
                 f"ltv_per_payer_{N}",
                 f"ltv_per_payer_{N}_ups_factonly", f"ltv_per_payer_{N}_ups_projected",
                 f"ltv_{N}_source", f"ltv_{N}_n_mature", f"ltv_{N}_ups_projection_quality"]
    tbl = tbl[cols].sort_values(["cohort_date", group_col])
    path = f"{OUT_DIR}/{out_name}"
    tbl.to_csv(path, index=False)

    n_low = tbl["low_n"].sum()
    n_rough = (tbl["ltv_52_ups_projection_quality"] == "rough_low_data").sum()
    print(f"\n=== {label} ===")
    print(f"rows(cells)={len(tbl)}, low_n (n_payers<{LOW_N_CELL_THRESHOLD}) cells={n_low} ({n_low/len(tbl):.1%}), "
          f"wk52 ups_projected=rough_low_data cells={n_rough} ({n_rough/len(tbl):.1%})")
    print(f"wrote {path}")
    print(tbl.head(6).to_string(index=False))

print("\nColumn legend (see reports/web_appsflyer_v2/README.md for the full boss-facing version):")
print("  ltv_per_attributed_* / ltv_per_payer_*  -- divide by n_attributed (all acquired) vs n_payers (payers only)")
print("  ltv_per_*_N              -- base only ($9.99/week subscription_started stream). Reliable, weekly-ratio projected.")
print("  ltv_per_*_N_ups_factonly -- base + ups, ups added ONLY from real observed data (frozen beyond the cohort's own age).")
print("  ltv_per_*_N_ups_projected-- base + ups, ups projected with a MONTHLY-cadence ratio (fix for the old +21% weekly-stretch bug).")
print("  ltv_{N}_ups_projection_quality -- 'fact' (real data) or 'rough_low_data' (projected, ~3 months of ups history, low confidence).")
print("  current = revenue realized as of the cohort's OWN CURRENT AGE today, not week 0.")
