# CODE_SKELETON

Скелетная инвентаризация живого кода (`core/`, `ios/`, `web/`, `tests/`) для
планирования документации. Только факты из кода: докстринг, def/class,
собственные импорты, литералы путей чтения/записи, наличие `__main__`.
Без разбора логики и без оценок.

`attic/`, `data/`, `reports/`, `docs/` — вне области.

Отдельно: `pipeline/pull.py` существует вне новой структуры (неперенесённый
хвост Фазы C, см. `TODO.md`) — не в заявленной области (`core/ios/web/tests`),
здесь не разбирается.

---

## core/

### core/__init__.py
Пустой (только пакетный маркер).

### core/common.py
**Докстринг:** «Общие константы и функции для всех LTV-моделей (empirical, logreg, hybrid). Перенесено дословно (без изменения формул) из archive/backtest.py, archive/unified_backtest.py и archive/unified_hybrid.py».
**def/class:**
- `load_matrix(path: Path = MATRIX_PATH) -> pd.DataFrame`
- `direct_survival(rows)`
- `detect_crash(per)`
- `empirical_hbase(rows, min_risk=30)`
- `select_apps(mx)`
**Свои импорты:** нет (сам является источником для core.map_model и др.).
**Читает:** `data/se_training.parquet` (константа `MATRIX_PATH`, аргумент по умолчанию `load_matrix`).
**Пишет:** —
**`__main__`:** нет.

### core/map_model.py
**Докстринг:** «МОДЕЛЬ MAP — карта множителей риска (h_base x residual-цепочка множителей x hr). Логика взята из MAP_MODEL_SPEC.md, Шаги 1-3».
**def/class:**
- `_prep(mx)`
- `_collapse_small(df, small_categories)`
- `_clean_zone(mx_prepped, crash_apps)`
- `fit(mx, apps_info)`
- `_subs_with_levers(state, app_rows)`
- `_combo_mult(state, row)`
- `personal_multiplier(state, app_rows)`
- `predict(state, app_rows, weeks)`
**Свои импорты:** `core.common` (HMAX, K_SHRINK, MIN_PAYERS, MIN_MATURE, HORIZONS, PRED_WEEKS_LIST, empirical_hbase).
**Читает:** — (принимает `mx`/`apps_info` как аргументы).
**Пишет:** —
**`__main__`:** нет.

### core/web_calibration.py
**Докстринг:** «Local-only comparison of the MAP forecast with a SQL-style empirical benchmark for the 2026-05-04..2026-05-10 cohort. No BigQuery export is required».
**def/class:**
- `require_columns(df, columns, label) -> None`
- `first_existing_column(df, candidates) -> str | None`
- `load_golden() -> pd.DataFrame`
- `filter_provider_and_app(golden) -> tuple`
- `subscription_start_table(golden) -> pd.DataFrame`
- `choose_price_cohort(golden, cohort_start_subs) -> tuple[pd.Index, pd.DataFrame, str]`
- `local_paid_events(golden, subs) -> pd.DataFrame`
- `load_web_matrix() -> pd.DataFrame`
- `visible_at_week_n(matrix, n) -> pd.DataFrame`
- `sql_style_summary(paid, denominator_subs, max_mature_rebill) -> pd.DataFrame`
- `raw_map_ltv(predicted_survival, arpu_by_rebill) -> pd.Series`
- `fit_web_ios_calibration(cohorts, h_ios, reliability_n_threshold) -> tuple[float, float, int]`
- `main() -> None`
**Свои импорты:** `core.common` (HMAX), `core` (common, map_model) внутри `main()`.
**Читает:** `data/golden/golden_all.parquet` (`GOLDEN_PATH`), `data/golden/golden_all_se_training.parquet` (`WEB_MATRIX_PATH`).
**Пишет** (только внутри `main()`, за `__main__`-гвардом): `reports/web_golden/02_map_vs_sql_cohort/{cohort_coverage.csv, cohort_membership_audit.csv, local_numbered_payments.csv, local_full_payer_fact.csv, common_cohort_fact.csv, map_vintage_predictions.csv, local_fact_vs_map.csv, retention_local_fact_vs_map.png, ltv_local_fact_vs_map.png, map_to_local_sql_style_may_04_10_error_chart.png}` (`REPORTS_DIR`, также создаётся модульно через `REPORTS_DIR.mkdir(...)` вне гварда).
**`__main__`:** да.

Как библиотека, `core.web_calibration` импортируется из: `web/calibration/web_hbase_smooth_correction.py`, `web/calibration/web_boss_charts_ab.py`, `web/golden/web_person_level_tables.py`, `web/golden/web_person_level_tables_v2.py`, `web/golden/reconcile.py`, `web/v2/build_tables.py`, `web/v2/build_triple_report_fixed.py`.

---

## ios/pipeline/

### ios/pipeline/__init__.py
Пустой.

### ios/pipeline/se_training.py
**Докстринг:** нет (файл начинается сразу с импортов).
**def/class:**
- `outcome(r)`
- `interval(g)`
**Свои импорты:** нет.
**Читает:** `data/ios_events.parquet` (`SRC`).
**Пишет:** `data/se_training.parquet` (`OUT`, через `mx.to_parquet(OUT)`).
**`__main__`:** нет (выполняется построчно на верхнем уровне).

---

## ios/validation/

### ios/validation/__init__.py
Пустой.

### ios/validation/compare.py
**Докстринг:** «Прогоняет LTV-модели (empirical, logreg, hybrid, hybrid_v2, map) на одной и той же обучающей матрице... пишет reports/ios/comparison.md, reports/ios/comparison_detail.csv».
**def/class:**
- `score_model(model, state, apps, facts, matures, crash_flag, mx)`
- `write_detail_csv(all_detail_rows)`
- `write_report(results)`
- `print_verdict(results)`
- `main()`
**Свои импорты:** `core` (common, map_model), `ios.alt_models` (empirical, hybrid, hybrid_v2, logreg).
**Читает:** `data/se_training.parquet` (через `common.load_matrix()`).
**Пишет:** `reports/ios/comparison.md` (`REPORT_PATH`), `reports/ios/comparison_prev.md` (`REPORT_PREV_PATH`), `reports/ios/comparison_detail.csv` (`DETAIL_PATH`), `reports/ios/comparison_detail_prev.csv` (`DETAIL_PREV_PATH`).
**`__main__`:** да.

