# attic/ — карантин мёртвого/superseded кода (Фаза A рефакторинга)

Ничего здесь не удалено — это резерв. Каждый файл переехал сюда `git mv`'ом (история сохранена, `git log --follow <путь>` находит прошлое), решение принято по итогам `REPO_AUDIT.md` §3/§6 и подтверждено grep-проверкой (ни один остающийся в репо файл не импортирует ни один из перечисленных ниже — проверено двумя независимыми способами: точный regex по import-паттернам и широкий substring-поиск по всем import-строкам).

| Текущий путь в attic/ | Откуда переехал | Почему |
|---|---|---|
| attic/archive/backtest.py | archive/backtest.py | Оригинал, из которого перенесена логика `models/empirical.py` (README.md) |
| attic/archive/censored.py | archive/censored.py | Отдельный расчёт флага `censored`, не используется дальше (README.md) |
| attic/archive/cycl.py | archive/cycl.py | Разведочный анализ сырых событий (README.md) |
| attic/archive/explore.py | archive/explore.py | Разведочный анализ (README.md) |
| attic/archive/forecast_demo2.py | archive/forecast_demo2.py | Ранняя итерация метода прогноза кривой дожития (README.md) |
| attic/archive/forecast_demo3.py | archive/forecast_demo3.py | Ранняя итерация метода прогноза (README.md) |
| attic/archive/handls.py | archive/handls.py | Baseline logreg-проверка фич (README.md) |
| attic/archive/narezka.py | archive/narezka.py | Старая версия `se_training.py` без `weeks_obs`/цензурирования (README.md) |
| attic/archive/trial_scan.py | archive/trial_scan.py | Разведочный анализ (README.md) |
| attic/archive/unified_backtest.py | archive/unified_backtest.py | logreg без sample_weight, перенесена в `models/logreg.py` (README.md) |
| attic/archive/unified_hybrid.py | archive/unified_hybrid.py | Оригинал, перенесён в `models/hybrid.py` (README.md) |
| attic/archive/unified_model.py | archive/unified_model.py | logreg с sample_weight (README.md) |
| attic/archive/se_training_cens.parquet | archive/se_training_cens.parquet | Выход archive/censored.py; гитигнорился (`archive/*.parquet`), правило продублировано на новый путь в `.gitignore` |
| attic/superseded/build_triple_report.py | web_appsflyer_v2/build_triple_report.py | Байт-в-байт та же математика, что и живой `build_triple_report_fixed.py`, отличие только в path-resolution; не запускался с 2026-07-14; писал в ТЕ ЖЕ выходные файлы, что и живой скрипт — риск гонки/случайной перезаписи |
| attic/superseded/compare_map_to_sql_may_04_10.py | compare_map_to_sql_may_04_10.py | Явно назван superseded в `MODULES.md` ("not imported by the live chain") |
| attic/superseded/compare_map_to_local_sql_style_may_04_10_error_chart.py | compare_map_to_local_sql_style_may_04_10_error_chart.py | Диагностика поверх живой калибровочной библиотеки, никем не импортируется |
| attic/superseded/web_person_level_ltv.py | web_person_level_ltv.py | До-рефакторный дубль `web_person_level_tables_v2.py`; независимо хардкодит `WHITELIST_FUNNELS` вместо импорта из `ltv.config`; `MODULES.md`'s live-chain называет `tables_v2.py`, не этот файл |
| attic/superseded/export_sql_cohort_may_04_10.sql | export_sql_cohort_may_04_10.sql | Одноразовый BQ-экспорт, привязанный к compare_map_to_sql_may_04_10.py, та же судьба |
| attic/diagnostics/diag_decline.py | diag_decline.py | Одноразовая диагностика, не импортируется |
| attic/diagnostics/diag_dups.py | diag_dups.py | Одноразовая диагностика, не импортируется |
| attic/diagnostics/diag_dups2.py | diag_dups2.py | Одноразовая диагностика, не импортируется |
| attic/diagnostics/diag_sg_dups.py | diag_sg_dups.py | Одноразовая диагностика, не импортируется |
| attic/diagnostics/diag_sg_dups2.py | diag_sg_dups2.py | Одноразовая диагностика, не импортируется |
| attic/diagnostics/dunning_window_diag.py | dunning_window_diag.py | Одноразовая диагностика (вывод — reports/dunning_window_diag.md, остался на месте) |
| attic/diagnostics/solidgate_geo_hunt.py | solidgate_geo_hunt.py | Одноразовая диагностика (вывод — reports/solidgate_geo_hunt.md, остался на месте) |
| attic/diagnostics/debug_sign_pw2.py | debug_sign_pw2.py | Разовая отладка (вывод — reports/sign_pw2_analysis.md, остался на месте) |
| attic/diagnostics/debug_app_6744300418.py | debug_app_6744300418.py | Разовая отладка (вывод — reports/app_6744300418.md, остался на месте) |
| attic/diagnostics/web_hazard_scalar_calibration.py | web_hazard_scalar_calibration.py | Не упомянут в MODULES.md ни в live-chain, ни в calibration-path секциях |
| attic/diagnostics/web_cohort_composition_vs_error.py | web_cohort_composition_vs_error.py | Не упомянут в MODULES.md |
| attic/diagnostics/web_map_error_profile_all_cohorts.py | web_map_error_profile_all_cohorts.py | Не упомянут в MODULES.md |
| attic/web_baseline/web_baseline_backtest.py | web_baseline_backtest.py | Не импортируется, не упомянут в MODULES.md |
| attic/web_baseline/web_baseline_chart.py | web_baseline_chart.py | Не импортируется, не упомянут в MODULES.md |
| attic/web_baseline/web_baseline_june_cohort_chart.py | web_baseline_june_cohort_chart.py | Не импортируется, не упомянут в MODULES.md |
| attic/web_baseline/web_baseline_ltv_chart.py | web_baseline_ltv_chart.py | Не импортируется, не упомянут в MODULES.md |
| attic/web_baseline/web_baseline_may_04_10_cohort_chart.py | web_baseline_may_04_10_cohort_chart.py | Не импортируется (вывод — reports/web_baseline_may_04_10_cohort_*.csv/.png, остался на месте) |
| attic/web_baseline/web_baseline_may_cohort_chart.py | web_baseline_may_cohort_chart.py | Не импортируется (вывод — reports/web_baseline_may_cohort_*.csv/.png, остался на месте) |
| attic/map_manual_checks/map_predict_check.py | models/map_predict_check.py | Импортирует `models.common`/`map_model` (исходящий, не блокирует), сам не импортируется; не упомянут в README/MODULES/ROADMAP — статус UNKNOWN, см. REPO_AUDIT.md §9 |
| attic/map_manual_checks/map_sanity.py | models/map_sanity.py | То же; дополнительно импортирует `models.hybrid_v2` (исходящий, не блокирует) |
| attic/map_manual_checks/map_preflight.py | models/map_preflight.py | То же |
| attic/oneoff_charts/forecast_chart/ (целиком) | reports/forecast_chart/ | One-off чарт-билдер (2026-07-07), не подключён ни к одному пайплайну; ссылки на соседние chart-папки — только прозой в докстроках, не программные |
| attic/oneoff_charts/growth_chart/ (целиком) | reports/growth_chart/ | То же |
| attic/oneoff_charts/ltv_chart/ (целиком) | reports/ltv_chart/ | То же |

