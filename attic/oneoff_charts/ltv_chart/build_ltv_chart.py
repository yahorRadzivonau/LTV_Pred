"""
Builds reports/ltv_chart/ltv_chart.png and ltv_numbers.md from the frozen raw
snapshot (data/raw/*.parquet). Read-only on data/raw/*. Does not touch
data/silver/ or any other reports/silver_* outputs (separate parallel task
owns those).

Run: .venv/Scripts/python.exe reports/ltv_chart/build_ltv_chart.py
"""
import json
import duckdb
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

pd.set_option("display.width", 200)

SNAPSHOT_TS = pd.Timestamp("2026-07-07 00:00:00")
OUT_DIR = "reports/ltv_chart"
WEEK = pd.Timedelta(days=7)
ELIGIBLE_MIN_N = 100  # minimum cohort/overall size to trust a week's average
MIN_COHORT_N = 20      # cohorts smaller than this are excluded from lines/calibration entirely

# Prior-run figures (build 1), kept only to log a before/after delta per owner request.
PREV_ANCHOR_WEEK = 37
PREV_ANCHOR_VALUE = 59.21
PREV_WK52 = 64.38
PREV_BAND = 0.533

con = duckdb.connect()
con.execute("SET TimeZone='UTC'")

log_lines = []
def log(msg):
    print(msg)
    log_lines.append(msg)

# ============================================================
# 1. Stripe QA / test subscription ids  (spec Section 9 QA rule)
# ============================================================
stripe_subs = con.execute("""
    SELECT sub_id, interval_unit, price_amount, utm_source, status
    FROM read_parquet('data/raw/stripe_subscriptions.parquet')
""").fetchdf()

qa_stripe_ids = set(
    stripe_subs.loc[
        (stripe_subs.interval_unit == "day") | (stripe_subs.utm_source == "test"),
        "sub_id",
    ]
)
log(f"[QA] Stripe interval_unit='day' subs: {(stripe_subs.interval_unit=='day').sum()}")
log(f"[QA] Stripe utm_source='test' subs: {(stripe_subs.utm_source=='test').sum()}")
log(f"[QA] Stripe QA-excluded sub_id count (union): {len(qa_stripe_ids)}")

# ============================================================
# 2. Stripe subscription start dates (week 0)
#    customer.subscription.created -> $.data.object.id, event created ts (UTC)
# ============================================================
stripe_starts = con.execute("""
    SELECT json_extract_string(data,'$.data.object.id') AS sub_id,
           MIN(created AT TIME ZONE 'UTC') AS start_ts
    FROM read_parquet('data/raw/stripe_events.parquet')
    WHERE event_type = 'customer.subscription.created'
    GROUP BY 1
""").fetchdf()
stripe_starts["start_ts"] = pd.to_datetime(stripe_starts["start_ts"])
stripe_starts["provider"] = "stripe"
log(f"[Stripe] distinct subscriptions with a start ts: {len(stripe_starts)}")

# ============================================================
# 3. Stripe revenue: invoice.paid, deduped by invoice id
#    amount_paid cents/100; sub id via parent.subscription_details.subscription
# ============================================================
inv_raw = con.execute("""
    SELECT json_extract_string(data,'$.data.object.id') AS invoice_id,
           json_extract_string(data,'$.data.object.parent.subscription_details.subscription') AS sub_id,
           TRY_CAST(json_extract(data,'$.data.object.amount_paid') AS BIGINT) AS amount_cents,
           created AT TIME ZONE 'UTC' AS created_utc
    FROM read_parquet('data/raw/stripe_events.parquet')
    WHERE event_type = 'invoice.paid'
""").fetchdf()
log(f"[Stripe] raw invoice.paid rows: {len(inv_raw)}")

inv_dedup = (
    inv_raw.groupby("invoice_id", as_index=False)
    .agg(sub_id=("sub_id", "first"), amount_cents=("amount_cents", "first"),
         payment_ts=("created_utc", "min"))
)
inv_dedup["payment_ts"] = pd.to_datetime(inv_dedup["payment_ts"])
inv_dedup["amount"] = inv_dedup["amount_cents"] / 100.0
inv_dedup["provider"] = "stripe"
log(f"[Stripe] invoice.paid deduped by invoice_id: {len(inv_dedup)} payments, "
    f"${inv_dedup['amount'].sum():,.2f} gross (pre-QA, pre-refund)")

