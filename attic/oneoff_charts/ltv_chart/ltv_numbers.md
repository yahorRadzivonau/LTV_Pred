# LTV chart — numbers and references

Snapshot: `data/raw/*.parquet`, snapshot_ts = 2026-07-07T00:00:00Z (per `data/raw/SNAPSHOT_MANIFEST.json`). Built by `reports/ltv_chart/build_ltv_chart.py`, run via `.venv/Scripts/python.exe`.

N subscribers in curve universe (non-QA, known start date): **11305** (stripe=5570, solidgate=5735). QA/test excluded: 70 subs (stripe daily-interval or utm_source='test': 70; solidgate >45-payments fallback rule: 0).

## LTV at key weeks

| Week | Cumulative $/subscriber | Status | N (denominator) | Code reference |
|---|---|---|---|---|
| 4 | $17.75 | fact | 3858 | `overall_curve` (build_ltv_chart.py, `cum_curve_for()`, Section 6) |
| 12 | $32.74 | fact | 1208 | `overall_curve` (build_ltv_chart.py, `cum_curve_for()`, Section 6) |
| 26 | $46.02 | fact | 414 | `overall_curve` (build_ltv_chart.py, `cum_curve_for()`, Section 6) |
| 52 | $54.38 | extrapolated | - | `fit_decay_smoothed()` + `extrapolate_from_anchor()` (Section 7): geometric decay fit on 4-week-smoothed increments, weeks 20-35, a=5.2598, r=0.9245, anchored at week 35 (wk36-37 excluded from anchor/fit as a cohort-timing spike) |

## Fact curve (overall, pooled cohorts)

Last reliable observed week (n >= 100): **37**. Denominator per week = subscribers whose age at snapshot_ts >= (week+1)*7 days (i.e. they have fully lived through that week — avoids right-censoring bias). Query/code: Stripe revenue = `invoice.paid` deduped by invoice id (Section 3); Solidgate revenue = orders with final status `settle_ok` under `$.invoices.*.orders.*`, deduped by order id keeping latest `updated_at` (Section 4); subscription start = `customer.subscription.created` event ts (Stripe, Section 2) / `$.subscription.started_at` (Solidgate, Section 4).

Note: weeks 36-37 are still real observed fact (shown solid on the chart, n>=100 throughout) but are **excluded from the trend extrapolation's anchor and fit window** — they carry a visible jump ($51.34 at wk35 -> $59.21 at wk37) driven by the large Oct'25 cohort's payment timing landing in that window, not a change in the underlying trend. See Section 7 in the code.

| week | n | cum_avg |
|---|---|---|
| 0.0000 | 7301.0000 | 5.9381 |
| 1.0000 | 6126.0000 | 10.7321 |
| 2.0000 | 5323.0000 | 14.7008 |
| 3.0000 | 4527.0000 | 16.4884 |
| 4.0000 | 3858.0000 | 17.7486 |
| 5.0000 | 3642.0000 | 19.9303 |
| 6.0000 | 3321.0000 | 21.8571 |
| 7.0000 | 2848.0000 | 22.9548 |
| 8.0000 | 2553.0000 | 23.7642 |
| 9.0000 | 2223.0000 | 26.8462 |
| 10.0000 | 1847.0000 | 29.3775 |
| 11.0000 | 1539.0000 | 31.4827 |
| 12.0000 | 1208.0000 | 32.7404 |
| 13.0000 | 999.0000 | 34.5362 |
| 14.0000 | 856.0000 | 36.9374 |
| 15.0000 | 808.0000 | 37.7365 |
| 16.0000 | 766.0000 | 37.8326 |
| 17.0000 | 730.0000 | 37.9726 |
| 18.0000 | 716.0000 | 38.4941 |
| 19.0000 | 699.0000 | 38.6608 |
| 20.0000 | 652.0000 | 39.1021 |
| 21.0000 | 592.0000 | 39.7186 |
| 22.0000 | 517.0000 | 42.3389 |
| 23.0000 | 454.0000 | 44.4167 |
| 24.0000 | 428.0000 | 45.4691 |
| 25.0000 | 424.0000 | 45.7566 |
| 26.0000 | 414.0000 | 46.0216 |
| 27.0000 | 370.0000 | 46.7389 |
| 28.0000 | 332.0000 | 47.8276 |
| 29.0000 | 288.0000 | 49.0250 |
| 30.0000 | 257.0000 | 50.1149 |
| 31.0000 | 257.0000 | 50.1149 |
| 32.0000 | 256.0000 | 49.8225 |
| 33.0000 | 237.0000 | 50.5060 |
| 34.0000 | 229.0000 | 50.3498 |
| 35.0000 | 206.0000 | 51.3377 |
| 36.0000 | 158.0000 | 58.5210 |
| 37.0000 | 156.0000 | 59.2073 |
| 38.0000 | 91.0000 | 65.7444 |
| 39.0000 | 19.0000 | 81.5474 |
| 40.0000 | 11.0000 | 49.9682 |
| 41.0000 | 11.0000 | 49.9682 |
| 42.0000 | 11.0000 | 49.9682 |
| 43.0000 | 11.0000 | 51.7855 |
| 44.0000 | 11.0000 | 51.7855 |
| 45.0000 | 11.0000 | 51.7855 |

