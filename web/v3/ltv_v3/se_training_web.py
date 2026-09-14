"""
Build the web payment-step matrix: one row per base payment of one person.

Schema is a SUPERSET of data/se_training.parquet (the iOS matrix): all 15 iOS
columns are present with the same meaning, so core/map_model.py's logic applies
unchanged, plus the web-only `funnel` lever and the `outcome_known` censoring
flag. Verified against data/golden/golden_all_se_training.parquet, which shares
the identical 15-column shape.

TWO THINGS DIFFER FROM ios/pipeline/se_training.py, both deliberate:

1. app_id = cohort_date, not a product id.
   This is the whole point of v3. In v2 the web matrix carries a single app_id
   ('invinci'), so map_model computes ONE hr for the entire product and the
   personal lever multipliers average away into a single pooled curve -- two
   cells of the same age end up with an identical forecast regardless of funnel,
   source or geo. Making the cohort the "app" gives every cohort its own hr,
   with the usual K_SHRINK shrinkage, and lets the levers survive aggregation.

2. A step is only evaluable once enough TIME has passed -- never conditioned on
   what happened. ios/pipeline/se_training.py labels outcomes with a 60-day
   return window (a later payment = "recovered" = ALIVE) but censors with
   weeks_obs >= step_k, which only guarantees ONE week has elapsed. Someone who
   pays again on day 20 is recorded dead if the snapshot catches them on day 8.
   Measured: 9.4% of web clean-zone rows are marked dead with the window still
   open, against 2.4% on iOS.

   A tempting fix -- "keep the row if the outcome is known: either a next
   payment exists, or the window elapsed" -- is badly biased and was measured
   and discarded. Survivors confirm in ~7 days, deaths take the full window, so
   the rule keeps nearly every survivor and drops most deaths: observed death
   rate in the clean zone collapsed from 0.182 to 0.030 and h_base went to ZERO
   for steps 2-8. Censoring must not depend on the outcome.

   The window is 21 days for web, not the iOS 60: this product bills WEEKLY, and
   99.5% of all continuation gaps land within 21 days. Full sweep and retention
   figures are in ltv_v3/config.py:RETURN_WINDOW_DAYS. The same value labels and
   censors, so the two rules cannot drift apart.

   Expect this to RAISE predicted survival (step 7 h_base: 0.107 unfiltered ->
   0.059 censored). The calendar backtest already shows over-prediction; the
   false deaths were partially masking it. Removing a compensating error is not
   a regression.
"""
import numpy as np
import pandas as pd

from ltv_v3.config import (
    DUNNING_EVENT_TYPES, VOLUNTARY_EVENT_TYPES, TRIAL_EVENT_TYPE,
    RETURN_WINDOW_DAYS, JOIN_KEY,
)
from ltv_v3 import revenue as R

# Same thresholds as ios/pipeline/se_training.py, kept identical on purpose so
# the two matrices stay comparable.
RECOVERED_DELAY_DAYS = 10   # a gap longer than this = came back, not a clean renewal

IOS_MATRIX_COLUMNS = [
    "sub_id", "step_k", "state_from", "outcome", "weeks_obs",
    "pay_ts", "days_to_next",
    "app_id", "geo", "media_source", "attribution_source",
    "plan_interval", "trial_days", "cohort_month", "billing_day_of_month",
]
WEB_EXTRA_COLUMNS = ["funnel", "evaluable", "cohort_date"]


def _outcome(row) -> str:
    """Port of ios/pipeline/se_training.py:outcome(), same branch order.

    A payment inside the 60-day return window continues the SAME chain rather
    than starting a new one -- that is what stops "card failed, salary arrives
    Friday" from being scored as death.
    """
    has_next_in_window = pd.notna(row["next_pay_ts"]) and row["days_to_next"] <= RETURN_WINDOW_DAYS
    if has_next_in_window:
        if row["had_issue"] or row["days_to_next"] > RECOVERED_DELAY_DAYS:
            return "recovered"
        return "renewed"
    if row["had_vol"]:
        return "voluntary_cancel"
    if row["had_issue"]:
        return "billing_issue"
    return "churned"


