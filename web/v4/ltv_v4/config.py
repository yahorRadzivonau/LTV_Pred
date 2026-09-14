"""
Config for the per-person web pipeline (ltv_v4). Self-contained: nothing here
is imported from ltv/config.py or ltv_v2/config.py, so a change here cannot
silently move the golden pipeline's reconcile.py gate or the frozen v2 tables.

Values carried over from ltv_v2/config.py keep their original provenance
comment; values that are NEW in v3 are marked "v3:" with the measurement that
produced them.
"""
import pandas as pd

# ============================================================ source
BQ_PROJECT = "appsflyer-data-411716"
BQ_DATASET = "silver_layer"
BQ_TABLE = "web_conversions"
SOURCE = f"{BQ_PROJECT}.{BQ_DATASET}.{BQ_TABLE}"

APP_NAMES = ("Invinci", "Unknown")

# customer_user_id is NOT usable as an identity key: Solidgate reissues
# customer_account_id on resubscribe/retry, so one person shows up under 2+ ids
# across systems (confirmed on a live example: one email, 3 distinct cus_ids).
JOIN_KEY = "email"

# ============================================================ event types
BASE_EVENT_TYPE = "subscription_started"
UPS_EVENT_TYPE = "upsale_converted"
TRIAL_EVENT_TYPE = "trial_started"

# v3 NEW: state-signal events. They carry NO money (billing_issue is an
# attempt/outcome pair, mostly $0 halves plus duplicated bursts, and dominates
# gross if naively summed; sub_cancelled's with-amount rows are 81.6% echoes of
# a billing_issue at the same customer+amount+timestamp) -- ltv_v4.revenue never
# counts them. They are pulled purely to LABEL outcomes, the way
# ios/pipeline/se_training.py uses its DUNNING/VOLUNTARY sets:
#   - without a dunning signal, "recovered" collapses to the days_to_next > 10
#     proxy alone, and the return-window fix below has nothing to key on
#   - they are also the raw material for the grey-state/Markov work deferred to
#     the next iteration (docs/WEB_V3_SPEC.md)
DUNNING_EVENT_TYPES = ("billing_issue",)
VOLUNTARY_EVENT_TYPES = ("sub_cancelled",)
STATE_EVENT_TYPES = DUNNING_EVENT_TYPES + VOLUNTARY_EVENT_TYPES

# Money-bearing events, in the sense of ltv_v4.revenue. Kept explicitly separate
# from STATE_EVENT_TYPES so a future edit cannot accidentally leak a $0-noise
# event into revenue.
CAPTURED_EVENT_TYPES = (BASE_EVENT_TYPE, UPS_EVENT_TYPE, TRIAL_EVENT_TYPE)

# trial_started carries three structurally different signals, separated by
# amount alone (verified in ltv_v2 against raw Stripe/Solidgate, not assumed):
#   amt = 0.99  -> REAL captured charge (Solidgate payment_action="auth_settle",
#                  invoice.amount=99; Stripe invoice.paid, amount_paid=99).
#                  100% of in-scope trial rows are exactly 0.99 -- no other tier.
#   amt = 1.00  -> PLACEHOLDER, Solidgate only. auth_0_amount, invoice.amount=0:
#                  a card-validation hold, zero dollars moved.
#   amt = NULL  -> no charge recorded (Stripe free-trial path, or paypal).
#   amt = 0.00  -> v3 ADDITION 2026-09-11: 205 Invinci/stripe rows carry a
#                  literal zero. They used to fall through to "real" (0 is
#                  neither NULL nor 1.00) and cost nothing, because zero money
#                  is zero money. Under net_revenue each would contribute
#                  -$0.057 -- the flat per-transaction fee applied to a zero
#                  charge, i.e. negative revenue from a free trial -- so a zero
#                  charge is now called what it is. See revenue.classify_trial.
TRIAL_PLACEHOLDER_AMOUNT = 1.00

