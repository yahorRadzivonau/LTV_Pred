"""
Web-side MAP model: h_base x residual lever chain x per-cohort hr.

WHY THIS IS A COPY AND NOT AN IMPORT
core/map_model.py holds MAP_LEVERS and MAP_MIN_ROWS as MODULE globals, shared
with the iOS model. Web needs a different lever list (funnel first, which iOS
does not have at all) and a far lower row floor (250 vs 2000 -- the web matrix
is ~17k rows, so a 2000-row floor would collapse everything but two funnels
into "other"). Rebinding those globals would silently move every iOS report
produced in the same process. So the logic is reimplemented here and
core/map_model.py is never touched. The same precedent is set by
ios/validation/segment_backtest.py, which copies Step 3.2/3.3 locally for the
same reason.

Pure helpers ARE imported from core (common.empirical_hbase, HMAX, K_SHRINK):
they are stateless functions and constants, nothing to rebind.

THE SCOPE SPLIT, which is the point of v3
  hr          <- computed on the WHOLE COHORT
  multipliers <- computed on the people of the CELL being predicted

They must not share a scope. An hr fitted on the cell would absorb the cell's
own lever effect and then get multiplied by it again -- the double-counting bug
that closed ios/alt_models/hybrid.py. Splitting them is also what makes small
cells behave: a 2-person cell yields n_seen ~= 6, so
hr = exp(log_hr * 6/(6+800)) ~= 1 and the cell falls back to the portfolio curve
shaped by its own levers, instead of amplifying two people's noise.
"""
import numpy as np
import pandas as pd

from core.common import HMAX, K_SHRINK, empirical_hbase

from ltv_v3.config import (
    MAP_LEVERS_WEB, MAP_MIN_ROWS_WEB, MAP_CLIP, CLEAN_STEP_MAX, SANITY_TOL, H_EXT,
)

# Curves run to H_EXT (104), not HMAX (52). core.common.HMAX stops at 52 because
# that is where the iOS h_base ends, but the web tables report week 104.
#
# Getting this wrong is expensive and silent: if the curve stops at 52, every
# lookup past it falls back to S[52] and SURVIVAL goes flat -- the model then
# assumes nobody churns at all between week 52 and week 104. The hazard is what
# should be held flat past the last measured step, not the survival it produces.
# Flat hazard at the week-52 level (~0.023) decays survival from 6.9% to ~1.9%
# over the second year; flat survival would have left it at 6.9%.
CURVE_MAX = H_EXT

# Minimum cohorts that must vote at a step for web's OWN h_base to be trusted
# there. empirical_hbase takes a median across app_id (= cohort here); a median
# of one number is not a median. Measured on the 2026-07-28 build: 14 cohorts
# vote at step 1, 9 at step 6, 6 at step 9, 2 at step 10, 1 at step 11, 0 at 12.
MIN_COHORTS_FOR_HBASE = 3


def _prep(mx: pd.DataFrame) -> pd.DataFrame:
    """Lever columns, identical treatment in fit() and at predict time.

    Both sides MUST go through this same function -- preprocessing drifting
    apart between training and prediction is the classic silent bug, and
    core/map_model.py carries the same warning.
    """
    mx = mx.copy()
    mx["billday_bin"] = pd.cut(
        mx["billing_day_of_month"], bins=[0, 10, 20, 31],
        labels=["d01_10", "d11_20", "d21_31"],
    ).astype(str)
    for col in ("geo", "media_source", "funnel"):
        if col in mx.columns:
            mx[col] = mx[col].astype("string").fillna("(none)").replace("", "(none)")
    # utm_source is carried as media_source in the matrix (schema parity with
    # iOS); alias it so MAP_LEVERS_WEB can name the web-native concept.
    if "utm_source" not in mx.columns and "media_source" in mx.columns:
        mx["utm_source"] = mx["media_source"]
    return mx


def _collapse_small(df: pd.DataFrame, small_categories: dict) -> pd.DataFrame:
    """Fold rare categories into "other" using the sets decided during fit().

    Must run on ANY data before looking up state["multipliers"]: the dict holds
    no key for a rare category (it was folded at fit time), so without this a
    .get(cat, __default__) quietly returns 1.0 instead of the "other" value.
    """
    df = df.copy()
    for lever, small in small_categories.items():
        if lever in df.columns:
            df[lever] = df[lever].where(~df[lever].isin(small), "other")
    return df