# Stripe refunds -- attempted attach via payment_intent, found unresolvable (see notes below)
refunds_raw = con.execute("""
    SELECT event_type,
           json_extract_string(data,'$.data.object.id') AS charge_or_refund_id,
           json_extract_string(data,'$.data.object.payment_intent') AS payment_intent,
           TRY_CAST(json_extract(data,'$.data.object.amount') AS BIGINT) AS amount_cents,
           created AT TIME ZONE 'UTC' AS created_utc
    FROM read_parquet('data/raw/stripe_events.parquet')
    WHERE event_type IN ('charge.refunded','refund.created')
""").fetchdf()
# refund.created's payment_intents are a full subset of charge.refunded's (verified: 45/45 overlap)
# -> counting both would double count the same underlying refund. Use charge.refunded only.
refunds_cr = refunds_raw[refunds_raw.event_type == "charge.refunded"].copy()
total_refund_usd = refunds_cr["amount_cents"].sum() / 100.0
n_refund_events = len(refunds_cr)
gross_stripe = inv_dedup["amount"].sum()
log(f"[Stripe] refunds (charge.refunded only, refund.created dropped as duplicate "
    f"payment_intent subset): {n_refund_events} events, ${total_refund_usd:,.2f} "
    f"= {100*total_refund_usd/gross_stripe:.2f}% of gross Stripe revenue")
# Attach check: invoice.paid never carries payment_intent (0/15088), charge.succeeded and
# payment_intent.succeeded never carry an invoice field (0/11847, 0/12981) -- confirmed via
# probe queries, matches SNAPSHOT_SUMMARY.md Section 5's "100% orphan rate" finding.
# No path in this dataset resolves a refund to a subscription or invoice. Per spec fallback:
# subtract from the global aggregate and disclose -- NOT netted into the per-subscriber curve
# below (curve is gross-of-refunds); disclosed in the chart caption + numbers doc.

# ============================================================
# 4. Solidgate: parse orders under $.invoices.*.orders.* (dynamic keys -> Python json)
#    "success" per spec doesn't literally occur; observed statuses are
#    auth_failed / processing / auth_ok / settle_ok / refunded / void_ok.
#    settle_ok = money captured (payment_action='auth_settle' completed) -- treated as the
#    "success" state. Dedup by order id, keeping the LATEST record by updated_at so that an
#    order later marked 'refunded' naturally drops out of revenue (self-netting refunds).
# ============================================================
sg_raw = con.execute("""
    SELECT data
    FROM read_parquet('data/raw/solidgate_events.parquet')
""").fetchdf()
log(f"[Solidgate] raw event rows scanned for order/started_at extraction: {len(sg_raw)}")

orders = {}
sub_start_sg = {}
for data_str in sg_raw["data"]:
    obj = json.loads(data_str)
    sub = obj.get("subscription") or {}
    sub_id = sub.get("id")
    started_at = sub.get("started_at")
    if sub_id and started_at:
        ts = pd.Timestamp(started_at)
        if sub_id not in sub_start_sg or ts < sub_start_sg[sub_id]:
            sub_start_sg[sub_id] = ts
    for inv_obj in (obj.get("invoices") or {}).values():
        for o in (inv_obj.get("orders") or {}).values():
            oid = o.get("id")
            if oid is None:
                continue
            updated_at = o.get("updated_at") or o.get("created_at") or ""
            prev = orders.get(oid)
            if prev is None or (updated_at and updated_at >= prev["updated_at"]):
                orders[oid] = {
                    "order_id": oid,
                    "sub_id": sub_id,
                    "amount_cents": o.get("amount", 0) or 0,
                    "status": o.get("status"),
                    "updated_at": updated_at,
                    "created_at": o.get("created_at"),
                }

orders_df = pd.DataFrame(orders.values())
log(f"[Solidgate] distinct order ids: {len(orders_df)}")
log(f"[Solidgate] order status distribution (post-dedup, latest status per order id):\n"
    f"{orders_df['status'].value_counts().to_string()}")

sg_pay = orders_df[(orders_df.status == "settle_ok") & (orders_df.amount_cents > 0)].copy()
sg_pay["payment_ts"] = pd.to_datetime(sg_pay["created_at"])
sg_pay["amount"] = sg_pay["amount_cents"] / 100.0
sg_pay["provider"] = "solidgate"
log(f"[Solidgate] settle_ok payments: {len(sg_pay)}, ${sg_pay['amount'].sum():,.2f} gross (pre-QA)")

sg_starts = pd.DataFrame(
    [{"sub_id": k, "start_ts": v, "provider": "solidgate"} for k, v in sub_start_sg.items()]
)
log(f"[Solidgate] distinct subscriptions with a started_at: {len(sg_starts)} "
    f"(of {orders_df['sub_id'].nunique()} distinct sub ids seen in orders)")

# QA fallback for Solidgate (no utm field on the subscription itself): spec's own
# ambiguous-case rule -- exclude subs with >45 payments.
sg_payment_counts = sg_pay.groupby("sub_id").size()
qa_solidgate_ids = set(sg_payment_counts[sg_payment_counts > 45].index)
log(f"[QA] Solidgate subs excluded via >45-payments fallback rule: {len(qa_solidgate_ids)}")

# ============================================================
# 5. Combine, apply QA exclusion, join payments to start dates
# ============================================================
subs_all = pd.concat(
    [stripe_starts[["sub_id", "provider", "start_ts"]], sg_starts[["sub_id", "provider", "start_ts"]]],
    ignore_index=True,
)
qa_ids = qa_stripe_ids | qa_solidgate_ids
subs_all = subs_all[~subs_all.sub_id.isin(qa_ids)].copy()
subs_all = subs_all.drop_duplicates(subset=["sub_id", "provider"])
log(f"[Combine] total non-QA subscriptions with known start date: {len(subs_all)} "
    f"(stripe={ (subs_all.provider=='stripe').sum() }, solidgate={ (subs_all.provider=='solidgate').sum() })")

