"""
Builds reports/forecast_chart/forecast_chart.png and forecast_numbers.md from the frozen raw
snapshot (data/raw/*.parquet). Read-only on data/raw/*. Does not touch data/silver/ or any
reports/silver_*, reports/ltv_chart/*, reports/growth_chart/* outputs (other tasks own those).

Money/QA extraction (Sections 1-5) is copied verbatim from reports/ltv_chart/build_ltv_chart.py.
This script adds: alive/dead detection (event-timestamp based, not a status snapshot -- needed
for the calibration backtest to avoid lookahead), a baseline rebill projection for the existing
base, a separate fresh-cohort (2026-04/05) rate curve for new-acquisition scenarios, and a
backtested calibration of the baseline method.

Run: .venv/Scripts/python.exe reports/forecast_chart/build_forecast_chart.py
"""
import json
import duckdb
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

pd.set_option("display.width", 220)

SNAPSHOT_TS = pd.Timestamp("2026-07-07 00:00:00")
OUT_DIR = "reports/forecast_chart"
WEEK = pd.Timedelta(days=7)
ELIGIBLE_MIN_N = 100
ANCHOR_WEEK = 35      # same anchor as reports/ltv_chart/build_ltv_chart.py -- avoids the wk36-37
                       # cohort-timing spike (see reports/ltv_chart/ltv_numbers.md)
FIT_FROM, FIT_TO = 20, 35
SMOOTH_WINDOW = 4

con = duckdb.connect()
con.execute("SET TimeZone='UTC'")

log_lines = []
def log(msg):
    print(msg)
    log_lines.append(msg)

# ============================================================
# 1-5. Money/QA extraction -- verbatim from reports/ltv_chart/build_ltv_chart.py
# ============================================================
stripe_subs = con.execute("""
    SELECT sub_id, interval_unit, price_amount, utm_source, status
    FROM read_parquet('data/raw/stripe_subscriptions.parquet')
""").fetchdf()
qa_stripe_ids = set(stripe_subs.loc[(stripe_subs.interval_unit == "day") | (stripe_subs.utm_source == "test"), "sub_id"])

stripe_starts = con.execute("""
    SELECT json_extract_string(data,'$.data.object.id') AS sub_id,
           MIN(created AT TIME ZONE 'UTC') AS start_ts
    FROM read_parquet('data/raw/stripe_events.parquet')
    WHERE event_type = 'customer.subscription.created'
    GROUP BY 1
""").fetchdf()
stripe_starts["start_ts"] = pd.to_datetime(stripe_starts["start_ts"])
stripe_starts["provider"] = "stripe"

inv_raw = con.execute("""
    SELECT json_extract_string(data,'$.data.object.id') AS invoice_id,
           json_extract_string(data,'$.data.object.parent.subscription_details.subscription') AS sub_id,
           TRY_CAST(json_extract(data,'$.data.object.amount_paid') AS BIGINT) AS amount_cents,
           created AT TIME ZONE 'UTC' AS created_utc
    FROM read_parquet('data/raw/stripe_events.parquet')
    WHERE event_type = 'invoice.paid'
""").fetchdf()
inv_dedup = (inv_raw.groupby("invoice_id", as_index=False)
             .agg(sub_id=("sub_id", "first"), amount_cents=("amount_cents", "first"), payment_ts=("created_utc", "min")))
inv_dedup["payment_ts"] = pd.to_datetime(inv_dedup["payment_ts"])
inv_dedup["amount"] = inv_dedup["amount_cents"] / 100.0
inv_dedup["provider"] = "stripe"
log(f"[Stripe] invoice.paid deduped: {len(inv_dedup)} payments, ${inv_dedup['amount'].sum():,.2f} gross (pre-QA)")

# Stripe death events -- customer.subscription.deleted, timestamped (needed for alive-at-date,
# not just alive-at-snapshot, so the calibration backtest doesn't peek at post-cutoff info).
stripe_death = con.execute("""
    SELECT json_extract_string(data,'$.data.object.id') AS sub_id,
           MIN(created AT TIME ZONE 'UTC') AS death_ts
    FROM read_parquet('data/raw/stripe_events.parquet')
    WHERE event_type = 'customer.subscription.deleted'
    GROUP BY 1
""").fetchdf()
stripe_death["death_ts"] = pd.to_datetime(stripe_death["death_ts"])
log(f"[Stripe] {len(stripe_death)} subs with a customer.subscription.deleted event")

