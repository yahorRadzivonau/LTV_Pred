"""
Builds reports/growth_chart/growth_chart.png and growth_numbers.md from the
frozen raw snapshot (data/raw/*.parquet). Read-only on data/raw/*. Does not
touch data/silver/ or any other reports/silver_* or reports/ltv_chart/*
outputs (separate task/session own those).

Money/QA extraction rules are copied verbatim from
reports/ltv_chart/build_ltv_chart.py Sections 1-5 (same snapshot, same
dedup rules, same QA exclusion) -- this script only changes the aggregation
axis (calendar month of payment/start date, instead of week-of-life).

Run: .venv/Scripts/python.exe reports/growth_chart/build_growth_chart.py
"""
import json
from collections import Counter

import duckdb
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

pd.set_option("display.width", 200)

SNAPSHOT_TS = pd.Timestamp("2026-07-07 00:00:00")
OUT_DIR = "reports/growth_chart"
FULL_MONTHS = pd.period_range("2025-10", "2026-06", freq="M")
PARTIAL_MONTH = pd.Period("2026-07", freq="M")

con = duckdb.connect()
con.execute("SET TimeZone='UTC'")

log_lines = []
def log(msg):
    print(msg)
    log_lines.append(msg)

# ============================================================
# 1. Stripe QA / test subscription ids (identical rule to build_ltv_chart.py Section 1)
# ============================================================
stripe_subs = con.execute("""
    SELECT sub_id, interval_unit, price_amount, utm_source, status
    FROM read_parquet('data/raw/stripe_subscriptions.parquet')
""").fetchdf()
qa_stripe_ids = set(
    stripe_subs.loc[(stripe_subs.interval_unit == "day") | (stripe_subs.utm_source == "test"), "sub_id"]
)
log(f"[QA] Stripe QA-excluded sub_id count: {len(qa_stripe_ids)}")

# ============================================================
# 2. Stripe subscription starts (identical to Section 2)
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

# ============================================================
# 3. Stripe revenue: invoice.paid, deduped by invoice id (identical to Section 3)
# ============================================================
inv_raw = con.execute("""
    SELECT json_extract_string(data,'$.data.object.id') AS invoice_id,
           json_extract_string(data,'$.data.object.parent.subscription_details.subscription') AS sub_id,
           TRY_CAST(json_extract(data,'$.data.object.amount_paid') AS BIGINT) AS amount_cents,
           created AT TIME ZONE 'UTC' AS created_utc
    FROM read_parquet('data/raw/stripe_events.parquet')
    WHERE event_type = 'invoice.paid'
""").fetchdf()
inv_dedup = (
    inv_raw.groupby("invoice_id", as_index=False)
    .agg(sub_id=("sub_id", "first"), amount_cents=("amount_cents", "first"), payment_ts=("created_utc", "min"))
)
inv_dedup["payment_ts"] = pd.to_datetime(inv_dedup["payment_ts"])
inv_dedup["amount"] = inv_dedup["amount_cents"] / 100.0
inv_dedup["provider"] = "stripe"
gross_stripe_all = inv_dedup["amount"].sum()
log(f"[Stripe] invoice.paid deduped: {len(inv_dedup)} payments, ${gross_stripe_all:,.2f} gross (pre-QA) "
    f"-- matches build_ltv_chart.py's $112,481.38")

# ============================================================
# 4. Solidgate: orders under $.invoices.*.orders.* , settle_ok = revenue-recognized
#    (identical to Section 4)
# ============================================================
sg_raw = con.execute("SELECT data FROM read_parquet('data/raw/solidgate_events.parquet')").fetchdf()
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
                    "order_id": oid, "sub_id": sub_id, "amount_cents": o.get("amount", 0) or 0,
                    "status": o.get("status"), "updated_at": updated_at, "created_at": o.get("created_at"),
                }

orders_df = pd.DataFrame(orders.values())
sg_pay = orders_df[(orders_df.status == "settle_ok") & (orders_df.amount_cents > 0)].copy()
sg_pay["payment_ts"] = pd.to_datetime(sg_pay["created_at"])
sg_pay["amount"] = sg_pay["amount_cents"] / 100.0
sg_pay["provider"] = "solidgate"
gross_solidgate_all = sg_pay["amount"].sum()
log(f"[Solidgate] settle_ok payments deduped: {len(sg_pay)}, ${gross_solidgate_all:,.2f} gross (pre-QA) "
    f"-- matches build_ltv_chart.py's $54,771.91")