# v3 CHANGE vs v2: v2 pulled ONLY real ($0.99) trials, because it only needed
# revenue. v3's denominator is per-trial (owner's definition: everyone who
# started a trial of ANY kind, plus everyone who paid for the base plan), so
# placeholder and free trials must be pulled too. They carry no money -- they
# enter the population, never the revenue.
TRIAL_TYPES_IN_POPULATION = ("real", "placeholder", "free")
TRIAL_TYPES_IN_REVENUE = ("real",)

# ============================================================ prices
# base: $9.99/week recurring. Measured IN-WINDOW (population from WINDOW_START,
# product B excluded): 97.9% of base payments are exactly $9.99, mean $9.94,
# drifting only from $9.955 at step 1 to $9.811 at step 8. The remainder are
# small discounts (8.49/4.99/3.50).
# Revenue always uses the ACTUAL per-row amount; this constant is the modelled
# FORWARD rate only, and at 0.5% off the observed mean it needs no correction.
#
# Caveat on how this was nearly got wrong: measured across the whole raw pull
# (which reaches back to PULL_FLOOR_DATE for first_date accuracy) the mean looks
# like $10.57, because legacy product B's $39.99 plan is in there. Always scope
# price measurements to the in-window population.
BASE_PRICE = 9.99
BASE_CADENCE_WEEKS = 1

# ups: $11.99/month. A SEPARATE product on its own Stripe subscription_id and
# its own ~30-day cadence, NOT a $2 add-on to base (confirmed on a live example,
# cus_TcJBDCcMrmqCt1). 91.7% of upsale_converted rows are exactly 11.99.
UPS_PRICE = 11.99
UPS_CADENCE_WEEKS = 4

# v3 MEASURED: the first upsell charge does NOT land at signup. Distribution of
# each person's first upsale_converted, in weeks since their entry (2026-07-28
# pull): week 1 -> 31, week 2 -> 1311, week 3 -> 38, week 4+ -> ~34. So it fires
# ~2 weeks in, then on the monthly cadence: weeks 2, 6, 10, ...
# Crediting it at week 0 (the obvious first guess) over-credits every short
# horizon, since 0% of upsells actually happen there.
UPS_FIRST_WEEK = 2

# ============================================================ net prices (v3: 2026-09-11)
# silver_layer.web_conversions gained a net_revenue column. It is NOT a refund
# figure -- it is the acquirer's cut, and it is a DETERMINISTIC function of
# (gross, payment_provider), verified to float precision on all 99,709 in-window
# rows (max deviation 1.8e-15):
#
#     net_revenue = gross * rate - 0.057
#     rate = 0.9638 (solidgate) | 0.9588 (stripe)
#
# i.e. a percentage fee plus a flat 5.7c per transaction. Being deterministic
# per (provider, gross) is also the PROOF that it contains no refunds -- refunds
# would be lumpy and would break the one-net-value-per-price-point pattern.
# REFUND_HAIRCUT therefore STAYS and is applied on top. Dropping it "because the
# figure is already net" would overstate revenue by 0.91%.
#
# These two constants are the modelled FORWARD rate only (ltv_v4.money). The
# observed side always uses the actual per-row net_revenue. Measured from
# WINDOW_START at the provider mix of the day (84% solidgate / 16% stripe):
#     base 9.99  -> mean net  9.5635  (ratio 0.9573)  over 60,105 rows
#     ups  11.99 -> mean net 11.4866  (ratio 0.95802) over  7,247 rows
# RE-MEASURE if the provider mix moves materially: stripe's cut is 0.5pp worse,
# so a swing toward stripe drags both numbers down.
NET_BASE_PRICE = 9.5635
NET_UPS_PRICE = 11.4866

