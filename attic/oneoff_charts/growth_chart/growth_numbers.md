# Growth chart — numbers and references

Snapshot: `data/raw/*.parquet`, snapshot_ts = 2026-07-07T00:00:00Z (per `data/raw/SNAPSHOT_MANIFEST.json`). Built by `reports/growth_chart/build_growth_chart.py`, run via `.venv/Scripts/python.exe`. Money/QA extraction rules copied verbatim from `reports/ltv_chart/build_ltv_chart.py` Sections 1-5 (Stripe `invoice.paid` deduped by invoice id /100; Solidgate `settle_ok` orders deduped by order id /100; QA subs excluded); only the aggregation axis changed (calendar month instead of week-of-life).

N subscribers in universe (non-QA, known start date): **11305** (stripe=5570, solidgate=5735). QA excluded: 70 subs (stripe=70, solidgate=0).

## Monthly table

Code reference: Section 6, `monthly_rev` (revenue, grouped by `payment_ts.dt.to_period('M')` and `provider`) and `monthly_new_subs` (grouped by `start_ts.dt.to_period('M')`), both computed over the same `pay_all`/`subs_all` frames as `reports/ltv_chart/build_ltv_chart.py` Section 5.

| month | stripe | solidgate | total | new_subs | mom_pct | note |
|---|---|---|---|---|---|---|
| 2025-07 | 0.00 | 0.00 | 0.00 | 11 | nan | pre-Oct, off-chart |
| 2025-08 | 0.00 | 0.00 | 0.00 | 14 | nan | pre-Oct, off-chart |
| 2025-09 | 0.00 | 0.00 | 0.00 | 18 | nan | pre-Oct, off-chart |
| 2025-10 | 0.00 | 4647.00 | 4647.00 | 219 | nan |  |
| 2025-11 | 0.00 | 3943.70 | 3943.70 | 31 | -15.13 |  |
| 2025-12 | 6558.36 | 2914.19 | 9472.55 | 299 | 140.19 |  |
| 2026-01 | 2544.20 | 1389.60 | 3933.80 | 206 | -58.47 |  |
| 2026-02 | 4688.26 | 0.00 | 4688.26 | 322 | 19.18 |  |
| 2026-03 | 4007.88 | 0.00 | 4007.88 | 401 | -14.51 |  |
| 2026-04 | 12586.51 | 0.00 | 12586.51 | 1447 | 214.04 |  |
| 2026-05 | 27892.94 | 1063.05 | 28955.99 | 1799 | 130.06 |  |
| 2026-06 | 44279.53 | 25946.30 | 70225.83 | 5720 | 142.53 |  |
| 2026-07 | 7627.85 | 14868.07 | 22495.92 | 818 | nan | partial (first 6 days) |

## Reconciliation (honesty check)

- Stripe: sum of all months' Stripe revenue = **$110,185.53**; expected = gross deduped `invoice.paid` (**$112,481.38**, matches `reports/ltv_chart/ltv_numbers.md`'s $112,481.38 pre-QA figure) minus QA-excluded-subs' Stripe revenue (**$2,295.85**) = **$110,185.53**. Match within $0.01: **True**.

- Solidgate: sum of all months' Solidgate revenue = **$54,771.91**; expected = gross deduped `settle_ok` orders (**$54,771.91**, matches the LTV task's $54,771.91 pre-QA figure) minus QA-excluded-subs' Solidgate revenue (**$0.00**, 0 because the Solidgate QA rule — >45 payments — matched 0 subs in this snapshot) = **$54,771.91**. Match within $0.01: **True**.

- Reconciliation **PASSED**. Code reference: Section 7.

## Last full month summary

- June 2026 total revenue: **$70,225.83** (Stripe $44,279.53 + Solidgate $25,946.30)

- MoM growth May -> Jun: ($70,225.83 - $28,955.99) / $28,955.99 = **+142.5%**

- New subscriptions started in June 2026: **5720**

- 2026-07 (partial, first 6 days of the month, cut off by snapshot_ts): revenue **$22,495.92**, new subs **818** — shown hatched/faded on the chart and excluded from the MoM calculation above (not a comparable full month).

## Notes

- Pre-October-2025 activity exists in the raw data (['2025-07', '2025-08', '2025-09']) from subscriptions whose recorded `start_ts`/`started_at` predates the event stream's own coverage start (Stripe events from 2025-10-10, Solidgate from 2025-10-02) — same subs noted in `reports/ltv_chart/ltv_numbers.md`'s dropped-cohort list. Included in the reconciliation table above for completeness, excluded from the chart's x-axis per the spec's 'cover 2025-10 through 2026-06' range (immaterial size, see table).

- Same refund treatment as the LTV chart: Stripe refunds (5.2% of gross) are not attached to any subscription/invoice in this snapshot and are not netted out — monthly Stripe bars are gross of refunds. Solidgate refunds are netted by construction (only orders whose *final* status is `settle_ok` count as revenue).