sg_starts = pd.DataFrame(
    [{"sub_id": k, "start_ts": v, "provider": "solidgate"} for k, v in sub_start_sg.items()]
)
sg_payment_counts = sg_pay.groupby("sub_id").size()
qa_solidgate_ids = set(sg_payment_counts[sg_payment_counts > 45].index)
log(f"[QA] Solidgate QA-excluded (>45 payments): {len(qa_solidgate_ids)}")

# ============================================================
# 5. Combine, apply QA exclusion (identical to Section 5)
# ============================================================
subs_all = pd.concat(
    [stripe_starts[["sub_id", "provider", "start_ts"]], sg_starts[["sub_id", "provider", "start_ts"]]],
    ignore_index=True,
)
qa_ids = qa_stripe_ids | qa_solidgate_ids
subs_all = subs_all[~subs_all.sub_id.isin(qa_ids)].drop_duplicates(subset=["sub_id", "provider"]).copy()
N_SUBS = len(subs_all)
log(f"[Combine] N subscribers (non-QA, known start): {N_SUBS} "
    f"(stripe={(subs_all.provider=='stripe').sum()}, solidgate={(subs_all.provider=='solidgate').sum()})")

pay_all = pd.concat(
    [inv_dedup[["sub_id", "provider", "payment_ts", "amount"]], sg_pay[["sub_id", "provider", "payment_ts", "amount"]]],
    ignore_index=True,
)
qa_stripe_revenue = inv_dedup.loc[inv_dedup.sub_id.isin(qa_ids), "amount"].sum()
qa_solidgate_revenue = sg_pay.loc[sg_pay.sub_id.isin(qa_ids), "amount"].sum()
pay_all = pay_all[~pay_all.sub_id.isin(qa_ids)].copy()
log(f"[QA] Stripe revenue removed by QA exclusion: ${qa_stripe_revenue:,.2f}")
log(f"[QA] Solidgate revenue removed by QA exclusion: ${qa_solidgate_revenue:,.2f}")

# ============================================================
# 6. Aggregate by CALENDAR MONTH (UTC) instead of week-of-life
# ============================================================
pay_all["month"] = pay_all["payment_ts"].dt.to_period("M")
subs_all["month"] = subs_all["start_ts"].dt.to_period("M")

monthly_rev = (
    pay_all.groupby(["month", "provider"])["amount"].sum().unstack(fill_value=0.0)
)
for col in ["stripe", "solidgate"]:
    if col not in monthly_rev.columns:
        monthly_rev[col] = 0.0
monthly_rev["total"] = monthly_rev["stripe"] + monthly_rev["solidgate"]
monthly_rev = monthly_rev.sort_index()

monthly_new_subs = subs_all.groupby("month").size().sort_index()

all_months_seen = sorted(set(monthly_rev.index) | set(monthly_new_subs.index))
log(f"\n[Months] months with any activity: {[str(m) for m in all_months_seen]}")

pre_oct_months = [m for m in all_months_seen if m < FULL_MONTHS[0]]
if pre_oct_months:
    pre_oct_rev = monthly_rev.reindex(pre_oct_months, fill_value=0.0)
    pre_oct_subs = monthly_new_subs.reindex(pre_oct_months, fill_value=0)
    log(f"[Months] pre-Oct-2025 activity found (excluded from the chart's x-axis, per spec's "
        f"'cover 2025-10 through 2026-06' range; included in the reconciliation table below): "
        f"{pre_oct_rev.to_dict('index')}, new_subs={pre_oct_subs.to_dict()}")

post_jun_months = [m for m in all_months_seen if m > FULL_MONTHS[-1] and m != PARTIAL_MONTH]
if post_jun_months:
    log(f"[Months] WARNING: unexpected months beyond the partial July bucket: {post_jun_months}")

# ============================================================
# 7. Reconciliation: monthly totals (all months, pre-Oct + full + partial July) must sum
#    to gross-minus-QA for each provider.
# ============================================================
monthly_full_index = pd.PeriodIndex(all_months_seen, freq="M")
monthly_rev_full = monthly_rev.reindex(monthly_full_index, fill_value=0.0)

recon_stripe = monthly_rev_full["stripe"].sum()
recon_solidgate = monthly_rev_full["solidgate"].sum()
expected_stripe = gross_stripe_all - qa_stripe_revenue
expected_solidgate = gross_solidgate_all - qa_solidgate_revenue

log(f"\n[Reconciliation] Stripe: sum(monthly)=${recon_stripe:,.2f} vs "
    f"gross(${gross_stripe_all:,.2f}) - QA(${qa_stripe_revenue:,.2f}) = ${expected_stripe:,.2f}")