# The formula above covers solidgate and stripe. It does NOT cover paypal:
# every paypal row has net_revenue NULL, no exceptions. Measured in the pull
# window that is 76 money rows worth $759.24 gross -- 0.156% of captured gross,
# all subscription_started at $9.99, all in 2026-04 (71) and 2026-05 (5), and
# paypal has issued nothing since. Those rows currently contribute $0 revenue.
#
# Not imputed on purpose: paypal's fee is unknown, so any rate would be a guess,
# and 0.16% on the two oldest cohorts of the window is not worth a code path for
# a dead provider. What IS worth it is that the hole stays loud -- revenue.
# load_events refuses to build if more than this share of captured gross has no
# net, so a returning paypal or a new provider cannot drain revenue silently.
NET_MISSING_MAX_SHARE = 0.005

# trial: a prepayment that activates a 1-week trial period. One-time -- 95.4% of
# trial payers have exactly one such charge. Owner confirmed the money is not
# refunded, and at the end of the week the person either buys the $9.99/week
# plan or leaves. Measured conversion trial -> base: 61.8%.
#
# Measured IN-WINDOW (population from WINDOW_START, product B excluded): $0.99
# x8452, placeholder $1.00 x1887, free/NULL x2237. No other tier exists here --
# ltv_v2/config.py's "no $4.99 tier appears in-scope" is correct after all.
# (A $4.99 trial does appear in the raw pull, 865 rows, but it belongs entirely
# to legacy product B and to people who entered before the window. Measuring
# across the whole pull instead of the in-window population is what made it look
# like a live second tier.)
#
# Still, the money model uses each person's ACTUAL charge rather than this
# constant: the trial sits at week 0, so it is always already observed by
# prediction time and there is nothing to gain from modelling it.
TRIAL_PRICE = 0.99

# v3 CHANGE vs v2: v2 folded trial revenue into the ups bucket, so ups_projected
# grew a one-time $0.99 with a MONTHLY compounding ratio (disclosed there as a
# known residual approximation). In v3 the trial sits at step k=0 as a one-time
# entry payment and is never projected forward.
TRIAL_IS_ONE_TIME = True

# v3 CHANGE vs v2: ups is charged at monthly CHECKPOINTS (weeks 0, 4, 8, ...),
# not smeared as UPS_PRICE/4 into every week. Smearing credits ~$3 in weeks 1-3
# when no charge has actually occurred yet. Owner approved checkpoints.
MONTH_STEP = UPS_CADENCE_WEEKS

# ups is bound to base survival for now: in the 2026-07-27 pull, 0 of 1417
# people with both streams had their last ups payment AFTER their last base
# payment. A separate ups survival curve + a "has upsell" lever is deliberately
# deferred to the next iteration (see docs/WEB_V3_SPEC.md).
UPS_BOUND_TO_BASE_SURVIVAL = True

# ============================================================ product scope (v3 NEW)
# The event stream contains TWO products, not one. v2 called both "base" and
# modelled everything as $9.99/week. Measured on the 2026-07-28 pull:
#
#            product A            product B
#   trial    $0.99                $4.99
#   plan     $9.99                $39.99
#   cadence  6-day median gap     28-day median gap   <- MONTHLY, not weekly
#   payers   6,483                231
#   linkage  78% came via $0.99   81% came via $4.99
#   crossover 1% had a $4.99      3% had a $0.99
#
# The two separate almost cleanly on trial price. Mixing them corrupts the
# survival curve on both axes: S[k] is "reached payment k+1", and a step means
# ~7 days for A but ~28 for B, so one curve is fitted across two different time
# scales, then priced at a single rate.
#
# Owner's decision for this iteration: model product A only, exclude B, document
# it. B is 1.6% of base payers and 5.4% of revenue -- too small to fit its own
# stable curve (231 people), and folding it in adds a fifth simultaneous change
# to an iteration that already has four. Give B its own curve when it grows.
#
# IT ALREADY STOPPED SELLING. Entries into product B by month: 2025-10 x214,
# 2025-12 x254, 2026-01 x137, 2026-02 x163, 2026-03 x65, 2026-04 x15, then
# nothing -- exactly 2 people entered after WINDOW_START. The $39.99 charges
# still landing in May-July are renewals from earlier signups.
# So this exclusion removes 873 people from the raw pull but only 2 from the
# population: the window boundary was already keeping B out. The guard stays
# anyway, because it protects the payment ladder (built over the full pull) and
# it fails loudly if B is ever relaunched.
#
# Membership is decided by PRICE, never by cadence. Cadence is derived from the
# gaps between a person's own payments, so it is unknowable for the 42.6% who
# made exactly one payment, and for everyone else it leaks the outcome -- the
# same trap that got plan_interval banned as a lever (see core/map_model.py).
EXCLUDE_PRODUCT_B = True
PRODUCT_B_TRIAL_AMOUNT = 4.99
PRODUCT_B_BASE_MIN_AMOUNT = 20.00   # $39.99 plan and anything above it; the $9.99
                                    # plan's discounts all land far below this

