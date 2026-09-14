"""
Shared machinery for the ml_web_weekly_curve_* breakdown tables.

One row per (cell x week of life 0..104): observed payments while the week has
settled, the anchored model beyond. The cell dimensions are a PARAMETER
(cohort x geo, cohort x campaign, ...) -- everything else (the v3 survival
model, the anchored money, the fact/model handover) is identical across the
tables, and identical to web/v4/build_weekly_curve.py, from which this module
was extracted. That script predates this module and stays as-is: it feeds the
already-loaded ml_web_weekly_curve table and has no gate, so it is not
refactored retroactively. The four build_weekly_curve_{geo,campaign,ad,full}.py
entrypoints are thin wrappers over this module.

THE INVARIANT the parameterisation preserves: every table slices the SAME
population by a different dimension, and every dimension column is
fallback-filled (no NaN groups), so the portfolio aggregate -- SUM(rebills_base)
over all cells at week w -- must be identical across all the tables of one
snapshot. The verification step checks exactly that.

sample_grade (new column, absent from the original ml_web_weekly_curve): how
much the cell's n_base_payer can be trusted, thresholds from the terraform
request (docs/BQ_TABLES_WEEKLY_CURVE_V2.md), derived from the binomial sampling
error at each size:
    insufficient   < 30    payers   error ±26% and worse
    rough          30..99            ±14-26%
    directional    100..299          ±8-14%
    reliable       >= 300            below the model's own 8.6% LOO error
"""
import numpy as np
import pandas as pd

from core import common
from ltv_v4.config import (
    DATA_DIR, MATRIX_GLOB, POPULATION_GLOB, H_EXT, UPS_EVENT_TYPE,
    RETURN_WINDOW_DAYS, MIN_COHORT_AGE_WEEKS,
    UPS_FIRST_WEEK, UPS_CADENCE_WEEKS, SESSION_COL,
)
from ltv_v4 import se_training_web as S, map_web as M, revenue as R, money as MON
from ltv_v4 import upsell as U

RETURN_WINDOW_WEEKS = RETURN_WINDOW_DAYS / 7.0

SAMPLE_GRADE_BOUNDS = ((300, "reliable"), (100, "directional"), (30, "rough"))


def sample_grade(n_base_payer: int) -> str:
    for floor, grade in SAMPLE_GRADE_BOUNDS:
        if n_base_payer >= floor:
            return grade
    return "insufficient"


def _newest(pattern):
    from pathlib import Path
    candidates = sorted(Path(DATA_DIR).glob(pattern))
    if not candidates:
        raise FileNotFoundError(f"no {pattern} under {DATA_DIR} -- run the pull/build first")
    return candidates[-1]