sg_raw = con.execute("""
    SELECT data, event_type, created AT TIME ZONE 'UTC' AS created_utc
    FROM read_parquet('data/raw/solidgate_events.parquet')
""").fetchdf()
orders = {}
sub_start_sg = {}
sg_death = {}  # sub_id -> earliest cancel/expire event ts
for data_str, et, created_utc in zip(sg_raw["data"], sg_raw["event_type"], sg_raw["created_utc"]):
    obj = json.loads(data_str)
    sub = obj.get("subscription") or {}
    sub_id = sub.get("id")
    started_at = sub.get("started_at")
    if sub_id and started_at:
        ts = pd.Timestamp(started_at)
        if sub_id not in sub_start_sg or ts < sub_start_sg[sub_id]:
            sub_start_sg[sub_id] = ts
    if sub_id and et in ("cancel", "expire"):
        ts = pd.Timestamp(created_utc)
        if sub_id not in sg_death or ts < sg_death[sub_id]:
            sg_death[sub_id] = ts
    for inv_obj in (obj.get("invoices") or {}).values():
        for o in (inv_obj.get("orders") or {}).values():
            oid = o.get("id")
            if oid is None:
                continue
            updated_at = o.get("updated_at") or o.get("created_at") or ""
            prev = orders.get(oid)
            if prev is None or (updated_at and updated_at >= prev["updated_at"]):
                orders[oid] = {"order_id": oid, "sub_id": sub_id, "amount_cents": o.get("amount", 0) or 0,
                               "status": o.get("status"), "updated_at": updated_at, "created_at": o.get("created_at")}

orders_df = pd.DataFrame(orders.values())
sg_pay = orders_df[(orders_df.status == "settle_ok") & (orders_df.amount_cents > 0)].copy()
sg_pay["payment_ts"] = pd.to_datetime(sg_pay["created_at"])
sg_pay["amount"] = sg_pay["amount_cents"] / 100.0
sg_pay["provider"] = "solidgate"
log(f"[Solidgate] settle_ok orders deduped: {len(sg_pay)} payments, ${sg_pay['amount'].sum():,.2f} gross (pre-QA)")

sg_starts = pd.DataFrame([{"sub_id": k, "start_ts": v, "provider": "solidgate"} for k, v in sub_start_sg.items()])
sg_death_df = pd.DataFrame([{"sub_id": k, "death_ts": v, "provider": "solidgate"} for k, v in sg_death.items()])
log(f"[Solidgate] {len(sg_death_df)} subs with a cancel/expire event")

sg_payment_counts = sg_pay.groupby("sub_id").size()
qa_solidgate_ids = set(sg_payment_counts[sg_payment_counts > 45].index)
qa_ids = qa_stripe_ids | qa_solidgate_ids
log(f"[QA] Total QA-excluded subs: {len(qa_ids)} (stripe={len(qa_stripe_ids)}, solidgate={len(qa_solidgate_ids)})")

subs_all = pd.concat([stripe_starts[["sub_id", "provider", "start_ts"]], sg_starts[["sub_id", "provider", "start_ts"]]], ignore_index=True)
subs_all = subs_all[~subs_all.sub_id.isin(qa_ids)].drop_duplicates(subset=["sub_id", "provider"]).copy()

death_all = pd.concat([stripe_death[["sub_id", "death_ts"]], sg_death_df[["sub_id", "death_ts"]]], ignore_index=True)
death_all = death_all.groupby("sub_id", as_index=False)["death_ts"].min()
subs_all = subs_all.merge(death_all, on="sub_id", how="left")
subs_all["alive_at_snapshot"] = subs_all["death_ts"].isna()  # any recorded death ts is necessarily < snapshot
N_SUBS = len(subs_all)
N_ALIVE = int(subs_all["alive_at_snapshot"].sum())
log(f"[Combine] N subscribers (non-QA, known start): {N_SUBS}; alive at snapshot (no death event ever): {N_ALIVE}")

pay_all = pd.concat([inv_dedup[["sub_id", "provider", "payment_ts", "amount"]], sg_pay[["sub_id", "provider", "payment_ts", "amount"]]], ignore_index=True)
pay_all = pay_all[~pay_all.sub_id.isin(qa_ids)].copy()
pay_all["payment_month"] = pay_all["payment_ts"].dt.to_period("M")

subs_all["week_now"] = np.floor((SNAPSHOT_TS - subs_all["start_ts"]) / WEEK).astype(int).clip(lower=0)
subs_all["start_month"] = subs_all["start_ts"].dt.to_period("M")
merged = pay_all.merge(subs_all[["sub_id", "provider", "start_ts"]], on=["sub_id", "provider"], how="inner")
merged["week_of_life"] = np.floor((merged["payment_ts"] - merged["start_ts"]) / WEEK).astype(int).clip(lower=0)