### ios/validation/calendar_backtest.py
**Докстринг:** «Календарный бэктест — симуляция прода: встаём в прошлую дату T, обучаемся ТОЛЬКО на том, что случилось до T... в отличие от степенного бэктеста compare.py».
**def/class:**
- `_compute_fingerprint()`
- `check_fingerprint()`
- `inventory(mx)`
- `print_inventory(rows)`
- `weeks_obs_at(df, T)`
- `fit_state_for_T(t_str, mx)`
- `evaluate(mx, inv_rows)`
- `aggregate_by_T(per_app_rows)`
- `step_test_baseline()`
- `write_report(inv_rows, per_app_rows, agg, baseline)`
- `main()`
**Свои импорты:** `core` (common, map_model), `ios.alt_models` (empirical).
**Читает:** `data/se_training.parquet` (через `common.load_matrix()`), `reports/ios/comparison_detail.csv` (`DETAIL_PATH`, вывод `compare.py`).
**Пишет:** `reports/ios/calendar_backtest.md` (`OUT_PATH`); кэш `reports/ios/calendar_cache/` (`CACHE_DIR`, `_fingerprint.txt` + `map_*.pkl`/`emp_*.pkl`).
**`__main__`:** да.
**Экспортирует для других модулей:** `CUTOFFS`, `inventory()` (импортируются `diag_portfolio_drift.py`).

### ios/validation/validate_loo.py
**Докстринг:** «LOO-валидация модели map (Задача A + Задача A2). Задача A: проверяет, что отрыв map от других моделей в reports/ios/comparison.md не объясняется тем, что множители рычагов учились в том числе на юзерах предсказываемого аппа».
**def/class:**
- `_compute_fingerprint()`
- `check_fingerprint()`
- `normal_model_column(model_name)`
- `overall_by_horizon(results, idx=0)`
- `build_states(mx, apps_info, clean_apps, model_name)`
- `score_loo(predict_fn, states, mx, clean_apps, facts, matures)`
- `write_old_section(lines, map_results, loo_results, empirical_overall)`
- `write_fair_section(lines, loo_results, emp_loo_results)`
- `hbase_support_counts(mx, steps, min_risk=30)`
- `write_sign_section(lines, mx, map_loo_detail, map_states)`
- `main()`
**Свои импорты:** `core` (common, map_model), `ios.alt_models` (empirical).
**Читает:** `data/se_training.parquet` (через `common.load_matrix()`), `reports/ios/comparison_detail.csv` (`DETAIL_PATH`).
**Пишет:** `reports/ios/loo_map.md` (`OUT_PATH`); кэш `reports/ios/loo_cache/` (`CACHE_DIR`, `_fingerprint.txt` + `<prefix><app>.pkl`).
**`__main__`:** да.

### ios/validation/segment_backtest.py
**Докстринг:** «Задача D: сегментный бэктест — точность map ВНУТРИ аппа по гео-сегментам... models/map_model.py НЕ меняется — используются только его fit()/personal_multiplier()/_prep()/_collapse_small() как есть».
**def/class:**
- `app_hr(state, app_rows, weeks)`
- `segment_curve(state, seg_rows, hr)`
- `geo_groups(state, app_rows)`
- `sanity_check(mx, clean_apps, map_state)`
- `build_segments(mx, clean_apps, apps_facts, map_state, emp_state)`
- `metric_a(segments)`
- `metric_b(segments)`
- `metric_c(segments)`
- `write_report(segments, sanity_worst, sanity_app, met_a, met_b, met_c)`
- `main()`
**Свои импорты:** `core` (common, map_model), `ios.alt_models` (empirical).
**Читает:** `data/se_training.parquet` (через `common.load_matrix()`).
**Пишет:** `reports/ios/segment_backtest.md` (`OUT_PATH`).
**`__main__`:** да.

### ios/validation/diag_portfolio_drift.py
**Докстринг:** «Диагностика систематического завышения на молодых аппах, найденного календарным бэктестом... Только диагностика: НИЧЕГО не фиксит».
**def/class:**
- `build_portfolio_table(mx)`
- `trend_regression(df)`
- `plot_scatter(df, slope, intercept, r, d_reg, out_path)`
- `write_report(df, slope, intercept, r, d_reg, calendar_apps, trend_pp, surv_pp, plot_path)`
- `main()`
**Свои импорты:** `core.common`, `ios.validation.calendar_backtest` (CUTOFFS, inventory).
**Читает:** `data/se_training.parquet` (через `common.load_matrix()`).
**Пишет:** `reports/ios/portfolio_drift.md` (`OUT_PATH`), `reports/ios/plots/portfolio_drift.png` (`PLOTS_DIR`).
**`__main__`:** да.

### ios/validation/plot_forecasts.py
**Докстринг:** «Задача C: визуальные графики "прогноз vs факт" для модели map. Только matplotlib, сохранение в PNG (backend Agg)».
**def/class:**
- `pick_apps(clean_apps, counts)`
- `censored_fact(fact, matures, min_mature=CENSOR_MIN_MATURE)`
- `plot_app(app, app_rows, fact, matures, map_state, emp_state, subs_count)`
- `plot_portfolio(clean_apps, facts, mx, map_state)`
- `main()`
**Свои импорты:** `core` (common, map_model), `ios.alt_models` (empirical).
**Читает:** `data/se_training.parquet` (через `common.load_matrix()`).
**Пишет:** `reports/ios/plot_forecasts.md` (`OUT_PATH`), `reports/ios/plots/{app}.png` и `reports/ios/plots/portfolio.png` (`PLOTS_DIR`).
**`__main__`:** да.