# ============================================================ refund haircut (TEMPORARY, carried from v2)
# Original provenance: golden nets refunds explicitly, appsflyer could not
# represent them, so appsflyer gross was structurally biased high by the refund
# rate. Measured on golden, symmetric window 2026-04-13..2026-07-07, aligned
# population:
#   golden refund_capped $1,000.57 / golden gross $109,589.75 = 0.00913
#
# CORRECTION 2026-09-11: the claim that used to stand here -- "appsflyer has NO
# refund event_type at all (0 negative amounts in the whole table -- verified)"
# -- is no longer true. web_conversions now carries refund_issued rows with
# negative transaction_amount_usd (824 rows for Invinci/Unknown, and growing:
# 3 in May, 18 in June, 144 in July, 484 in August). Measured against captured
# gross over the window: 7,154.01 / 718,126.40 = 0.00996, i.e. within 0.8pp of
# the constant below. The v2 estimate holds up, so it stays for now.
#
# It should still be replaced, for a reason accuracy alone does not reveal: a
# flat haircut shaves 0.9% off EVERY person, whereas a refund actually zeroes
# ONE person's revenue, and those two distribute differently across cohorts and
# funnels. Two notes for whoever does it:
#   - refund_issued rows have net_revenue IS NULL, so refund work must key on
#     transaction_amount_usd (gross), never on amt_net
#   - refund_issued is in neither CAPTURED_EVENT_TYPES nor STATE_EVENT_TYPES,
#     so pull_v4_events.py does not fetch those rows at all today
REFUND_HAIRCUT = 0.00913
REFUND_HAIRCUT_TODO = "TEMPORARY: replace the flat rate with per-person refund_issued events (present upstream since ~2026-05)."

# ============================================================ window
# Training/reporting window. Owner's decision, not negotiable: before this date
# the web data is inconsistent (sparse geo, malformed payments), and including
# it would corrupt the fit.
WINDOW_START = pd.Timestamp("2026-04-13", tz="UTC")

# v3: the PULL floor is deliberately EARLIER than WINDOW_START. A person whose
# trial started 2026-04-10 and whose first base payment landed 2026-04-20 would
# get a wrong first_date (and therefore a wrong cohort) if the pull itself were
# cut at WINDOW_START. So we pull from further back and apply WINDOW_START
# downstream, when building the population and the training matrix.
# 2025-10-01 = earliest first payment present in the golden web matrix; anything
# before that is empty for this product.
PULL_FLOOR_DATE = pd.Timestamp("2025-10-01", tz="UTC")