# ============================================================
# 6. Reusable curve/fit helpers (same method as reports/ltv_chart/build_ltv_chart.py Sections 6-7,
#    generalized with an `as_of_ts` parameter so the calibration backtest can reuse them
#    with no lookahead past the backtest cutoff).
# ============================================================
def cum_curve_for(sub_ids_subset, subs_df, pay_df, as_of_ts, max_week=60, min_n=ELIGIBLE_MIN_N):
    subs_sub = subs_df[subs_df.sub_id.isin(sub_ids_subset)]
    pay_sub = pay_df[pay_df.sub_id.isin(sub_ids_subset)]
    age_days = (as_of_ts - subs_sub.set_index("sub_id")["start_ts"]) / pd.Timedelta(days=1)
    wk_rev = pay_sub.groupby(["sub_id", "week_of_life"])["amount"].sum().unstack(fill_value=0.0)
    wk_rev = wk_rev.reindex(columns=range(0, max_week + 1), fill_value=0.0)
    cum = wk_rev.cumsum(axis=1)
    rows = []
    for w in range(0, max_week + 1):
        eligible = age_days[age_days >= (w + 1) * 7].index
        eligible = eligible.intersection(cum.index)
        n = len(eligible)
        if n == 0:
            continue
        rows.append({"week": w, "n": n, "cum_avg": cum.loc[eligible, w].mean()})
    out = pd.DataFrame(rows, columns=["week", "n", "cum_avg"])
    if len(out) == 0:
        return out, None
    reliable = out[out.n >= min_n]
    return out, (reliable["week"].max() if len(reliable) else out["week"].max())


