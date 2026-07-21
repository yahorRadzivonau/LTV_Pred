# LTV pipeline — module map

Refactored 2026-07-11: the money/cohort logic that had spread across ~10 web_* scripts
is now consolidated into one `ltv/` package. **No output number changed** — see the
bit-for-bit gate below.

## The consolidated core — `ltv/`

| Module | Owns | Key surface |
|---|---|---|
| `ltv/config.py` | Every tunable constant, in one place. Model-math constants (`K_SHRINK=800`, `HMAX=52`) are **re-exported** from `models/common.py`, never redefined. | `BASE_PRICE`, `TARGET_STRIPE_PRICE_ID`, `PAID_EVENTS`, `SNAPSHOT_TS`, `POP_START_DATE`, `WHITELIST_FUNNELS`, `ORGANIC_SENTINELS`, `RELIABILITY_N_THRESHOLD`, `TAPER_WIDTH`, `MIN_FIRST_PAYERS`, `MIN_MATURE_REBILL`, `LOW_N_CELL_THRESHOLD`, `H_EXT`, `HORIZONS_REPORT`, `LOO_BAND_PCT` |
| `ltv/revenue.py` | **The one revenue rule.** Captured money only (renewed + trial_converted − refunds, refund deduped + capped, no billing_issue); base/ups split. Pure functions. | `paid_events`, `split_base_ups`, `dedup_refunds`, `subscription_net_revenue`, `event_level_net_events` |
| `ltv/cohorts.py` | Population + denominators. Captures the previously **uncommitted** glue (the 5406 build, cus↔email map) so the pipeline is reproducible from raw. | `build_cus_email_map`, `build_population_5406`, `add_is_payer`, `denominators`, `filter_golden_sub_shape` |

## The frozen math — DO NOT CHANGE (unchanged by this refactor)

| File | Why frozen |
|---|---|
| `models/common.py` | `K_SHRINK=800`, `HMAX`, `empirical_hbase`, `direct_survival` — single source of the model-math constants. |
| `models/map_model.py` | `predict()`, `MAP_LEVERS` order, `MAP_CLIP`, the `hr = exp(log_hr·n/(n+K_SHRINK))` line. |
| Calibration values | `alpha=-0.3933, beta=0.0675, k_max_reliable=11` are **recomputed live** each run (weighted lstsq), not hardcoded — the refactor keeps them recomputed. |

## Live deliverable chain (produces the two CSV tables)

```
ltv.cohorts.build_population_5406()  ─┐  (was ad-hoc; now committed)
ltv.cohorts.build_cus_email_map()    ─┤
                                      ▼
web_person_level_revenue.py  → _tmp_revenue_events_with_email.parquet   [uses ltv.revenue]
web_person_level_tables.py   → _tmp_pop_final.parquet (attributes)
web_person_level_fix.py      → _tmp_{pop_fixed,subs_fixed,cum_*}.parquet  [uses ltv.revenue + ltv.cohorts]
web_person_level_tables_v2.py→ reports/web_model/05_person_level_clean/table_{A,B}_*_v2.csv  [uses ltv.config]
```

Calibration path (unchanged math): `compare_map_to_local_sql_style_may_04_10.py` (helpers)
→ `web_hbase_smooth_correction.py` (2-param fit) → `web_boss_charts_ab.py` (charts).
Both calibration scripts now pull their thresholds (`MIN_FIRST_PAYERS`, `MIN_MATURE_REBILL`,
`RELIABILITY_N_THRESHOLD`, `TAPER_WIDTH`, `LOO_BAND_PCT`) from `ltv.config`; the fitted
`alpha/beta/k_max_reliable` are still recomputed live (verified unchanged: `-0.3933 / 0.0675 / 11`).

## Bit-for-bit guarantee

- `reports/reconcile_baseline.json` — the frozen reference outputs.
- `reconcile.py` — recomputes them from current code + data and diffs. **Run after every change.**
  - anchor 05-04: per-sub rebill7 `$55.162174` (N=92); per-payer base+ups `$67.804382` (89 payers)
  - calibration: `alpha=-0.3933196671, beta=0.0674853235, k_max_reliable=11`
  - both deliverable CSVs by md5
- `tests/test_ltv.py` — 8 synthetic tests (base/ups split, refund dedup, refund cap→no-negative,
  proportional allocation, running-cap, organic sentinels, is_payer). Dependency-free runner.

```
.venv/Scripts/python.exe reconcile.py        # -> ALL 10 CHECKS PASS
.venv/Scripts/python.exe tests/test_ltv.py   # -> 8/8 tests passed
```

Note: per-payer anchor is **$67.80** (post refund-fix). The `$64.72` quoted earlier was the
pre-fix value; bit-for-bit means reproducing what the code produces today.

## Removed (2026-07-11 cleanup — verified no references before deletion)

- `compare_map_to_local_sql_style_may_04_10 (1..4).py` — 4 numbered editor copies of the helper (624 lines vs the 630-line canonical; defined nothing the canonical lacked). Python can't import a name with spaces/parens, so they were unreachable.
- `web_baseline_may_cohort_chart (2).py` — byte-identical clone of `web_baseline_may_cohort_chart.py`.
- `archive/forecast_demo.py` — dead, and the only place the divergent `K_SHRINK=400` still lived (a latent mine).

Still present, superseded but harmless (not imported by the live chain): `compare_map_to_sql_may_04_10.py`
(BigQuery-SQL-export variant), the various `diag_*`/`debug_*` one-offs.
