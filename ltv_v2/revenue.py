"""
Per-person revenue from the appsflyer source, keyed on LOWER(email).

Pure functions over the frozen local pull (ltv_v2.config.RAW_EVENTS_PATH) --
no live BQ calls at run time, same discipline as the golden pipeline: pull
once, freeze, verify counts, process locally.
"""
import pandas as pd

from ltv_v2.config import (
    RAW_EVENTS_PATH, BASE_EVENT_TYPE, UPS_EVENT_TYPE, TRIAL_EVENT_TYPE, REFUND_HAIRCUT,
    WINDOW_START, JOIN_KEY,
)


def load_raw_events() -> pd.DataFrame:
    """The frozen captured-revenue events (subscription_started + upsale_converted +
    real-trial trial_started, already scoped to app_name/date/amount at pull time --
    see config.py "trial revenue rule" for how placeholder trial_started rows were
    excluded from this pull). Adds a UTC timestamp column."""
    df = pd.read_parquet(RAW_EVENTS_PATH)
    df["event_datetime"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df


def per_person_revenue(events: pd.DataFrame = None, window_end: pd.Timestamp = None) -> pd.DataFrame:
    """Per-email gross base/ups + haircut-adjusted net, optionally capped at window_end
    (for symmetric comparison against golden's frozen snapshot boundary).

    base = sum(subscription_started amounts) -- the $9.99/week recurring stream
    ups  = sum(upsale_converted amounts) + sum(real trial_started amounts) -- both
           one-time/non-weekly, kept together per the trial revenue rule (config.py)
    net  = (base + ups) * (1 - REFUND_HAIRCUT)  [see config.py: TEMPORARY stopgap]

    is_trial: True if this person has >=1 real trial charge in-window (audit flag).
    """
    if events is None:
        events = load_raw_events()
    scoped = events[events["event_datetime"] >= WINDOW_START]
    if window_end is not None:
        scoped = scoped[scoped["event_datetime"] < window_end]

    base = scoped[scoped["event_type"] == BASE_EVENT_TYPE].groupby(JOIN_KEY)["amt"].sum().rename("base_gross")
    ups_only = scoped[scoped["event_type"] == UPS_EVENT_TYPE].groupby(JOIN_KEY)["amt"].sum().rename("ups_only_gross")
    trial = scoped[scoped["event_type"] == TRIAL_EVENT_TYPE].groupby(JOIN_KEY)["amt"].sum().rename("trial_gross")
    n_trial = scoped[scoped["event_type"] == TRIAL_EVENT_TYPE].groupby(JOIN_KEY).size().rename("n_trial")

    per_person = pd.concat([base, ups_only, trial, n_trial], axis=1).fillna(0.0).reset_index()
    per_person["is_trial"] = per_person["n_trial"] > 0
    per_person["ups_gross"] = per_person["ups_only_gross"] + per_person["trial_gross"]
    per_person["gross"] = per_person["base_gross"] + per_person["ups_gross"]
    per_person["net"] = per_person["gross"] * (1 - REFUND_HAIRCUT)
    # haircut applied proportionally to base/ups so the two streams still sum to net
    per_person["base_net"] = per_person["base_gross"] * (1 - REFUND_HAIRCUT)
    per_person["ups_net"] = per_person["ups_gross"] * (1 - REFUND_HAIRCUT)
    return per_person


def per_person_week_cumulative(first_date: pd.DataFrame, events: pd.DataFrame = None) -> tuple:
    """Per-email cumulative base_net/ups_net by week-of-life (weeks since that
    person's own attribution first_date), haircut-adjusted. Mirrors the golden
    pipeline's cum_base/cum_ups tables so the same table-building logic applies.

    first_date: DataFrame with columns [JOIN_KEY, 'first_date'] (tz-aware UTC).
    Returns (cum_base, cum_ups): two DataFrames indexed by email, columns 0..max_week.
    """
    if events is None:
        events = load_raw_events()
    scoped = events[events["event_datetime"] >= WINDOW_START].merge(first_date, on=JOIN_KEY, how="inner")
    scoped["week_of_life"] = ((scoped["event_datetime"] - scoped["first_date"]).dt.days // 7).clip(lower=0)
    scoped["amt_net"] = scoped["amt"] * (1 - REFUND_HAIRCUT)

    max_week = int(scoped["week_of_life"].max())
    all_emails = first_date[JOIN_KEY]

    def _cum(event_types):
        pw = scoped[scoped["event_type"].isin(event_types)].groupby([JOIN_KEY, "week_of_life"])["amt_net"].sum()
        piv = pw.unstack("week_of_life", fill_value=0.0)
        piv = piv.reindex(columns=range(0, max_week + 1), fill_value=0.0).cumsum(axis=1)
        piv = piv.reindex(all_emails, fill_value=0.0)
        piv.index = all_emails.values
        return piv

    # ups cumulative includes real trial charges too (trial revenue rule, config.py)
    return _cum([BASE_EVENT_TYPE]), _cum([UPS_EVENT_TYPE, TRIAL_EVENT_TYPE])