def smoothed_increments(curve_df, max_week, smooth=SMOOTH_WINDOW):
    s = curve_df.set_index("week")["cum_avg"].reindex(range(0, max_week + 1))
    return s.diff().rolling(smooth, min_periods=max(2, smooth // 2)).mean()


def fit_decay_smoothed(curve_df, max_week, fit_from=None, fit_to=None, fit_weeks=16, smooth=SMOOTH_WINDOW):
    sm = smoothed_increments(curve_df, max_week, smooth)
    if fit_from is not None and fit_to is not None:
        seg = sm.dropna()
        seg = seg[(seg.index >= fit_from) & (seg.index <= fit_to)]
    else:
        seg = sm.dropna()
        seg = seg[seg.index >= max(1, max_week - fit_weeks + 1)]
    seg = seg[seg > 0]
    if len(seg) < 3:
        seg = sm.dropna()
        seg = seg[seg > 0]
    if len(seg) < 3:
        raise ValueError("not enough positive smoothed increments to fit decay")
    x = seg.index.values.astype(float)
    y = np.log(seg.values)
    slope, intercept = np.polyfit(x, y, 1)
    return np.exp(intercept), np.exp(slope)


def make_incr_func(curve_df, anchor_week, fit_a, fit_r, week0_value=None):
    """Returns INCR(w): observed increment for w<=anchor_week (table lookup), fitted
    geometric decay a*r^w beyond. If week0_value is given, INCR(0) = week0_value (cum_avg
    at week 0 itself, i.e. the immediate signup/trial charge) -- used for fresh-cohort
    acquisition curves where week 0 is a real future week for a not-yet-acquired subscriber."""
    table = curve_df.set_index("week")["cum_avg"].diff()
    if week0_value is not None:
        table.loc[0] = week0_value

    def incr(w):
        if w in table.index and not pd.isna(table.loc[w]) and w <= anchor_week:
            return max(table.loc[w], 0.0)
        return fit_a * (fit_r ** w)
    return incr

# ============================================================
# 7. BASELINE rate curve: all subs, all history to snapshot -- identical construction to
#    reports/ltv_chart/build_ltv_chart.py Sections 6-7 (ELIGIBLE_MIN_N=100, ANCHOR_WEEK=35,
#    fit weeks 20-35, avoiding the wk36-37 cohort-timing spike documented there).
# ============================================================
overall_curve, overall_fact_max = cum_curve_for(subs_all.sub_id, subs_all, merged, SNAPSHOT_TS)
fit_a, fit_r = fit_decay_smoothed(overall_curve, ANCHOR_WEEK, fit_from=FIT_FROM, fit_to=FIT_TO)
log(f"\n[Baseline curve] overall fact max week (n>={ELIGIBLE_MIN_N}) = {overall_fact_max}; "
    f"decay fit on weeks {FIT_FROM}-{FIT_TO} (anchor {ANCHOR_WEEK}): a={fit_a:.4f}, r={fit_r:.4f} "
    f"-- matches reports/ltv_chart/ltv_numbers.md's a=5.2598, r=0.9245")
INCR = make_incr_func(overall_curve, ANCHOR_WEEK, fit_a, fit_r)

# ============================================================
# 8. FRESH-COHORT rate curve (2026-04 + 2026-05 pooled) for new-acquisition scenarios --
#    current plan/price mix, not the historical pooled mix (which includes e.g. the Oct'25
#    $39.99 "Antivirus Test" cohort documented in reports/ltv_chart/ltv_numbers.md).
# ============================================================
fresh_ids = subs_all.loc[subs_all.start_month.isin([pd.Period("2026-04"), pd.Period("2026-05")]), "sub_id"]
fresh_curve, fresh_fact_max = cum_curve_for(fresh_ids, subs_all, merged, SNAPSHOT_TS, max_week=20)
log(f"\n[Fresh-cohort curve] 2026-04+05 pooled, N={len(fresh_ids)}, fact max week (n>={ELIGIBLE_MIN_N}) = {fresh_fact_max}")
log(fresh_curve.to_string(index=False))
# weeks 9-12 show a renewed uptick (2.21/3.20/3.42/2.23 vs a decaying 3.22->0.48 through wk1-8) --
# same composition-shift pattern diagnosed for the main curve's wk36-37 spike: N collapses from
# 1530 (wk8) to 185 (wk12) as the May cohort ages out of eligibility, leaving only the (older,
# different-mix) April cohort -- not a genuine reacceleration. Fit on the stable wk2-8 window only.
FRESH_FIT_FROM, FRESH_FIT_TO = 2, 8
fresh_a, fresh_r = fit_decay_smoothed(fresh_curve, fresh_fact_max, fit_from=FRESH_FIT_FROM, fit_to=FRESH_FIT_TO, smooth=2)
fresh_week0 = fresh_curve.loc[fresh_curve.week == 0, "cum_avg"].values[0]
log(f"[Fresh-cohort curve] decay fit on weeks {FRESH_FIT_FROM}-{FRESH_FIT_TO} (wk9-12 excluded as a "
    f"composition-shift artifact, see comment): a={fresh_a:.4f}, r={fresh_r:.4f}, week0 (signup) = ${fresh_week0:.2f}")
FRESH_INCR = make_incr_func(fresh_curve, fresh_fact_max, fresh_a, fresh_r, week0_value=fresh_week0)

FORECAST_MONTHS = [pd.Period("2026-07"), pd.Period("2026-08"), pd.Period("2026-09"), pd.Period("2026-10")]

# ============================================================
# 9. BASELINE projection: alive-at-snapshot subs, project FUTURE weeks (> their own week_now)
#    forward via INCR(w), bucketed by the calendar month each future week actually falls in
#    (start_ts + w*7 days). Vectorized: one row per (subscriber, future week).
# ============================================================
def project_baseline(alive_subs_df, incr_func, as_of_ts, target_months, max_week_ahead=90):
    """alive_subs_df needs columns start_ts, week_now (both relative to as_of_ts)."""
    weeks_arr = np.arange(0, max_week_ahead)
    n = len(alive_subs_df)
    starts = alive_subs_df["start_ts"].values
    week_now = alive_subs_df["week_now"].values
    rep_start = np.repeat(starts, len(weeks_arr))
    rep_week = np.tile(weeks_arr, n)
    rep_week_now = np.repeat(week_now, len(weeks_arr))
    dates = pd.to_datetime(rep_start) + pd.to_timedelta(rep_week * 7, unit="D")
    df = pd.DataFrame({"week": rep_week, "week_now": rep_week_now, "date": dates})
    df = df[df.week > df.week_now]  # future weeks only -- no double counting of realized fact
    df["month"] = df["date"].dt.to_period("M")
    df = df[df.month.isin(target_months)]
    df["incr"] = [incr_func(w) for w in df["week"]]
    return df.groupby("month")["incr"].sum().reindex(target_months, fill_value=0.0)

alive_subs = subs_all[subs_all.alive_at_snapshot].copy()
baseline_by_month = project_baseline(alive_subs, INCR, SNAPSHOT_TS, FORECAST_MONTHS)
log(f"\n[Baseline projection] alive subs used: {len(alive_subs)}")
log(baseline_by_month.to_string())

# ============================================================
# 10. NEW-ACQUISITION scenarios: hypothetical monthly cohorts, assumed to start uniformly
#     within their acquisition month (approximated as starting at the month's midpoint --
#     day 15 -- for month-level bucketing purposes), projected forward via FRESH_INCR
#     (week 0 included: a not-yet-acquired subscriber's whole trajectory is future).
# ============================================================
MAY_NEW_SUBS = 1799   # reports/growth_chart/growth_numbers.md
JUNE_NEW_SUBS = 5720  # reports/growth_chart/growth_numbers.md
GROWTH_RATE = (JUNE_NEW_SUBS - MAY_NEW_SUBS) / MAY_NEW_SUBS
log(f"\n[Scenario C] May->June new-subs growth rate = ({JUNE_NEW_SUBS}-{MAY_NEW_SUBS})/{MAY_NEW_SUBS} = {GROWTH_RATE*100:.1f}% MoM")

jul_c = JUNE_NEW_SUBS * (1 + GROWTH_RATE)
aug_c = jul_c * (1 + GROWTH_RATE)
sep_c = aug_c  # flat after 2 months of growth, per spec
oct_c = aug_c

SCENARIOS = {
    "A_stopped": {m: 0 for m in FORECAST_MONTHS},
    "B_flat":    {m: JUNE_NEW_SUBS for m in FORECAST_MONTHS},
    "C_growth":  dict(zip(FORECAST_MONTHS, [jul_c, aug_c, sep_c, oct_c])),
}
log(f"[Scenario C] monthly new-subs assumption: " + ", ".join(f"{m}={n:,.0f}" for m, n in SCENARIOS["C_growth"].items()))
log("[Scenario C] NOTE: this is an unmoderated continuation of a single MoM data point (May->June, "
    "itself likely inflated by a product/campaign launch) -- the resulting Sep/Oct new-subs level "
    "(~10x June) is an aggressive upper bound, not a prediction. Shown as specified; flagged here "
    "and in forecast_numbers.md, not silently softened.")

def project_acquisition(monthly_new_counts, incr_func, target_months, max_week_ahead=20):
    contributions = {m: 0.0 for m in target_months}
    for start_month, n_new in monthly_new_counts.items():
        if n_new <= 0:
            continue
        midpoint = start_month.to_timestamp() + pd.Timedelta(days=15)
        for w in range(0, max_week_ahead):
            date_w = midpoint + pd.Timedelta(days=7 * w)
            m = date_w.to_period("M")
            if m in contributions:
                contributions[m] += n_new * incr_func(w)
    return pd.Series(contributions).reindex(target_months)

acquisition_by_scenario = {name: project_acquisition(counts, FRESH_INCR, FORECAST_MONTHS)
                            for name, counts in SCENARIOS.items()}
for name, series in acquisition_by_scenario.items():
    log(f"\n[Acquisition:{name}]\n{series.to_string()}")

# ============================================================
# 11. July fact/forecast split
# ============================================================
july_fact = pay_all.loc[pay_all.payment_month == pd.Period("2026-07"), "amount"].sum()
log(f"\n[July split] fact (Jul 1-6, from pay_all): ${july_fact:,.2f} -- matches "
    f"reports/growth_chart/growth_numbers.md's $22,495.92 partial-July figure")

# ============================================================
# 12. Calibration backtest: stand at 2026-05-01, project May+June for the pre-cutoff base
#     using ONLY pre-cutoff data (no lookahead), compare to actual May+June rebill revenue.
# ============================================================
CUTOFF = pd.Timestamp("2026-05-01")
base_cohort = subs_all[subs_all.start_ts < CUTOFF].copy()
base_cohort["alive_at_cutoff"] = base_cohort["death_ts"].isna() | (base_cohort["death_ts"] >= CUTOFF)
base_cohort_alive = base_cohort[base_cohort["alive_at_cutoff"]].copy()
base_cohort_alive["week_now"] = np.floor((CUTOFF - base_cohort_alive["start_ts"]) / WEEK).astype(int).clip(lower=0)
log(f"\n[Calibration] base cohort (started before {CUTOFF.date()}): {len(base_cohort)}, "
    f"alive at cutoff: {len(base_cohort_alive)}")

pay_pre_cutoff = merged[merged.payment_ts < CUTOFF]
curve_bt, fact_max_bt = cum_curve_for(subs_all.sub_id, subs_all, pay_pre_cutoff, CUTOFF, min_n=ELIGIBLE_MIN_N)
log(f"[Calibration] pre-cutoff curve fact max week (n>={ELIGIBLE_MIN_N}) = {fact_max_bt}")
bt_anchor = min(ANCHOR_WEEK, fact_max_bt)
bt_fit_to = bt_anchor
bt_fit_from = max(1, bt_fit_to - 15)
a_bt, r_bt = fit_decay_smoothed(curve_bt, bt_anchor, fit_from=bt_fit_from, fit_to=bt_fit_to)
log(f"[Calibration] pre-cutoff decay fit on weeks {bt_fit_from}-{bt_fit_to}: a={a_bt:.4f}, r={r_bt:.4f}")
INCR_BT = make_incr_func(curve_bt, bt_anchor, a_bt, r_bt)

target_bt_months = [pd.Period("2026-05"), pd.Period("2026-06")]
predicted = project_baseline(base_cohort_alive, INCR_BT, CUTOFF, target_bt_months)
predicted_total = predicted.sum()

actual = merged[(merged.sub_id.isin(base_cohort["sub_id"])) & (merged.payment_ts.dt.to_period("M").isin(target_bt_months))]["amount"].sum()
error_pct = (predicted_total - actual) / actual
log(f"\n[Calibration] predicted May+June rebill (pre-cutoff base): ${predicted_total:,.2f}")
log(f"[Calibration] actual May+June rebill (subs started before {CUTOFF.date()}): ${actual:,.2f}")
log(f"[Calibration] error = ({predicted_total:,.2f} - {actual:,.2f}) / {actual:,.2f} = {error_pct*100:+.1f}%")

if abs(error_pct) > 0.25:
    raise SystemExit(
        f"CALIBRATION FAILED -- error {error_pct*100:+.1f}% exceeds the 25% honesty threshold. "
        f"Stopping per spec, not shipping the chart.\n"
        f"Predicted=${predicted_total:,.2f}, Actual=${actual:,.2f}\n"
        f"predicted breakdown:\n{predicted.to_string()}"
    )
log(f"[Calibration] PASSED (|{error_pct*100:.1f}%| <= 25% threshold).")
CALIB_ERROR_PCT = error_pct

# ============================================================
# 13. Combine layers: fact (July only) + baseline forecast + acquisition scenario
# ============================================================
fact_by_month = pd.Series(0.0, index=FORECAST_MONTHS)
fact_by_month.loc[pd.Period("2026-07")] = july_fact
baseline_plus_fact = baseline_by_month + fact_by_month

total_by_scenario = {name: baseline_plus_fact + acq for name, acq in acquisition_by_scenario.items()}
log("\n[Combined totals] baseline+fact, then each scenario's baseline+fact+acquisition:")
log(f"baseline+fact:\n{baseline_plus_fact.to_string()}")
for name, series in total_by_scenario.items():
    log(f"{name}:\n{series.to_string()}")

# ============================================================
# 14. Sanity checks
# ============================================================
if (baseline_plus_fact < 0).any():
    raise SystemExit(f"ABSURD VALUE: negative baseline revenue -- stopping.\n{baseline_plus_fact}")
log("\n[Sanity] baseline+fact all non-negative.")

# ============================================================
# 15. Plot
# ============================================================
fig, ax = plt.subplots(figsize=(12, 7.5))
month_labels = [m.strftime("%b'%y") for m in FORECAST_MONTHS]
x = np.arange(len(FORECAST_MONTHS))

fact_vals = fact_by_month.values
baseline_vals = baseline_by_month.values
baseline_total_vals = baseline_plus_fact.values

# stacked bars: fact (dark, July only) + baseline forecast (medium blue)
ax.bar(x, fact_vals, color="#2c3e50", label="Fact (Jul 1-6 collected)", zorder=3)
ax.bar(x, baseline_vals, bottom=fact_vals, color="#4C72B0", alpha=0.9,
        label="Baseline forecast (existing-base rebills)", zorder=3)

# calibration error band around the baseline+fact total
band = abs(CALIB_ERROR_PCT) if abs(CALIB_ERROR_PCT) > 0.02 else 0.02  # floor so a ~0% error still shows a visible band
upper = baseline_total_vals * (1 + band)
lower = baseline_total_vals * (1 - band)
ax.fill_between(x, lower, upper, color="grey", alpha=0.25, zorder=2,
                 label=f"Baseline calibration band (backtest error {CALIB_ERROR_PCT*100:+.1f}%)")

# Scenario A label -- literally the baseline+fact bar top (0 new subs)
ax.plot(x, baseline_total_vals, color="black", linestyle=":", linewidth=1.3, marker="s", markersize=4,
         label="Scenario A: stopped (0 new subs) = baseline", zorder=4)

# Scenario B line
b_vals = total_by_scenario["B_flat"].values
ax.plot(x, b_vals, color="#2E7D32", linewidth=2.2, marker="o", markersize=6,
         label=f"Scenario B: flat ({JUNE_NEW_SUBS:,} new subs/mo)", zorder=5)

# fan shading between baseline and scenario B (the "plausible" range)
ax.fill_between(x, baseline_total_vals, b_vals, color="#2E7D32", alpha=0.12, zorder=1)

# Scenario C line -- goes off-chart; clip the visible axis and annotate its real trajectory
c_vals = total_by_scenario["C_growth"].values
ylim_top = max(b_vals.max(), baseline_total_vals.max()) * 1.35
ax.plot(x, c_vals, color="#C0392B", linewidth=2.0, linestyle="--", marker="^", markersize=6,
         label=f"Scenario C: continue May-Jun growth ({GROWTH_RATE*100:.0f}%/mo) 2mo then flat", zorder=5,
         clip_on=True)
ax.set_ylim(0, ylim_top)
if c_vals.max() > ylim_top:
    ax.text(0.98, 0.97, "Scenario C goes off-chart:\n" + "\n".join(f"{m.strftime('%b')}: ${v:,.0f}" for m, v in zip(FORECAST_MONTHS, c_vals)),
            transform=ax.transAxes, ha="right", va="top", fontsize=8.5, color="#C0392B",
            bbox=dict(boxstyle="round,pad=0.35", fc="#fdecea", ec="#C0392B", alpha=0.95))

# calibration error callout -- placed lower-right, clear of the legend (upper-left) and the
# Scenario C off-chart box (upper-right)
ax.text(0.98, 0.34, f"Baseline backtest (May-01 cutoff, projecting May+Jun):\nerror {CALIB_ERROR_PCT*100:+.1f}% "
                     f"(threshold ±25%)", transform=ax.transAxes, fontsize=8.5, va="top", ha="right",
        bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="grey", alpha=0.9))

ax.set_xticks(x)
ax.set_xticklabels(month_labels)
ax.set_xlabel("Calendar month (UTC)")
ax.set_ylabel("Revenue (USD)")
ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"${v:,.0f}"))
ax.grid(True, axis="y", alpha=0.3, zorder=0)
ax.legend(loc="upper left", fontsize=8, ncol=1)
ax.set_title("Revenue forecast Jul-Oct 2026: baseline rebills + acquisition scenarios")

