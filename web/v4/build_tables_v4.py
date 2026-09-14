"""
Per-person LTV tables on the v3 web pipeline.

  Table A  cohort_date x utm_source
  Table B  cohort_date x first_funnel
  Table C  cohort_date x first_funnel x utm_source

HOW A CELL IS FORECAST -- the v3 change

    hr          from the WHOLE COHORT (shrunk by K_SHRINK)
    multipliers from the CELL's own people
    LTV         cell's observed revenue so far + modelled increment

v2 instead took one pooled curve for the entire product and scaled each cell's
observed fact by a single global ratio, so two cells of the same age got an
identical multiplier no matter their source or geo, and a 2-person cell had its
noise amplified 5.3x. Here a thin cell yields n_seen ~= 6, hr ~= 1, and it falls
back to the portfolio curve shaped by its own levers instead.

DENOMINATORS ARE NAMED FOR WHAT THEY COUNT
v2 shipped `ltv_per_attributed_*` and `ltv_per_payer_*`, and those names are
exactly how its regression hid: "attributed" silently stopped containing
non-payers and nothing failed. Here:

    ltv_per_trial_*       divided by everyone in the per-trial population
                          (any trial + anyone who bought base). The CAC unit.
    ltv_per_base_payer_*  divided by people who actually bought the $9.99 plan.
                          Unit economics of the subscription itself.

Writes: reports/web_v4/table_{A,B,C}_v3_<today>.csv
Run: .venv/Scripts/python.exe web/v4/build_tables_v4.py
"""
import os
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
os.chdir(ROOT)

from core import common  # noqa: E402
from ltv_v4.config import (  # noqa: E402
    OUT_DIR, DATA_DIR, MATRIX_GLOB, POPULATION_GLOB, LOW_N_CELL_THRESHOLD,
    RETURN_WINDOW_DAYS, MAP_LEVERS_WEB, MIN_COHORT_AGE_WEEKS, BASE_EVENT_TYPE,
)
from ltv_v4 import se_training_web as S, map_web as M, money as MON, revenue as R  # noqa: E402
from ltv_v4 import upsell as U  # noqa: E402

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 40)

HORIZONS = [4, 12, 26, 52, 104]
TODAY = date.today().isoformat()
RETURN_WINDOW_WEEKS = RETURN_WINDOW_DAYS / 7.0


def newest(glob_pat):
    files = sorted(Path(DATA_DIR).glob(glob_pat))
    if not files:
        raise FileNotFoundError(f"No {glob_pat} in {DATA_DIR}. Run build_web_se_training.py first.")
    return files[-1]


# ================================================================ 1. inputs
mx = S.add_survived(pd.read_parquet(newest(MATRIX_GLOB)))
train = S.training_rows(mx)
pop = pd.read_parquet(newest(POPULATION_GLOB))
events = R.load_events()
max_week = int(pop["age_weeks_now"].max())
cum = R.per_person_week_cumulative(events, pop, max_week)
# Base-only series, so the "without upsell" figures are computed rather than
# zero-filled. v3's headline number folds the upsell in; the legacy consumers
# still want the two apart.
cum_base_only = R.per_person_week_cumulative(events, pop, max_week, streams=(BASE_EVENT_TYPE,))

ios_h = common.empirical_hbase(common.load_matrix())
state = M.fit(train, ios_h_base=ios_h)
K_MAX_WEB = state["k_max_web"]

print(f"matrix {len(mx)} rows | trainable {len(train)} | population {len(pop)} | cohorts {mx['app_id'].nunique()}")
print(f"levers: {MAP_LEVERS_WEB}")
print(f"web's own h_base holds to step {K_MAX_WEB}; beyond that the iOS tail is spliced in")

sanity = M.sanity_check(state)
if not sanity["ok"].all():
    raise SystemExit(f"Lever sanity FAILED, refusing to build tables:\n{sanity.to_string(index=False)}")
print("lever sanity passed:", ", ".join(f"{r.lever}={r.weighted_mean:.3f}" for r in sanity.itertuples()))

# ================================================================ 2. per-cohort hr, computed ONCE
# hr belongs to the cohort, never to the cell -- an hr fitted on the cell would
# absorb that cell's own lever effect and then get multiplied by it again.
cohort_hr = {}
for cohort, rows in train.groupby("app_id"):
    weeks = int(rows["weeks_obs"].max())
    cohort_hr[cohort] = M.cohort_hr(state, rows, weeks)
print(f"per-cohort hr: {len(cohort_hr)} cohorts, range "
      f"{min(cohort_hr.values()):.3f}..{max(cohort_hr.values()):.3f}")

