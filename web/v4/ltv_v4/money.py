"""
Turn a survival curve into money.

THE ANCHORED FORM, and why it matters

    LTV(N) = observed revenue through the anchor week
           + projected revenue for weeks anchor+1 .. N

Revenue the cohort has already earned is a FACT -- re-deriving it from modelled
prices and modelled survival only injects error into a number we already know.
Only the increment past the anchor is forecast. v2 got this part right (its
`fact x ratio` is exactly an anchored projection); the first v3 draft did not,
projecting everything from week 0, and measured +6.8% biased high at every
horizon as a result.

Projection uses UNCONDITIONAL survival, S[k] measured from entry -- NOT
S[k]/S[anchor].

  CORRECTION 2026-09-11: this paragraph asserted the exact opposite until that
  date, while _survival() below has always done the unconditional thing. The
  code was right and the docstring was wrong, which cost two separate readings
  of this file to notice. The reason unconditional is correct: the anchor value
  the increment is added to is a population MEAN over everyone in the cell,
  churned members included, so the increment must sit on that same base.
  Conditioning describes survivors only and roughly doubles it -- measured on
  weeks 4->8, conditional predicts a $32.50 increment, unconditional $16.06,
  and the fact is $15.39.

WHAT EACH STREAM CONTRIBUTES PAST THE ANCHOR

All prices here are NET of the acquirer's cut (v3: 2026-09-11), which is what
makes them comparable to the observed side -- that sums the net_revenue column.
The gross list prices BASE_PRICE / UPS_PRICE stay in config as PRODUCT
IDENTIFIERS for the classifiers in ltv_v4.revenue, and must never be used for
money here.

  base  NET_BASE_PRICE every week the person is still paying.
  ups   NET_UPS_PRICE at the monthly checkpoints only. Measured: the first upsell
        lands ~2 weeks in (week 1 x31, week 2 x1311, week 3 x38), NOT at signup
        -- 0% happen in week 0 -- so checkpoints run 2, 6, 10, ... Crediting it
        at week 0 over-credits every short horizon.
        ups rides BASE survival: 0 of 1417 people with both streams had their
        last ups payment after their last base payment. A separate ups curve
        plus a "has upsell" lever is the next iteration.
  trial nothing. It is a one-time charge at week 0, so by prediction time it is
        always inside the observed anchor already -- and its real amount varies
        ($0.99 x8577, $4.99 x865, $0.00 x201), which a flat constant would
        misstate anyway.
"""
import numpy as np
import pandas as pd

from ltv_v4.config import (
    NET_BASE_PRICE, NET_UPS_PRICE, UPS_CADENCE_WEEKS, UPS_FIRST_WEEK, H_EXT,
)


def ups_checkpoints(upto: int, start: int = UPS_FIRST_WEEK, step: int = UPS_CADENCE_WEEKS):
    """Weeks an upsell is actually charged: start, start+step, ... <= upto."""
    return list(range(start, upto + 1, step))


def _survival(curve: pd.Series, k: int) -> float:
    """P(the person makes payment k+1), UNCONDITIONAL -- measured from entry.

    Deliberately not conditioned on being alive at the anchor. The anchor value
    this is added to is a population MEAN over everyone in the cell, churned
    members included, so the increment has to be on that same base. Conditioning
    ("given still paying at the anchor") describes survivors only and roughly
    doubles the projection: on weeks 4->8 it predicts a $32.50 increment where
    the unconditional form predicts $16.06 and the fact is $15.39.
    """
    tail = float(curve.iloc[-1]) if len(curve) else 0.0
    return float(np.clip(curve.get(k, tail), 0.0, 1.0))


def project_increment(curve: pd.Series, anchor: int, horizon: int,
                      share_base: float = 1.0, share_ups: float = 0.0) -> float:
    """Expected revenue per person for weeks anchor+1 .. horizon."""
    if horizon <= anchor:
        return 0.0
    total = 0.0
    checkpoints = set(ups_checkpoints(horizon))
    for k in range(anchor + 1, horizon + 1):
        alive = _survival(curve, k)
        total += share_base * NET_BASE_PRICE * alive
        if k in checkpoints:
            total += share_ups * NET_UPS_PRICE * alive
    return total


def cell_ltv(curve: pd.Series, observed_at_anchor: float, anchor: int, horizon: int,
             people: pd.DataFrame, share_ups: float = None) -> float:
    """LTV per person for a cell: its observed revenue so far plus the forecast.

    The curve belongs to the CELL (its lever mix, its cohort's hr); the stream
    shares belong to its PEOPLE, because whether someone took the upsell is a
    fact about them, not about their segment.

    share_ups: P(charged for the upsell at a checkpoint | still alive), from
    ltv_v4.upsell. Pass it explicitly -- the fallback below uses the share of
    people who EVER took an upsell, which is a different and much smaller number
    (24.5% vs 49.5% measured), because it counts everyone who churned before the
    week-2 checkpoint as a non-taker when they simply never had the chance.
    Relying on the fallback understates upsell revenue by roughly half.
    """
    if people.empty:
        return observed_at_anchor
    has_base = people["has_base"].to_numpy() if "has_base" in people else np.ones(len(people), bool)
    share_base = float(has_base.mean())
    if share_ups is None:
        share_ups = float((people["has_ups"].to_numpy() & has_base).mean()) if "has_ups" in people else 0.0
    return observed_at_anchor + project_increment(curve, anchor, horizon, share_base, share_ups)


def ltv_curve(curve: pd.Series, observed_at_anchor: float, anchor: int,
              people: pd.DataFrame, horizon: int = H_EXT) -> pd.Series:
    """Full cumulative-LTV curve from the anchor out to `horizon`, for reporting."""
    return pd.Series(
        {n: cell_ltv(curve, observed_at_anchor, anchor, n, people) for n in range(anchor, horizon + 1)}
    )