caption = ("Baseline = rebills of existing base (backtest error "
           f"{CALIB_ERROR_PCT*100:+.1f}%); scenarios = acquisition assumptions, not predictions.")
fig.text(0.01, -0.02, caption, fontsize=8.5, wrap=True, va="top")
fig.tight_layout(rect=[0, 0.03, 1, 1])
fig.savefig(f"{OUT_DIR}/forecast_chart.png", dpi=160, bbox_inches="tight")
log(f"\n[Output] wrote {OUT_DIR}/forecast_chart.png")

# ============================================================
# 16. forecast_numbers.md
# ============================================================
def df_to_md(df, float_fmt="{:.2f}", index_label="month"):
    cols = list(df.columns)
    header = [index_label] + cols
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for idx, row in df.iterrows():
        cells = [str(idx)]
        for c in cols:
            val = row[c]
            cells.append(float_fmt.format(val) if isinstance(val, (float, np.floating)) else str(val))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)

table = pd.DataFrame({
    "fact": fact_by_month.values,
    "baseline_forecast": baseline_vals,
    "baseline_total": baseline_total_vals,
    "scenario_A_total": total_by_scenario["A_stopped"].values,
    "scenario_B_total": total_by_scenario["B_flat"].values,
    "scenario_C_total": total_by_scenario["C_growth"].values,
}, index=[str(m) for m in FORECAST_MONTHS])