---

## ios/alt_models/

### ios/alt_models/__init__.py
Пустой.

### ios/alt_models/empirical.py
**Докстринг:** «МОДЕЛЬ A — эмпирическая (эталон, самая устойчивая на дальних горизонтах)... Перенесено дословно из archive/backtest.py».
**def/class:**
- `fit(mx)`
- `predict(state, app_rows, weeks)`
**Свои импорты:** `core.common` (HMAX, K_SHRINK, empirical_hbase).
**Читает:** —
**Пишет:** —
**`__main__`:** нет.

### ios/alt_models/logreg.py
**Докстринг:** «МОДЕЛЬ B — logreg (чистая версия, без sample_weight по аппам)... Перенесено дословно из archive/unified_backtest.py».
**def/class:**
- `_fit_tops(mx)`
- `_prep_features(mx, tops)`
- `fit(mx_clean)`
- `_precompute_app(state, app_rows)`
- `predict(state, app_rows, weeks)`
**Свои импорты:** `core.common` (HMAX, K_SHRINK).
**Читает:** —
**Пишет:** —
**`__main__`:** нет.

### ios/alt_models/hybrid.py
**Докстринг:** «МОДЕЛЬ C — гибрид (рекомендуемая production-модель)... Перенесено дословно из archive/unified_hybrid.py».
**def/class:**
- `_fit_tops(mx)`
- `_prep_features(mx, tops)`
- `_portfolio_step_hazard_from_model(model, train)`
- `fit(mx_clean)`
- `_precompute_app(state, app_rows)`
- `predict(state, app_rows, weeks)`
**Свои импорты:** `core.common` (HMAX, K_SHRINK, RR_CLIP, empirical_hbase).
**Читает:** —
**Пишет:** —
**`__main__`:** нет.

### ios/alt_models/hybrid_v2.py
**Докстринг:** «МОДЕЛЬ C2 — гибрид v2 (rr по чистым ступеням + исправленная нормировка)... ИСПРАВЛЕН БАГ НОРМИРОВКИ».
**def/class:**
- `_fit_tops(mx)`
- `_prep_features(mx, tops)`
- `_portfolio_step_hazard_from_model(model, prepped_clean)`
- `fit(mx_clean)`
- `_precompute_app(state, app_rows)`
- `predict(state, app_rows, weeks)`
**Свои импорты:** `core.common` (HMAX, K_SHRINK, RR_CLIP, empirical_hbase).
**Читает:** —
**Пишет:** —
**`__main__`:** нет.

---

## web/golden/

### web/golden/ltv/__init__.py
Докстринг: «ltv: consolidated core of the web-subscription LTV pipeline» (config/revenue/cohorts перечислены как модули). Def/class нет.

### web/golden/ltv/config.py
**Докстринг:** «Single home for every LTV-pipeline constant. Model-math constants re-exported from core.common, never redefined; web-layer constants defined once here».
**def/class:** нет (только константы + ре-экспорт).
**Свои импорты:** `core.common` (K_SHRINK, HMAX).
**Читает / Пишет:** —

### web/golden/ltv/cohorts.py
**Докстринг:** «Population construction + denominators for the web LTV pipeline. Captures logic that previously existed ONLY as ad-hoc shell commands».
**def/class:**
- `build_cus_email_map(stripe_subscriptions_path="data/raw/stripe_subscriptions.parquet", solidgate_events_path="data/raw/solidgate_events.parquet") -> pd.DataFrame`
- `_is_organic(funnel) -> bool`
- `build_population_5406(person_dim_path="data/raw/bq_appsflyer_person_dim_2026-07-11.parquet") -> pd.DataFrame`
- `add_is_payer(pop, total_col="total") -> pd.DataFrame`
- `denominators(cell) -> dict`
- `filter_golden_sub_shape(golden) -> pd.DataFrame`
**Свои импорты:** `ltv.config` (ORGANIC_SENTINELS, POP_START_DATE, TARGET_STRIPE_PRICE_ID, WHITELIST_FUNNELS).
**Читает:** `data/raw/stripe_subscriptions.parquet`, `data/raw/solidgate_events.parquet` (аргументы по умолчанию `build_cus_email_map`), `data/raw/bq_appsflyer_person_dim_2026-07-11.parquet` (аргумент по умолчанию `build_population_5406`).
**Пишет:** —
**`__main__`:** нет.

### web/golden/ltv/revenue.py
**Докстринг:** «The ONE revenue rule for the web LTV pipeline. Captured money only... reproduce web_person_level_fix.py bit-for-bit; see reconcile.py».
**def/class:**
- `split_base_ups(paid, amount_col="amt") -> pd.DataFrame`
- `paid_events(golden) -> pd.DataFrame`
- `dedup_refunds(golden) -> pd.DataFrame`
- `subscription_net_revenue(golden) -> pd.DataFrame`
- `_apply_running_cap(g) -> pd.DataFrame`
- `event_level_net_events(golden) -> pd.DataFrame`
**Свои импорты:** `ltv.config` (BASE_PRICE, PAID_EVENTS, REFUND_EVENT).
**Читает / Пишет:** — (чистые функции, DataFrame-in/DataFrame-out).
**`__main__`:** нет.

### web/golden/web_person_level_revenue.py
**Докстринг:** «Unified per-person revenue module: real captured money only... Dunning-reduced successful renewals ARE real (base). subscription_refunded is subtracted».
**def/class:**
- `revenue_events(golden)`
**Свои импорты:** `ltv.revenue`, `ltv.config` (BASE_PRICE, SNAPSHOT_TS_REVENUE).
**Читает:** `data/golden/golden_all.parquet`, `data/raw/_tmp_stripe_cus_email.parquet`, `data/raw/_tmp_solidgate_cus_email.parquet`.
**Пишет:** `data/raw/_tmp_revenue_events_with_email.parquet`.
**`__main__`:** нет.