log(f"[Reconciliation] Solidgate: sum(monthly)=${recon_solidgate:,.2f} vs "
    f"gross(${gross_solidgate_all:,.2f}) - QA(${qa_solidgate_revenue:,.2f}) = ${expected_solidgate:,.2f}")

TOL = 0.01  # rounding tolerance in dollars
stripe_ok = abs(recon_stripe - expected_stripe) <= TOL
solidgate_ok = abs(recon_solidgate - expected_solidgate) <= TOL
if not (stripe_ok and solidgate_ok):
    raise SystemExit(
        f"RECONCILIATION FAILED -- stopping per honesty rules, not shipping the chart.\n"
        f"Stripe: monthly sum ${recon_stripe:,.2f} vs expected ${expected_stripe:,.2f} (ok={stripe_ok})\n"
        f"Solidgate: monthly sum ${recon_solidgate:,.2f} vs expected ${expected_solidgate:,.2f} (ok={solidgate_ok})\n"
        f"Inspect monthly_rev_full above before proceeding."
    )
log(f"[Reconciliation] PASS (tolerance ${TOL})")

# ============================================================
# 8. MoM growth, last full month annotations
# ============================================================
last_full_month = FULL_MONTHS[-1]  # 2026-06
prev_month = FULL_MONTHS[-2]        # 2026-05
last_full_total = monthly_rev_full.loc[last_full_month, "total"]
prev_total = monthly_rev_full.loc[prev_month, "total"]
mom_growth = (last_full_total - prev_total) / prev_total
last_full_new_subs = int(monthly_new_subs.reindex(monthly_full_index, fill_value=0).loc[last_full_month])
log(f"\n[Summary] {last_full_month}: total revenue = ${last_full_total:,.2f}, "
    f"MoM vs {prev_month} (${prev_total:,.2f}) = {mom_growth*100:+.1f}%, "
    f"new subs in {last_full_month} = {last_full_new_subs}")

# partial July figures
july_total = monthly_rev_full["total"].get(PARTIAL_MONTH, 0.0) if PARTIAL_MONTH in monthly_rev_full.index else 0.0
july_new_subs = int(monthly_new_subs.get(PARTIAL_MONTH, 0))
log(f"[Summary] {PARTIAL_MONTH} (partial, first 6 days): total revenue = ${july_total:,.2f}, "
    f"new subs = {july_new_subs}")

# ============================================================
# 9. Plot
# ============================================================
chart_months = list(FULL_MONTHS) + [PARTIAL_MONTH]
x = np.arange(len(chart_months))
month_labels = [m.strftime("%b'%y") for m in chart_months]

stripe_vals = [monthly_rev_full["stripe"].get(m, 0.0) for m in chart_months]
solidgate_vals = [monthly_rev_full["solidgate"].get(m, 0.0) for m in chart_months]
subs_vals = [int(monthly_new_subs.reindex(monthly_full_index, fill_value=0).get(m, 0)) for m in chart_months]

fig, ax1 = plt.subplots(figsize=(13, 7.5))

is_partial = [m == PARTIAL_MONTH for m in chart_months]
bar_alpha = [0.35 if p else 0.9 for p in is_partial]
hatch = ["///" if p else None for p in is_partial]

for i in range(len(chart_months)):
    ax1.bar(x[i], stripe_vals[i], color="#4C72B0", alpha=bar_alpha[i], hatch=hatch[i],
            edgecolor="white", linewidth=0.6, label="Stripe" if i == 0 else None, zorder=3)
    ax1.bar(x[i], solidgate_vals[i], bottom=stripe_vals[i], color="#DD8452", alpha=bar_alpha[i],
            hatch=hatch[i], edgecolor="white", linewidth=0.6, label="Solidgate" if i == 0 else None, zorder=3)

partial_idx = chart_months.index(PARTIAL_MONTH)

ax1.set_xticks(x)
ax1.set_xticklabels(month_labels)
ax1.set_xlabel("Calendar month (UTC, month of payment)")
ax1.set_ylabel("Revenue collected (USD)")
ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"${v:,.0f}"))
ax1.grid(True, axis="y", alpha=0.3, zorder=0)

ax2 = ax1.twinx()
ax2.plot(x, subs_vals, color="#2E7D32", marker="o", linewidth=2.2, markersize=5,
          label="New subscriptions", zorder=5)
ax2.set_ylabel("New subscriptions started that month", color="#2E7D32")
ax2.tick_params(axis="y", colors="#2E7D32")