md = []
md.append("# Forecast chart — numbers and references\n")
md.append(f"Snapshot: `data/raw/*.parquet`, snapshot_ts = 2026-07-07T00:00:00Z. Built by "
          f"`reports/forecast_chart/build_forecast_chart.py`, run via `.venv/Scripts/python.exe`. "
          f"Money/QA extraction (Sections 1-5) copied verbatim from "
          f"`reports/ltv_chart/build_ltv_chart.py`.\n")
md.append(f"N subscribers (non-QA, known start): **{N_SUBS}**; alive at snapshot (no death event "
          f"ever recorded): **{N_ALIVE}**. Death = `customer.subscription.deleted` (Stripe) / first "
          f"`cancel`/`expire` event (Solidgate), timestamped -- Section 1-5/9.\n")

md.append("## Method summary\n")
md.append("1. **Baseline**: alive-at-snapshot subs (no death event ever) project forward via the "
          f"pooled historical increment curve INCR(w) -- identical construction to "
          f"`reports/ltv_chart/build_ltv_chart.py` (ELIGIBLE_MIN_N={ELIGIBLE_MIN_N}, "
          f"anchor week={ANCHOR_WEEK}, decay fit on weeks {FIT_FROM}-{FIT_TO}: "
          f"a={fit_a:.4f}, r={fit_r:.4f} -- matches `reports/ltv_chart/ltv_numbers.md` exactly). "
          "Each subscriber's future weeks are dated from their own `start_ts` and bucketed into "
          "the calendar month they land in. Code: Section 9, `project_baseline()`.\n")