### web/golden/web_person_level_fix.py
**Докстринг:** «FIX 1: refund dedup ... + clip refund <= payments per subscription. FIX 2: cohort tables с ДВУМЯ явными знаменателями (n_attributed vs n_payers)».
**def/class:** нет вспомогательных def — тело исполняется на верхнем уровне.
**Свои импорты:** `ltv.cohorts`, `ltv.revenue`, `ltv.config` (BASE_PRICE, SNAPSHOT_TS).
**Читает:** `data/golden/golden_all.parquet`, `data/raw/_tmp_pop_final.parquet`, `data/raw/_tmp_cohort0504_emails.csv`.
**Пишет:** `data/raw/_tmp_pop_fixed.parquet`, `data/raw/_tmp_subs_fixed.parquet`, `data/raw/_tmp_cum_base_fixed.parquet`, `data/raw/_tmp_cum_ups_fixed.parquet`.
**`__main__`:** нет.

### web/golden/web_person_level_tables.py
**Докстринг:** «STEP 3: per-person LTV tables by (cohort_date, utm_source) и (cohort_date, funnel)... Reuses the ALREADY-CALIBRATED per-sub MAP model... purely as a RELATIVE growth shape».
**def/class:**
- `build_cohort_data(cw)`
- `shape_ratio(target_k, anchor_k)`
- `person_cum_at(email_weeks_df, max_week)`
- `cell_ltv_row(emails, group_key_vals)`
**Свои импорты:** `core.web_calibration`, `core` (common, map_model), `core.common` (HMAX), `ltv.config` (RELIABILITY_N_THRESHOLD, MIN_FIRST_PAYERS, MIN_MATURE_REBILL, TAPER_WIDTH, LOW_N_CELL_THRESHOLD, H_EXT).
**Читает:** `data/raw/_tmp_person_pop_5406.parquet`, `data/raw/_tmp_revenue_events_with_email.parquet`, `data/raw/web_conversions.parquet`.
**Пишет:** `data/raw/_tmp_pop_final.parquet`, `data/raw/_tmp_cum_base.parquet`, `data/raw/_tmp_cum_ups.parquet`, `reports/web_golden/05_person_level_clean/{out_name}.csv` (`OUT_DIR`).
**`__main__`:** нет.

### web/golden/web_person_level_tables_v2.py
**Докстринг:** «FIX 2: cohort x utm_source / cohort x funnel tables, built on the refund-fixed revenue... Two EXPLICIT denominators per horizon».
**def/class:**
- `build_cohort_data(cw)`
- `shape_ratio(target_k, anchor_k)`
- `cell_row(emails)`
**Свои импорты:** `core.web_calibration`, `ltv.config` (HMAX, H_EXT, RELIABILITY_N_THRESHOLD, TAPER_WIDTH, MIN_FIRST_PAYERS, MIN_MATURE_REBILL, LOW_N_CELL_THRESHOLD, HORIZONS_REPORT), `core` (common, map_model).
**Читает:** `data/raw/_tmp_pop_fixed.parquet`, `data/raw/_tmp_cum_base_fixed.parquet`, `data/raw/_tmp_cum_ups_fixed.parquet`.
**Пишет:** `reports/web_golden/05_person_level_clean/{out_name}.csv` (`OUT_DIR`) — это те же `table_A/B_..._v2.csv`, что сверяет `reconcile.py` по md5.
**`__main__`:** нет.

### web/golden/reconcile.py
**Докстринг:** «Reconciliation gate for the LTV pipeline refactor. Recomputes the frozen reference outputs... compares them to reports/web_golden/reconcile_baseline.json. Every refactor step must leave this PASS».
**def/class:**
- `check(name, expected, got, exact=False)`
- `md5(path)`
- `recompute_anchor_and_calibration()`
- `recompute_per_payer_anchor()`
**Свои импорты (внутри функций):** `core.web_calibration`, `core` (common, map_model), `ltv.config` (MIN_FIRST_PAYERS, MIN_MATURE_REBILL, RELIABILITY_N_THRESHOLD).
**Читает:** `reports/web_golden/reconcile_baseline.json` (`BASELINE`), `data/raw/_tmp_pop_fixed.parquet`, `data/raw/_tmp_cohort0504_emails.csv`, плюс два CSV из `deliverable_csv_md5` (`reports/web_golden/05_person_level_clean/table_{A,B}_..._v2.csv`).
**Пишет:** — (только stdout + `sys.exit` код).
**`__main__`:** нет (гейт-скрипт, тело верхнего уровня).

---

## web/v2/

### web/v2/ltv_v2/__init__.py
Докстринг: «ltv_v2: EXPERIMENTAL pipeline built directly on the appsflyer BQ source... Isolation contract: nothing under ltv_v2/ imports from or writes into ltv/, models/, or any of the golden-pipeline scripts/reports».

### web/v2/ltv_v2/config.py
**Докстринг:** «Config for the appsflyer-source pipeline (ltv_v2). Fully separate from ltv/config.py — no shared constants».
**def/class:** нет (только константы).
**Свои импорты:** нет.
**Читает:** `data/raw/appsflyer_captured_events_with_trial_2026-07-20.parquet` (`RAW_EVENTS_PATH`, используется потребителями).
**Пишет:** —

### web/v2/ltv_v2/revenue.py
**Докстринг:** «Per-person revenue from the appsflyer source, keyed on LOWER(email). Pure functions over the frozen local pull (ltv_v2.config.RAW_EVENTS_PATH)».
**def/class:**
- `load_raw_events() -> pd.DataFrame`
- `per_person_revenue(events=None, window_end=None) -> pd.DataFrame`
- `per_person_week_cumulative(first_date, events=None) -> tuple`
**Свои импорты:** `ltv_v2.config` (RAW_EVENTS_PATH, BASE_EVENT_TYPE, UPS_EVENT_TYPE, TRIAL_EVENT_TYPE, REFUND_HAIRCUT, WINDOW_START, JOIN_KEY).
**Читает:** `data/raw/appsflyer_captured_events_with_trial_2026-07-20.parquet` (через `RAW_EVENTS_PATH` в `load_raw_events()`).
**Пишет:** —
**`__main__`:** нет.