# v3 TEMPORARY CEILING, added 2026-09-11. trial_started stopped arriving in
# silver_layer.web_conversions on 2026-09-02: 94 rows in all of September
# against 8,291 in August, while subscription_started and billing_issue keep
# flowing normally. Verified upstream -- web-payment-orchestration.
# prod_web_events.stripe_events_parsed still receives trial_started at the usual
# 275-537/day -- so the break is in the scheduled query that builds
# web_conversions (that query lives in data_repo, not in this repo).
#
# Pulling past the break would build September cohorts on a near-empty
# population and silently wreck n_trial / conversion / ltv_per_trial_*. This
# ceiling moves snapshot_ts back to 2026-09-01 instead, so the model is simply
# "as of 1 September": coherent, just 10 days less fresh.
#
# SET BACK TO None the moment trial_started is fixed upstream.
PULL_CEILING_DATE = pd.Timestamp("2026-09-01", tz="UTC")

# ============================================================ survival / censoring
HMAX = 52          # forecast horizon in payment steps, same as core.common.HMAX
H_EXT = 104        # reporting horizon (weeks 53..104 are extrapolated)

# v3 NEW FIX, and the constant that took the most measuring to settle.
#
# THE BUG: ios/pipeline/se_training.py labels an outcome with a 60-day return
# window (a payment inside it = "recovered", i.e. ALIVE) but censors with
# weeks_obs >= step_k, which only guarantees ONE week has passed since that
# step. Someone who pays again on day 20 is recorded dead if the snapshot
# catches them on day 8. Measured: 9.4% of web clean-zone rows are marked dead
# with the window still open (iOS 2.4% -- web is 4x worse because it is young,
# so far more of its payments are recent).
#
# THE WRONG FIX (tried, measured, discarded): "keep a row if its outcome is
# known -- either a next payment already exists, or the window elapsed". It
# looks reasonable and it is catastrophically biased. A SURVIVOR is confirmed
# within ~7 days (their next payment lands); a DEATH takes the full window to
# confirm. So the rule keeps nearly every survivor and drops most deaths --
# censoring that depends on the outcome. Measured on the 2026-07-28 build it
# removed 84% of deaths: observed death rate in the clean zone fell from 0.182
# to 0.030 and h_base went to ZERO for steps 2-8, i.e. "nobody ever churns
# after their first payment".
#
# THE FIX: censor strictly by TIME. A step is evaluable only once it is at
# least this old, regardless of what happened -- survivors and deaths wait the
# same amount, so the risk set stays unbiased.
#
# WHY 21 AND NOT THE iOS 60: 60 days is calibrated for mobile monthly/yearly
# plans. This product bills $9.99 WEEKLY, and a 60-day gap is not a recovery,
# it is a new subscription after 8 missed payments. Measured web continuation
# gaps: 99.5% of ALL next payments land within 21 days (median 9, p95 20,
# p99 36). Sweep of the censoring window against data retained:
#     W=7   13289 rows  obs 0.170   cohorts@step1/3/5/8 = 14/12/10/7
#     W=14  10328 rows  obs 0.162                        13/11/ 9/6
#     W=21   8127 rows  obs 0.161                        12/10/ 8/5   <- chosen
#     W=28   6260 rows  obs 0.155                        11/ 9/ 7/2
#     W=60   1807 rows  obs 0.177                         6/ 4/ 2/0   (starved)
# The observed death rate plateaus at ~0.16 from W=14 onward -- that plateau is
# the bias being gone. 21 sits on it while keeping 8k rows and >=3 voting
# cohorts through step 8.
#
# Used for BOTH labelling and censoring, deliberately: labelling with one window
# and waiting a different one is internally inconsistent.
RETURN_WINDOW_DAYS = 21

# Expect this fix to RAISE predicted survival: h_base drops at the later steps
# (e.g. step 7: 0.107 unfiltered -> 0.059 at W=21), which is exactly the false
# deaths leaving. The calendar backtest already shows the model over-predicting
# by +16%; those false deaths were partially masking it. Removing a
# compensating error is not a regression, but the over-prediction will look
# worse before it looks better.