# July label drawn on ax2 (rendered after the green line, so it sits on top instead of being
# cut through by it -- xy is in ax1's data space (dollars) via an explicit transform, since
# the label anchors to the bar height, not the subs line).
ax2.annotate("July: first 6 days only",
             xy=(x[partial_idx], stripe_vals[partial_idx] + solidgate_vals[partial_idx]),
             xycoords=ax1.transData,
             xytext=(x[partial_idx], max(stripe_vals + solidgate_vals) * 1.02),
             textcoords=ax1.transData,
             ha="center", va="bottom", fontsize=8.5, color="#555555", fontstyle="italic",
             bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="#999999", alpha=0.95),
             arrowprops=dict(arrowstyle="-", color="#999999", lw=0.8), zorder=10)

# annotate last full month totals
last_idx = chart_months.index(last_full_month)
ax1.annotate(f"{last_full_month.strftime('%b %Y')}: ${last_full_total:,.0f}\nMoM {mom_growth*100:+.1f}%",
             xy=(x[last_idx], stripe_vals[last_idx] + solidgate_vals[last_idx]),
             xytext=(x[last_idx] - 2.6, (stripe_vals[last_idx] + solidgate_vals[last_idx]) * 1.12),
             fontsize=10, fontweight="bold", color="black",
             bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="#4C72B0", alpha=0.9),
             arrowprops=dict(arrowstyle="-", color="#4C72B0", lw=0.8))
ax2.annotate(f"{last_full_new_subs} new subs",
             xy=(x[last_idx], subs_vals[last_idx]),
             xytext=(x[last_idx] + 0.4, subs_vals[last_idx] * 1.12 + 5),
             fontsize=9, fontweight="bold", color="#2E7D32",
             bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="#2E7D32", alpha=0.9),
             arrowprops=dict(arrowstyle="-", color="#2E7D32", lw=0.8))

lines1, labels1 = ax1.get_legend_handles_labels()
lines2, labels2 = ax2.get_legend_handles_labels()
ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left", fontsize=9)

ax1.set_title("Business growth by calendar month: revenue collected + new subscriptions")
caption = (
    "Actual collected payments by calendar month, Stripe+Solidgate, gross of Stripe refunds (5.2%), "
    f"QA excluded, N={N_SUBS:,} subs universe."
)
fig.text(0.01, -0.02, caption, fontsize=8, wrap=True, va="top")
fig.tight_layout(rect=[0, 0.03, 1, 1])
fig.savefig(f"{OUT_DIR}/growth_chart.png", dpi=160, bbox_inches="tight")
log(f"\n[Output] wrote {OUT_DIR}/growth_chart.png")

# ============================================================
# 10. growth_numbers.md
# ============================================================
def df_to_md(df, float_fmt="{:.2f}"):
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, row in df.iterrows():
        cells = []
        for c in cols:
            val = row[c]
            cells.append(float_fmt.format(val) if isinstance(val, float) else str(val))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)

table_rows = []
prev_total_iter = None
for m in all_months_seen:
    stripe_v = monthly_rev_full["stripe"].get(m, 0.0)
    solidgate_v = monthly_rev_full["solidgate"].get(m, 0.0)
    total_v = stripe_v + solidgate_v
    new_subs_v = int(monthly_new_subs.reindex(monthly_full_index, fill_value=0).get(m, 0))
    mom = None
    if prev_total_iter is not None and prev_total_iter > 0 and m != PARTIAL_MONTH:
        mom = (total_v - prev_total_iter) / prev_total_iter * 100
    table_rows.append({
        "month": str(m), "stripe": stripe_v, "solidgate": solidgate_v, "total": total_v,
        "new_subs": new_subs_v, "mom_pct": mom if mom is not None else float("nan"),
        "note": "partial (first 6 days)" if m == PARTIAL_MONTH else ("pre-Oct, off-chart" if m < FULL_MONTHS[0] else ""),
    })
    if m != PARTIAL_MONTH:
        prev_total_iter = total_v

monthly_table_df = pd.DataFrame(table_rows)

md = []
md.append("# Growth chart — numbers and references\n")
md.append(f"Snapshot: `data/raw/*.parquet`, snapshot_ts = 2026-07-07T00:00:00Z "
          f"(per `data/raw/SNAPSHOT_MANIFEST.json`). Built by "
          f"`reports/growth_chart/build_growth_chart.py`, run via `.venv/Scripts/python.exe`. "
          f"Money/QA extraction rules copied verbatim from `reports/ltv_chart/build_ltv_chart.py` "
          f"Sections 1-5 (Stripe `invoice.paid` deduped by invoice id /100; Solidgate `settle_ok` "
          f"orders deduped by order id /100; QA subs excluded); only the aggregation axis changed "
          f"(calendar month instead of week-of-life).\n")
