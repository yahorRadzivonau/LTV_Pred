"""
FIX 1: refund dedup (same burst-duplicate pattern as billing_issue, just clustered
within seconds/same-day instead of identical timestamp) + clip refund to <= payments
per subscription, so net_revenue per subscription never goes negative.
FIX 2: cohort tables with TWO explicit denominators (n_attributed vs n_payers) and
unambiguous column names.
Revenue computation itself (which event_types count) is UNCHANGED from
web_person_level_revenue.py -- only the refund handling changes.

Run: .venv/Scripts/python.exe web/golden/web_person_level_fix.py
"""
import os
from pathlib import Path

import numpy as np
import pandas as pd

# Resolve all project-relative paths from the repository root, regardless of
# the working directory configured in PyCharm or the shell.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
os.chdir(ROOT)

from ltv import cohorts, revenue
from ltv.config import BASE_PRICE, SNAPSHOT_TS  # noqa: F401 SNAPSHOT_TS kept for parity

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 25)

OUT_DIR = "reports/web_model/05_person_level_clean"

# ================================================================ FIX 1: refund dedup + clip
# All money arithmetic (base/ups split, refund dedup, cap, proportional net) now lives
# in ltv.revenue -- these calls reproduce the previous inline logic bit-for-bit.
golden = pd.read_parquet("data/golden/golden_all.parquet")

paid = revenue.paid_events(golden)
refund_raw = golden[golden["event_name"] == "subscription_refunded"].copy()
refund_dedup = revenue.dedup_refunds(golden)
n_refund_raw = len(refund_raw)
n_refund_dedup = len(refund_dedup)
print(f"=== FIX 1a: refund dedup (same sub_id + amount + calendar day -> keep 1) ===")
print(f"refund rows before dedup: {n_refund_raw}, after dedup: {n_refund_dedup}, "
      f"removed {n_refund_raw - n_refund_dedup} duplicate rows "
      f"(${refund_raw['price_amount'].sum() - refund_dedup['price_amount'].sum():,.2f} phantom refund removed)")

subs = revenue.subscription_net_revenue(golden)
n_clipped = (subs["refund_dedup_total"] > subs["refund_capped"]).sum()
print(f"\n=== FIX 1b: clip refund_capped = min(refund_dedup, payments) ===")
print(f"subscriptions where dedup-refund STILL exceeded payments (needed clipping): {n_clipped} "
      f"(total clipped amount: ${(subs['refund_dedup_total']-subs['refund_capped']).sum():,.2f})")

n_neg_subs = (subs["net_revenue"] < -1e-9).sum()
n_neg_before_fix = (subs["total_payments"] - subs["refund_dedup_total"] < -1e-9).sum()
print(f"\nsubscriptions with negative net_revenue AFTER fix: {n_neg_subs} "
      f"(would have been {n_neg_before_fix} without the clip, using deduped refunds only)")

# ================================================================ rebuild per-person revenue from subscription-level net
cus_email = cohorts.build_cus_email_map()  # cus_id -> email (Stripe + Solidgate), single source
subs = subs.merge(cus_email, left_on="customer_user_id", right_on="cus_id", how="left")

pop = pd.read_parquet("data/raw/_tmp_pop_final.parquet")[["email", "first_date", "first_funnel", "utm_source", "age_weeks_now"]]

per_person = subs.groupby("email").agg(
    base_to_date=("base_net", "sum"), ups_to_date=("ups_net", "sum"), n_subs=("subscription_id", "nunique"),
).reset_index()
per_person["total"] = per_person["base_to_date"] + per_person["ups_to_date"]
n_neg_people = (per_person["total"] < -1e-9).sum()
print(f"people with negative total LTV AFTER fix: {n_neg_people}")

