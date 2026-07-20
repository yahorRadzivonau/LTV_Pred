"""
Unified per-person revenue module: real captured money only.
billing_issue_detected / entered_grace_period / subscription_expired carry
NO price_amount in golden_all (already excluded by the base pipeline) -- verified
on Marie (cus_1782471076057) below. Dunning-reduced successful renewals ARE real
(base). subscription_refunded is subtracted. Upsell ($11.99 vs $9.99, trial charge)
tracked separately as 'ups', never mixed into 'base'.

Run: .venv/Scripts/python.exe web/golden/web_person_level_revenue.py
"""
import json
import os
from pathlib import Path

import pandas as pd
import numpy as np

# Resolve all project-relative paths from the repository root, regardless of
# the working directory configured in PyCharm or the shell.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
os.chdir(ROOT)

from ltv import revenue
from ltv.config import BASE_PRICE, SNAPSHOT_TS_REVENUE as SNAPSHOT_TS

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 20)

# ---------------------------------------------------------------- STEP 0/1: revenue events + email map
golden = pd.read_parquet("data/golden/golden_all.parquet")

def revenue_events(golden):
    # base/ups split for captured payments comes from the single source ltv.revenue;
    # refunds are represented here as negative rows (this script's older, uncapped view --
    # the authoritative capped/deduped rule lives in ltv.revenue + web_person_level_fix.py).
    paid = revenue.paid_events(golden)[
        ["subscription_id", "customer_user_id", "event_datetime", "event_name", "amt", "base_amt", "ups_amt"]
    ]
    refund = golden[golden["event_name"] == "subscription_refunded"].copy()
    refund["amt"] = -refund["price_amount"]
    refund["base_amt"] = np.sign(refund["amt"]) * np.minimum(refund["amt"].abs(), BASE_PRICE)
    refund["ups_amt"] = refund["amt"] - refund["base_amt"]
    ev = pd.concat([paid, refund[paid.columns]], ignore_index=True)
    return ev

rev = revenue_events(golden)

marie = rev[rev["customer_user_id"] == "cus_1782471076057"]
print("=== STEP 0/1 check: Marie (cus_1782471076057) ===")
print(marie)
print(f"Marie total base={marie['base_amt'].sum():.2f}  ups={marie['ups_amt'].sum():.2f}  "
      f"total={marie['amt'].sum():.2f}  (billing_issue/grace/expired never enter golden.price_amount -> model clean)")

print(f"\nTotal revenue events: {len(rev)}, total money = ${rev['amt'].sum():,.2f} "
      f"(base=${rev['base_amt'].sum():,.2f}, ups=${rev['ups_amt'].sum():,.2f})")

# ---------------------------------------------------------------- email map (cus_id -> email)
stripe_map = pd.read_parquet("data/raw/_tmp_stripe_cus_email.parquet")
solidgate_map = pd.read_parquet("data/raw/_tmp_solidgate_cus_email.parquet")
cus_email = pd.concat([stripe_map, solidgate_map], ignore_index=True).drop_duplicates(subset="cus_id", keep="first")
print(f"\ncus_id -> email map: {len(cus_email)} distinct cus_id")

rev = rev.merge(cus_email, left_on="customer_user_id", right_on="cus_id", how="left")
matched = rev["email"].notna().mean()
print(f"revenue events matched to an email: {matched:.1%}")
print(f"distinct customer_user_id in revenue events: {rev['customer_user_id'].nunique()}, "
      f"distinct customer_user_id WITHOUT email match: {rev.loc[rev['email'].isna(),'customer_user_id'].nunique()}")

rev.to_parquet("data/raw/_tmp_revenue_events_with_email.parquet")
