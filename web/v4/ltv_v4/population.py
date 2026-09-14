"""
Population (the denominator) for the v3 web pipeline.

WHY THIS MODULE EXISTS -- the v2 regression it replaces:

  web/v2/pull_ltv_v2_population.py selected people FROM PAID EVENTS ONLY, so a
  non-payer could not structurally appear. The result was n_attributed ==
  n_payers (3512 == 3512), which silently made ltv_per_attributed_* identical to
  ltv_per_payer_* -- inflated ~1.38x. per_attributed is exactly the column you
  divide CAC by, so the error pointed the wrong way for buying decisions.

  The original 07-11 population file had a 57% payer rate, but the script that
  produced it never existed in the repo (docs/REPO_AUDIT.md section 9), so its
  rule could not simply be re-run.

THE v3 DEFINITION (owner's, explicit):

  population = everyone who started a trial of ANY kind (real $0.99 /
               placeholder $1.00 hold / free NULL)
             UNION everyone who paid for the base plan

Measured on the 2026-07-28 pull, after WINDOW_START: 11,057 people, 56.6% of
them payers. For reference: v2 at git HEAD had 4,902 / 73.6%, and the broken
working-tree version had 3,512 / 100%.

first_funnel/geo/utm are taken from the person's EARLIEST entry event, so
non-payers carry a funnel too -- otherwise they would all land in "unknown" and
drop out of the funnel breakdown, quietly reintroducing the same bias.
"""
import pandas as pd

from ltv_v4.config import (
    BASE_EVENT_TYPE, TRIAL_EVENT_TYPE, WINDOW_START, JOIN_KEY,
)
from ltv_v4.dims import normalize_dim

# Events that count as ENTERING the product. upsale_converted is deliberately
# absent: an upsell is never a first touch, it can only follow a base plan.
ENTRY_EVENT_TYPES = (TRIAL_EVENT_TYPE, BASE_EVENT_TYPE)


def build_population(events: pd.DataFrame, snapshot_now=None, window_start=WINDOW_START) -> pd.DataFrame:
    """One row per person: entry date, cohort, levers, age.

    snapshot_now: reference date for age_weeks_now. None = latest event in the
    pull (appsflyer is live, unlike golden's frozen boundary).
    """
    entry = events[events["event_type"].isin(ENTRY_EVENT_TYPES)].copy()
    if entry.empty:
        raise RuntimeError("No entry events (trial_started / subscription_started) in the pull")

    entry = entry.sort_values([JOIN_KEY, "ts"])
    first = entry.groupby(JOIN_KEY, as_index=False).first()

    # campaign_name/ad_name entered the pull 2026-08-06 (for the weekly-curve
    # breakdown tables); older event files predate them, so their absence must
    # not break a rebuild-at-T from an old snapshot.
    dim_cols = [c for c in ("campaign_name", "ad_name") if c in first.columns]

    pop = first[[JOIN_KEY, "ts", "funnel_name", "country", "utm_source"] + dim_cols].rename(
        columns={"ts": "first_date", "funnel_name": "first_funnel", "country": "geo"}
    )

    for col, fallback in [("first_funnel", "unknown"), ("geo", "(none)"), ("utm_source", "(missing)")]:
        pop[col] = pop[col].astype("string").fillna(fallback).replace("", fallback)

    # Same earliest-entry-event rule as the levers above, so non-payers carry a
    # campaign/ad too. Normalisation (url-decode, macro leftovers and 'unknown'
    # into one explicit bucket) is measured and documented in ltv_v4/dims.py.
    for col in dim_cols:
        pop[col] = normalize_dim(pop[col], col)

    # WINDOW_START is applied HERE, not in the pull: the pull reaches further
    # back (PULL_FLOOR_DATE) precisely so that a person's true first_date is
    # visible even when it predates the training window. Someone who entered
    # before the window is dropped whole -- not silently re-dated into it.
    pop = pop[pop["first_date"] >= window_start].reset_index(drop=True)

    snapshot_now = pd.Timestamp(snapshot_now, tz="UTC") if snapshot_now is not None else events["ts"].max()
    pop["snapshot_now"] = snapshot_now
    pop["age_weeks_now"] = ((snapshot_now - pop["first_date"]).dt.days // 7).clip(lower=0)

    # cohort = Monday of the entry week. This becomes app_id in the training
    # matrix, which is what gives each cohort its own hr.
    pop["cohort_date"] = (
        pop["first_date"] - pd.to_timedelta(pop["first_date"].dt.weekday, unit="D")
    ).dt.normalize()

    return pop


def attach_revenue(pop: pd.DataFrame, revenue: pd.DataFrame) -> pd.DataFrame:
    """Left-join per-person revenue onto the population, zero-filling non-payers.

    Left join is the whole point: a person with no revenue row is a real member
    of the denominator, not a missing record.

    DELIBERATELY NO COLUMN NAMED is_payer / n_attributed. Those two names are
    what let the v2 regression hide: "payer" silently meant three different
    populations at three points in the pipeline, and "attributed" stopped
    containing non-payers without anything failing. v3 names each denominator
    after what it literally counts, and the LTV columns downstream are suffixed
    to match (ltv_per_trial_* vs ltv_per_base_payer_*):

      has_base        bought the $9.99/week plan -- the real conversion, and the
                      population the survival curve is actually about
      has_paid_trial  paid the one-time $0.99 (placeholder/free trials are False)
      has_ups         took the $11.99/month upsell
      paid_anything   any money at all, INCLUDING a $0.99-trial-only person.
                      Kept for reconciliation only: it flatters conversion
                      (84.5% vs 56.6% on the 2026-07-28 pull) because someone
                      who paid $0.99 and left counts as a "payer" with $0.99 of
                      LTV. Do not use it as an LTV denominator.
    """
    cols = ["trial_net", "base_net", "ups_net", "total_net", "has_ups", "has_paid_trial"]
    out = pop.merge(revenue[[JOIN_KEY] + cols], on=JOIN_KEY, how="left")
    out[["trial_net", "base_net", "ups_net", "total_net"]] = out[
        ["trial_net", "base_net", "ups_net", "total_net"]
    ].fillna(0.0)
    out[["has_ups", "has_paid_trial"]] = out[["has_ups", "has_paid_trial"]].fillna(False).astype(bool)

    out["has_base"] = out["base_net"] > 0
    out["paid_anything"] = out["total_net"] > 0
    return out
