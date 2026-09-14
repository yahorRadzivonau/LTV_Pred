"""
Build and freeze the v3 web payment-step matrix.

Reads the newest data/web_v3/events_*.parquet, derives the per-trial population
and the per-person revenue, and writes the payment-step matrix that map_web.py
fits on.

All the logic lives in ltv_v3/se_training_web.py so that the LOO and calendar
harnesses can rebuild the matrix at an earlier snapshot T without shelling out
to this script.

Writes: data/web_v3/web_se_training_<today>.parquet
        data/web_v3/population_<today>.parquet
(nothing else, ever)

Run: .venv/Scripts/python.exe web/v3/build_web_se_training.py
"""
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
os.chdir(ROOT)

from ltv_v3.config import DATA_DIR, WINDOW_START, RETURN_WINDOW_DAYS  # noqa: E402
from ltv_v3 import revenue as R, population as P, se_training_web as S  # noqa: E402

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 40)

OUT_DIR = ROOT / DATA_DIR
TODAY = date.today().isoformat()
MATRIX_OUT = OUT_DIR / f"web_se_training_{TODAY}.parquet"
POP_OUT = OUT_DIR / f"population_{TODAY}.parquet"

# ================================================================ 1. load
events = R.load_events()
print(f"events: {events.shape} | span {events['ts'].min().date()} .. {events['ts'].max().date()}")
excluded = events.attrs.get("product_b_excluded")
if excluded:
    print(f"product B excluded ($4.99 trial -> $39.99/month, monthly cadence): {excluded} people "
          f"— see ltv_v3/config.py:EXCLUDE_PRODUCT_B")

# ================================================================ 2. population (per-trial denominator)
pop = P.build_population(events)
rev = R.per_person_revenue(events)
pop = P.attach_revenue(pop, rev)

print(f"\n=== population (per-trial denominator), window from {WINDOW_START.date()} ===")
print(f"people:          {len(pop)}")
print(f"  has_base:      {int(pop['has_base'].sum())} ({pop['has_base'].mean():.1%})  <- bought $9.99/wk, the real conversion")
print(f"  has_paid_trial:{int(pop['has_paid_trial'].sum())} ({pop['has_paid_trial'].mean():.1%})")
print(f"  has_ups:       {int(pop['has_ups'].sum())} ({pop['has_ups'].mean():.1%})")
print(f"  paid_anything: {int(pop['paid_anything'].sum())} ({pop['paid_anything'].mean():.1%})  <- reconciliation only, NOT an LTV denominator")
print(f"revenue: trial ${pop['trial_net'].sum():,.2f} | base ${pop['base_net'].sum():,.2f} | "
      f"ups ${pop['ups_net'].sum():,.2f} | total ${pop['total_net'].sum():,.2f}")
print("\nfor reference -- v2 denominators:")
print("  git HEAD (clean):     4902 people, 73.6% payers")
print("  working tree (broken): 3512 people, 100% payers  <- the regression v3 fixes")

# ================================================================ 3. matrix
mx = S.build_matrix(events, pop)
mx = S.add_survived(mx)

print(f"\n=== payment-step matrix ===")
print(f"rows: {len(mx)} | people: {mx['sub_id'].nunique()} | cohorts (app_id): {mx['app_id'].nunique()}")
print(f"max step_k: {mx['step_k'].max()}")

print("\noutcome distribution:")
print(mx["outcome"].value_counts(normalize=True).round(4).to_string())

# ---- the right-edge fix, quantified on this build ----
ok = mx["evaluable"]
print(f"\n=== time censoring ({RETURN_WINDOW_DAYS}-day window, applied to survivors and deaths alike) ===")
print(f"evaluable (old enough to score): {int(ok.sum())} ({ok.mean():.1%})")
print(f"too recent (censored):           {int((~ok).sum())} ({(~ok).mean():.1%})")
print(f"observed death rate — uncensored {1 - mx['survived'].mean():.4f}"
      f"  vs censored {1 - mx.loc[ok, 'survived'].mean():.4f}"
      f"   (the gap is the false deaths v2 trained on)")

train = S.training_rows(mx)
print(f"\ntraining rows after the fix: {len(train)}")
print("cohorts with >=30 rows at risk, by step (how wide the h_base median is):")
at_risk = train[train["weeks_obs"] >= train["step_k"]]
per_step = at_risk.groupby(["app_id", "step_k"]).size().rename("n").reset_index()
wide = per_step[per_step["n"] >= 30].groupby("step_k")["app_id"].nunique()
for k in range(1, min(int(at_risk["step_k"].max()), 16) + 1):
    print(f"  step {k:2d}: {wide.get(k, 0):2d} cohorts")

# ================================================================ 4. write
OUT_DIR.mkdir(parents=True, exist_ok=True)
mx.to_parquet(MATRIX_OUT)
pop.to_parquet(POP_OUT)
print(f"\nwrote {MATRIX_OUT}")
print(f"wrote {POP_OUT}")
print("\nСледующий шаг: .venv/Scripts/python.exe web/v3/validate_loo_web.py")
