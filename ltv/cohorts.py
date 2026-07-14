"""
Population construction + denominators for the web LTV pipeline.

This module captures logic that previously existed ONLY as ad-hoc shell commands
(the loose data/raw/_tmp_*.parquet inputs had no committed builder):
  - build_cus_email_map()   : cus_id -> email, from Stripe subs + Solidgate event JSON
  - build_population_5406()  : the 5,406-person population (whitelist funnels + organic,
                               first_date >= POP_START_DATE) from the appsflyer person dim
  - the attributed / payer denominators used by the deliverable tables

Filters (invinci / QA / fraud) are applied UPSTREAM in the golden build
(build_web_silver.py + the golden filter); filter_golden_sub_shape() re-states the
Stripe-only $9.99 selection used by the calibration shape so it is documented in one place.

All builders are verified to reproduce the existing loose files (see tests/ and
reconcile.py); nothing here changes a deliverable number.
"""
import json

import pandas as pd

from ltv.config import (
    ORGANIC_SENTINELS,
    POP_START_DATE,
    TARGET_STRIPE_PRICE_ID,
    WHITELIST_FUNNELS,
)


# ============================================================ cus_id <-> email map
def build_cus_email_map(
    stripe_subscriptions_path: str = "data/raw/stripe_subscriptions.parquet",
    solidgate_events_path: str = "data/raw/solidgate_events.parquet",
) -> pd.DataFrame:
    """One row per cus_id -> lowercased email, Stripe first then Solidgate.

    Stripe: (customer_id, email) from the subscriptions table.
    Solidgate: (customer.customer_account_id, customer.customer_email) parsed from the
    raw event JSON. On duplicate cus_id the first (Stripe) wins -- same as the ad-hoc build.
    """
    stripe = pd.read_parquet(stripe_subscriptions_path)
    stripe_map = (
        stripe[["customer_id", "email"]].dropna().drop_duplicates()
        .rename(columns={"customer_id": "cus_id"})
    )
    stripe_map["email"] = stripe_map["email"].str.lower()

    se = pd.read_parquet(solidgate_events_path)
    recs = []
    for d in se["data"]:
        try:
            j = json.loads(d)
        except Exception:
            continue
        c = j.get("customer") or {}
        cid = c.get("customer_account_id")
        em = c.get("customer_email")
        if cid and em:
            recs.append((cid, em.lower()))
    solidgate_map = pd.DataFrame(recs, columns=["cus_id", "email"]).drop_duplicates()

    return pd.concat([stripe_map, solidgate_map], ignore_index=True).drop_duplicates(subset="cus_id", keep="first")


# ============================================================ 5,406-person population
def _is_organic(funnel) -> bool:
    return (funnel is None) or (str(funnel).strip().lower() in ORGANIC_SENTINELS)


def build_population_5406(person_dim_path: str = "data/raw/bq_appsflyer_person_dim_2026-07-11.parquet") -> pd.DataFrame:
    """The population feeding the deliverable tables: people whose first qualifying
    event is >= POP_START_DATE and whose first funnel is either a whitelist funnel or
    an organic sentinel. Reproduces data/raw/_tmp_person_pop_5406.parquet."""
    dim = pd.read_parquet(person_dim_path)
    dim["first_date"] = pd.to_datetime(dim["first_date"])
    scoped = dim[dim["first_date"] >= POP_START_DATE.tz_localize(None)].copy()
    keep = scoped["first_funnel"].isin(WHITELIST_FUNNELS) | scoped["first_funnel"].apply(_is_organic)
    return scoped[keep]


# ============================================================ denominators
def add_is_payer(pop: pd.DataFrame, total_col: str = "total") -> pd.DataFrame:
    """A person is a payer iff net revenue to date > 0 (the deliverable definition)."""
    out = pop.copy()
    out["is_payer"] = out[total_col] > 0
    return out


def denominators(cell: pd.DataFrame) -> dict:
    """The two denominators for a cohort cell: n_attributed = everyone acquired,
    n_payers = people with >=1 real captured payment."""
    return {
        "n_attributed": len(cell),
        "n_payers": int(cell["is_payer"].sum()) if "is_payer" in cell else 0,
    }


# ============================================================ calibration shape cohort filter
def filter_golden_sub_shape(golden: pd.DataFrame) -> pd.DataFrame:
    """The Stripe-only, $9.99-price-id subscription universe used to build the per-sub
    survival/growth SHAPE (distinct from the all-provider person-revenue universe).
    Documented here; the canonical implementation stays in
    compare_map_to_local_sql_style_may_04_10.filter_provider_and_app for the calibration path."""
    g = golden.copy()
    prov = g["payment_provider"].astype("string").str.lower()
    g = g[prov.eq("stripe")]
    app = g["app_id"].astype("string").str.lower()
    g = g[app.eq("invinci")]
    return g[g["price_id"].eq(TARGET_STRIPE_PRICE_ID)]