pay_all = pd.concat(
    [
        inv_dedup[["sub_id", "provider", "payment_ts", "amount"]],
        sg_pay[["sub_id", "provider", "payment_ts", "amount"]],
    ],
    ignore_index=True,
)
pay_all = pay_all[~pay_all.sub_id.isin(qa_ids)].copy()

merged = pay_all.merge(subs_all, on=["sub_id", "provider"], how="inner")
n_unmatched = len(pay_all) - len(merged)
log(f"[Join] payments joined to a known subscription start: {len(merged)} / {len(pay_all)} "
    f"({n_unmatched} payments dropped -- no matching start-date record for that sub_id)")

merged["week_of_life"] = np.floor((merged["payment_ts"] - merged["start_ts"]) / WEEK).astype(int)
neg = (merged.week_of_life < 0).sum()
if neg:
    neg_days = (merged.loc[merged.week_of_life < 0, "payment_ts"] - merged.loc[merged.week_of_life < 0, "start_ts"]).dt.total_seconds() / 86400.0
    log(f"[Clean] negative offset magnitude (days before recorded start): "
        f"median={neg_days.median():.2f}, p90={neg_days.quantile(0.1):.2f} (10th pct), min={neg_days.min():.2f}")
if neg:
    log(f"[Clean] {neg} payments with negative week_of_life (payment before recorded start "
        f"ts, clock-skew/delivery-order noise) clipped to week 0")
    merged["week_of_life"] = merged["week_of_life"].clip(lower=0)

merged["cohort_month"] = merged["start_ts"].dt.to_period("M").astype(str)
subs_all["cohort_month"] = subs_all["start_ts"].dt.to_period("M").astype(str)
subs_all["age_days"] = (SNAPSHOT_TS - subs_all["start_ts"]).dt.total_seconds() / 86400.0

N_SUBS = len(subs_all)
log(f"[Combine] N subscribers in curve universe: {N_SUBS}")

# ============================================================
# 6. Per-subscriber cumulative revenue by week, then overall + cohort curves
# ============================================================
MAX_WEEK_GRID = 45

def cum_curve_for(sub_ids_subset, subs_df, pay_df, max_week=MAX_WEEK_GRID, min_n=ELIGIBLE_MIN_N):
    """cumulative avg revenue per subscriber by week_of_life, denominator = subs old enough
    to have fully lived through that week by snapshot_ts."""
    subs_sub = subs_df[subs_df.sub_id.isin(sub_ids_subset)]
    pay_sub = pay_df[pay_df.sub_id.isin(sub_ids_subset)]
    # per-sub, per-week revenue, pivoted to cumulative
    wk_rev = pay_sub.groupby(["sub_id", "week_of_life"])["amount"].sum().unstack(fill_value=0.0)
    wk_rev = wk_rev.reindex(columns=range(0, max_week + 1), fill_value=0.0)
    cum = wk_rev.cumsum(axis=1)  # index=sub_id, columns=week -> cumulative $ as of that week

    rows = []
    for w in range(0, max_week + 1):
        eligible = subs_sub[subs_sub.age_days >= (w + 1) * 7]["sub_id"]
        eligible = eligible[eligible.isin(cum.index)]
        n = len(eligible)
        if n == 0:
            continue
        avg = cum.loc[eligible, w].mean() if w in cum.columns else 0.0
        rows.append({"week": w, "n": n, "cum_avg": avg})
    out = pd.DataFrame(rows, columns=["week", "n", "cum_avg"])
    if len(out) == 0:
        return out, None
    # last reliable week = last week where n >= min_n, contiguous from week 0
    reliable = out[out.n >= min_n]
    return out, (reliable["week"].max() if len(reliable) else out["week"].max())

overall_curve, last_reliable_week = cum_curve_for(subs_all.sub_id, subs_all, merged)
log(f"[Overall curve] weeks with n>={ELIGIBLE_MIN_N}: last reliable week = {last_reliable_week}")
log(f"[Overall curve] N(week0)={overall_curve.iloc[0]['n']:.0f}, "
    f"N(week={last_reliable_week})={overall_curve.loc[overall_curve.week==last_reliable_week,'n'].values}")
log(overall_curve.to_string(index=False))

FACT_MAX_WEEK = int(last_reliable_week)