### web/v2/build_tables.py
**Докстринг:** «Per-person LTV tables (cohort_date x utm_source, cohort_date x funnel) on the appsflyer-source pipeline (ltv_v2). Isolated from the golden pipeline: money comes ONLY from ltv_v2.revenue».
**def/class:**
- `build_cohort_data(cw)`
- `shape_ratio(target_k, anchor_k)`
- `_floor_month(k)`
- `ups_shape_ratio(target_k, anchor_k)`
- `cell_row(emails)`
**Свои импорты:** `core.web_calibration`, `core` (common, map_model), `core.common` (HMAX), `ltv.cohorts`, `ltv.config` (RELIABILITY_N_THRESHOLD, MIN_FIRST_PAYERS, MIN_MATURE_REBILL, TAPER_WIDTH, LOW_N_CELL_THRESHOLD, H_EXT, HORIZONS_REPORT), `ltv_v2.revenue` (as R2), `ltv_v2.config` (WINDOW_START, MONTH_STEP, SNAPSHOT_DATE, OUT_DIR).
**Читает:** `data/raw/web_conversions.parquet`, плюс через `ltv.cohorts.build_population_5406()` → `data/raw/bq_appsflyer_person_dim_2026-07-11.parquet`, плюс через `ltv_v2.revenue.load_raw_events()` → `RAW_EVENTS_PATH`.
**Пишет:** `data/raw/_tmp_v2_pop_final.parquet`, `data/raw/_tmp_v2_cum_base.parquet`, `data/raw/_tmp_v2_cum_ups.parquet`, `reports/web_v2/table_A_cohort_utm_appsflyer.csv`, `reports/web_v2/table_B_cohort_funnel_appsflyer.csv`.
**`__main__`:** нет.

### web/v2/build_triple_report_fixed.py
**Докстринг:** «Triple breakdown: cohort_date x first_funnel x utm_source, on the appsflyer-source pipeline (ltv_v2). Reads ONLY already-built local artifacts... NO BigQuery calls anywhere in this script».
**def/class:**
- `build_cohort_data(cw)`
- `shape_ratio(target_k, anchor_k)`
- `_floor_month(k)`
- `ups_shape_ratio(target_k, anchor_k)`
- `cell_row(emails)`
**Свои импорты:** `core.web_calibration`, `core` (common, map_model), `core.common` (HMAX), `ltv.config` (RELIABILITY_N_THRESHOLD, MIN_FIRST_PAYERS, MIN_MATURE_REBILL, TAPER_WIDTH, LOW_N_CELL_THRESHOLD, H_EXT, HORIZONS_REPORT), `ltv_v2.config` (MONTH_STEP, SNAPSHOT_DATE, OUT_DIR, MIN_COHORT_AGE_WEEKS).
**Читает:** `data/raw/_tmp_v2_pop_final.parquet` (`POP_PATH`), `data/raw/_tmp_v2_cum_base.parquet` (`CUM_BASE_PATH`), `data/raw/_tmp_v2_cum_ups.parquet` (`CUM_UPS_PATH`).
**Пишет:** `reports/web_v2/table_C_cohort_funnel_utm_appsflyer.csv`, `reports/web_v2/table_C_cohort_funnel_utm_appsflyer.parquet`.
**`__main__`:** нет.

### web/v2/build_xlsx_report.py
**Докстринг:** «Boss-facing Excel workbook for the ltv_v2 (appsflyer-source) LTV tables — color-coded by confidence zone... Does not touch ltv/, the golden pipeline, or its reconcile.py gate».
**def/class:**
- `build_column_meta(group_col)`
- `header_label(colname, kind)`
- `fill_for(kind, rowd, N)`
- `write_detail_sheet(wb, sheet_name, df, group_col)`
- `build_summary_sheet(wb, sheet_name_b, df_b, col_map_b)`
- `build_readme_sheet(wb, stats)`
- `main()`
**Свои импорты:** `ltv.config` (HORIZONS_REPORT).
**Читает:** `reports/web_v2/table_A_cohort_utm_appsflyer.csv` (`TABLE_A_PATH`), `reports/web_v2/table_B_cohort_funnel_appsflyer.csv` (`TABLE_B_PATH`).
**Пишет:** `reports/web_v2/LTV_v2_tables.xlsx` (`XLSX_PATH`).
**`__main__`:** да.

### web/v2/upload.py
**Докстринг:** нет (файл начинается сразу с импортов).
**def/class:**
- `get_schema_from_yaml(path)`
- `cast_columns(df, name, typ)`
**Свои импорты:** нет.
**Читает:** `web/v2/schema_ml_web_predictions.yaml` (`SCHEMA_YAML`), `reports/web_v2/table_C_cohort_funnel_utm_appsflyer.parquet` (`DATA_PARQUET`).
**Пишет:** BigQuery `appsflyer-data-411716.ad_hock_tables.ml_web_predictions` (не локальный файл; `write_disposition="WRITE_TRUNCATE"`). Никогда не запускался в рамках этого рефакторинга.
**`__main__`:** нет (тело верхнего уровня — включая сам вызов BigQuery).