# Maturity filter: young cohorts are dropped from the REPORT entirely, not
# flagged low_n. They stay in the training matrix -- their early steps are real
# evidence about the portfolio; what they cannot support is a per-cell forecast.
n_before = len(pop)
pop = pop[pop["age_weeks_now"] >= MIN_COHORT_AGE_WEEKS]
print(f"maturity filter: age >= {MIN_COHORT_AGE_WEEKS} week(s) -> "
      f"{n_before} people -> {len(pop)} (dropped {n_before - len(pop)})")

# Upsell attach rate: P(charged at a checkpoint | still alive), measured rather
# than taken as the lifetime share of upsell takers. See ltv_v4/upsell.py for
# why the two differ by ~2x.
ups_obs = U.checkpoint_observations(events, pop, R.base_payment_ladder(events))
UPS_PORTFOLIO = U.portfolio_rate(ups_obs)
print(f"upsell attach rate: portfolio {UPS_PORTFOLIO:.1%} over {len(ups_obs)} checkpoint observations "
      f"(lifetime-share fallback would have been {pop.loc[pop['has_base'], 'has_ups'].mean():.1%})")

pop_idx = pop.set_index("email")
matrix_people = set(mx["sub_id"])


def cell_row(people: pd.DataFrame, cohort_key: str) -> dict:
    """One output row: counts, observed money, and the forecast at each horizon."""
    n_trial = len(people)
    base_people = people[people["has_base"]]
    n_base = len(base_people)
    age = int(people["age_weeks_now"].mean())

    row = {
        "n_trial": n_trial,
        "n_base_payer": n_base,
        "conversion": n_base / n_trial if n_trial else 0.0,
        "avg_age_weeks": age,
        "low_n": n_base < LOW_N_CELL_THRESHOLD,
        "revenue_observed": float(people["total_net"].sum()),
        "ltv_per_trial_current": float(people["total_net"].mean()) if n_trial else 0.0,
        "ltv_per_base_payer_current": float(base_people["total_net"].mean()) if n_base else 0.0,
        # base-only "current": the $9.99 stream on its own, no upsell, no trial
        "ltv_per_trial_current_base": float(people["base_net"].mean()) if n_trial else 0.0,
        "ltv_per_base_payer_current_base": float(base_people["base_net"].mean()) if n_base else 0.0,
        "hr_cohort": cohort_hr.get(cohort_key, 1.0),
    }

    # Survival curve for this cell: its own people's levers x the cohort's hr.
    cell_rows = mx[mx["sub_id"].isin(set(base_people.index) & matrix_people)]
    if cell_rows.empty or n_base == 0:
        row["ups_attach_rate"] = 0.0   # nobody to charge
        for N in HORIZONS:
            row[f"ltv_per_trial_{N}"] = row["ltv_per_trial_current"]
            row[f"ltv_per_base_payer_{N}"] = row["ltv_per_base_payer_current"]
            row[f"ltv_per_trial_{N}_base"] = row["ltv_per_trial_current_base"]
            row[f"ltv_per_base_payer_{N}_base"] = row["ltv_per_base_payer_current_base"]
            row[f"ltv_{N}_source"] = "no_payers"
            row[f"ltv_{N}_evidence"] = "none"
            row[f"ltv_{N}_n_mature"] = 0
        return row

    curve = M.cell_curve(state, cell_rows, cohort_hr.get(cohort_key, 1.0))

    # Anchor on what the cell has actually earned by its current age -- only the
    # increment past it is modelled.
    anchor = min(age, max_week)
    obs_full = float(cum.reindex(base_people.index)[anchor].mean())
    obs_base_only = float(cum_base_only.reindex(base_people.index)[anchor].mean())
    ages = base_people["age_weeks_now"]
    share_ups = U.cell_rate(ups_obs, base_people.index, UPS_PORTFOLIO)
    row["ups_attach_rate"] = share_ups

    for N in HORIZONS:
        if N <= anchor:
            per_base = float(cum.reindex(base_people.index)[min(N, max_week)].mean())
            per_base_only = float(cum_base_only.reindex(base_people.index)[min(N, max_week)].mean())
            source = "fact"
        else:
            per_base = MON.cell_ltv(curve, obs_full, anchor, N, base_people, share_ups=share_ups)
            # share_ups=0 -> the $9.99 stream alone, for the legacy "no upsell"
            # columns. Same curve, same anchor, upsell simply not credited.
            per_base_only = obs_base_only + MON.project_increment(
                curve, anchor, N, share_base=1.0, share_ups=0.0
            )
            source = "model"

        row[f"ltv_per_base_payer_{N}"] = per_base
        row[f"ltv_per_trial_{N}"] = per_base * row["conversion"]
        row[f"ltv_per_base_payer_{N}_base"] = per_base_only
        row[f"ltv_per_trial_{N}_base"] = per_base_only * row["conversion"]
        row[f"ltv_{N}_source"] = source
        # How many of the cell's payers have actually lived through week N --
        # the sample behind a "fact", and 0 when the number is pure forecast.
        row[f"ltv_{N}_n_mature"] = int((ages >= N + RETURN_WINDOW_DAYS / 7.0).sum())
        # Where the number stops being measurement. Web's own h_base holds to
        # K_MAX_WEB steps; past that the curve rides a tail borrowed from iOS.
        row[f"ltv_{N}_evidence"] = "web_data" if N <= K_MAX_WEB else "ios_tail_extrapolation"
    return row