def _state_flags(events: pd.DataFrame, ladder: pd.DataFrame) -> pd.DataFrame:
    """had_issue / had_vol per (person, step): did a dunning or cancel event fall
    between this payment and the next one?"""
    state = events[events["event_type"].isin(DUNNING_EVENT_TYPES + VOLUNTARY_EVENT_TYPES)]
    state = state[[JOIN_KEY, "ts", "event_type"]]
    if state.empty:
        return pd.DataFrame(columns=[JOIN_KEY, "step_k", "had_issue", "had_vol"])

    merged = state.merge(
        ladder[[JOIN_KEY, "step_k", "ts", "next_pay_ts"]],
        on=JOIN_KEY, suffixes=("_ev", "_step"),
    )
    in_window = (merged["ts_ev"] > merged["ts_step"]) & (
        merged["next_pay_ts"].isna() | (merged["ts_ev"] <= merged["next_pay_ts"])
    )
    merged = merged[in_window]
    return merged.groupby([JOIN_KEY, "step_k"], as_index=False).agg(
        had_issue=("event_type", lambda s: s.isin(DUNNING_EVENT_TYPES).any()),
        had_vol=("event_type", lambda s: s.isin(VOLUNTARY_EVENT_TYPES).any()),
    )


def _plan_interval(ladder: pd.DataFrame) -> pd.Series:
    """Median gap between payments, bucketed. Present ONLY for schema parity.

    NEVER use as a lever: it is derived from the subscription's own outcomes, so
    "unknown" largely means "died after the first payment" -- a ready-made answer
    leaking into a feature. core/map_model.py excludes it for exactly this
    reason (see its module docstring).
    """
    gaps = ladder.groupby(JOIN_KEY)["ts"].diff().dt.total_seconds() / 86400
    sane = gaps[gaps <= 60]
    med = sane.groupby(ladder.loc[sane.index, JOIN_KEY]).median()

    def bucket(g):
        if pd.isna(g):
            return "unknown"
        if 5 <= g <= 10:
            return "week"
        if 25 <= g <= 35:
            return "month"
        if g >= 350:
            return "year"
        return "unknown"

    return med.map(bucket)


def _trial_days(events: pd.DataFrame, ladder: pd.DataFrame) -> pd.Series:
    """Days from trial start to first base payment. Owner states the trial is 1
    week; this measures what actually happened."""
    trial_first = events[events["event_type"].eq(TRIAL_EVENT_TYPE)].groupby(JOIN_KEY)["ts"].min()
    base_first = ladder.groupby(JOIN_KEY)["ts"].min()
    return (base_first - trial_first).dt.days