# Cohort curves (by start month), for the thin/faded lines + calibration source.
# Cohorts below MIN_COHORT_N are dropped entirely -- too few subs for a week-by-week
# average to mean anything (e.g. the 11-14 sub cohorts from before stripe_events/
# solidgate_events coverage begins, Oct 2025 -- see SNAPSHOT_SUMMARY.md Section 4).
cohort_months = sorted(subs_all.cohort_month.unique())
cohort_curves = {}
dropped_small_cohorts = []
for cm in cohort_months:
    ids = subs_all.loc[subs_all.cohort_month == cm, "sub_id"]
    if len(ids) < MIN_COHORT_N:
        dropped_small_cohorts.append((cm, len(ids)))
        continue
    curve, last_wk = cum_curve_for(ids, subs_all, merged, min_n=max(10, len(ids) // 10))
    cohort_curves[cm] = (curve, last_wk, len(ids))

log(f"\n[Cohorts] dropped for N<{MIN_COHORT_N}: {dropped_small_cohorts}")
log("[Cohorts]")
for cm, (curve, last_wk, n) in cohort_curves.items():
    log(f"  {cm}: N={n}, last_reliable_week={last_wk}")

# ============================================================
# 7. Extrapolation: fit geometric decay of weekly increments on the last 6-8
#    stable observed weeks of the OVERALL curve, extend to week 52.
# ============================================================
# Raw week-over-week increments on a pooled cohort curve are lumpy: weekly and
# monthly billers are pooled together, so a subset of survivors' monthly renewals
# land on the same week-of-life bucket and create a ~4-week echo (visible in
# build_log.txt: e.g. weeks 36/38 spike well above neighboring weeks despite a
# *shrinking* population). A raw last-6-8-week fit picks up this echo, not real
# decay, and can even come out growing (r>=1). Smoothing with a trailing 4-week
# rolling average of increments before fitting removes the billing-cycle echo
# while preserving the genuine decay trend -- disclosed here and in ltv_numbers.md.
SMOOTH_WINDOW = 4
FIT_WEEKS = 16  # how many (smoothed) trailing weeks to regress over, when no explicit window given

# ANCHOR_WEEK override (owner request, build 2): week 37's value ($59.21) is the tail of a
# spike driven by the large Oct'25 cohort's payment timing landing at wk36-37 (N=156 at wk37,
# still "reliable" by the n>=100 threshold, but not representative of the smooth trend -- see
# PREV_ANCHOR_WEEK/PREV_ANCHOR_VALUE above for the pre-fix figures). Re-anchored to week 35
# ($51.34, the last stable point before the spike) and the fit window narrowed to weeks 20-35
# so the spike enters neither the anchor nor the fit. FACT_MAX_WEEK / the solid black "fact"
# line and the reliability table are unchanged -- only the extrapolation anchor+window moved.
ANCHOR_WEEK = 35
FIT_FROM = 20
FIT_TO = 35


def smoothed_increments(curve_df, max_week, smooth=SMOOTH_WINDOW):
    s = curve_df.set_index("week")["cum_avg"].reindex(range(0, max_week + 1))
    incr = s.diff()
    return incr.rolling(smooth, min_periods=max(2, smooth // 2)).mean()


def fit_decay_smoothed(curve_df, max_week, fit_weeks=FIT_WEEKS, smooth=SMOOTH_WINDOW,
                        fit_from=None, fit_to=None):
    """Fit smoothed_increment(w) = a * r^w via log-linear regression. If fit_from/fit_to are
    given, regress over that explicit [fit_from, fit_to] window of the smoothed series;
    otherwise use the last `fit_weeks` weeks ending at max_week. Returns (a, r, smoothed_series)."""
    sm = smoothed_increments(curve_df, max_week, smooth)
    if fit_from is not None and fit_to is not None:
        seg = sm.dropna()
        seg = seg[(seg.index >= fit_from) & (seg.index <= fit_to)]
    else:
        seg = sm.dropna()
        seg = seg[seg.index >= max(1, max_week - fit_weeks + 1)]
    seg = seg[seg > 0]
    if len(seg) < 4:
        seg = sm.dropna()
        seg = seg[seg > 0]
    if len(seg) < 3:
        raise ValueError("not enough positive smoothed increments to fit decay")
    x = seg.index.values.astype(float)
    y = np.log(seg.values)
    slope, intercept = np.polyfit(x, y, 1)
    return np.exp(intercept), np.exp(slope), sm


def extrapolate_from_anchor(anchor_week, anchor_value, a, r, extend_to_week):
    """Project increment(w) = a*r^w forward from anchor_week+1..extend_to_week,
    added cumulatively onto the true observed anchor_value (so the dashed line
    starts exactly where fact ends, with no double-counting of already-observed
    weeks that may have been used only to estimate the decay rate)."""
    weeks_ext = list(range(anchor_week + 1, extend_to_week + 1))
    cum_ext = []
    cur = anchor_value
    for w in weeks_ext:
        cur = cur + a * (r ** w)
        cum_ext.append(cur)
    return weeks_ext, cum_ext


fit_a, fit_r, smoothed_series = fit_decay_smoothed(overall_curve, ANCHOR_WEEK,
                                                     fit_from=FIT_FROM, fit_to=FIT_TO)
anchor_value = overall_curve.loc[overall_curve.week == ANCHOR_WEEK, "cum_avg"].values[0]

if fit_r >= 1.0:
    log(f"[Extrapolation] smoothed fit on weeks {FIT_FROM}-{FIT_TO} still gave r={fit_r:.4f} "
        f">= 1.0 -- widening to full smoothed history up to week {ANCHOR_WEEK} and retrying")
    seg = smoothed_series.dropna()
    seg = seg[seg > 0]
    x = seg.index.values.astype(float)
    y = np.log(seg.values)
    slope, intercept = np.polyfit(x, y, 1)
    fit_a, fit_r = np.exp(intercept), np.exp(slope)

ext_weeks, ext_cum = extrapolate_from_anchor(ANCHOR_WEEK, anchor_value, fit_a, fit_r, 52)
log(f"\n[Extrapolation] fit on {SMOOTH_WINDOW}-week-smoothed increments, weeks {FIT_FROM}-{FIT_TO} "
    f"(re-anchored away from the wk36-37 cohort-timing spike, see comment above): "
    f"decay increment(w) = {fit_a:.4f} * {fit_r:.4f}^w, anchored at week {ANCHOR_WEEK} = ${anchor_value:.2f}")
if fit_r >= 1.0:
    log(f"[Extrapolation] WARNING: fitted decay ratio r={fit_r:.4f} >= 1.0 (non-decaying) even "
        f"after widening -- check overall_curve / smoothed_series before trusting the chart.")
log(f"[Extrapolation] projected cum_avg at week 52 = ${ext_cum[-1]:.2f}")
log(f"[Extrapolation] DELTA vs prior build: anchor week {PREV_ANCHOR_WEEK}->{ANCHOR_WEEK}, "
    f"anchor value ${PREV_ANCHOR_VALUE:.2f}->${anchor_value:.2f}, "
    f"wk52 projection ${PREV_WK52:.2f}->${ext_cum[-1]:.2f} "
    f"({'+' if ext_cum[-1] >= PREV_WK52 else ''}{ext_cum[-1]-PREV_WK52:.2f})")

# ============================================================
# 8. Calibration: cohorts with >=26 weeks fact, truncate to 12, blind-extrapolate to 26,
#    compare to actual week-26 value.
# ============================================================
def compute_calibration(min_n):
    rows = []
    excluded_small = []
    for cm, (curve, last_wk, n) in cohort_curves.items():
        if last_wk is None or last_wk < 26:
            continue
        if n < min_n:
            excluded_small.append((cm, n))
            continue
        actual_26 = curve.loc[curve.week == 26, "cum_avg"]
        if actual_26.empty:
            continue
        actual_26 = actual_26.values[0]
        truncated = curve[curve.week <= 12]
        try:
            a_c, r_c, _ = fit_decay_smoothed(truncated, 12, fit_weeks=8, smooth=3)
        except ValueError:
            continue
        anchor_12 = truncated.loc[truncated.week == 12, "cum_avg"]
        if anchor_12.empty:
            continue
        _, cum_ext_c = extrapolate_from_anchor(12, anchor_12.values[0], a_c, r_c, 26)
        pred_26 = cum_ext_c[-1]
        rel_err = (pred_26 - actual_26) / actual_26 if actual_26 else np.nan
        rows.append({"cohort": cm, "n": n, "actual_wk26": actual_26,
                      "pred_wk26_from_wk12": pred_26, "rel_err": rel_err})
    return pd.DataFrame(rows), excluded_small

# Owner request (build 2): exclude noisy small cohorts from the calibration set -- N=31
# (2025-11) was inflating the band to 53% off a single small-sample cohort. Try N>=100 first;
# only fall back to N>=50 if that leaves fewer than 2 cohorts to calibrate against.
CALIB_MIN_N = 100
calib_df, calib_excluded = compute_calibration(CALIB_MIN_N)
calib_fallback_used = False
if len(calib_df) < 2:
    calib_fallback_used = True
    CALIB_MIN_N = 50
    calib_df, calib_excluded = compute_calibration(CALIB_MIN_N)

log(f"\n[Calibration] N threshold = {CALIB_MIN_N}"
    f"{' (fallback: N>=100 left <2 cohorts)' if calib_fallback_used else ''}; "
    f"excluded for N<{CALIB_MIN_N}: {calib_excluded}")
log("[Calibration] cohorts with >=26 weeks fact, N>=" + str(CALIB_MIN_N) +
    ", truncated-to-12 blind extrapolation vs actual wk26:")
log(calib_df.to_string(index=False) if len(calib_df) else "  (no cohort qualified)")

if len(calib_df):
    band = calib_df["rel_err"].abs().max()
else:
    band = 0.30  # fallback if no cohort qualifies -- disclosed below
    log("[Calibration] WARNING: no qualifying cohort found for calibration; using fallback band of 30%, disclosed in caption.")
log(f"[Calibration] band (max abs relative error observed) = {band*100:.1f}%")
log(f"[Calibration] DELTA vs prior build: band {PREV_BAND*100:.1f}% -> {band*100:.1f}% "
    f"(prior build included the N=31 2025-11 cohort, which is now excluded by the N>={CALIB_MIN_N} filter)")

# ============================================================
# 9. Sanity / honesty checks
# ============================================================
def val_at_week(curve_df, w):
    row = curve_df.loc[curve_df.week == w]
    return float(row["cum_avg"].values[0]) if len(row) else None

ltv4 = val_at_week(overall_curve, 4)
ltv12 = val_at_week(overall_curve, 12) if FACT_MAX_WEEK >= 12 else None
ltv26 = val_at_week(overall_curve, 26) if FACT_MAX_WEEK >= 26 else None

for label, v in [("week4", ltv4), ("week12", ltv12), ("week26", ltv26)]:
    if v is not None and (v < 0 or v > 200):
        raise SystemExit(f"ABSURD VALUE at {label}: ${v:.2f} -- stopping per honesty rules, "
                          f"not shipping the chart. Inspect intermediate numbers above.")
log(f"\n[Sanity] LTV week4=${ltv4:.2f} week12={'n/a (beyond fact)' if ltv12 is None else f'${ltv12:.2f}'} "
    f"week26={'n/a (beyond fact)' if ltv26 is None else f'${ltv26:.2f}'} -- all within sane bounds.")

# ============================================================
# 10. Plot
# ============================================================
fig, ax = plt.subplots(figsize=(12, 7.5))

# cohort lines (thin/faded) -- truncated to each cohort's own reliable window; beyond that,
# n shrinks to a handful of survivors and the average is not meaningful (a real cliff in the
# untruncated data for tiny late-week n, not a rendering artifact -- see build_log.txt).
cmap = plt.get_cmap("viridis")
cohort_list = [(cm, v) for cm, v in cohort_curves.items() if v[1] is not None and len(v[0])]
for i, (cm, (curve, last_wk, n)) in enumerate(cohort_list):
    color = cmap(i / max(1, len(cohort_list) - 1))
    curve_trunc = curve[curve.week <= last_wk]
    ax.plot(curve_trunc["week"], curve_trunc["cum_avg"], color=color, alpha=0.4, linewidth=1.2,
            label=f"{cm} (N={n})")

# fact curve, overall (solid, thick)
fact = overall_curve[overall_curve.week <= FACT_MAX_WEEK]
ax.plot(fact["week"], fact["cum_avg"], color="black", linewidth=3.0, label="Overall (fact)", zorder=5)

# extrapolation (dashed), starting exactly at the anchor week (wk35 -- see ANCHOR_WEEK comment
# in Section 7: wk36-37 are real observed fact but excluded from the anchor/fit as a cohort-
# timing spike, so the dashed trend is shown starting one week before the solid fact line ends)
join_week = [ANCHOR_WEEK] + ext_weeks
join_cum = [anchor_value] + ext_cum
ax.plot(join_week, join_cum, color="black", linewidth=2.2, linestyle="--",
         label="Trend extrapolation", zorder=5)

# grey error fan around the dashed segment
upper = [v * (1 + band) for v in join_cum]
lower = [v * (1 - band) for v in join_cum]
ax.fill_between(join_week, lower, upper, color="grey", alpha=0.25, zorder=1,
                 label=f"Calibrated error band (±{band*100:.0f}%)")

# annotations at weeks 12/26/52
annot_weeks = [12, 26, 52]
for w in annot_weeks:
    if w <= FACT_MAX_WEEK:
        v = val_at_week(overall_curve, w)
        is_fact = True
    else:
        idx = ext_weeks.index(w) if w in ext_weeks else None
        v = ext_cum[idx] if idx is not None else None
        is_fact = False
    if v is None:
        continue
    ax.scatter([w], [v], color="crimson" if not is_fact else "black", zorder=7, s=30)
    ax.annotate(f"wk{w}: ${v:.2f}{'' if is_fact else ' (proj)'}",
                xy=(w, v), xytext=(w + 1, v + v * 0.14 + 3),
                fontsize=9, fontweight="bold", zorder=8,
                color="black" if is_fact else "crimson",
                bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none", alpha=0.85),
                arrowprops=dict(arrowstyle="-", color="grey", lw=0.7))

ax.axvline(ANCHOR_WEEK, color="grey", linestyle=":", linewidth=1)
ax.text(ANCHOR_WEEK + 0.3, ax.get_ylim()[1] * 0.02,
        f"trend anchor (wk {ANCHOR_WEEK}) -- wk36-37 fact excluded as a cohort-timing spike",
        fontsize=8, color="grey", rotation=90, va="bottom")

ax.set_xlabel("Week of life (weeks since subscription start)")
ax.set_ylabel("Cumulative revenue per subscriber (USD)")
ax.set_title("Web subscriber LTV: actual cumulative revenue by week of life, with trend extrapolation to week 52")
ax.xaxis.set_major_locator(mticker.MultipleLocator(4))
ax.grid(True, alpha=0.3)
ax.legend(loc="upper left", fontsize=8, ncol=2)

caption = (
    f"Actual payments, Stripe+Solidgate, cohorts {cohort_months[0]}–{cohort_months[-1]}, "
    f"N={N_SUBS} subs; dashed = trend extrapolation, band calibrated by backtest on early cohorts.\n"
    f"Footnote: Stripe refunds (${total_refund_usd:,.0f}, {n_refund_events} events, "
    f"{100*total_refund_usd/gross_stripe:.1f}% of gross Stripe revenue) could not be attached to "
    f"any subscription/invoice in this snapshot (payment_intent/invoice link fields are 0% populated "
    f"across invoice.paid, charge.succeeded, payment_intent.succeeded) and are NOT netted out below — "
    f"curve is gross-of-refunds for Stripe. QA/test subs excluded: {len(qa_ids)}."
)
fig.text(0.01, -0.02, caption, fontsize=7.5, wrap=True, va="top")
fig.tight_layout(rect=[0, 0.04, 1, 1])
fig.savefig(f"{OUT_DIR}/ltv_chart.png", dpi=160, bbox_inches="tight")
log(f"\n[Output] wrote {OUT_DIR}/ltv_chart.png")

# ============================================================
# 11. ltv_numbers.md
# ============================================================
def fmt(v):
    return f"${v:.2f}" if v is not None else "n/a"

def df_to_md(df, float_fmt="{:.4f}"):
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, row in df.iterrows():
        cells = []
        for c in cols:
            val = row[c]
            cells.append(float_fmt.format(val) if isinstance(val, float) else str(val))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)

md = []
md.append("# LTV chart — numbers and references\n")
md.append(f"Snapshot: `data/raw/*.parquet`, snapshot_ts = 2026-07-07T00:00:00Z "
          f"(per `data/raw/SNAPSHOT_MANIFEST.json`). Built by "
          f"`reports/ltv_chart/build_ltv_chart.py`, run via `.venv/Scripts/python.exe`.\n")
md.append(f"N subscribers in curve universe (non-QA, known start date): **{N_SUBS}** "
          f"(stripe={(subs_all.provider=='stripe').sum()}, solidgate={(subs_all.provider=='solidgate').sum()}). "
          f"QA/test excluded: {len(qa_ids)} subs "
          f"(stripe daily-interval or utm_source='test': {len(qa_stripe_ids)}; "
          f"solidgate >45-payments fallback rule: {len(qa_solidgate_ids)}).\n")

md.append("## LTV at key weeks\n")
md.append("| Week | Cumulative $/subscriber | Status | N (denominator) | Code reference |")
md.append("|---|---|---|---|---|")
for w in [4, 12, 26, 52]:
    if w <= FACT_MAX_WEEK:
        v = val_at_week(overall_curve, w)
        n = overall_curve.loc[overall_curve.week == w, "n"].values[0]
        status = "fact"
        ref = "`overall_curve` (build_ltv_chart.py, `cum_curve_for()`, Section 6)"
    elif w in ext_weeks:
        idx = ext_weeks.index(w)
        v = ext_cum[idx]
        n = None
        status = "extrapolated"
        ref = ("`fit_decay_smoothed()` + `extrapolate_from_anchor()` (Section 7): geometric "
               f"decay fit on {SMOOTH_WINDOW}-week-smoothed increments, weeks {FIT_FROM}-{FIT_TO}, "
               f"a={fit_a:.4f}, r={fit_r:.4f}, anchored at week {ANCHOR_WEEK} "
               f"(wk{ANCHOR_WEEK+1}-{FACT_MAX_WEEK} excluded from anchor/fit as a cohort-timing spike)")
    else:
        continue
    md.append(f"| {w} | {fmt(v)} | {status} | {n if n is not None else '-'} | {ref} |")

md.append("\n## Fact curve (overall, pooled cohorts)\n")
md.append(f"Last reliable observed week (n >= {ELIGIBLE_MIN_N}): **{FACT_MAX_WEEK}**. "
          "Denominator per week = subscribers whose age at snapshot_ts >= (week+1)*7 days "
          "(i.e. they have fully lived through that week — avoids right-censoring bias). "
          "Query/code: Stripe revenue = `invoice.paid` deduped by invoice id "
          "(Section 3); Solidgate revenue = orders with final status `settle_ok` "
          "under `$.invoices.*.orders.*`, deduped by order id keeping latest `updated_at` "
          "(Section 4); subscription start = `customer.subscription.created` event ts (Stripe, Section 2) "
          "/ `$.subscription.started_at` (Solidgate, Section 4).\n")
md.append(f"Note: weeks {ANCHOR_WEEK+1}-{FACT_MAX_WEEK} are still real observed fact (shown solid "
          f"on the chart, n>={ELIGIBLE_MIN_N} throughout) but are **excluded from the trend "
          f"extrapolation's anchor and fit window** — they carry a visible jump "
          f"(${val_at_week(overall_curve, ANCHOR_WEEK):.2f} at wk{ANCHOR_WEEK} -> "
          f"${val_at_week(overall_curve, FACT_MAX_WEEK):.2f} at wk{FACT_MAX_WEEK}) driven by the "
          f"large Oct'25 cohort's payment timing landing in that window, not a change in the "
          f"underlying trend. See Section 7 in the code.\n")
md.append(df_to_md(overall_curve))

md.append("\n## Per-cohort N and last reliable week\n")
md.append("| Cohort (start month) | N | Last reliable week |")
md.append("|---|---|---|")
for cm, (curve, last_wk, n) in cohort_curves.items():
    md.append(f"| {cm} | {n} | {last_wk} |")

md.append("\n## Calibration (band derivation)\n")
md.append("Method: cohorts with >=26 weeks of observed fact **and N >= "
          f"{CALIB_MIN_N}** are truncated to their first 12 weeks, the same geometric-decay "
          "fit/extrapolation method (Section 7/8 code, `compute_calibration()`) is applied "
          "blindly forward to week 26, and the result is compared to that cohort's real week-26 "
          "value. Band = max absolute relative error observed across qualifying cohorts.\n")
md.append(f"N filter: cohorts with N < {CALIB_MIN_N} are excluded from calibration as too small "
          f"to trust a truncated-then-refit backtest on "
          f"(excluded: {calib_excluded if calib_excluded else 'none'}). "
          f"Threshold tried first: N>=100"
          + (f", but that left fewer than 2 qualifying cohorts so it fell back to N>=50 per the "
             f"owner's fallback rule." if calib_fallback_used
             else " — 2 cohorts qualified, no fallback needed.") + "\n")
if len(calib_df):
    md.append(df_to_md(calib_df))
else:
    md.append("No cohort in this snapshot has >=26 weeks of fact data yet — band fell back to a "
              "conservative 30% and this is disclosed in the chart caption.")
md.append(f"\n**Band used in chart: ±{band*100:.1f}%** "
          f"(prior build: ±{PREV_BAND*100:.1f}%, computed before the N-filter was applied and "
          f"skewed wide by the N=31 2025-11 cohort)\n")

md.append("## Refund handling (honesty disclosure)\n")
md.append(f"- Stripe `charge.refunded`: {n_refund_events} events, ${total_refund_usd:,.2f} total "
          f"({100*total_refund_usd/gross_stripe:.2f}% of gross Stripe `invoice.paid` revenue of "
          f"${gross_stripe:,.2f}). `refund.created` (45 events) was dropped as a duplicate "
          "representation of a subset of `charge.refunded` (verified 45/45 payment_intent overlap).\n")
md.append("- Attach attempt: `invoice.paid` never carries `payment_intent` (0/15088 populated); "
          "`charge.succeeded` and `payment_intent.succeeded` never carry an `invoice` field "
          "(0/11847, 0/12981) in this snapshot. No available join path resolves a refund to a "
          "subscription or invoice — consistent with `reports/SNAPSHOT_SUMMARY.md` Section 5's "
          "\"100% orphan rate\" finding. Per the build spec's fallback rule, refunds are **not** "
          "netted into the per-subscriber curve above; the curve is gross-of-refunds for Stripe. "
          "This is stated on the chart caption as a footnote, not silently absorbed.\n")
md.append("- Solidgate: orders that transition to a terminal `refunded` status are excluded from "
          "revenue by construction (dedup keeps the latest status per order id; only orders whose "
          "*final* status is `settle_ok` count as revenue) — Solidgate refunds are therefore netted "
          "out, unlike Stripe's.\n")

md.append("## QA / test exclusion detail\n")
md.append(f"- Stripe: `interval_unit='day'` ({(stripe_subs.interval_unit=='day').sum()} subs) OR "
          f"`utm_source='test'` ({(stripe_subs.utm_source=='test').sum()} subs) from "
          "`data/raw/stripe_subscriptions.parquet` — union = "
          f"{len(qa_stripe_ids)} subs. Code: Section 1.\n")
md.append(f"- Solidgate: no utm field on the subscription object itself; used the spec's own "
          f"ambiguous-case fallback (\"exclude subs with >45 payments\") — "
          f"{len(qa_solidgate_ids)} subs excluded. Code: Section 4, `qa_solidgate_ids`.\n")

md.append("## Data note: Solidgate order status vocabulary\n")
md.append("The spec's literal `status='success'` string does not occur in this snapshot. Observed "
          "statuses: `auth_failed`, `processing`, `auth_ok`, `settle_ok`, `refunded`, `void_ok`. "
          "`settle_ok` (payment captured/settled) was used as the revenue-recognized state — the "
          "closest analog to Stripe's `invoice.paid`. `auth_ok` (authorization/hold only, not yet "
          "captured) was excluded from revenue.\n")

with open(f"{OUT_DIR}/ltv_numbers.md", "w", encoding="utf-8") as f:
    f.write("\n".join(md))
log(f"[Output] wrote {OUT_DIR}/ltv_numbers.md")

with open(f"{OUT_DIR}/build_log.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(log_lines))
log(f"[Output] wrote {OUT_DIR}/build_log.txt (full run log)")
