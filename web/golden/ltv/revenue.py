"""
The ONE revenue rule for the web LTV pipeline.

Captured money only:
  - counts event_name in PAID_EVENTS (trial_converted, subscription_renewed)
  - subtracts refunds (subscription_refunded), AFTER de-duplicating burst-duplicate
    refund rows and capping total refund per subscription at total payments
  - billing_issue_detected / entered_grace_period / subscription_expired / trial_started
    carry NO money (they have price_amount NaN in golden and are simply not counted)

Money is split into two columns everywhere:
  - base : the $9.99 recurring plan, capped at BASE_PRICE (dunning-reduced renewals
           stay in base at their reduced amount)
  - ups  : the remainder above BASE_PRICE for a renewal, and the WHOLE amount for a
           trial_converted (trial activation / upsell)

These functions are pure (DataFrame in, DataFrame out) so they are unit-testable and
called from every script instead of re-implemented. They reproduce web_person_level_fix.py
bit-for-bit; see reconcile.py.
"""
import numpy as np
import pandas as pd

from ltv.config import BASE_PRICE, PAID_EVENTS, REFUND_EVENT


def split_base_ups(paid: pd.DataFrame, amount_col: str = "amt") -> pd.DataFrame:
    """Add base_amt / ups_amt to a frame of PAID_EVENTS rows.

    renewal  -> base = min(amount, BASE_PRICE), ups = amount - base
    trial    -> base = 0,                       ups = amount
    """
    out = paid.copy()
    out["base_amt"] = np.where(
        out["event_name"] == "trial_converted",
        0.0,
        np.minimum(out[amount_col], BASE_PRICE),
    )
    out["ups_amt"] = out[amount_col] - out["base_amt"]
    return out


def paid_events(golden: pd.DataFrame) -> pd.DataFrame:
    """Rows of captured payments (PAID_EVENTS) with amt + base/ups columns."""
    paid = golden[golden["event_name"].isin(PAID_EVENTS)].copy()
    paid["amt"] = paid["price_amount"]
    return split_base_ups(paid)


def dedup_refunds(golden: pd.DataFrame) -> pd.DataFrame:
    """Refund rows, de-duplicated on (subscription_id, price_amount, calendar day).

    Refunds arrive as burst duplicates (same amount, same day, many rows); only one
    is a real clawback. Adds an 'ev_date' column (used by the dedup key).
    """
    refund = golden[golden["event_name"] == REFUND_EVENT].copy()
    refund["ev_date"] = pd.to_datetime(refund["event_datetime"]).dt.date
    return refund.drop_duplicates(subset=["subscription_id", "price_amount", "ev_date"], keep="first")


def subscription_net_revenue(golden: pd.DataFrame) -> pd.DataFrame:
    """Per-subscription net revenue after refund dedup + cap.

    Returns one row per subscription_id with:
      base_payments, ups_payments, total_payments, customer_user_id,
      refund_dedup_total, refund_capped, base_net, ups_net, net_revenue
    refund_capped = min(deduped refund, total payments); the capped refund is removed
    proportionally from base/ups and each is floored at 0 (net can never go negative).
    """
    paid = paid_events(golden)
    refund_dedup = dedup_refunds(golden)

    sub_payments = paid.groupby("subscription_id").agg(
        base_payments=("base_amt", "sum"),
        ups_payments=("ups_amt", "sum"),
        total_payments=("amt", "sum"),
        customer_user_id=("customer_user_id", "first"),
    ).reset_index()
    sub_refund = refund_dedup.groupby("subscription_id").agg(
        refund_dedup_total=("price_amount", "sum")
    ).reset_index()

    subs = sub_payments.merge(sub_refund, on="subscription_id", how="left")
    subs["refund_dedup_total"] = subs["refund_dedup_total"].fillna(0.0)
    subs["refund_capped"] = np.minimum(subs["refund_dedup_total"], subs["total_payments"])

    share_base = np.where(subs["total_payments"] > 0, subs["base_payments"] / subs["total_payments"], 0.0)
    subs["base_net"] = (subs["base_payments"] - subs["refund_capped"] * share_base).clip(lower=0.0)
    subs["ups_net"] = (subs["ups_payments"] - subs["refund_capped"] * (1 - share_base)).clip(lower=0.0)
    subs["net_revenue"] = subs["base_net"] + subs["ups_net"]
    return subs


def _apply_running_cap(g: pd.DataFrame) -> pd.DataFrame:
    """Walk one subscription's events in time order; a refund can never take
    net-paid-so-far below 0 (causal version of the subscription-level cap)."""
    net_base = 0.0
    net_ups = 0.0
    out_base, out_ups = [], []
    for _, row in g.iterrows():
        if row["kind"] == "pay":
            net_base += row["base_amt"]
            net_ups += row["ups_amt"]
            out_base.append(row["base_amt"])
            out_ups.append(row["ups_amt"])
        else:
            avail = net_base + net_ups
            refund_amt = min(row["amt"], avail) if avail > 0 else 0.0
            b_share = net_base / avail if avail > 0 else 0.0
            db = -refund_amt * b_share
            du = -refund_amt * (1 - b_share)
            net_base += db
            net_ups += du
            out_base.append(db)
            out_ups.append(du)
    g = g.copy()
    g["base_net_ev"] = out_base
    g["ups_net_ev"] = out_ups
    return g


def event_level_net_events(golden: pd.DataFrame) -> pd.DataFrame:
    """Time-ordered payment+refund events per subscription with the causal running-cap
    applied, yielding base_net_ev / ups_net_ev per event. Used to build weekly curves."""
    paid = paid_events(golden)
    refund_dedup = dedup_refunds(golden)
    events = pd.concat([
        paid[["subscription_id", "customer_user_id", "event_datetime", "base_amt", "ups_amt"]].assign(kind="pay"),
        refund_dedup[["subscription_id", "customer_user_id", "event_datetime", "price_amount"]]
            .rename(columns={"price_amount": "amt"}).assign(kind="refund"),
    ], ignore_index=True)
    events["event_datetime"] = pd.to_datetime(events["event_datetime"])
    events = events.sort_values(["subscription_id", "event_datetime"])
    return events.groupby("subscription_id", group_keys=False).apply(_apply_running_cap)
