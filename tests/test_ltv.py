"""
Synthetic unit tests for the consolidated LTV core (ltv.revenue, ltv.cohorts).

Dependency-free: run directly with
    .venv/Scripts/python.exe tests/test_ltv.py
(also discoverable by pytest if installed). Each test builds a tiny hand-checked
DataFrame so the expected numbers are obvious by eye.
"""
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "web", "golden"))

from ltv import cohorts, revenue
from ltv.config import BASE_PRICE


def _golden(rows):
    """rows: list of (sub, cus, 'YYYY-MM-DD HH:MM:SS', event_name, price_amount)"""
    return pd.DataFrame(
        [{"subscription_id": s, "customer_user_id": c,
          "event_datetime": pd.Timestamp(t, tz="UTC"), "event_name": e, "price_amount": p}
         for (s, c, t, e, p) in rows]
    )


# ------------------------------------------------------------ base/ups split
def test_split_base_ups_rule():
    g = _golden([
        ("s1", "c1", "2026-01-01 00:00:00", "subscription_renewed", 9.99),   # full base
        ("s1", "c1", "2026-01-08 00:00:00", "subscription_renewed", 11.99),  # base 9.99 + ups 2.00
        ("s1", "c1", "2026-01-15 00:00:00", "subscription_renewed", 5.99),   # dunning-reduced, all base
        ("s2", "c2", "2026-01-01 00:00:00", "trial_converted", 0.99),        # trial: all ups
        ("s2", "c2", "2026-01-01 00:00:00", "trial_converted", 9.99),        # trial at 9.99: all ups
    ])
    p = revenue.paid_events(g).reset_index(drop=True)
    assert np.allclose(p.loc[0, ["base_amt", "ups_amt"]].tolist(), [9.99, 0.0])
    assert np.allclose(p.loc[1, ["base_amt", "ups_amt"]].tolist(), [9.99, 2.0])
    assert np.allclose(p.loc[2, ["base_amt", "ups_amt"]].tolist(), [5.99, 0.0])
    assert np.allclose(p.loc[3, ["base_amt", "ups_amt"]].tolist(), [0.0, 0.99])
    assert np.allclose(p.loc[4, ["base_amt", "ups_amt"]].tolist(), [0.0, 9.99])
    # base is never above the plan price; ups is never negative
    assert (p["base_amt"] <= BASE_PRICE + 1e-9).all()
    assert (p["ups_amt"] >= -1e-9).all()


def test_billing_issue_carries_no_money():
    g = _golden([
        ("s1", "c1", "2026-01-01 00:00:00", "subscription_renewed", 9.99),
        ("s1", "c1", "2026-01-05 00:00:00", "billing_issue_detected", np.nan),
        ("s1", "c1", "2026-01-06 00:00:00", "subscription_expired", np.nan),
        ("s1", "c1", "2026-01-02 00:00:00", "trial_started", np.nan),
    ])
    subs = revenue.subscription_net_revenue(g)
    assert len(subs) == 1
    # only the single 9.99 renewal is counted; the three non-money events are ignored
    assert np.isclose(subs.loc[0, "net_revenue"], 9.99)


# ------------------------------------------------------------ refund dedup
def test_refund_dedup_same_sub_amount_day():
    g = _golden([
        ("s1", "c1", "2026-01-01 00:00:00", "subscription_renewed", 9.99),
        # 3 burst-duplicate refund rows: same sub, same amount, same calendar day -> 1
        ("s1", "c1", "2026-02-01 10:00:00", "subscription_refunded", 9.99),
        ("s1", "c1", "2026-02-01 10:00:02", "subscription_refunded", 9.99),
        ("s1", "c1", "2026-02-01 10:05:00", "subscription_refunded", 9.99),
        # a genuinely different refund on another day survives
        ("s1", "c1", "2026-02-08 10:00:00", "subscription_refunded", 9.99),
    ])
    d = revenue.dedup_refunds(g)
    assert len(d) == 2, f"expected 2 deduped refunds, got {len(d)}"


# ------------------------------------------------------------ refund cap / no-negative
def test_refund_capped_never_negative():
    g = _golden([
        ("s1", "c1", "2026-01-01 00:00:00", "subscription_renewed", 9.99),
        ("s1", "c1", "2026-01-08 00:00:00", "subscription_renewed", 9.99),  # total payments 19.98
        # deduped refund total 29.97 > payments -> must cap at 19.98, net -> 0
        ("s1", "c1", "2026-02-01 00:00:00", "subscription_refunded", 9.99),
        ("s1", "c1", "2026-02-02 00:00:00", "subscription_refunded", 9.99),
        ("s1", "c1", "2026-02-03 00:00:00", "subscription_refunded", 9.99),
    ])
    subs = revenue.subscription_net_revenue(g)
    row = subs.iloc[0]
    assert np.isclose(row["total_payments"], 19.98)
    assert np.isclose(row["refund_dedup_total"], 29.97)
    assert np.isclose(row["refund_capped"], 19.98), "refund must be capped at total payments"
    assert row["net_revenue"] >= -1e-9 and np.isclose(row["net_revenue"], 0.0)


def test_refund_proportional_base_ups():
    # one $11.99 renewal -> base 9.99, ups 2.00; a $5.995 refund removed pro-rata
    g = _golden([
        ("s1", "c1", "2026-01-01 00:00:00", "subscription_renewed", 11.99),
        ("s1", "c1", "2026-02-01 00:00:00", "subscription_refunded", 5.995),
    ])
    subs = revenue.subscription_net_revenue(g)
    row = subs.iloc[0]
    share_base = 9.99 / 11.99
    assert np.isclose(row["base_net"], 9.99 - 5.995 * share_base)
    assert np.isclose(row["ups_net"], 2.0 - 5.995 * (1 - share_base))
    assert np.isclose(row["net_revenue"], 11.99 - 5.995)


# ------------------------------------------------------------ event-level running cap
def test_running_cap_never_dips_negative():
    g = _golden([
        ("s1", "c1", "2026-01-01 00:00:00", "subscription_renewed", 9.99),
        # over-refund after a single payment: causal net must floor at 0, not go negative
        ("s1", "c1", "2026-01-05 00:00:00", "subscription_refunded", 9.99),
        ("s1", "c1", "2026-01-06 00:00:00", "subscription_refunded", 9.99),
    ])
    ev = revenue.event_level_net_events(g).sort_values("event_datetime")
    running = (ev["base_net_ev"] + ev["ups_net_ev"]).cumsum()
    assert (running >= -1e-9).all(), f"running net went negative: {running.tolist()}"


# ------------------------------------------------------------ cohorts: organic classification
def test_organic_sentinels():
    assert cohorts._is_organic(None)
    assert cohorts._is_organic("")
    assert cohorts._is_organic("unknown")
    assert cohorts._is_organic("None")   # case-insensitive
    assert not cohorts._is_organic("device-security-check-gate")
    assert not cohorts._is_organic("world-cup")


def test_is_payer_definition():
    pop = pd.DataFrame({"email": ["a", "b", "c"], "total": [0.0, 9.99, -0.0]})
    out = cohorts.add_is_payer(pop)
    assert out["is_payer"].tolist() == [False, True, False]
    d = cohorts.denominators(out)
    assert d == {"n_attributed": 3, "n_payers": 1}


# ------------------------------------------------------------ runner
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    n_pass = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            n_pass += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{n_pass}/{len(tests)} tests passed")
    sys.exit(0 if n_pass == len(tests) else 1)