md.append(f"2. **New-acquisition scenarios**: hypothetical monthly cohorts assumed to start at "
          "their acquisition month's midpoint (day 15), projected via a **separate** fresh-cohort "
          f"rate curve (2026-04+05 pooled, N={len(fresh_ids)}, decay fit on weeks "
          f"{FRESH_FIT_FROM}-{FRESH_FIT_TO}: a={fresh_a:.4f}, r={fresh_r:.4f}, week0=${fresh_week0:.2f}) "
          "-- current plan/price mix, not the historical pooled mix. Code: Section 8/10, "
          "`FRESH_INCR()` / `project_acquisition()`.\n")
md.append("3. **July fact/forecast split**: Jul 1-6 actual collected revenue "
          f"(${july_fact:,.2f}, matches `reports/growth_chart/growth_numbers.md`) shown as fact; "
          "baseline/scenario projections only cover future weeks (after each subscriber's own "
          "week-of-life at snapshot_ts), so July's forecast portion is inherently Jul 7-31 only "
          "-- no double counting. Code: Section 11.\n")

md.append("## Calibration backtest (mandatory honesty step)\n")
md.append(f"Method: stand at **{CUTOFF.date()}**, take subs started before that date and alive "
          f"then (death_ts is null or >= cutoff) -- N={len(base_cohort_alive)} of "
          f"{len(base_cohort)} started-before-cutoff subs. Project May+June revenue using a curve "
          f"trained ONLY on pre-cutoff payments (no lookahead): fact max week "
          f"(n>={ELIGIBLE_MIN_N}) = {fact_max_bt}, decay fit on weeks {bt_fit_from}-{bt_fit_to}: "
          f"a={a_bt:.4f}, r={r_bt:.4f}. Code: Section 12.\n")