def load_pipeline_state() -> dict:
    """Everything the cell loop needs, computed once per build run.

    Same prologue as build_weekly_curve.py: newest matrix/population/events,
    the fitted v3 state (with the lever sanity stop), per-cohort hr, observed
    weekly payment counts, the upsell attach rate, and the observed cumulative
    revenue the forecasts anchor on.
    """
    mx = S.add_survived(pd.read_parquet(_newest(MATRIX_GLOB)))
    train = S.training_rows(mx)
    pop = pd.read_parquet(_newest(POPULATION_GLOB))
    events = R.load_events()
    max_week = int(pop["age_weeks_now"].max())

    ios_h = common.empirical_hbase(common.load_matrix())
    state = M.fit(train, ios_h_base=ios_h)

    sanity = M.sanity_check(state)
    if not sanity["ok"].all():
        raise SystemExit(f"Lever sanity FAILED, refusing to build:\n{sanity.to_string(index=False)}")

    pop = pop[pop["age_weeks_now"] >= MIN_COHORT_AGE_WEEKS]

    # per-cohort hr, computed once and NEVER per cell -- an hr fitted on the
    # cell would absorb its own lever effect and double it (map_web docstring).
    cohort_hr = {}
    for cohort, rows in train.groupby("app_id"):
        cohort_hr[cohort] = M.cohort_hr(state, rows, int(rows["weeks_obs"].max()))

    ladder = R.base_payment_ladder(events)
    ups = R.money_events(events)
    ups = ups[ups["event_type"].eq(UPS_EVENT_TYPE)]
    # По СЕССИИ. build_weekly_curve.py держит свою копию этой логики -- обе
    # должны ключеваться одинаково, иначе основная кривая и разрезы разойдутся.
    first_date = pop.set_index(SESSION_COL)["first_date"]

    def weekly_counts(df):
        """(email, week) -> 1 if the person paid that week. People, not
        payments: 0.92% of person-weeks carry two base payments, and counting
        rows would put the fact above the model's people-based scale."""
        d = df[df[SESSION_COL].isin(first_date.index)].copy()
        d["week"] = ((d["ts"] - d[SESSION_COL].map(first_date)).dt.days // 7).clip(lower=0)
        d = d[d["week"] <= H_EXT]
        return d.groupby([SESSION_COL, "week"]).size().clip(upper=1)

    ups_obs = U.checkpoint_observations(events, pop, ladder)

    return {
        "mx": mx,
        "pop": pop,
        "max_week": max_week,
        "state": state,
        "k_max_web": state["k_max_web"],
        "cohort_hr": cohort_hr,
        "base_counts": weekly_counts(ladder),
        "ups_counts": weekly_counts(ups),
        "ups_obs": ups_obs,
        "ups_portfolio": U.portfolio_rate(ups_obs),
        # the SAME observed series the LTV tables anchor on: actual charged
        # amounts, trial included, REFUND_HAIRCUT applied
        "cum": R.per_person_week_cumulative(events, pop, max_week),
        "matrix_people": set(mx["sub_id"]),
    }


def build_cells(ps: dict, group_cols: list, rename: dict = None) -> pd.DataFrame:
    """The weekly-curve rows for one choice of cell dimensions.

    group_cols: population columns, cohort_date FIRST (the hr is keyed on it).
    rename: population column -> output column (e.g. geo -> country,
    first_funnel -> funnel), applied to the finished frame.
    """
    assert group_cols[0] == "cohort_date", "cohort_date must be the first dimension -- hr is per cohort"
    rename = rename or {}

    pop_idx = ps["pop"].set_index(SESSION_COL)
    mx, state = ps["mx"], ps["state"]
    cum, max_week = ps["cum"], ps["max_week"]
    base_counts, ups_counts = ps["base_counts"], ps["ups_counts"]
    k_max_web = ps["k_max_web"]

    rows = []
    for keys, people in pop_idx.groupby(group_cols):
        keys = keys if isinstance(keys, tuple) else (keys,)
        cohort_key = pd.Timestamp(keys[0]).date().isoformat()
        dims = dict(zip(group_cols, keys))
        dims["cohort_date"] = cohort_key

        base_people = people[people["has_base"]]
        n_base = len(base_people)
        age = int(people["age_weeks_now"].mean())
        emails = set(base_people.index)

        obs_base = base_counts[base_counts.index.get_level_values(0).isin(emails)]
        obs_base = obs_base.groupby(level=1).sum() if len(obs_base) else pd.Series(dtype=int)
        obs_ups = ups_counts[ups_counts.index.get_level_values(0).isin(emails)]
        obs_ups = obs_ups.groupby(level=1).sum() if len(obs_ups) else pd.Series(dtype=int)

        # survival curve for this cell: ITS people's lever mix x ITS COHORT's hr
        cell_rows = mx[mx["sub_id"].isin(emails & ps["matrix_people"])]
        curve = M.cell_curve(state, cell_rows, ps["cohort_hr"].get(cohort_key, 1.0)) if (
            n_base and not cell_rows.empty) else None

        # fact only where the week has settled (same censoring as everywhere)
        fact_until = max(0, min(age - int(np.ceil(RETURN_WINDOW_WEEKS)), max_week))
        share_ups = U.cell_rate(ps["ups_obs"], base_people.index, ps["ups_portfolio"]) if n_base else 0.0

        # scale the model to meet the last observed week exactly -- otherwise
        # the chart steps at the boundary for reasons that are not in the data
        scale = 1.0
        if curve is not None and fact_until >= 1 and n_base:
            modelled_at_anchor = n_base * float(curve.get(fact_until, np.nan))
            observed_at_anchor = float(obs_base.get(fact_until, 0))
            if modelled_at_anchor > 0 and observed_at_anchor > 0:
                scale = observed_at_anchor / modelled_at_anchor

        # money identical to the LTV tables: observed cumulative at the anchor,
        # anchored projection beyond (money.cell_ltv), never rebills x price
        anchor = min(age, max_week)
        obs_at_anchor = float(cum.reindex(base_people.index)[anchor].mean()) if n_base else 0.0

        grade = sample_grade(n_base)
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
                **dims,
                "week": w,
                "n_base_payer": n_base,
                "sample_grade": grade,
                "rebills_base": round(rb, 4),
                "rebills_ups": round(ru, 4),
                "rebills_base_cum": round(cum_b, 4),
                "rebills_ups_cum": round(cum_u, 4),
                "active_share": round(rb / n_base, 6) if n_base else 0.0,
                "revenue_week": 0.0,   # filled below, as a diff of revenue_cum
                "revenue_cum": round(rev_cum, 2),
                "source": src,
                "evidence": "web_data" if w <= k_max_web else "ios_tail_extrapolation",
            })

    out = pd.DataFrame(rows).rename(columns=rename)
    dim_cols = [rename.get(c, c) for c in group_cols]

    # revenue_week as the week-over-week increment of revenue_cum, so the two
    # can never disagree the way independently-computed columns could
    out = out.sort_values(dim_cols + ["week"]).reset_index(drop=True)
    out["revenue_week"] = (
        out.groupby(dim_cols)["revenue_cum"].diff().fillna(out["revenue_cum"])
    ).round(2)
    return out


def report(out: pd.DataFrame, dim_cols: list, out_csv) -> None:
    """The standard end-of-build summary print, shared by the four entrypoints."""
    n_cells = out.groupby(dim_cols).ngroups
    print(f"\nrows {len(out)} = {n_cells} cells x {H_EXT + 1} weeks")
    print(f"source: {out['source'].value_counts().to_dict()}")
    week0 = out[out["week"] == 0]
    print(f"sample_grade (cells): {week0['sample_grade'].value_counts().to_dict()}")
    print(f"portfolio invariant hooks: SUM(n_base_payer)@w0 = {int(week0['n_base_payer'].sum())}, "
          f"SUM(rebills_base)@w0 = {week0['rebills_base'].sum():.1f}")
    print(f"wrote {out_csv}")