# ================================================================ 3. build the three tables
SPECS = [
    ("A", ["utm_source"], f"table_A_v3_cohort_utm_{TODAY}.csv"),
    ("B", ["first_funnel"], f"table_B_v3_cohort_funnel_{TODAY}.csv"),
    ("C", ["first_funnel", "utm_source"], f"table_C_v3_cohort_funnel_utm_{TODAY}.csv"),
]

out_dir = ROOT / OUT_DIR
out_dir.mkdir(parents=True, exist_ok=True)

for label, group_cols, fname in SPECS:
    rows = []
    for keys, people in pop_idx.groupby(["cohort_date"] + group_cols):
        keys = keys if isinstance(keys, tuple) else (keys,)
        cohort_key = pd.Timestamp(keys[0]).date().isoformat()
        r = cell_row(people, cohort_key)
        r["cohort_date"] = cohort_key
        for col, val in zip(group_cols, keys[1:]):
            r[col] = val
        rows.append(r)

    tbl = pd.DataFrame(rows)
    lead = ["cohort_date"] + group_cols + [
        "n_trial", "n_base_payer", "conversion", "avg_age_weeks", "low_n",
        "hr_cohort", "ups_attach_rate", "revenue_observed",
        "ltv_per_trial_current", "ltv_per_base_payer_current",
        "ltv_per_trial_current_base", "ltv_per_base_payer_current_base",
    ]
    for N in HORIZONS:
        lead += [f"ltv_per_trial_{N}", f"ltv_per_base_payer_{N}",
                 f"ltv_per_trial_{N}_base", f"ltv_per_base_payer_{N}_base",
                 f"ltv_{N}_source", f"ltv_{N}_n_mature", f"ltv_{N}_evidence"]
    tbl = tbl[lead].sort_values(["cohort_date"] + group_cols)

    # Round for readability. Full float precision on money columns
    # (72.38506743877753) is noise -- nothing downstream needs more than cents,
    # and it makes the file painful to review by eye.
    money_cols = [c for c in tbl.columns if c.startswith("ltv_per_") or c == "revenue_observed"]
    tbl[money_cols] = tbl[money_cols].round(2)
    tbl["conversion"] = tbl["conversion"].round(4)
    tbl["hr_cohort"] = tbl["hr_cohort"].round(4)
    tbl["ups_attach_rate"] = tbl["ups_attach_rate"].round(4)

    tbl.to_csv(out_dir / fname, index=False)

    n_low = int(tbl["low_n"].sum())
    print(f"\n=== Table {label} ({' x '.join(['cohort_date'] + group_cols)}) ===")
    print(f"cells={len(tbl)} | low_n (n_base<{LOW_N_CELL_THRESHOLD})={n_low} ({n_low/len(tbl):.0%}) "
          f"| people={int(tbl['n_trial'].sum())} | payers={int(tbl['n_base_payer'].sum())}")
    print(f"wrote {out_dir / fname}")

print("\nLegend:")
print("  ltv_per_trial_*      -- per person in the per-trial population (any trial + base buyers). The CAC unit.")
print("  ltv_per_base_payer_* -- per person who bought the $9.99/week plan. Subscription unit economics.")
print("  ltv_{N}_source       -- 'fact' (the cell already reached age N) or 'model' (forecast).")
print(f"  ltv_{{N}}_evidence     -- 'web_data' up to step {K_MAX_WEB}; beyond that 'ios_tail_extrapolation',")
print("                          i.e. the shape is borrowed from iOS, not measured on web.")
print("  hr_cohort            -- the cohort's own hazard ratio vs the portfolio, K_SHRINK-shrunk.")