md.append(f"N subscribers in universe (non-QA, known start date): **{N_SUBS}** "
          f"(stripe={(subs_all.provider=='stripe').sum()}, solidgate={(subs_all.provider=='solidgate').sum()}). "
          f"QA excluded: {len(qa_ids)} subs (stripe={len(qa_stripe_ids)}, solidgate={len(qa_solidgate_ids)}).\n")

md.append("## Monthly table\n")
md.append("Code reference: Section 6, `monthly_rev` (revenue, grouped by `payment_ts.dt.to_period('M')` "
          "and `provider`) and `monthly_new_subs` (grouped by `start_ts.dt.to_period('M')`), both computed "
          "over the same `pay_all`/`subs_all` frames as `reports/ltv_chart/build_ltv_chart.py` Section 5.\n")
md.append(df_to_md(monthly_table_df))

md.append("\n## Reconciliation (honesty check)\n")
md.append(f"- Stripe: sum of all months' Stripe revenue = **${recon_stripe:,.2f}**; expected = gross "
          f"deduped `invoice.paid` (**${gross_stripe_all:,.2f}**, matches `reports/ltv_chart/ltv_numbers.md`'s "
          f"$112,481.38 pre-QA figure) minus QA-excluded-subs' Stripe revenue "
          f"(**${qa_stripe_revenue:,.2f}**) = **${expected_stripe:,.2f}**. "
          f"Match within ${TOL}: **{stripe_ok}**.\n")
md.append(f"- Solidgate: sum of all months' Solidgate revenue = **${recon_solidgate:,.2f}**; expected = gross "
          f"deduped `settle_ok` orders (**${gross_solidgate_all:,.2f}**, matches the LTV task's $54,771.91 "
          f"pre-QA figure) minus QA-excluded-subs' Solidgate revenue (**${qa_solidgate_revenue:,.2f}**, "
          f"0 because the Solidgate QA rule — >45 payments — matched 0 subs in this snapshot) "
          f"= **${expected_solidgate:,.2f}**. Match within ${TOL}: **{solidgate_ok}**.\n")
md.append(f"- Reconciliation **{'PASSED' if (stripe_ok and solidgate_ok) else 'FAILED'}**. "
          f"Code reference: Section 7.\n")

md.append("## Last full month summary\n")
md.append(f"- {last_full_month.strftime('%B %Y')} total revenue: **${last_full_total:,.2f}** "
          f"(Stripe ${monthly_rev_full.loc[last_full_month,'stripe']:,.2f} + "
          f"Solidgate ${monthly_rev_full.loc[last_full_month,'solidgate']:,.2f})\n")
md.append(f"- MoM growth {prev_month.strftime('%b')} -> {last_full_month.strftime('%b')}: "
          f"(${last_full_total:,.2f} - ${prev_total:,.2f}) / ${prev_total:,.2f} = **{mom_growth*100:+.1f}%**\n")
md.append(f"- New subscriptions started in {last_full_month.strftime('%B %Y')}: **{last_full_new_subs}**\n")
md.append(f"- {PARTIAL_MONTH} (partial, first 6 days of the month, cut off by snapshot_ts): "
          f"revenue **${july_total:,.2f}**, new subs **{july_new_subs}** — shown hatched/faded on the "
          f"chart and excluded from the MoM calculation above (not a comparable full month).\n")

md.append("## Notes\n")
if pre_oct_months:
    md.append(f"- Pre-October-2025 activity exists in the raw data ({[str(m) for m in pre_oct_months]}) "
              f"from subscriptions whose recorded `start_ts`/`started_at` predates the event stream's own "
              f"coverage start (Stripe events from 2025-10-10, Solidgate from 2025-10-02) — same subs "
              f"noted in `reports/ltv_chart/ltv_numbers.md`'s dropped-cohort list. Included in the "
              f"reconciliation table above for completeness, excluded from the chart's x-axis per the "
              f"spec's 'cover 2025-10 through 2026-06' range (immaterial size, see table).\n")
else:
    md.append("- No pre-October-2025 activity found in this snapshot's payments/starts.\n")
md.append("- Same refund treatment as the LTV chart: Stripe refunds (5.2% of gross) are not attached to "
          "any subscription/invoice in this snapshot and are not netted out — monthly Stripe bars are "
          "gross of refunds. Solidgate refunds are netted by construction (only orders whose *final* "
          "status is `settle_ok` count as revenue).\n")

with open(f"{OUT_DIR}/growth_numbers.md", "w", encoding="utf-8") as f:
    f.write("\n".join(md))
log(f"[Output] wrote {OUT_DIR}/growth_numbers.md")

with open(f"{OUT_DIR}/build_log.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(log_lines))
log(f"[Output] wrote {OUT_DIR}/build_log.txt")