# ============================================================ levers (v3 NEW)
# Order IS the residual-chain order and must not be reshuffled casually: each
# lever is fitted on the residual left by the previous ones (mult = obs/expected
# where expected already carries them), which is what stops correlated levers
# from double-counting the same effect.
# funnel first: it is the offer -- what the person was actually sold -- and sits
# closest to the mechanism of whether they keep paying. utm_source is the buyer,
# which acts largely THROUGH the funnel they run, so it gets the honest residual
# ("buyer quality beyond their funnel"). Cheap to test both orders; see
# docs/WEB_V3_SPEC.md stage 5.
MAP_LEVERS_WEB = ["utm_source", "geo", "billday_bin"]

# funnel was built, measured, and left OUT of the default. Full study:
# reports/web_v4/lever_study_web.md, reproduced by web/v4/measure_levers_web.py.
#
# What the levers as a GROUP buy (paired over 101 LOO folds, no-levers vs all):
#   LTV       levers better in 70% of folds, Wilcoxon p < 0.001   -> keep them
#   survival  no difference, p = 0.87                             -> noise
#
# What funnel SPECIFICALLY buys, on top of utm+geo+billday:
#   survival  worse in 54% of folds, median delta +0.013, p = 0.30
#   LTV       worse in 54% of folds, median delta +0.002, p = 0.43
# Neither is significant, and both point estimates lean negative. Reversing the
# chain to utm -> funnel is no better (p = 0.17 / 0.14, also slightly worse).
#
# The lever is not inert -- its multipliers spread 0.88..1.29 (device-security
# 1.160 vs device-security-check-gate 0.882), so it makes confident bets. They
# simply do not survive leave-one-cohort-out: with 5 surviving categories over
# ~8k rows the map fits cohort-specific noise. Exactly what iOS's own LOO found
# for the map levers (reports/ios/loo_map.md: "отрыв map НЕ подтверждён при
# равных условиях").
#
# Ties go to the simpler model, so it is off for THIS iteration. Set
# MAP_LEVERS_WEB to include "funnel" to turn it back on -- nothing else needs
# changing, and the tables still BREAK DOWN by funnel regardless. Being a
# reporting dimension and being a model lever are different questions.
MAP_LEVERS_WEB_WITH_FUNNEL = ["funnel", "utm_source", "geo", "billday_bin"]

# OWNER'S POSITION, recorded so the next phase does not restart this argument:
# revisit funnel as a lever next phase rather than drop it. The objection raised
# above -- that the surviving categories are A/B variants of one funnel while
# genuinely different funnels get collapsed into "other" -- is not actually a
# problem, because device-security IS the business. Checked: the
# device-security* and detect-security* families together are 85% of the
# population (6,414 + 3,026 of 11,051), against ~950 for the entertainment
# funnels. So the lever spends its resolution where the money is, and the
# structure is a foundation for when the funnel mix diversifies.
# What the measurement says stands: it does not pay off YET (p = 0.30 / 0.43).
#
# Worth carrying into that work: funnel's real signal sits in CONVERSION, not in
# post-conversion hazard, and that is a different model. Conversion to the base
# plan by funnel ranges 20.2% (use-to-watch) to 72.5% (product) -- a 3.6x
# spread, against 1.5x for the hazard multipliers this lever fits. Conversion is
# currently taken empirically per cell, which is right when a cell is big and
# noisy when it is not; shrinking a cell's conversion toward its funnel's mean
# is probably worth more than any hazard lever.

# v3 CHANGE vs iOS/v2 (2000): the web matrix is ~13k rows total, so a 2000-row
# floor would collapse everything but 2-3 funnels into "other". Measured funnel
# support among payers: 21 distinct values, >=100 payers for 3, >=50 for 8,
# >=20 for 13; top-5 hold 76.6%. At 250 we expect 5-8 live categories + "other".
MAP_MIN_ROWS_WEB = 250

