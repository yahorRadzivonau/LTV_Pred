"""
ltv: consolidated core of the web-subscription LTV pipeline.

Modules
-------
config   : every tunable constant (prices, dates, thresholds, funnel whitelist),
           with the model-math constants re-exported from core.common (single
           source of truth -- NOT redefined here).
revenue  : the ONE revenue rule -- captured payments only (renewed + trial_converted
           minus refunds, refund deduped + capped, no billing_issue), base/ups split.
cohorts  : population construction (5406 people), cus_id<->email map, the 05-04
           control cohort, and the attributed/payer denominators.

Everything a script needs about "how money is counted" and "who is in the cohort"
lives here; scripts import, they do not re-implement. See reports/web_golden/reconcile_baseline.json
and reconcile.py for the bit-for-bit guarantee.
"""
