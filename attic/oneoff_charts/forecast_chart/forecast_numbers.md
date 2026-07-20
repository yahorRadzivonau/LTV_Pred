# Forecast chart — numbers and references

Snapshot: `data/raw/*.parquet`, snapshot_ts = 2026-07-07T00:00:00Z. Built by `reports/forecast_chart/build_forecast_chart.py`, run via `.venv/Scripts/python.exe`. Money/QA extraction (Sections 1-5) copied verbatim from `reports/ltv_chart/build_ltv_chart.py`.

N subscribers (non-QA, known start): **11305**; alive at snapshot (no death event ever recorded): **5914**. Death = `customer.subscription.deleted` (Stripe) / first `cancel`/`expire` event (Solidgate), timestamped -- Section 1-5/9.

## Method summary

1. **Baseline**: alive-at-snapshot subs (no death event ever) project forward via the pooled historical increment curve INCR(w) -- identical construction to `reports/ltv_chart/build_ltv_chart.py` (ELIGIBLE_MIN_N=100, anchor week=35, decay fit on weeks 20-35: a=5.2598, r=0.9245 -- matches `reports/ltv_chart/ltv_numbers.md` exactly). Each subscriber's future weeks are dated from their own `start_ts` and bucketed into the calendar month they land in. Code: Section 9, `project_baseline()`.

2. **New-acquisition scenarios**: hypothetical monthly cohorts assumed to start at their acquisition month's midpoint (day 15), projected via a **separate** fresh-cohort rate curve (2026-04+05 pooled, N=3246, decay fit on weeks 2-8: a=7.2294, r=0.7635, week0=$3.26) -- current plan/price mix, not the historical pooled mix. Code: Section 8/10, `FRESH_INCR()` / `project_acquisition()`.

3. **July fact/forecast split**: Jul 1-6 actual collected revenue ($22,495.92, matches `reports/growth_chart/growth_numbers.md`) shown as fact; baseline/scenario projections only cover future weeks (after each subscriber's own week-of-life at snapshot_ts), so July's forecast portion is inherently Jul 7-31 only -- no double counting. Code: Section 11.

## Calibration backtest (mandatory honesty step)

Method: stand at **2026-05-01**, take subs started before that date and alive then (death_ts is null or >= cutoff) -- N=1614 of 2968 started-before-cutoff subs. Project May+June revenue using a curve trained ONLY on pre-cutoff payments (no lookahead): fact max week (n>=100) = 28, decay fit on weeks 13-28: a=1.9618, r=0.9703. Code: Section 12.

- Predicted May+June rebill revenue: **$28,738.30**

- Actual May+June rebill revenue (same started-before-cutoff cohort): **$26,452.73**

- **Error: +8.6%** (threshold ±25% — PASSED)

- This error is applied as the **±8.6% band** around the baseline layer in the chart.

## Monthly table (USD)

| month | fact | baseline_forecast | baseline_total | scenario_A_total | scenario_B_total | scenario_C_total |
|---|---|---|---|---|---|---|
| 2026-07 | 22495.92 | 42386.23 | 64882.15 | 64882.15 | 127841.13 | 265063.01 |
| 2026-08 | 0.00 | 44166.09 | 44166.09 | 44166.09 | 152959.84 | 826383.65 |
| 2026-09 | 0.00 | 38454.06 | 38454.06 | 38454.06 | 185257.51 | 1259158.16 |
| 2026-10 | 0.00 | 18536.80 | 18536.80 | 18536.80 | 200531.17 | 1614537.58 |

`baseline_total` = fact + baseline_forecast. `scenario_X_total` = baseline_total + that scenario's acquisition-layer revenue. Scenario A (stopped) therefore equals `baseline_total` exactly by construction.

## Scenario assumptions

- **A (stopped)**: 0 new subs/month, Jul-Oct.

- **B (flat)**: 5,720 new subs/month (June 2026 actual level, per `reports/growth_chart/growth_numbers.md`), Jul-Oct.

- **C (growth)**: continues the May->June new-subs growth rate = (5,720-1,799)/1,799 = **218.0% MoM** for 2 more months (July, August), then flat (September, October = August's level). Monthly new-subs assumption: Jul=18,187, Aug=57,826, Sep=57,826, Oct=57,826.

  **Caveat**: this is a literal, unmoderated continuation of a single MoM data point (May->June), which was itself likely inflated by a one-time product/campaign launch (see `reports/growth_chart/growth_numbers.md`'s +142.5% MoM note). The resulting Sep/Oct new-subs level (~10x June) is an aggressive upper-bound scenario, not a realistic prediction -- shown exactly as specified, flagged rather than softened.

## Reconciliation / consistency checks

- July fact ($22,495.92) matches `reports/growth_chart/growth_numbers.md`'s partial-July figure exactly (same `pay_all` extraction).

- Baseline decay parameters (a=5.2598, r=0.9245, anchor week 35) match `reports/ltv_chart/ltv_numbers.md` exactly (same code, same inputs).

- May 2026 / June 2026 new-subs counts (1,799 / 5,720) match `reports/growth_chart/growth_numbers.md` exactly.