MAP_CLIP = (0.4, 2.5)      # clip on each lever multiplier AND on their product
CLEAN_STEP_MAX = 8         # clean-zone boundary for fitting multipliers
SANITY_TOL = (0.9, 1.1)    # allowed weighted mean of a lever's multipliers

# ============================================================ cell thresholds (carried from ltv/config.py values used by v2)
LOW_N_CELL_THRESHOLD = 40  # n_payers below this -> cell flagged low_n

# Cohorts younger than this are dropped from the reported tables entirely (not
# flagged low_n -- out of the sample). v2 used 3 weeks on the grounds that
# upsells and rebills have not had a chance to fire for a 0-1 week old cohort.
#
# v3 sets 1, owner's decision. Two things changed that make it defensible where
# it was not in v2:
#   - the forecast is ANCHORED on observed revenue and only projects forward, so
#     a young cohort contributes its real money plus a short projection, instead
#     of v2's "multiply this cell's fact by 5.33"
#   - hr is K_SHRINK-shrunk, so a cohort with almost no history sits at hr ~= 1
#     and rides the portfolio curve rather than inventing its own
# Age 0 is still excluded: those cohorts have essentially no payment history at
# all, so there is nothing to anchor on. On the 2026-07-28 build that removes
# 2,572 people across 39 cells of table C.
MIN_COHORT_AGE_WEEKS = 1

# ============================================================ session grain (v4 NEW)
# THE v4 CHANGE, and the only switch that decides what a "row" means everywhere
# downstream.
#
#   "person"        session_id = email                     -- identical to v3
#   "subscription"  session_id = email|customer_user_id    -- v4
#
# WHY customer_user_id. This config rejects it as an IDENTITY key, correctly:
# Solidgate reissues it on resubscribe, so one person appears under several.
# That is exactly what makes it the right key for WHICH SUBSCRIPTION a payment
# belongs to. email = person, customer_user_id = subscription -- not a broken
# key, a key at a different grain.
#
# WHAT IT FIXES. 996 people (8.3% of base payers) run two subscriptions at
# overlapping times, median overlap 13 days. Keyed by email their payments
# interleave into one chain: gaps between consecutive base payments are 39.8%
# under 3 days, median 4.0d. Keyed by (email, customer_user_id) the same data
# is a textbook weekly subscription -- 1.7% under 3 days, p25=median=p75=7.0d.
# Full measurement: reports/web_v4/session_rule_findings.md.
#
# Consequences of the email grain that this removes, all measured:
#   - base_payment_ladder's 3-day dedup deletes 4,672 real payments ($46,190,
#     9.0% of base revenue, 1,590 people) from the survival ladder while
#     per_person_revenue still counts them -- anchor and curve describe
#     different realities
#   - step_k counts across two chains at once
#   - days_to_next measures the gap to the OTHER subscription's payment, so a
#     dying subscription reads as alive
#   - a billing_issue on one subscription mislabels the other's step
#
# NO MERGING of keys. 296 people have a key split without a second trial; 32%
# of those pairs OVERLAP in time and the sequential ones sit at a median 37-day
# gap, with no cluster near zero to put a threshold on. Every key is its own
# session.
#
# The switch exists so "person" reproduces v3 bit-for-bit. Change it only after
# that no-op is demonstrated on numbers, never as a convenience.
SESSION_KEY = "subscription"
SESSION_COL = "session_id"

# ============================================================ paths
DATA_DIR = "data/web_v4"
OUT_DIR = "reports/web_v4"

# Bumped by each pull; the build scripts read the newest file matching the
# pattern unless an explicit path is passed.
EVENTS_GLOB = "events_*.parquet"
# Derived locally from the event pull by ltv_v4.population -- unlike v2 there is
# no separate population query, so the two can never describe different snapshots.
POPULATION_GLOB = "population_*.parquet"
MATRIX_GLOB = "web_se_training_*.parquet"
