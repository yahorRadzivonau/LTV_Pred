"""
Config for the appsflyer-source pipeline (ltv_v2). Fully separate from
ltv/config.py -- no shared constants, so a change here can never silently
affect the golden pipeline's reconcile.py gate.

Freeze date: 2026-07-20 (refresh of the 2026-07-13 freeze -- same window
start, same rules, later cutoff). Rule change 2026-07-13: real paid-trial
charges (trial_started, amount != placeholder) are now included -- see
"trial revenue rule" below. Prior freezes kept on disk for rollback/diff:
appsflyer_captured_events_2026-07-13.parquet (no trial, oldest) and
appsflyer_captured_events_with_trial_2026-07-13.parquet (previous freeze).
"""
import pandas as pd

# ============================================================ source
BQ_PROJECT = "appsflyer-data-411716"
BQ_DATASET = "silver_layer"
BQ_TABLE = "web_conversions"
SOURCE = f"{BQ_PROJECT}.{BQ_DATASET}.{BQ_TABLE}"

# Frozen local pull of the captured-revenue event types (see revenue.py),
# event_date>=WINDOW_START, app_name IN APP_NAMES. Verified count 23,402 =
# 15,760 subscription_started + 1,872 upsale_converted + 5,770 trial_started
# (real trial charges only, amount=0.99 -- see "trial revenue rule" below;
# 100% of pulled trial rows verified amt==0.99, zero placeholders/nulls).
# Pulled 2026-07-20 in 10 date-range chunks, each <3000 rows, each chunk's
# row count cross-checked against an independent unchunked COUNT(*) query
# (grand total 23,402 matched exactly, per event_type breakdown too).
RAW_EVENTS_PATH = "data/raw/appsflyer_captured_events_with_trial_2026-07-20.parquet"

APP_NAMES = ("Invinci", "Unknown")

# ============================================================ join key
# customer_user_id is NOT usable as a join/identity key: Solidgate reissues
# customer_account_id on resubscribe/retry, so the same person can show up
# under 2+ different ids across golden and appsflyer (confirmed on a live
# example: one email mapped to 3 distinct cus_ids across two systems).
# LOWER(email) is the only reliable identity key for this pipeline.
JOIN_KEY = "email"

# ============================================================ captured-revenue rule
# Confirmed by direct inspection (10-person spot check + bulk event_type
# breakdown): only these two event_types represent real captured money.
# - billing_issue: attempt/outcome pair, mostly $0 half + duplicated bursts,
#   dominates gross $ if naively summed (>$400k) -- pure noise for revenue.
# - sub_cancelled: 81.6% of its with-amount rows echo a billing_issue at the
#   same customer+amount+timestamp -- terminal-attempt echo, not revenue.
# - upsale_created: 100% flat $1.00 placeholder (offer-shown flag, not a price).
# - trial_cancelled: on-amount rows (23@0.99, 50@1.00 in-window) are, on every
#   example checked, a same-transaction echo of an already-counted
#   trial_started row seconds/minutes later (same email, same amount) -- never
#   counted, to avoid double-counting the one real trial charge per attempt.
CAPTURED_EVENT_TYPES = ("subscription_started", "upsale_converted", "trial_started")
EXCLUDED_EVENT_TYPES = ("billing_issue", "sub_cancelled", "upsale_created", "trial_cancelled")

# base = subscription_started ($9.99/week recurring plan)
# ups = upsale_converted ($11.99/month plan) -- a SEPARATE product/cadence,
# NOT a $2 add-on top of base. Confirmed on a live example (cus_TcJBDCcMrmqCt1):
# upsale_converted fires on its own ~30-day cadence, independent of the
# weekly subscription_started cadence, on a DIFFERENT underlying Stripe
# subscription_id. Do not decompose into 9.99+2 -- keep as two whole streams.
BASE_EVENT_TYPE = "subscription_started"
UPS_EVENT_TYPE = "upsale_converted"