### web/v2/reconcile_v2.py
**Докстринг:** «Validation gate for ltv_v2 (appsflyer-source pipeline). Separate from reconcile.py (golden pipeline)... CHECK1 симметричное окно, CHECK2 якорь 05-04. BASELINE CHANGE, dated 2026-07-13».
**def/class:** нет вспомогательных def — тело исполняется на верхнем уровне.
**Свои импорты:** `ltv_v2.revenue` (as R2), `ltv_v2.config` (GOLDEN_SNAPSHOT_TS, REFUND_HAIRCUT).
**Читает:** `reports/web_golden/reconcile_baseline.json`, `data/raw/_tmp_golden_per_email_windowed.parquet`, `data/raw/_tmp_cohort0504_emails.csv`, плюс через `R2.load_raw_events()` → `RAW_EVENTS_PATH`.
**Пишет:** — (только stdout).
**`__main__`:** нет.

---

## web/calibration/

### web/calibration/web_hbase_smooth_correction.py
**Докстринг:** «Soft correction of iOS h_base toward web, in the reliable zone only... via a 2-parameter smooth trend multiplier on hazard... hr in map_model.predict() is untouched — K_SHRINK stays 800».
**def/class:**
- `build_cohort_data(cw)`
- `predict_with_state(cohort, state)`
- `compute_hr(state, app_rows, weeks)`
- `fit_correction(cohorts_subset)`
**Свои импорты:** `core.web_calibration`, `core` (common, map_model), `core.map_model` (MAP_LEVERS, _subs_with_levers, personal_multiplier, K_SHRINK), `ltv.config` (MIN_FIRST_PAYERS, MIN_MATURE_REBILL, RELIABILITY_N_THRESHOLD, TAPER_WIDTH).
**Читает:** через `core.web_calibration` → `data/golden/golden_all.parquet`, `data/golden/golden_all_se_training.parquet`; через `core.common.load_matrix()` → `data/se_training.parquet`.
**Пишет:** нет файловых записей найдено (только `print`).
**`__main__`:** нет (тело верхнего уровня).

### web/calibration/web_boss_charts_ab.py
**Докстринг:** «Two boss-facing charts using the calibrated (2-parameter smooth h_base correction) MAP model: A = accuracy on the reference cohort, B = app-level forecast with an honest +-8% (LOO) uncertainty band».
**def/class:**
- `build_cohort_data(cw)`
**Свои импорты:** `core.web_calibration`, `core` (common, map_model), `ltv.config` (MIN_FIRST_PAYERS, MIN_MATURE_REBILL, RELIABILITY_N_THRESHOLD, TAPER_WIDTH, LOO_BAND_PCT).
**Читает:** через `core.web_calibration`/`core.common` — те же `data/golden/golden_all.parquet`, `data/golden/golden_all_se_training.parquet`, `data/se_training.parquet`.
**Пишет:** `reports/web_golden/03_hazard_calibration/boss_A_accuracy.png` (`OUT_A`), `reports/web_golden/03_hazard_calibration/boss_B_forecast.png` (`OUT_B`).
**`__main__`:** нет (тело верхнего уровня).

---

## web/silver/

### web/silver/pull_web_raw.py
**Докстринг:** «Pull web raw tables from BigQuery to local parquet, frozen at SNAPSHOT_TS. Read-only... Rerunning with the same SNAPSHOT_TS reproduces the same dataset».
**def/class:**
- `save(table: pa.Table, name: str) -> None`
- `pull_event_table(full_name: str) -> None`
- `pull_dim_table(full_name: str) -> None`
**Свои импорты:** нет.
**Читает:** BigQuery (`EVENT_TABLES`: prod_web_events.stripe_events, prod_web_events.solidgate_events; `DIM_TABLES`: prod_web_events.solidgate_old_transactions, prod_silver_layer.stripe_subscriptions, prod_silver_layer.web_conversions, prod_silver_layer.stripe_payments).
**Пишет:** `data/raw/{name}.parquet` для каждой таблицы из `EVENT_TABLES`/`DIM_TABLES` (`OUT`).
**`__main__`:** да.

### web/silver/check_pull.py
**Докстринг:** «Integrity checks for the pulled snapshot. Run after pull_web_raw.py. Checks... no event_id with MEANINGFUL payload differences... max(created) is strictly before SNAPSHOT».
**def/class:** нет — тело верхнего уровня, циклы по `EVENT_TABLES`/`DIM_TABLES`.
**Свои импорты:** нет.
**Читает:** `data/raw/{stripe_events,solidgate_events}.parquet` (`EVENT_TABLES`), `data/raw/{stripe_subscriptions,web_conversions,stripe_payments,solidgate_old_transactions}.parquet` (`DIM_TABLES`).
**Пишет:** —
**`__main__`:** нет.

### web/silver/make_manifest.py
**Докстринг:** «Write the snapshot manifest. Run after check_pull.py passes. The manifest (committed to the repo) + SNAPSHOT_TS in pull_web_raw.py make the local dataset reproducible».
**def/class:** нет — тело верхнего уровня.
**Свои импорты:** нет.
**Читает:** `data/raw/{name}.parquet` для каждого имени в `TABLES` (stripe_events, solidgate_events, stripe_subscriptions, web_conversions, stripe_payments, solidgate_old_transactions), через duckdb `COUNT(*)`.
**Пишет:** `data/raw/SNAPSHOT_MANIFEST.json`.
**`__main__`:** нет.