def build_h_base(train_rows: pd.DataFrame, ios_h_base: pd.Series = None) -> tuple:
    """Web's own h_base where the data supports it, iOS tail spliced beyond.

    Returns (h_base, k_max_web) so callers can report where evidence actually
    ends instead of letting a borrowed tail masquerade as measurement.

    The splice is scaled to be continuous at the seam: the iOS tail is
    multiplied by web_h[k_max] / ios_h[k_max], so the curve does not jump. With
    no iOS series supplied, the last trusted web value is carried forward flat.
    """
    at_risk = train_rows[train_rows["weeks_obs"] >= train_rows["step_k"]]
    support = (
        at_risk.groupby(["app_id", "step_k"]).size().rename("n").reset_index()
        .query("n >= 30").groupby("step_k")["app_id"].nunique()
    )
    trusted = [k for k in range(1, HMAX + 1) if support.get(k, 0) >= MIN_COHORTS_FOR_HBASE]
    if not trusted:
        raise RuntimeError("No step has enough cohorts to estimate a web h_base")
    k_max_web = max(trusted)

    web_h = empirical_hbase(train_rows).reindex(range(1, CURVE_MAX + 1))
    h = web_h.copy()

    if ios_h_base is not None:
        ios = ios_h_base.reindex(range(1, CURVE_MAX + 1))
        seam = web_h.loc[k_max_web] / ios.loc[k_max_web] if ios.loc[k_max_web] > 0 else 1.0
        for k in range(k_max_web + 1, CURVE_MAX + 1):
            h.loc[k] = ios.loc[k] * seam
    else:
        h.loc[k_max_web + 1:] = web_h.loc[k_max_web]

    # iOS itself only reaches HMAX (52), so weeks 53..104 have no source at all.
    # ffill holds the last known HAZARD flat there, which keeps survival decaying
    # at the week-52 rate instead of freezing it. See CURVE_MAX above.
    h = h.ffill().bfill()
    return h, k_max_web


def fit(mx: pd.DataFrame, ios_h_base: pd.Series = None, crash_cohorts: set = None,
        levers: list = None, min_rows: int = MAP_MIN_ROWS_WEB) -> dict:
    """Build the web state: h_base + a residual chain of lever multipliers.

    mx must already be filtered to rows whose outcome is settled
    (se_training_web.training_rows) -- otherwise undecided steps train as deaths.

    levers: defaults to MAP_LEVERS_WEB. Order IS the residual-chain order; each
    lever is fitted on the residual left by the previous ones, which is what
    stops correlated levers (buyers run particular funnels) from double-counting
    the same effect.
    """
    levers = list(levers) if levers is not None else list(MAP_LEVERS_WEB)
    crash_cohorts = crash_cohorts or set()

    mx = _prep(mx)
    h_base, k_max_web = build_h_base(mx, ios_h_base)

    cz = mx[
        (mx["step_k"] <= CLEAN_STEP_MAX)
        & (mx["weeks_obs"] >= mx["step_k"])
        & (~mx["app_id"].isin(crash_cohorts))
    ].copy()
    if cz.empty:
        raise RuntimeError("Clean zone is empty -- cannot fit lever multipliers")
    cz["died"] = 1 - cz["survived"]
    cz["expected"] = h_base.reindex(cz["step_k"]).values

    multipliers, small_categories, support = {}, {}, {}
    for lever in levers:
        counts = cz[lever].value_counts()
        small = counts[counts < min_rows].index
        cz[lever] = cz[lever].where(~cz[lever].isin(small), "other")
        small_categories[lever] = small.tolist()

        g = cz.groupby(lever).agg(obs=("died", "mean"), exp=("expected", "mean"), n=("died", "size"))
        m = (g["obs"] / g["exp"]).clip(*MAP_CLIP)
        multipliers[lever] = m.to_dict()
        multipliers[lever]["__default__"] = 1.0
        support[lever] = g

        # Residual: update expectation BEFORE fitting the next lever.
        cz["expected"] = cz["expected"] * cz[lever].map(m).fillna(1.0)

    return {
        "h_base": h_base,
        "k_max_web": k_max_web,
        "multipliers": multipliers,
        "small_categories": small_categories,
        "support": support,
        "levers": levers,
        "crash_cohorts": crash_cohorts,
    }


def sanity_check(state: dict, tol: tuple = SANITY_TOL) -> pd.DataFrame:
    """Weighted mean of each lever's multipliers must sit near 1.0.

    Run this BEFORE trusting any accuracy number -- the lesson of the hybrid.py
    rr bug is that a lever can look fine on a scoreboard while being nonsense.
    Returns a report frame; the caller decides whether to stop. Do NOT
    renormalise to pass.
    """
    rows = []
    for lever in state["levers"]:
        g = state["support"][lever]
        mult = pd.Series({k: v for k, v in state["multipliers"][lever].items() if k != "__default__"})
        common = g.index.intersection(mult.index)
        w_mean = float(np.average(mult.loc[common], weights=g.loc[common, "n"]))
        rows.append({
            "lever": lever,
            "weighted_mean": w_mean,
            "ok": tol[0] <= w_mean <= tol[1],
            "categories": len(common),
            "collapsed_to_other": len(state["small_categories"][lever]),
            "rows": int(g["n"].sum()),
        })
    return pd.DataFrame(rows)