## Per-cohort N and last reliable week

| Cohort (start month) | N | Last reliable week |
|---|---|---|
| 2025-10 | 219 | 38 |
| 2025-11 | 31 | 32 |
| 2025-12 | 299 | 29 |
| 2026-01 | 206 | 23 |
| 2026-02 | 322 | 20 |
| 2026-03 | 401 | 16 |
| 2026-04 | 1447 | 12 |
| 2026-05 | 1799 | 8 |
| 2026-06 | 5720 | 3 |
| 2026-07 | 818 | None |

## Calibration (band derivation)

Method: cohorts with >=26 weeks of observed fact **and N >= 100** are truncated to their first 12 weeks, the same geometric-decay fit/extrapolation method (Section 7/8 code, `compute_calibration()`) is applied blindly forward to week 26, and the result is compared to that cohort's real week-26 value. Band = max absolute relative error observed across qualifying cohorts.

N filter: cohorts with N < 100 are excluded from calibration as too small to trust a truncated-then-refit backtest on (excluded: [('2025-11', 31)]). Threshold tried first: N>=100 — 2 cohorts qualified, no fallback needed.

| cohort | n | actual_wk26 | pred_wk26_from_wk12 | rel_err |
|---|---|---|---|---|
| 2025-10 | 219 | 47.1364 | 43.8593 | -0.0695 |
| 2025-12 | 299 | 39.3212 | 39.0613 | -0.0066 |

**Band used in chart: ±7.0%** (prior build: ±53.3%, computed before the N-filter was applied and skewed wide by the N=31 2025-11 cohort)

## Refund handling (honesty disclosure)

- Stripe `charge.refunded`: 476 events, $5,802.31 total (5.16% of gross Stripe `invoice.paid` revenue of $112,481.38). `refund.created` (45 events) was dropped as a duplicate representation of a subset of `charge.refunded` (verified 45/45 payment_intent overlap).

- Attach attempt: `invoice.paid` never carries `payment_intent` (0/15088 populated); `charge.succeeded` and `payment_intent.succeeded` never carry an `invoice` field (0/11847, 0/12981) in this snapshot. No available join path resolves a refund to a subscription or invoice — consistent with `reports/SNAPSHOT_SUMMARY.md` Section 5's "100% orphan rate" finding. Per the build spec's fallback rule, refunds are **not** netted into the per-subscriber curve above; the curve is gross-of-refunds for Stripe. This is stated on the chart caption as a footnote, not silently absorbed.

- Solidgate: orders that transition to a terminal `refunded` status are excluded from revenue by construction (dedup keeps the latest status per order id; only orders whose *final* status is `settle_ok` count as revenue) — Solidgate refunds are therefore netted out, unlike Stripe's.

## QA / test exclusion detail

- Stripe: `interval_unit='day'` (65 subs) OR `utm_source='test'` (5 subs) from `data/raw/stripe_subscriptions.parquet` — union = 70 subs. Code: Section 1.

- Solidgate: no utm field on the subscription object itself; used the spec's own ambiguous-case fallback ("exclude subs with >45 payments") — 0 subs excluded. Code: Section 4, `qa_solidgate_ids`.

## Data note: Solidgate order status vocabulary

The spec's literal `status='success'` string does not occur in this snapshot. Observed statuses: `auth_failed`, `processing`, `auth_ok`, `settle_ok`, `refunded`, `void_ok`. `settle_ok` (payment captured/settled) was used as the revenue-recognized state — the closest analog to Stripe's `invoice.paid`. `auth_ok` (authorization/hold only, not yet captured) was excluded from revenue.
