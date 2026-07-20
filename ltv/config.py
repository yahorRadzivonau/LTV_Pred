"""
Single home for every LTV-pipeline constant.

Two rules:
  1. Model-math constants (K_SHRINK, HMAX, ...) are RE-EXPORTED from core.common,
     never redefined -- that file stays the single source of truth for the frozen
     math. Changing those is out of scope for the refactor.
  2. Web-layer constants (prices, dates, thresholds, funnels) that were copy-pasted
     across web_* scripts are defined ONCE here. Values are exactly those in the
     scripts as of the freeze date below; the refactor must not change them.

Freeze date: 2026-07-11 (see reports/reconcile_baseline.json).
"""
import pandas as pd

# --- re-exported model-math constants (defined in core/common.py, DO NOT redefine) ---
from core.common import K_SHRINK, HMAX  # noqa: F401  K_SHRINK=800, HMAX=52

# ============================================================ prices / plan
BASE_PRICE = 9.99                 # base weekly recurring price; base/ups split boundary
TARGET_WEEKLY_PRICE = 9.99        # $9.99/week cohort selector (price-inference fallback)
PRICE_TOLERANCE = 0.05            # tolerance when matching the $9.99 plan by amount
TARGET_STRIPE_PRICE_ID = "price_1RVWUuJzVYkL7XCuWyUpC9bH"  # the $9.99/week Stripe price id

# revenue rule: only these carry captured money; refunds handled separately; billing_issue excluded
PAID_EVENTS = ("trial_converted", "subscription_renewed")
REFUND_EVENT = "subscription_refunded"

# ============================================================ snapshot / cohort dates
# Canonical frozen snapshot for the cohort/model side (censoring, ages, calibration).
SNAPSHOT_TS = pd.Timestamp("2026-07-07", tz="UTC")
# The per-person revenue stage (web_person_level_revenue.py) was written against a
# slightly later pull; kept distinct to preserve its output bit-for-bit. Does NOT
# reach the deliverable (fix.py recomputes revenue from golden under SNAPSHOT_TS).
SNAPSHOT_TS_REVENUE = pd.Timestamp("2026-07-10", tz="UTC")

# reference / control cohort (validated against colleague's SQL: per-sub rebill7 = $55.16)
COHORT_START = pd.Timestamp("2026-05-04", tz="UTC")
COHORT_END_EXCLUSIVE = pd.Timestamp("2026-05-11", tz="UTC")

# person population lower bound (first_date >= this)
POP_START_DATE = pd.Timestamp("2026-04-13", tz="UTC")

# data-snapshot file dates (were inline path literals)
BQ_PERSON_DIM_PATH = "data/raw/bq_appsflyer_person_dim_2026-07-11.parquet"
BQ_PERSON_WEEK_PATH = "data/raw/bq_appsflyer_person_week_2026-07-11.parquet"

# ============================================================ funnel whitelist
WHITELIST_FUNNELS = frozenset({
    "device-security-check-gate",
    "device-security-gate",
    "detect-security-warnigs-gate-now",  # (sic) misspelled slug as it appears in data
    "device-security",
    "device-security-check",
})
# a funnel value counts as "organic" (kept) when it is one of these sentinels
ORGANIC_SENTINELS = frozenset({None, "", "unknown", "none"})

# ============================================================ web calibration thresholds
# (copy-pasted verbatim across web_boss_charts_ab / web_hbase_smooth_correction /
#  web_person_level_tables{,_v2}; all copies agreed on these values)
MIN_FIRST_PAYERS = 15          # min first-payers for a cohort to enter the pool
MIN_MATURE_REBILL = 3          # min mature rebill for a cohort to be usable
RELIABILITY_N_THRESHOLD = 40   # N_at_risk floor for a week to be "reliable" (hazard fit)
TAPER_WIDTH = 3                # weeks over which the web correction fades to pure iOS
LOO_BAND_PCT = 0.0843          # leave-one-cohort-out cross-cohort spread (from validate_loo)

# ============================================================ report tables (v2 deliverable)
H_EXT = 104                    # extended horizon (52 -> 104 flat-tail extrapolation)
LOW_N_CELL_THRESHOLD = 40      # a cell with fewer payers than this is flagged low_n
HORIZONS_REPORT = (4, 12, 26, 52, 104)  # per-person report horizons