def build_matrix(events: pd.DataFrame, pop: pd.DataFrame, snapshot_ts=None) -> pd.DataFrame:
    """Payment-step matrix restricted to the population.

    snapshot_ts: the "now" the matrix is censored against. Defaults to the latest
    event in the pull. The LOO / calendar harness passes an earlier T to rebuild
    the matrix as it would have looked back then -- which is why weeks_obs and
    outcome_known are computed here rather than baked into the pull.
    """
    snapshot_ts = pd.Timestamp(snapshot_ts, tz="UTC") if snapshot_ts is not None else events["ts"].max()

    members = set(pop[JOIN_KEY])
    events = events[events[JOIN_KEY].isin(members) & (events["ts"] <= snapshot_ts)].copy()

    ladder = R.base_payment_ladder(events)
    if ladder.empty:
        raise RuntimeError("No base payments left after restricting to the population")

    flags = _state_flags(events, ladder)
    steps = ladder.merge(flags, on=[JOIN_KEY, "step_k"], how="left")
    steps[["had_issue", "had_vol"]] = steps[["had_issue", "had_vol"]].fillna(False).astype(bool)
    steps["outcome"] = steps.apply(_outcome, axis=1)

    # --- per-person attributes -------------------------------------------------
    attrs = pop.set_index(JOIN_KEY)
    first_pay = ladder.groupby(JOIN_KEY)["ts"].min()

    mx = steps.rename(columns={"ts": "pay_ts"})
    mx["sub_id"] = mx[JOIN_KEY]

    # app_id = cohort. THE change that makes hr per-cohort instead of global.
    mx["cohort_date"] = mx[JOIN_KEY].map(attrs["cohort_date"])
    mx["app_id"] = mx["cohort_date"].dt.date.astype(str)

    mx["geo"] = mx[JOIN_KEY].map(attrs["geo"])
    mx["media_source"] = mx[JOIN_KEY].map(attrs["utm_source"])
    mx["funnel"] = mx[JOIN_KEY].map(attrs["first_funnel"])
    mx["attribution_source"] = "(none)"          # appsflyer web carries no equivalent field
    mx["state_from"] = "active"                  # placeholder, as in iOS; grey state is next iteration
    mx["cohort_month"] = mx["cohort_date"].dt.to_period("M").astype(str)
    mx["billing_day_of_month"] = mx["pay_ts"].dt.day
    mx["plan_interval"] = mx[JOIN_KEY].map(_plan_interval(ladder)).fillna("unknown")
    mx["trial_days"] = mx[JOIN_KEY].map(_trial_days(events, ladder))

    # weeks_obs: how long this person has been observable, in weeks since their
    # first base payment. Same definition as iOS.
    mx["weeks_obs"] = mx[JOIN_KEY].map(
        (snapshot_ts - first_pay).dt.total_seconds() / (7 * 86400)
    )

    # evaluable: is this step old enough to score at all? Strictly a function of
    # TIME, never of what happened -- survivors and deaths must wait the same
    # amount, or the risk set is biased. See config.RETURN_WINDOW_DAYS for the
    # measured sweep behind the 21 days, and for the outcome-dependent version
    # of this filter that was tried and discarded (it removed 84% of deaths).
    mx["evaluable"] = (snapshot_ts - mx["pay_ts"]).dt.days >= RETURN_WINDOW_DAYS

    mx = mx[IOS_MATRIX_COLUMNS + WEB_EXTRA_COLUMNS].reset_index(drop=True)
    _assert_schema(mx)
    return mx


def _assert_schema(mx: pd.DataFrame) -> None:
    """Fail loudly rather than let a silently-renamed column reach map_web."""
    missing = set(IOS_MATRIX_COLUMNS) - set(mx.columns)
    assert not missing, f"matrix is missing iOS columns: {sorted(missing)}"
    assert mx["step_k"].min() == 1, f"step_k must start at 1, got {mx['step_k'].min()}"
    assert (mx["weeks_obs"] >= 0).all(), "negative weeks_obs"
    assert mx["billing_day_of_month"].between(1, 31).all(), "billing_day_of_month out of range"
    allowed = {"renewed", "recovered", "voluntary_cancel", "billing_issue", "churned"}
    unknown = set(mx["outcome"].unique()) - allowed
    assert not unknown, f"unexpected outcome values: {unknown}"


def add_survived(mx: pd.DataFrame) -> pd.DataFrame:
    """survived / died, exactly as core.common.load_matrix defines them."""
    mx = mx.copy()
    mx["survived"] = mx["outcome"].isin(["renewed", "recovered"]).astype(int)
    mx["died"] = 1 - mx["survived"]
    return mx


def training_rows(mx: pd.DataFrame) -> pd.DataFrame:
    """Rows eligible to FIT on: old enough that their outcome has settled.

    Pass the result to core.common.empirical_hbase -- its own internal
    `weeks_obs >= step_k` filter still applies on top, so core is never touched.
    """
    return mx[mx["evaluable"]].copy()