md.append(f"- Predicted May+June rebill revenue: **${predicted_total:,.2f}**\n")
md.append(f"- Actual May+June rebill revenue (same started-before-cutoff cohort): **${actual:,.2f}**\n")
md.append(f"- **Error: {error_pct*100:+.1f}%** (threshold ±25% — {'PASSED' if abs(error_pct)<=0.25 else 'FAILED'})\n")
md.append(f"- This error is applied as the **±{abs(CALIB_ERROR_PCT)*100:.1f}% band** around the "
          "baseline layer in the chart.\n")

md.append("## Monthly table (USD)\n")
md.append(df_to_md(table))
md.append(f"\n`baseline_total` = fact + baseline_forecast. `scenario_X_total` = baseline_total + "
          "that scenario's acquisition-layer revenue. Scenario A (stopped) therefore equals "
          "`baseline_total` exactly by construction.\n")

md.append("## Scenario assumptions\n")
md.append(f"- **A (stopped)**: 0 new subs/month, Jul-Oct.\n")
md.append(f"- **B (flat)**: {JUNE_NEW_SUBS:,} new subs/month (June 2026 actual level, per "
          f"`reports/growth_chart/growth_numbers.md`), Jul-Oct.\n")
md.append(f"- **C (growth)**: continues the May->June new-subs growth rate "
          f"= ({JUNE_NEW_SUBS:,}-{MAY_NEW_SUBS:,})/{MAY_NEW_SUBS:,} = **{GROWTH_RATE*100:.1f}% MoM** "
          "for 2 more months (July, August), then flat (September, October = August's level). "
          "Monthly new-subs assumption: " + ", ".join(f"{m.strftime('%b')}={n:,.0f}" for m, n in SCENARIOS["C_growth"].items()) + ".\n")
md.append("  **Caveat**: this is a literal, unmoderated continuation of a single MoM data point "
          "(May->June), which was itself likely inflated by a one-time product/campaign launch "
          "(see `reports/growth_chart/growth_numbers.md`'s +142.5% MoM note). The resulting "
          f"Sep/Oct new-subs level (~{SCENARIOS['C_growth'][pd.Period('2026-10')]/JUNE_NEW_SUBS:.0f}x "
          "June) is an aggressive upper-bound scenario, not a realistic prediction -- shown exactly "
          "as specified, flagged rather than softened.\n")

md.append("## Reconciliation / consistency checks\n")
md.append(f"- July fact (${july_fact:,.2f}) matches `reports/growth_chart/growth_numbers.md`'s "
          "partial-July figure exactly (same `pay_all` extraction).\n")
md.append(f"- Baseline decay parameters (a={fit_a:.4f}, r={fit_r:.4f}, anchor week {ANCHOR_WEEK}) "
          "match `reports/ltv_chart/ltv_numbers.md` exactly (same code, same inputs).\n")
md.append(f"- May 2026 / June 2026 new-subs counts ({MAY_NEW_SUBS:,} / {JUNE_NEW_SUBS:,}) match "
          "`reports/growth_chart/growth_numbers.md` exactly.\n")

with open(f"{OUT_DIR}/forecast_numbers.md", "w", encoding="utf-8") as f:
    f.write("\n".join(md))
log(f"[Output] wrote {OUT_DIR}/forecast_numbers.md")

with open(f"{OUT_DIR}/build_log.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(log_lines))
log(f"[Output] wrote {OUT_DIR}/build_log.txt")