### web/silver/build_web_silver.py
**Докстринг:** «Build web_events_silver per WEB_SILVER_BUILD_SPEC.md. Staged pipeline with caching: extract / map / trace».
**def/class:**
- `get_con()`
- `to_utc(df, cols)`
- `extract_queries()`
- `run_extract(con, dry=False, force=False)`
- `stripe_bursts(con)`
- `map_stripe_invoice_outcomes(bursts_df)`
- `map_stripe_state_and_refunds(con)`
- `trace_stripe(con, sub_id)`
- `trace_solidgate(con, sub_id)`
- `norm_null(v)`
- `parse_interval_from_name(name)`
- `build_solidgate_silver(con)`
- `load_web_conversions(con)`
- `resolve_geo_media(wc, email, install_date)`
- `build_silver(con)`
- `run_asserts(con, silver)`
- `_dist(series)`
- `weekly_survival(silver, provider)`
- `write_profile(con, silver)`
- `write_chains(silver)`
**Свои импорты:** нет.
**Читает:** `data/raw/stripe_events.parquet` (`STRIPE`), `data/raw/solidgate_events.parquet` (`SOLIDGATE`), плюс из `data/tmp/*.parquet` (кэш промежуточных стадий: `stripe_invoice_family.parquet` и др.), `data/raw/web_conversions.parquet`, `data/raw/stripe_subscriptions.parquet`.
**Пишет:** `data/tmp/*.parquet` (кэш стадии extract), `data/silver/web_events_silver.parquet` (`SILVER_PATH`), `reports/silver_profile.md` (`PROFILE_PATH`), `reports/silver_chains_sample.md` (`CHAINS_PATH`) — **оба report-пути записаны буквально как старый `reports/...` без `web_golden`/`ios`/`attic`; после переезда reports/ в Фазе C5 это устаревший путь относительно текущей раскладки, факт кода на сегодня, не исправлялось.**
**`__main__`:** да.

### web/silver/reclassify_app_id_v2.py
**Докстринг:** «Step 1 of the web golden-build task: reclassify app_id in data/silver/web_events_silver.parquet by product_id... Backs up the pre-existing silver file to data/silver/web_events_silver_pre_appid_v2_backup.parquet before writing (already done by caller)».
**def/class:**
- `classify(row)`
**Свои импорты:** нет.
**Читает:** `data/silver/web_events_silver.parquet` (через duckdb `read_parquet`), `data/raw/bq_enrich_stripe_product_id_2026-07-09.parquet`.
**Пишет:** `data/silver/web_events_silver.parquet` (перезапись того же файла).
**`__main__`:** нет.

### web/silver/build_golden_se_training.py
**Докстринг:** «Step D of the golden-build task: reformat data/golden/golden_all.parquet and golden_good.parquet... into the se_training schema (same columns as data/se_training.parquet, the iOS golden matrix)».
**def/class:**
- `build_se_training(golden_df, label)`
**Свои импорты:** нет.
**Читает:** `data/golden/golden_all.parquet`, `data/golden/golden_good.parquet`.
**Пишет:** `data/golden/golden_all_se_training.parquet`, `data/golden/golden_good_se_training.parquet`.
**`__main__`:** нет.

---

## tests/

### tests/test_ltv.py
**Докстринг:** «Synthetic unit tests for the consolidated LTV core (ltv.revenue, ltv.cohorts). Dependency-free... Each test builds a tiny hand-checked DataFrame».
**def/class:**
- `_golden(rows)`
- `test_split_base_ups_rule()`
- `test_billing_issue_carries_no_money()`
- `test_refund_dedup_same_sub_amount_day()`
- `test_refund_capped_never_negative()`
- `test_refund_proportional_base_ups()`
- `test_running_cap_never_dips_negative()`
- `test_organic_sentinels()`
- `test_is_payer_definition()`
**Свои импорты:** `ltv` (cohorts, revenue), `ltv.config` (BASE_PRICE).
**Читает:** — (синтетические DataFrame в памяти, без файлов).
**Пишет:** —
**`__main__`:** нет (запускается напрямую как скрипт, без гварда: тело верхнего уровня вызывает все `test_*` и печатает summary).

---

## Data flow кандидаты

Файл → читает (литералы путей) → пишет (литералы путей). Отсортировано по цепочкам.

### iOS-цепочка
| Файл | Читает | Пишет |
|---|---|---|
| `ios/pipeline/se_training.py` | `data/ios_events.parquet` | `data/se_training.parquet` |
| `core/common.py` (`load_matrix`) | `data/se_training.parquet` | — |
| `ios/alt_models/{empirical,logreg,hybrid,hybrid_v2}.py` | (in-memory `mx`, аргумент) | — |
| `core/map_model.py` | (in-memory `mx`/`apps_info`, аргумент) | — |
| `ios/validation/compare.py` | `data/se_training.parquet` | `reports/ios/comparison.md`, `reports/ios/comparison_prev.md`, `reports/ios/comparison_detail.csv`, `reports/ios/comparison_detail_prev.csv` |
| `ios/validation/calendar_backtest.py` | `data/se_training.parquet`, `reports/ios/comparison_detail.csv` | `reports/ios/calendar_backtest.md`, `reports/ios/calendar_cache/*` |
| `ios/validation/diag_portfolio_drift.py` | `data/se_training.parquet`, (импорт `CUTOFFS`/`inventory` из `calendar_backtest.py`) | `reports/ios/portfolio_drift.md`, `reports/ios/plots/portfolio_drift.png` |
| `ios/validation/validate_loo.py` | `data/se_training.parquet`, `reports/ios/comparison_detail.csv` | `reports/ios/loo_map.md`, `reports/ios/loo_cache/*` |
| `ios/validation/segment_backtest.py` | `data/se_training.parquet` | `reports/ios/segment_backtest.md` |
| `ios/validation/plot_forecasts.py` | `data/se_training.parquet` | `reports/ios/plot_forecasts.md`, `reports/ios/plots/{app}.png`, `reports/ios/plots/portfolio.png` |

