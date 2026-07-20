"""
Step 1 of the web golden-build task: reclassify app_id in data/silver/web_events_silver.parquet
by product_id (NOT app_name -- app_name is NULL before 2026-04-16, leaky).

Stripe: prod_id -> app_id via a fixed map (invinci / atelier), else other_old; NULL product_id
(no product_id/attribution at all) -> unknown_old.
Solidgate: dominant Invinci-branded product_ids -> invinci, else other_old; NULL -> unknown_old.

Backs up the pre-existing silver file to
data/silver/web_events_silver_pre_appid_v2_backup.parquet before writing (already done by caller).
Old app_id is kept alongside as app_id_v1_silver for audit / rollback comparison.

Run: .venv/Scripts/python.exe web/silver/reclassify_app_id_v2.py
"""
import os
from pathlib import Path

import duckdb
import pandas as pd

# Resolve all project-relative paths from the repository root, regardless of
# the working directory configured in PyCharm or the shell.
ROOT = Path(__file__).resolve().parent.parent.parent
os.chdir(ROOT)

con = duckdb.connect()
con.execute("SET TimeZone='UTC'")

silver = con.execute("SELECT * FROM read_parquet('data/silver/web_events_silver.parquet')").fetchdf()
print(f"[Load] silver rows: {len(silver)}, columns: {list(silver.columns)}")
print(f"[Load] old app_id distribution:\n{silver['app_id'].value_counts()}")

# ============================================================
# Fresh Stripe product_id by sub_id (Step 0 BQ pull) -- prefer this over silver's own
# product_id for Stripe rows, since it's the live/authoritative dimension table.
# ============================================================
bq_stripe = pd.read_parquet("data/raw/bq_enrich_stripe_product_id_2026-07-09.parquet")
bq_stripe = bq_stripe.rename(columns={"sub_id": "subscription_id", "product_id": "product_id_bq"})
print(f"\n[BQ enrich] stripe product_id pull: {len(bq_stripe)} rows, "
      f"{bq_stripe.subscription_id.nunique()} distinct sub_id")

silver = silver.merge(bq_stripe, on="subscription_id", how="left")

is_stripe = silver["payment_provider"] == "stripe"
has_bq_product = silver["product_id_bq"].notna()
n_overridden = (is_stripe & has_bq_product).sum()
n_stripe_no_bq_match = (is_stripe & ~has_bq_product).sum()
print(f"[BQ enrich] Stripe rows with a BQ product_id match: {n_overridden} "
      f"(unmatched, kept silver's own product_id: {n_stripe_no_bq_match})")

silver["product_id_effective"] = silver["product_id"]
silver.loc[is_stripe & has_bq_product, "product_id_effective"] = silver.loc[is_stripe & has_bq_product, "product_id_bq"]

# ============================================================
# Classification maps -- product_id -> app_id (Step 1 spec, verified against
# reports/silver_profile.md product_id counts + raw solidgate product.name lookup)
# ============================================================
STRIPE_MAP = {
    "prod_SQNFtsL7NYSNdY": "invinci",
    "prod_UVk5DBWXTsemwf": "atelier",
}

# Verified via: SELECT product_id, product.name, amount, COUNT(*) FROM solidgate raw events.
# a9730996 = "weekly" $9.99 (Invinci's signature price) -- generic label but Invinci's
#            recurring-charge SKU (25,800 raw events, single largest Solidgate product_id).
# 5a337f23 = "Invinci Paid Trial" (EXPLICITLY named) -- 21,494 raw events, 2nd largest.
#            FLAG: a literal "dominant (singular) -> invinci" reading would misclassify
#            this one as other_old despite being explicitly Invinci-branded -- treated
#            both as invinci here; surfaced at the stop-point for confirmation, not
#            silently resolved either way.
# 86870f81 / 6bffb570 / ce467a85 = "Invisi prod - ..." -- "Invisi" is an evident
#            typo/variant spelling of "Invinci" (monthly/weekly/6mo paid-trial SKUs),
#            small volume (118/103/17 raw events) -- included as invinci.
# e5dc766f (TRIAL_UPSALE_COMBO), fbc98c19 (TRIAL_UPSALE_WEB), d26782cc (TRIAL_UPSALE_IDENTITY)
#            = upsell/bundle SKUs, no Invinci branding evidence -- other_old.
# e357b6fb = "Antivirus Test" -- explicitly a different/test product -- other_old.
SOLIDGATE_INVINCI_IDS = {
    "a9730996-e563-42d7-8857-fe8ff20c4034",
    "5a337f23-dbb3-4569-b7e5-04cd1008dadf",
    "86870f81-884b-4a62-995b-e22ee8cf286b",
    "6bffb570-3130-4081-a009-a59ec5cc207c",
    "ce467a85-8f94-4336-8ad9-98778bb24098",
}


def classify(row):
    pid = row["product_id_effective"]
    provider = row["payment_provider"]
    if pd.isna(pid) or pid is None or pid == "":
        return "unknown_old"
    if provider == "stripe":
        return STRIPE_MAP.get(pid, "other_old")
    elif provider == "solidgate":
        return "invinci" if pid in SOLIDGATE_INVINCI_IDS else "other_old"
    return "unknown_old"


silver["app_id_v1_silver"] = silver["app_id"]  # keep old classification for audit
silver["app_id"] = silver.apply(classify, axis=1)

print(f"\n[Reclassify] new app_id distribution:\n{silver['app_id'].value_counts()}")
print(f"\n[Reclassify] old (v1) vs new (v2) app_id crosstab:\n"
      f"{pd.crosstab(silver['app_id_v1_silver'], silver['app_id'])}")

# ============================================================
# Cross-tab: app_id x product_id (post-reclassification) -- for the stop-point review
# ============================================================
crosstab = (
    silver.groupby(["app_id", "payment_provider", "product_id_effective"])
    .size().reset_index(name="n")
    .sort_values(["app_id", "n"], ascending=[True, False])
)
print(f"\n[Cross-tab] app_id x provider x product_id (post-reclassification):\n"
      f"{crosstab.to_string(index=False)}")

n_subs_by_app = silver.groupby("app_id")["subscription_id"].nunique()
print(f"\n[Cross-tab] distinct subscriptions per app_id:\n{n_subs_by_app}")

# drop the helper columns before writing back, keep product_id_effective as documentation
# of what was actually used (rename to avoid confusion with the original product_id column)
silver = silver.drop(columns=["product_id_bq"])
silver = silver.rename(columns={"product_id_effective": "product_id_used_for_app_id_v2"})

silver.to_parquet("data/silver/web_events_silver.parquet", index=False)
print(f"\n[Output] wrote data/silver/web_events_silver.parquet ({len(silver)} rows, "
      f"app_id reclassified by product_id; app_id_v1_silver + product_id_used_for_app_id_v2 "
      f"columns added for audit)")
