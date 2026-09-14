"""
Long-format weekly curve for charting: one row per (cohort x funnel x utm_source x week).

Same cell breakdown as table C, unrolled over weeks 0..104. Fact where the cell
has lived through the week, model beyond it.

WHY THE MODEL PART IS ANCHORED
The forecast is scaled so it meets the last observed point exactly. Without that
the chart shows a visible step at the fact/model boundary, which is an artefact
of the two being computed differently, not something real in the data. Same
anchoring principle the LTV tables use.

ROW COUNT: cells x 105 weeks. Nothing is written to BigQuery here -- this only
produces the CSV. Suggested schema for the terraform table definition is printed
at the end.

Writes: reports/web_v4/weekly_curve_v3_<today>.csv
Run: .venv/Scripts/python.exe web/v4/build_weekly_curve.py
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
    OUT_DIR, DATA_DIR, MATRIX_GLOB, POPULATION_GLOB, H_EXT, BASE_EVENT_TYPE,
    UPS_EVENT_TYPE, RETURN_WINDOW_DAYS, MIN_COHORT_AGE_WEEKS, MAP_LEVERS_WEB,
    UPS_FIRST_WEEK, UPS_CADENCE_WEEKS, UPS_PRICE, BASE_PRICE,
)
from ltv_v4 import se_training_web as S, map_web as M, revenue as R, money as MON  # noqa: E402
from ltv_v4 import upsell as U  # noqa: E402

TODAY = date.today().isoformat()
OUT_CSV = ROOT / OUT_DIR / f"weekly_curve_v3_{TODAY}.csv"
RETURN_WINDOW_WEEKS = RETURN_WINDOW_DAYS / 7.0
GROUP = ["cohort_date", "first_funnel", "utm_source"]


def newest(pat):
    return sorted(Path(DATA_DIR).glob(pat))[-1]


# ================================================================ inputs
mx = S.add_survived(pd.read_parquet(newest(MATRIX_GLOB)))
train = S.training_rows(mx)
pop = pd.read_parquet(newest(POPULATION_GLOB))
events = R.load_events()
max_week = int(pop["age_weeks_now"].max())

ios_h = common.empirical_hbase(common.load_matrix())
state = M.fit(train, ios_h_base=ios_h)
K_MAX_WEB = state["k_max_web"]

sanity = M.sanity_check(state)
if not sanity["ok"].all():
    raise SystemExit(f"Lever sanity FAILED, refusing to build:\n{sanity.to_string(index=False)}")

pop = pop[pop["age_weeks_now"] >= MIN_COHORT_AGE_WEEKS]
print(f"cells source: {len(pop)} people | levers {MAP_LEVERS_WEB} | k_max_web {K_MAX_WEB}")

# per-cohort hr, computed once (never per cell -- see map_web docstring)
cohort_hr = {}
for cohort, rows in train.groupby("app_id"):
    cohort_hr[cohort] = M.cohort_hr(state, rows, int(rows["weeks_obs"].max()))

# ================================================================ observed payments per person-week
ladder = R.base_payment_ladder(events)
ups = R.money_events(events)
ups = ups[ups["event_type"].eq(UPS_EVENT_TYPE)]
first_date = pop.set_index("email")["first_date"]

def weekly_counts(df):
    """(email, week) -> 1 if the person paid that week, for people in the population.

    Counts PEOPLE, not payments. 0.92% of person-weeks carry two base payments
    (a retry that settled twice, a mid-week plan change), and counting rows would
    make the observed part exceed the number of people alive -- while the model
    side is a survival probability, i.e. people. Mixing the two puts a ~1% step
    at the fact/model boundary of every chart.
    """
    d = df[df["email"].isin(first_date.index)].copy()
    d["week"] = ((d["ts"] - d["email"].map(first_date)).dt.days // 7).clip(lower=0)
    d = d[d["week"] <= H_EXT]
    return d.groupby(["email", "week"]).size().clip(upper=1)

base_counts = weekly_counts(ladder)
ups_counts = weekly_counts(ups)

# Upsell attach rate: P(charged at a checkpoint | still alive), measured, not the
# lifetime share of upsell takers. The two differ ~2x -- see ltv_v4/upsell.py.
# Must match what build_tables_v4.py uses, or the chart and the LTV table
# disagree about the same cell.
ups_obs = U.checkpoint_observations(events, pop, ladder)
UPS_PORTFOLIO = U.portfolio_rate(ups_obs)
print(f"upsell attach rate: portfolio {UPS_PORTFOLIO:.1%} over {len(ups_obs)} checkpoint observations")

# Observed per-person cumulative revenue -- the SAME series build_tables_v4.py
# anchors on: actual charged amounts, trial included, REFUND_HAIRCUT applied.
cum = R.per_person_week_cumulative(events, pop, max_week)

pop_idx = pop.set_index("email")
matrix_people = set(mx["sub_id"])

# ================================================================ build
rows = []
for keys, people in pop_idx.groupby(GROUP):
    cohort_key = pd.Timestamp(keys[0]).date().isoformat()
    base_people = people[people["has_base"]]
    n_base = len(base_people)
    age = int(people["age_weeks_now"].mean())
    emails = set(base_people.index)

    # observed payments in each week, summed over the cell
    obs_base = base_counts[base_counts.index.get_level_values(0).isin(emails)]
    obs_base = obs_base.groupby(level=1).sum() if len(obs_base) else pd.Series(dtype=int)
    obs_ups = ups_counts[ups_counts.index.get_level_values(0).isin(emails)]
    obs_ups = obs_ups.groupby(level=1).sum() if len(obs_ups) else pd.Series(dtype=int)

    # survival curve for this cell (cell levers x cohort hr)
    cell_rows = mx[mx["sub_id"].isin(emails & matrix_people)]
    curve = M.cell_curve(state, cell_rows, cohort_hr.get(cohort_key, 1.0)) if (
        n_base and not cell_rows.empty) else None

    # Fact is trustworthy only where the week has settled (same censoring as
    # everywhere else). Beyond that the model takes over.
    fact_until = max(0, min(age - int(np.ceil(RETURN_WINDOW_WEEKS)), max_week))
    share_ups = U.cell_rate(ups_obs, base_people.index, UPS_PORTFOLIO) if n_base else 0.0

    # Scale the model so it meets the last observed week exactly -- otherwise the
    # chart steps at the boundary for reasons that are not in the data.
    scale = 1.0
    if curve is not None and fact_until >= 1 and n_base:
        modelled_at_anchor = n_base * float(curve.get(fact_until, np.nan))
        observed_at_anchor = float(obs_base.get(fact_until, 0))
        if modelled_at_anchor > 0 and observed_at_anchor > 0:
            scale = observed_at_anchor / modelled_at_anchor

    # MONEY IS COMPUTED THE SAME WAY build_tables_v4.py COMPUTES IT, deliberately.
    # Pricing the weeks here as rebills x BASE_PRICE gave a number that could not
    # be reconciled with the LTV table for the same cell -- a constant ~1.9% gap,
    # from three sources: the table includes trial revenue, applies REFUND_HAIRCUT
    # and counts every subscription_started row, while the ladder behind
    # `rebills_base` dedups payments within 3 days. Rather than paper over it, the
    # money now comes from the identical observed series and the identical anchored
    # projection, so chart and table agree by construction.
    #
    # The 3-day dedup difference is NOT resolved by this -- it is only moved out of
    # the money columns. rebills_base still counts deduped steps (one billing
    # period), revenue still counts every charge. Whether those duplicate rows are
    # log artefacts or real double charges is unknown: web_conversions carries no
    # transaction id to tell them apart. Owner's call to leave it for later.
    anchor = min(age, max_week)
    obs_at_anchor = float(cum.reindex(base_people.index)[anchor].mean()) if n_base else 0.0

    cum_b = cum_u = 0.0
    for w in range(0, H_EXT + 1):
        if w <= fact_until:
            rb = float(obs_base.get(w, 0))
            ru = float(obs_ups.get(w, 0))
            src = "fact"
        elif curve is None:
            rb = ru = 0.0
            src = "no_payers"
        else:
            rb = n_base * float(curve.get(w, curve.iloc[-1])) * scale
            is_checkpoint = w >= UPS_FIRST_WEEK and (w - UPS_FIRST_WEEK) % UPS_CADENCE_WEEKS == 0
            ru = rb * share_ups if is_checkpoint else 0.0
            src = "model"
        cum_b += rb
        cum_u += ru

        if not n_base:
            rev_cum = 0.0
        elif w <= anchor:
            rev_cum = float(cum.reindex(base_people.index)[min(w, max_week)].mean()) * n_base
        elif curve is not None:
            rev_cum = MON.cell_ltv(curve, obs_at_anchor, anchor, w, base_people,
                                   share_ups=share_ups) * n_base
        else:
            rev_cum = obs_at_anchor * n_base

        rows.append({
            "cohort_date": cohort_key,
            "funnel": keys[1],
            "utm_source": keys[2],
            "week": w,
            "n_base_payer": n_base,
            "rebills_base": round(rb, 4),
            "rebills_ups": round(ru, 4),
            "rebills_base_cum": round(cum_b, 4),
            "rebills_ups_cum": round(cum_u, 4),
            "active_share": round(rb / n_base, 6) if n_base else 0.0,
            "revenue_week": 0.0,          # filled after the loop, as a diff of revenue_cum
            "revenue_cum": round(rev_cum, 2),
            "source": src,
            "evidence": "web_data" if w <= K_MAX_WEB else "ios_tail_extrapolation",
        })

out = pd.DataFrame(rows)

# revenue_week as the week-over-week increment of revenue_cum, so the two can
# never disagree with each other the way they could when computed separately.
out = out.sort_values(GROUP[:1] + ["funnel", "utm_source", "week"]).reset_index(drop=True)
out["revenue_week"] = (
    out.groupby(["cohort_date", "funnel", "utm_source"])["revenue_cum"].diff().fillna(out["revenue_cum"])
).round(2)

OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
out.to_csv(OUT_CSV, index=False)

# ================================================================ report
print(f"\nrows {len(out)} = {out.groupby(GROUP[:1] + ['funnel', 'utm_source']).ngroups} cells x {H_EXT + 1} weeks")
print(f"source: {out['source'].value_counts().to_dict()}")
print(f"wrote {OUT_CSV}")

print("\n--- sample: one big cell ---")
big = out[out["n_base_payer"] == out["n_base_payer"].max()]
key = big.iloc[0][["cohort_date", "funnel", "utm_source"]].tolist()
print(f"{key[0]} x {key[1]} x {key[2]}  (n_base_payer={big.iloc[0]['n_base_payer']})")
print(big[big["week"].isin([0, 1, 2, 4, 8, 12, 26, 52, 104])][
    ["week", "rebills_base", "rebills_ups", "rebills_base_cum", "active_share",
     "revenue_cum", "source"]].to_string(index=False))

print("\n--- suggested BigQuery schema for terraform ---")
TYPES = {
    "cohort_date": "DATE", "funnel": "STRING", "utm_source": "STRING",
    "week": "INTEGER", "n_base_payer": "INTEGER",
    "rebills_base": "FLOAT", "rebills_ups": "FLOAT",
    "rebills_base_cum": "FLOAT", "rebills_ups_cum": "FLOAT",
    "active_share": "FLOAT", "revenue_week": "FLOAT", "revenue_cum": "FLOAT",
    "source": "STRING", "evidence": "STRING",
}
for c in out.columns:
    print(f'  {{ name = "{c}", type = "{TYPES[c]}", mode = "NULLABLE" }},')
print("\n  suggested: partition none, cluster by cohort_date, funnel, week")