## Поправка к REPO_AUDIT.md §4 (найдена в процессе переезда)

`attic/archive/censored.py` хардкодит один абсолютный путь: `C:\Users\yahor\PycharmProjects\LTV\se_training_cens.parquet`. Аудит (§4, "Абсолютные пути `C:\Users\...` — Не найдено ни одного") пропустил его из-за бага экранирования в grep-паттерне (искал `C:\\\\Users` вместо `C:\Users`). Не блокирует ничего — файл и так в карантине вместе с остальным `archive/`, но фиксирую как исправление, чтобы не потерялось.

## Что НЕ переехало (осталось на месте до Фазы C)

`validate_loo.py`, `compare.py`, `calendar_backtest.py`, `segment_backtest.py`, `plot_forecasts.py`, `diag_portfolio_drift.py`, `compare_map_to_local_sql_style_may_04_10.py`, все `web_person_level_{revenue,tables,fix,tables_v2}.py`, `web_hbase_smooth_correction.py`, `web_boss_charts_ab.py`, `ltv/`, `ltv_v2/`, `models/{common,map_model,empirical,hybrid,hybrid_v2,logreg}.py`, `pipeline/`, `web_appsflyer_v2/` (кроме `build_triple_report.py`), `reconcile.py`, `reconcile_v2.py`, `tests/`, `data/`, `reports/` (кроме трёх chart-папок выше) — см. `REPO_AUDIT.md` за обоснованием каждого.