pop_fixed = pop.merge(per_person, on="email", how="left")
pop_fixed[["base_to_date", "ups_to_date", "total", "n_subs"]] = pop_fixed[["base_to_date", "ups_to_date", "total", "n_subs"]].fillna(0.0)
pop_fixed = cohorts.add_is_payer(pop_fixed)  # is_payer = total > 0 (single-source denominator)
print(f"\ntotal clean revenue (post-fix), 5406 population: ${pop_fixed['base_to_date'].sum() + pop_fixed['ups_to_date'].sum():,.2f}")
print(f"n_payers (post-fix): {pop_fixed['is_payer'].sum()} of {len(pop_fixed)}")

pop_fixed.to_parquet("data/raw/_tmp_pop_fixed.parquet")
subs.to_parquet("data/raw/_tmp_subs_fixed.parquet")

# ================================================================ CONTROL: re-check the 05-04 anchor
m = pd.read_csv("data/raw/_tmp_cohort0504_emails.csv")
emails92 = set(m["email"])
anchor = pop_fixed[pop_fixed["email"].isin(emails92)]
print(f"\n=== CONTROL: cohort 05-04 anchor after fix ===")
print(f"n_people found: {len(anchor)} (was 92 originally / 90 matched into the 5406 pop before)")
print(f"per-payer (base+ups) mean = ${anchor.loc[anchor['is_payer'], 'total'].mean():.2f}  "
      f"(target: stay ~$64.72, sub-level fact anchor = $55.16)")
print(f"per-attributed (base+ups) mean = ${anchor['total'].mean():.2f}")

# ================================================================ event-level net revenue timeline (for weekly fact curves)
# time-ordered payment/refund events with the causal running-cap (refund can never
# take net-paid-so-far below 0) -- now in ltv.revenue, bit-for-bit identical.
events = revenue.event_level_net_events(golden)
events = events.merge(cus_email, left_on="customer_user_id", right_on="cus_id", how="left")
events = events.dropna(subset=["email"]).merge(pop[["email", "first_date"]], on="email", how="left")
events["week_of_life"] = ((events["event_datetime"].dt.tz_convert("UTC") - events["first_date"]).dt.days // 7).clip(lower=0)

pw = events.groupby(["email", "week_of_life"]).agg(base=("base_net_ev", "sum"), ups=("ups_net_ev", "sum")).reset_index()
MAX_OBS_WEEK = int(pop["age_weeks_now"].max())
piv_base = pw.pivot_table(index="email", columns="week_of_life", values="base", aggfunc="sum", fill_value=0.0)
piv_ups = pw.pivot_table(index="email", columns="week_of_life", values="ups", aggfunc="sum", fill_value=0.0)
piv_base = piv_base.reindex(columns=range(0, MAX_OBS_WEEK + 1), fill_value=0.0).cumsum(axis=1)
piv_ups = piv_ups.reindex(columns=range(0, MAX_OBS_WEEK + 1), fill_value=0.0).cumsum(axis=1)
cum_base = piv_base.reindex(pop["email"], fill_value=0.0)
cum_ups = piv_ups.reindex(pop["email"], fill_value=0.0)
cum_base.index = pop["email"].values
cum_ups.index = pop["email"].values

n_neg_curve = ((cum_base + cum_ups) < -1e-9).any(axis=1).sum()
print(f"\npeople whose cumulative curve ever dips negative at ANY week (event-level check): {n_neg_curve}")

cum_base.to_parquet("data/raw/_tmp_cum_base_fixed.parquet")
cum_ups.to_parquet("data/raw/_tmp_cum_ups_fixed.parquet")

# sanity: event-level total should match subscription-level total closely
tot_event_level = (cum_base.iloc[:, -1] + cum_ups.iloc[:, -1]).sum()
tot_sub_level = pop_fixed["total"].sum()
print(f"total via event-level running-cap: ${tot_event_level:,.2f}  vs  total via subscription-level cap: ${tot_sub_level:,.2f}  "
      f"(diff ${tot_event_level - tot_sub_level:,.2f}, small timing-order difference expected)")
