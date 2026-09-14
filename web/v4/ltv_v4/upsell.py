"""
How likely is a person to be charged for the upsell at a monthly checkpoint?

WHY THIS EXISTS -- the bug it replaces

The money model needs P(pays upsell at checkpoint w | still alive at w). The
first version used the share of base payers who EVER took an upsell, measured
over their whole life. Those are not the same number and the gap is large:

    lifetime share, all base payers      24.5%
    measured among people ALIVE at a checkpoint    49.5%   (775 of 1565)

The lifetime share is diluted by everyone who churned before week 2 -- the first
upsell lands at week 2, so anyone who died at step 1 is counted as "no upsell"
when in fact they never had the chance. Exactly the same survivorship trap that
keeps plan_interval out of the levers, and that made the raw "do upsell takers
retain better?" comparison unusable.

Using the lifetime share understated upsell revenue ~2x, which came out as
roughly -7.5% on every LTV horizon.

Measured per checkpoint (2026-08-03 pull), the rate is flat with tenure:
    week 2   49.3%  (n=1149)
    week 6   49.1%  (n=334)
    week 10  53.7%  (n=82)
so a single rate per cell is a fair simplification -- it is not drifting.

PER CELL, WITH SHRINKAGE

Cells are small (most have 1-9 payers), so a raw per-cell rate is noise. Each
cell's own observed rate is shrunk toward the portfolio rate by the usual
n/(n+K) weighting, the same idea K_SHRINK applies to hr: a cell with plenty of
checkpoint observations gets its own number, a thin one inherits the portfolio's.
"""
import numpy as np
import pandas as pd

from ltv_v4.config import (
    UPS_EVENT_TYPE, UPS_FIRST_WEEK, UPS_CADENCE_WEEKS, RETURN_WINDOW_DAYS,
    BASE_EVENT_TYPE, JOIN_KEY, SESSION_COL,
)

# Prior weight for shrinking a cell's attach rate toward the portfolio rate.
# 30 checkpoint observations is roughly where a cell's own rate stops being
# coin-flips; below that the portfolio number dominates.
ATTACH_SHRINK = 30


def checkpoint_observations(events: pd.DataFrame, pop: pd.DataFrame,
                            ladder: pd.DataFrame, max_checkpoint: int = 60) -> pd.DataFrame:
    """One row per (person, checkpoint) where the person was alive AND the week
    has settled. Columns: email, week, paid_ups (0/1).

    "Alive" means they made a base payment in that week. Censoring matches the
    rest of the pipeline: a checkpoint only counts once RETURN_WINDOW_DAYS have
    passed, so a person still inside their window is neither alive nor dead here.
    """
    first_date = pop.set_index(SESSION_COL)["first_date"]
    age = pop.set_index(SESSION_COL)["age_weeks_now"]
    settle_weeks = int(np.ceil(RETURN_WINDOW_DAYS / 7.0))

    def to_week(df):
        d = df[df[SESSION_COL].isin(first_date.index)].copy()
        d["week"] = ((d["ts"] - d[SESSION_COL].map(first_date)).dt.days // 7).clip(lower=0)
        return d

    base_w = to_week(ladder)
    ups_w = to_week(events[events["event_type"].eq(UPS_EVENT_TYPE)])

    rows = []
    for w in range(UPS_FIRST_WEEK, max_checkpoint + 1, UPS_CADENCE_WEEKS):
        settled = set(age[age >= w + settle_weeks].index)
        alive = set(base_w.loc[base_w["week"] == w, SESSION_COL]) & settled
        if not alive:
            continue
        # Per SESSION: an upsell attaches to the subscription that carries its
        # own customer_user_id (99.8% resolve). Keyed by email, a person with
        # two subscriptions where only one took the upsell is counted as an
        # upsell taker on both.
        paid = set(ups_w.loc[ups_w["week"] == w, SESSION_COL]) & alive
        for sess in alive:
            rows.append({SESSION_COL: sess, "week": w, "paid_ups": int(sess in paid)})
    return pd.DataFrame(rows, columns=[SESSION_COL, "week", "paid_ups"])


def portfolio_rate(obs: pd.DataFrame) -> float:
    """Pooled attach rate across every checkpoint. The prior a thin cell falls back to."""
    if obs.empty:
        return 0.0
    return float(obs["paid_ups"].mean())


def cell_rate(obs: pd.DataFrame, emails, prior: float, k: int = ATTACH_SHRINK) -> float:
    """This cell's attach rate, shrunk toward the portfolio prior.

    Returns the prior unchanged when the cell has no checkpoint observations at
    all, which is the common case for young or tiny cells.
    """
    if obs.empty:
        return prior
    sub = obs[obs[SESSION_COL].isin(set(emails))]
    n = len(sub)
    if n == 0:
        return prior
    return float((n * sub["paid_ups"].mean() + k * prior) / (n + k))