def _subs_with_levers(state: dict, rows: pd.DataFrame) -> pd.DataFrame:
    """One row per person (their first step), preprocessed exactly as in fit()."""
    rows = _prep(rows)
    rows = _collapse_small(rows, state["small_categories"])
    return rows.sort_values("step_k").groupby("sub_id").first().reset_index()


def _combo_mult(state: dict, row) -> float:
    p = 1.0
    for lever in state["levers"]:
        table = state["multipliers"][lever]
        p *= table.get(row[lever], table["__default__"])
    return float(np.clip(p, *MAP_CLIP))


def personal_multiplier(state: dict, rows: pd.DataFrame) -> pd.DataFrame:
    """Unique lever combos among these people, with a weight and a multiplier."""
    subs = _subs_with_levers(state, rows)
    if not state["levers"]:
        # Lever-free configuration (used as the floor in the lever study):
        # everyone shares one combo at multiplier 1.0, so the curve collapses to
        # h_base x hr -- exactly the "no levers" baseline we want to compare to.
        return pd.DataFrame({"w": [len(subs)], "mult": [1.0]})
    combos = subs.groupby(state["levers"]).size().rename("w").reset_index()
    combos["mult"] = combos.apply(lambda r: _combo_mult(state, r), axis=1)
    return combos


def cohort_hr(state: dict, cohort_rows: pd.DataFrame, weeks: int) -> float:
    """How much this cohort deviates from the portfolio, shrunk by K_SHRINK.

    Measured against h_base * the person's OWN multiplier, never against bare
    h_base: otherwise hr relearns the lever effect and it gets applied twice.

    Shrinkage is what makes this safe on thin data -- n_seen small pulls hr
    toward 1, so a sparse cohort inherits the portfolio curve rather than
    inventing its own from noise.
    """
    h_base = state["h_base"]
    combos = personal_multiplier(state, cohort_rows)
    subs = _subs_with_levers(state, cohort_rows)
    if state["levers"]:
        subs = subs.merge(combos[state["levers"] + ["mult"]], on=state["levers"], how="left")
        sub_mult = subs.set_index("sub_id")["mult"]
    else:
        # Lever-free configuration: every person sits at multiplier 1.0, so hr is
        # measured straight against h_base.
        sub_mult = pd.Series(1.0, index=subs["sub_id"])

    obs = cohort_rows[cohort_rows["weeks_obs"] >= cohort_rows["step_k"] + 1].copy()
    obs = obs[obs["step_k"] <= weeks]
    n_seen = len(obs)
    if n_seen == 0:
        return 1.0

    obs["mult"] = obs["sub_id"].map(sub_mult)
    obs["exp_die"] = (h_base.reindex(obs["step_k"]).values * obs["mult"].values)
    obs["exp_die"] = obs["exp_die"].clip(0.001, 0.999)
    g = obs.groupby("step_k").agg(o=("died", "mean"), e=("exp_die", "mean"), n=("died", "size"))
    g = g[(g["n"] >= 20) & (g["e"] > 0)]
    log_hr = float(np.average(np.log(np.clip(g["o"] / g["e"], 0.2, 5.0)), weights=g["n"])) if len(g) else 0.0
    return float(np.exp(log_hr * (n_seen / (n_seen + K_SHRINK))))


def cell_curve(state: dict, cell_rows: pd.DataFrame, hr: float) -> pd.Series:
    """Survival curve for a cell: its own people's multipliers x h_base x an
    EXTERNAL hr (the cohort's).

    Weighted average of the personal curves -- a discrete expectation, not a
    simulation.
    """
    h_base = state["h_base"]
    combos = personal_multiplier(state, cell_rows)
    if combos.empty or combos["w"].sum() == 0:
        # No people -> no curve. Returning NaNs here would poison every
        # downstream average silently; an explicit flat-1.0 curve is wrong too,
        # so callers must check emptiness before relying on the result.
        return pd.Series(np.nan, index=range(1, CURVE_MAX + 1))
    haz = np.clip(np.outer(combos["mult"].values, h_base.values) * hr, 0.001, 0.999)
    curves = np.cumprod(1 - haz, axis=1)
    w = combos["w"].values[:, None]
    return pd.Series((curves * w).sum(axis=0) / w.sum(), index=range(1, CURVE_MAX + 1))


def predict(state: dict, rows: pd.DataFrame, weeks: int) -> pd.Series:
    """Convenience: hr and curve from the same rows.

    Use for whole-cohort predictions. For a CELL, call cohort_hr() on the parent
    cohort and pass that hr to cell_curve() -- see the module docstring on why
    the scopes must differ.
    """
    return cell_curve(state, rows, cohort_hr(state, rows, weeks))