### Web silver → golden цепочка
| Файл | Читает | Пишет |
|---|---|---|
| `web/silver/pull_web_raw.py` | BigQuery (`EVENT_TABLES`, `DIM_TABLES`) | `data/raw/{name}.parquet` (6 таблиц) |
| `web/silver/check_pull.py` | `data/raw/{stripe_events,solidgate_events,stripe_subscriptions,web_conversions,stripe_payments,solidgate_old_transactions}.parquet` | — |
| `web/silver/make_manifest.py` | `data/raw/{6 таблиц}.parquet` | `data/raw/SNAPSHOT_MANIFEST.json` |
| `web/silver/build_web_silver.py` | `data/raw/stripe_events.parquet`, `data/raw/solidgate_events.parquet`, `data/raw/web_conversions.parquet`, `data/raw/stripe_subscriptions.parquet`, `data/tmp/*.parquet` (свой же кэш) | `data/tmp/*.parquet`, `data/silver/web_events_silver.parquet`, `reports/silver_profile.md`, `reports/silver_chains_sample.md` (пути буквально как в коде, не поправлены под Фазу C5) |
| `web/silver/reclassify_app_id_v2.py` | `data/silver/web_events_silver.parquet`, `data/raw/bq_enrich_stripe_product_id_2026-07-09.parquet` | `data/silver/web_events_silver.parquet` (перезапись) |
| `web/silver/build_golden_se_training.py` | `data/golden/golden_all.parquet`, `data/golden/golden_good.parquet` | `data/golden/golden_all_se_training.parquet`, `data/golden/golden_good_se_training.parquet` |

### Web golden pipeline
| Файл | Читает | Пишет |
|---|---|---|
| `web/golden/web_person_level_revenue.py` | `data/golden/golden_all.parquet`, `data/raw/_tmp_stripe_cus_email.parquet`, `data/raw/_tmp_solidgate_cus_email.parquet` | `data/raw/_tmp_revenue_events_with_email.parquet` |
| `web/golden/web_person_level_tables.py` | `data/raw/_tmp_person_pop_5406.parquet`, `data/raw/_tmp_revenue_events_with_email.parquet`, `data/raw/web_conversions.parquet` | `data/raw/_tmp_pop_final.parquet`, `data/raw/_tmp_cum_base.parquet`, `data/raw/_tmp_cum_ups.parquet`, `reports/web_golden/05_person_level_clean/table_{A,B}_cohort_*.csv` |
| `web/golden/web_person_level_fix.py` | `data/golden/golden_all.parquet`, `data/raw/_tmp_pop_final.parquet`, `data/raw/_tmp_cohort0504_emails.csv` | `data/raw/_tmp_pop_fixed.parquet`, `data/raw/_tmp_subs_fixed.parquet`, `data/raw/_tmp_cum_base_fixed.parquet`, `data/raw/_tmp_cum_ups_fixed.parquet` |
| `web/golden/web_person_level_tables_v2.py` | `data/raw/_tmp_pop_fixed.parquet`, `data/raw/_tmp_cum_base_fixed.parquet`, `data/raw/_tmp_cum_ups_fixed.parquet` | `reports/web_golden/05_person_level_clean/table_{A,B}_cohort_*_v2.csv` |
| `web/golden/reconcile.py` | `reports/web_golden/reconcile_baseline.json`, `data/raw/_tmp_pop_fixed.parquet`, `data/raw/_tmp_cohort0504_emails.csv`, `reports/web_golden/05_person_level_clean/table_{A,B}_..._v2.csv` | — (stdout/exit code) |
| `core/web_calibration.py` (библиотека, используется golden+v2+calibration) | `data/golden/golden_all.parquet`, `data/golden/golden_all_se_training.parquet` | `reports/web_golden/02_map_vs_sql_cohort/*` (только под `__main__`) |

### Web v2 (appsflyer) pipeline
| Файл | Читает | Пишет |
|---|---|---|
| `web/v2/ltv_v2/revenue.py` (`load_raw_events`) | `data/raw/appsflyer_captured_events_with_trial_2026-07-20.parquet` | — |
| `web/v2/build_tables.py` | `data/raw/web_conversions.parquet`, (через `ltv.cohorts.build_population_5406()`) `data/raw/bq_appsflyer_person_dim_2026-07-11.parquet`, (через `ltv_v2.revenue`) `RAW_EVENTS_PATH` | `data/raw/_tmp_v2_pop_final.parquet`, `data/raw/_tmp_v2_cum_base.parquet`, `data/raw/_tmp_v2_cum_ups.parquet`, `reports/web_v2/table_A_cohort_utm_appsflyer.csv`, `reports/web_v2/table_B_cohort_funnel_appsflyer.csv` |
| `web/v2/build_triple_report_fixed.py` | `data/raw/_tmp_v2_pop_final.parquet`, `data/raw/_tmp_v2_cum_base.parquet`, `data/raw/_tmp_v2_cum_ups.parquet` | `reports/web_v2/table_C_cohort_funnel_utm_appsflyer.csv`, `reports/web_v2/table_C_cohort_funnel_utm_appsflyer.parquet` |
| `web/v2/build_xlsx_report.py` | `reports/web_v2/table_A_cohort_utm_appsflyer.csv`, `reports/web_v2/table_B_cohort_funnel_appsflyer.csv` | `reports/web_v2/LTV_v2_tables.xlsx` |
| `web/v2/upload.py` (не запускался) | `web/v2/schema_ml_web_predictions.yaml`, `reports/web_v2/table_C_cohort_funnel_utm_appsflyer.parquet` | BigQuery `ad_hock_tables.ml_web_predictions` |
| `web/v2/reconcile_v2.py` | `reports/web_golden/reconcile_baseline.json`, `data/raw/_tmp_golden_per_email_windowed.parquet`, `data/raw/_tmp_cohort0504_emails.csv`, (через `ltv_v2.revenue`) `RAW_EVENTS_PATH` | — (stdout) |

### Web calibration
| Файл | Читает | Пишет |
|---|---|---|
| `web/calibration/web_hbase_smooth_correction.py` | (через `core.web_calibration`/`core.common`) `data/golden/golden_all.parquet`, `data/golden/golden_all_se_training.parquet`, `data/se_training.parquet` | — |
| `web/calibration/web_boss_charts_ab.py` | те же, что выше | `reports/web_golden/03_hazard_calibration/boss_A_accuracy.png`, `reports/web_golden/03_hazard_calibration/boss_B_forecast.png` |

### Прочее
`tests/test_ltv.py` — файлов не читает и не пишет (синтетические DataFrame).