# ============================================================ trial revenue rule (added 2026-07-13)
# trial_started carries TWO structurally different signals, cleanly separated
# by amount alone (verified against raw provider data, not just appsflyer):
#   - amount = $0.99 (Stripe OR Solidgate): a REAL captured charge. Solidgate
#     raw event confirms payment_action="auth_settle", invoice.amount=99 (real
#     cents moved). Stripe raw confirms invoice.paid, amount_paid=99,
#     status="paid". Checked 14/14 Solidgate examples + 3/3 Stripe examples --
#     100% consistent, zero exceptions. Within this pipeline's scope
#     (app_name IN APP_NAMES, event_date>=WINDOW_START) every real trial charge
#     found was exactly $0.99 -- no $4.99 tier appears in-scope.
#   - amount = $1.00 (Solidgate only; Stripe never produces this value): a
#     PLACEHOLDER. Confirmed on the FULL local solidgate_events table (7,848
#     create/active rows with product.trial=true and trial_amount=100): 100%
#     of them have payment_action="auth_0_amount" and invoice.amount=0 -- a
#     card-validation hold, zero dollars actually taken. Never auth_settle.
#   - amount = NULL: no charge recorded at all (Stripe free-trial path, or
#     paypal where no amount is ever tracked). Not revenue.
# Distinguisher: the raw dollar amount itself is sufficient and exact for this
# dataset -- no need to join to raw provider data at runtime. Real = amount
# NOT NULL AND amount != 1.00. (Placeholder is always exactly $1.00 here; if a
# future pull ever shows a different placeholder value, re-verify against
# payment_action/invoice.amount before trusting amount alone again.)
# Where the money goes: per explicit instruction, real trial revenue is folded
# into the UPS bucket (not base) -- it is a one-time, non-weekly amount like
# upsale_converted, so keeping it out of `base` preserves base's clean
# weekly-only cadence (the one the growth-shape ratio assumes). Each person's
# per_person_revenue() row gets `is_trial=True` if they have >=1 real trial
# charge, for auditability.
# Caveat (disclosed, not fixed here): ups_projected's monthly-cadence ratio
# (see build_tables.py) now applies to trial dollars too, since they're pooled
# into the same ups_gross bucket -- a one-time $0.99 will inherit the SAME
# monthly growth multiplier as a recurring $11.99 upsell. This is a much
# smaller version of the original ups-cadence bug (trial $ is a small, capped,
# one-time sliver, not a recurring stream) but is a known residual
# approximation, not a full fix.
TRIAL_EVENT_TYPE = "trial_started"
TRIAL_PLACEHOLDER_AMOUNT = 1.00
TRIAL_RULE_DATED = "2026-07-13"

# ============================================================ refund haircut (TEMPORARY)
# appsflyer has NO refund/return event_type at all (0 negative amounts, 0
# refund-labeled events in the whole table -- verified). Golden nets out
# refunds explicitly; appsflyer cannot represent them, so its gross total
# is structurally biased high by the refund rate. Measured on the golden
# side, symmetric window 2026-04-13..2026-07-07, aligned population:
#   golden refund_capped = $1,000.57 on golden gross = $109,589.75
#   => REFUND_HAIRCUT = 1000.57 / 109589.75 = 0.00913
# THIS IS A STOPGAP, not a real refund signal. It assumes the refund RATE
# measured on golden's population/window generalizes to appsflyer's. Replace
# with a real refund event the moment one exists upstream (ask the colleague).
REFUND_HAIRCUT = 0.00913
REFUND_HAIRCUT_DATED = "2026-07-12"
REFUND_HAIRCUT_SOURCE = "golden refund_capped $1,000.57 / golden gross $109,589.75, symmetric window 04-13..07-07"
REFUND_HAIRCUT_TODO = "TEMPORARY: replace with a real refund/return event from appsflyer once the colleague adds one."

# ============================================================ ups monthly-cadence sampling
# upsale_converted fires on a ~30-day cadence, not weekly like base ($9.99/week) --
# projecting it forward with the SAME weekly-compounding ratio used for base
# overstates growth (the "+21% weekly-stretch bug"; see
# web_appsflyer_v2/build_tables.py section 1b for the full derivation and the
# monthly_ratio-vs-weekly_ratio sanity print). Fix: sample the same retention
# curve at monthly checkpoints (week 4, 8, 12, ...) instead of weekly. Shared by
# build_tables.py and build_triple_report_fixed.py (Phase B consolidation,
# previously copy-pasted in both).
MONTH_STEP = 4

# ============================================================ output directory
# Shared by build_tables.py, build_triple_report_fixed.py, and build_xlsx_report.py
# (Phase C consolidation, previously copy-pasted as a literal in both build scripts).
OUT_DIR = "reports/web_appsflyer_v2"

# ============================================================ maturity filter (2026-07-20 rule change)
# Cohorts younger than this are dropped ENTIRELY from the triple-report sample
# (not flagged low_n, OUT of the sample) -- upsell/rebills haven't had a chance
# to fire yet for a 0-1 week old cohort, so any LTV number for them is noise,
# not signal. Used by build_triple_report_fixed.py.
MIN_COHORT_AGE_WEEKS = 2

# ============================================================ snapshot date (run date)
# The date this specific pull/rebuild was frozen at -- update together with
# RAW_EVENTS_PATH every time a fresh appsflyer pull replaces it. Single source
# for what used to be two independent constants that had to be bumped by hand
# in lockstep: build_tables.py's SNAPSHOT_NOW (age-reference date for
# population age_weeks_now) and build_triple_report_fixed.py's SNAPSHOT_DATE
# (informational snapshot_date column written into table C).
SNAPSHOT_DATE = "2026-07-20"

# ============================================================ window
WINDOW_START = pd.Timestamp("2026-04-13", tz="UTC")
# Golden's frozen snapshot boundary -- comparisons against golden MUST cap at
# this same upper bound (symmetric window), or appsflyer (live) always looks
# artificially higher just from having more recent days of data.
GOLDEN_SNAPSHOT_TS = pd.Timestamp("2026-07-07", tz="UTC")
