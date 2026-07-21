# REPO_AUDIT.md — инвентаризация LTV_Pred перед рефакторингом

Дата аудита: 2026-07-20. Метод: статический анализ (Glob/Grep/Read + `git log`), БЕЗ выполнения кода и БЕЗ обращений к BigQuery, как и требовалось. Ничего в репозитории не удалено/не перемещено/не отредактировано — единственный новый файл — этот.

**Область аудита.** В корне репозитория лежат две ПОЛНОСТЬЮ ПОСТОРОННИЕ вложенные инфраструктурные копии — `data_repo/` (991 файлов, имеет свой собственный `.git`, включает dashboard-приложение с `node_modules`, terraform, GCP cloud functions) и `data_repo-prod/` (464 файла, тот же набор без dashboard/node_modules). Это соседний data-infra репозиторий (источник данных для `pipeline/pull.py`), явно описанный в README.md как «не часть модели прогноза, не должен меняться, не публикуется». Оба каталога **исключены из инвентаризации по существу** — ниже они упомянуты одной строкой каждый, без файлового разбора. Реальная область LTV-пайплайна — **224 файла** (.py/.sql/.yaml/.yml/.json/.md/.parquet/.csv/.xlsx) вне `.git`/`__pycache__`/`.venv`/`data_repo*`.

---

## 1. Executive Summary

1. Репозиторий сейчас держит на себе **два независимых боевых продукта**: iOS LTV-прогноз (`pipeline/`, `models/`, калибровочные бэктесты в корне) и веб-LTV-прогноз, который сам раздваивается на **golden** (`ltv/`, замороженный референс на сырых Stripe/Solidgate) и **v2/appsflyer** (`ltv_v2/`, `web_appsflyer_v2/`, кандидат на замену источника данных). Изоляция между `ltv/` и `ltv_v2/` реальна и подтверждена: ни один модуль одного не импортирует другой.
2. **Главная находка**: `web_appsflyer_v2/build_triple_report.py` и `build_triple_report_fixed.py` пишут **в один и тот же выходной файл** (`reports/web_appsflyer_v2/table_C_cohort_funnel_utm_appsflyer.{csv,parquet}`), при этом отличаются только путь-резолюшеном (не логикой). `_fixed` — актуальный (перезаписан сегодня, 2026-07-20 12:10, фильтром зрелости и свежими данными); `build_triple_report.py` не запускался с 2026-07-14 и является риском гонки: случайный запуск не того файла молча перезапишет боевой deliverable старой (немного другой) версией.
3. Документация внутренне противоречива в двух местах: (а) README.md утверждает, что `web_person_level_*.py` «пока не включены в репозиторий» — неверно, все 4 файла присутствуют и являются частью боевой цепочки golden per `MODULES.md`; (б) README.md перечисляет `archive/forecast_demo.py` как существующий референсный файл, а `MODULES.md` документирует его удаление 2026-07-11 (и git показывает `D` — удалён в рабочем дереве, но не закоммичен). README.md также вообще не упоминает `build_triple_report_fixed.py`/`upload.py`/таблицу C/`ml_web_predictions` — самую свежую боевую ветку.
4. Найдено реальное дублирование констант, которое `ltv/config.py` заявляет устранённым, но не устранило: `RELIABILITY_N_THRESHOLD`, `MIN_FIRST_PAYERS`, `MIN_MATURE_REBILL`, `TAPER_WIDTH`, `LOW_N_CELL_THRESHOLD`, `H_EXT`, `HORIZONS` захардкожены НЕЗАВИСИМО (не импортированы) внутри `web_appsflyer_v2/build_tables.py` и `build_triple_report_fixed.py`, а не взяты из `ltv.config`/`ltv_v2.config`. Также `WHITELIST_FUNNELS` независимо задублирован в `web_person_level_ltv.py` (стриггерами того же набора воронок).
5. `data/` (74 файла, 74 из них с датами в имени или в `_tmp_*` без даты) — почти весь каталог гитигнорен (не в git), содержит и АКТУАЛЬНЫЕ входы боевых цепочек, и явно устаревшие промежуточные `_tmp_*` файлы от предыдущих прогонов (например, одновременно лежат `appsflyer_captured_events_with_trial_2026-07-13.parquet` и `..._2026-07-20.parquet` — первый специально оставлен для отката, это НЕ мусор).
6. iOS map-модель («карта рычагов») найдена и живёт целиком в `models/` (`map_model.py`, `common.py`) + `pipeline/` (`pull.py`, `se_training.py`) — она же переиспользуется веб-пайплайном (`compare_map_to_local_sql_style_may_04_10.py` вызывает `map_model.fit/predict` для реконструкции формы роста).
7. Якорные числа $55.16 / $67.80 воспроизводит **`reconcile.py`** (golden-гейт, сверяет с `reports/reconcile_baseline.json` день-в-день); число $68.13 (appsflyer-версия того же 05-04 когорты) — **`reconcile_v2.py`**, который хардкодит golden-значение $67.804382 как якорь сравнения. Обе цепочки проверены построчным чтением кода, не пересказом.
8. Явных кандидатов на удаление немного и все они уже самоописаны как таковые в README.md/MODULES.md (`archive/*`, `compare_map_to_sql_may_04_10.py`, `diag_*`/`debug_*` одноразовые скрипты) — репозиторий на удивление хорошо задокументирован для своего возраста, проблема не в отсутствии документации, а в том, что она **не поспевает** за самой свежей веткой (table C/upload.py).
9. Три скрипта (`models/map_predict_check.py`, `map_sanity.py`, `map_preflight.py`) — самостоятельно запускаемые (`__main__`), но без входящих импортов и без упоминания в README/MODULES/ROADMAP; не могу утверждать, используются ли они всё ещё вручную — вопрос человеку.
10. BigQuery не запрашивался ни разу; ни один пайплайн/скрипт не выполнялся; `upload.py` не запускался и не импортировался.

---

## 2. Live-chain diagrams

### 2.1 golden (`ltv/`, гейт `reconcile.py`)

```
ltv.cohorts.build_population_5406()      (читает data/raw/bq_appsflyer_person_dim_2026-07-11.parquet)
ltv.cohorts.build_cus_email_map()        (читает data/raw/stripe_subscriptions.parquet + solidgate_events.parquet)
        │
        ▼
web_person_level_revenue.py   [uses ltv.revenue, ltv.config]  → data/raw/_tmp_revenue_events_with_email.parquet
        │
        ▼
web_person_level_tables.py    [uses compare_map_to_local_sql_style_may_04_10, models.common/map_model]
                                → data/raw/_tmp_pop_final.parquet
        │
        ▼
web_person_level_fix.py       [uses ltv.revenue, ltv.cohorts]  → data/raw/_tmp_{pop_fixed,subs_fixed,cum_base_fixed,cum_ups_fixed}.parquet
        │
        ▼
web_person_level_tables_v2.py [uses ltv.config, compare_map_to_local_sql_style_may_04_10, models]
                                → reports/web_model/05_person_level_clean/table_{A,B}_cohort_*_v2.csv
        │
        ▼
reconcile.py  (GATE)  — читает reports/reconcile_baseline.json + сам пересчитывает anchor/calibration
              inline через compare_map_to_local_sql_style_may_04_10 + models, плюс md5 двух CSV выше,
              плюс data/raw/_tmp_pop_fixed.parquet + _tmp_cohort0504_emails.csv для per-payer якоря.
```

Калибровочная ветка (переиспользуется и golden, и v2, и iOS): `compare_map_to_local_sql_style_may_04_10.py` (общая библиотека — `load_golden`, `filter_provider_and_app`, `subscription_start_table`, `load_web_matrix`, `sql_style_summary`, `raw_map_ltv`, ...) → читает `data/golden/golden_all.parquet` + `data/golden/golden_all_se_training.parquet` (последний строится `build_golden_se_training.py` из `golden_all.parquet`/`golden_good.parquet`) → используется `web_hbase_smooth_correction.py` (2-параметрический фит коррекции) → `web_boss_charts_ab.py` (сравнительные графики). Ничего из этой тройки не пишет boевой deliverable сама по себе — это калибровочно-диагностическая ветка, чьи ЧИСЛА (alpha/beta) пересчитываются заново живьём внутри каждого из entry point'ов (`build_tables.py`, `build_triple_report_fixed.py`, `reconcile.py`) — не читаются из файла.

**Разрыв, который я НЕ смог закрыть**: откуда берётся `data/raw/bq_appsflyer_person_dim_2026-07-11.parquet` (вход в `build_population_5406`) — постоянного пул-скрипта под этот файл в репо нет (только россыпь `_tmp_*.sql` в `data/raw/`, явно ad-hoc BigQuery-запросы из прошлой сессии, не оформленные в переиспользуемый скрипт). Помечено UNKNOWN, см. §9.

### 2.2 v2 / appsflyer (`ltv_v2/`, `web_appsflyer_v2/`, гейт `reconcile_v2.py`)

```
ltv_v2.config.RAW_EVENTS_PATH = data/raw/appsflyer_captured_events_with_trial_2026-07-20.parquet
        │  (пул из BigQuery appsflyer-data-411716.silver_layer.web_conversions,
        │   зафиксирован ad-hoc в этой сессии; предыдущий фриз 07-13 сохранён рядом для отката)
        ▼
ltv_v2.revenue.load_raw_events() / per_person_revenue() / per_person_week_cumulative()
        │
        ▼
web_appsflyer_v2/build_tables.py
   [импортирует: compare_map_to_local_sql_style_may_04_10, models.common/map_model,
                 ltv.cohorts (население 5406 — ТА ЖЕ функция, что и в golden),
                 ltv_v2.revenue, ltv_v2.config.WINDOW_START]
   → data/raw/_tmp_v2_pop_final.parquet, _tmp_v2_cum_base.parquet, _tmp_v2_cum_ups.parquet
   → reports/web_appsflyer_v2/table_A_cohort_utm_appsflyer.csv
   → reports/web_appsflyer_v2/table_B_cohort_funnel_appsflyer.csv
        │
        ├──────────────────────────────────────────────┐
        ▼                                               ▼
build_xlsx_report.py                       build_triple_report_fixed.py  ⚠ см. п.2 Executive Summary
  [читает table_A/B csv напрямую]            [читает _tmp_v2_pop_final/cum_base/cum_ups.parquet
  → reports/web_appsflyer_v2/                 напрямую (НЕ через ltv_v2.revenue) + пересчитывает
    LTV_v2_tables.xlsx (конечный deliverable,  форму роста живьём (compare_map_to_local_sql_style...)]
    независимая ветка, не идёт в BigQuery)     → reports/web_appsflyer_v2/table_C_cohort_funnel_utm_appsflyer.{csv,parquet}
                                                        │
                                                        ▼
                                              web_appsflyer_v2/upload.py
                                              [читает schema_ml_web_predictions.yaml
                                               + table_C_cohort_funnel_utm_appsflyer.parquet]
                                              → BigQuery appsflyer-data-411716.ad_hock_tables.ml_web_predictions
                                                (WRITE_TRUNCATE — я НЕ запускал upload.py в этом аудите)

reconcile_v2.py (GATE) — читает ltv_v2.revenue напрямую (не через build_tables.py/table_C),
                          data/raw/_tmp_golden_per_email_windowed.parquet, _tmp_cohort0504_emails.csv,
                          хардкодит golden_anchor=67.804382 (см. §8).
```

**build_triple_report.py vs build_triple_report_fixed.py** — построчный diff (не пересказ агента, я читал оба файла целиком в предыдущих ходах этой сессии): единственное отличие —
```python
# build_triple_report.py:
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# build_triple_report_fixed.py:
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
```
Больше НИКАКИХ отличий в математике/константах/выводе. `_fixed` дополнительно делает `os.chdir(ROOT)`, что делает его устойчивым к рабочей директории запуска (например, из PyCharm). Оба пишут в ОДИНАКОВЫЕ выходные пути. `_fixed` — актуальный (mtime 2026-07-20 12:10, содержит фильтр зрелости `age_weeks_now>=2`, добавленный в этой же сессии); `build_triple_report.py` не трогался с 2026-07-14 17:50 и НЕ содержит фильтр зрелости — если его запустить, он молча перезапишет сегодняшний table_C версией без фильтра.

**Что производит parquet, который читает `upload.py`**: `web_appsflyer_v2/build_triple_report_fixed.py` (единолично; `build_triple_report.py` — тот же путь, но при использовании даёт другой by-content результат, см. выше).

### 2.3 iOS (`pipeline/`, `models/`)

```
pipeline/pull.py         → data/ios_events.parquet   (BigQuery appsflyer-data-411716.silver_layer.conversions)
        │
        ▼
pipeline/se_training.py  → data/se_training.parquet  (добавляет step_k/outcome/weeks_obs/survived/died/...)
        │
        ▼
models/common.py  (load_matrix, select_apps, HMAX=52, K_SHRINK=800, HORIZONS, PRED_WEEKS_LIST, empirical_hbase)
models/map_model.py (fit, predict, MAP_LEVERS, MAP_CLIP, personal_multiplier, _subs_with_levers)
        │
        ├─ используется ПРЯМО iOS-валидацией: validate_loo.py, compare.py, calendar_backtest.py,
        │  plot_forecasts.py, segment_backtest.py, diag_portfolio_drift.py (LOO/календарные/портфельные
        │  бэктесты — не производят deliverable, производят reports/*.md диагностику)
        │
        └─ используется ВЕБ-пайплайном как источник калибровки (см. 2.1, 2.2): и golden, и v2
           реконструируют "форму роста" через map_model.fit/predict на iOS-матрице, затем
           накладывают на неё веб-специфичную коррекцию (alpha/beta от web h_base vs iOS h_base).
```

`models/empirical.py`, `hybrid.py`, `hybrid_v2.py`, `logreg.py` — альтернативные модели, используемые ТОЛЬКО внутри сравнительных бэктестов (`compare.py`, `validate_loo.py`, `calendar_backtest.py`, `segment_backtest.py`) — не используются веб- или деливери-цепочками, которые жёстко завязаны на `map_model` конкретно.

`models/map_predict_check.py`, `map_sanity.py`, `map_preflight.py` — импортируют `models.common`/`map_model`, сами не импортируются никем, есть `__main__` — похоже на ручные preflight/sanity-скрипты для map-модели, но не упомянуты ни в README, ни в MODULES.md, ни в ROADMAP.md. **UNKNOWN** — используются ли ещё, см. §9.

---

## 3. File inventory

Легенда статусов: **LIVE** — участвует в цепочке §2 или явно документирован как активный; **GATE** — reconcile-гейт; **DUPLICATE** — дублирует другой файл; **DEAD** — не импортируется и не упомянут ни в одной живой цепочке/актуальной документации; **DATA_CURRENT** / **DATA_STALE** — данные; **DOC** — документация (не код, отдельная колонка вместо LIVE/DEAD); **OUT_OF_SCOPE** — вне LTV-пайплайна (харнесс/внешний репо); **UNKNOWN**.

### 3.1 Корень — документация

| Путь | Размер | Последний git-коммит | Статус | Evidence |
|---|---|---|---|---|
| README.md | 27.3 KB | 2026-07-14 11:05 | DOC / **STALE (частично)** | Не упоминает `build_triple_report_fixed.py`/`upload.py`/table C/`ml_web_predictions` вообще (последняя актуальная ветка); секция "Что в archive/" перечисляет `forecast_demo.py`, которого по MODULES.md больше нет (см. ниже) |
| MODULES.md | 4.6 KB | 2026-07-14 11:05 | DOC / LIVE, авторитетный | Единственный источник, где явно расписан golden live-chain (§2.1) и explicit-removed список (архив, дубли) |
| ROADMAP.md | 17.2 KB | 2026-07-06 15:56 | DOC | Планирование по этапам (iOS map v1→веб→деньги); не проверялся на актуальность построчно |
| MAP_MODEL_SPEC.md | 16.7 KB | 2026-07-06 14:50 | DOC / LIVE | Спецификация iOS map-модели (карта рычагов) |
| WEB_SILVER_BUILD_SPEC.md | 21.7 KB | UNTRACKED | DOC / LIVE | Спека silver-слоя (Stripe/Solidgate → `web_events_silver.parquet`); прочитана целиком в этой сессии ранее |
| .claude/settings.json | 1.3 KB | 2026-07-06 14:50 | OUT_OF_SCOPE | Конфиг харнесса Claude Code, не пайплайн |
| .claude/settings.local.json | 8.1 KB | UNTRACKED | OUT_OF_SCOPE | То же |

### 3.2 `archive/` — весь каталог DEAD (самоописан)

| Путь | Размер | git | Статус | Evidence |
|---|---|---|---|---|
| archive/backtest.py | 5.5 KB | 2026-07-06 | DEAD | README §"Что в archive/": "оригинал, из которого перенесена логика `models/empirical.py`" |
| archive/censored.py | 1.8 KB | 2026-07-06 | DEAD | README: "отдельный расчёт флага censored" |
| archive/cycl.py | 1.1 KB | 2026-07-06 | DEAD | README: "разведочный анализ" |
| archive/explore.py | 1.3 KB | 2026-07-06 | DEAD | README: "разведочный анализ" |
| archive/forecast_demo2.py | 3.5 KB | 2026-07-06 | DEAD | README: "ранняя итерация метода прогноза" |
| archive/forecast_demo3.py | 4.8 KB | 2026-07-06 | DEAD | README: то же |
| archive/handls.py | 2.9 KB | 2026-07-06 | DEAD | README: "baseline logreg-проверка фич" |
| archive/narezka.py | 5.6 KB | 2026-07-06 | DEAD | README: "старая версия se_training.py" |
| archive/trial_scan.py | 1.2 KB | 2026-07-06 | DEAD | README: "разведочный анализ" |
| archive/unified_backtest.py | 6.9 KB | 2026-07-06 | DEAD | README: "logreg без sample_weight, перенесена в models/logreg.py" |
| archive/unified_hybrid.py | 9.0 KB | 2026-07-06 | DEAD | README: "перенесена в models/hybrid.py" |
| archive/unified_model.py | 7.7 KB | 2026-07-06 | DEAD | README: "logreg с sample_weight" |
| archive/se_training_cens.parquet | 23.8 MB | UNTRACKED | DATA_STALE | Выход archive/censored.py, сам archive DEAD |
| **archive/forecast_demo.py** | — | 2026-07-06 (git), **удалён в рабочем дереве, не закоммичен** | **DEAD + doc-несоответствие** | `git status`: ` D archive/forecast_demo.py`. MODULES.md §"Removed (2026-07-11 cleanup)": "archive/forecast_demo.py — dead, и единственное место, где ещё жил расходящийся K_SHRINK=400 (латентная мина)". README.md, однако, всё ещё перечисляет его как существующий референс — **README не обновлён после этого удаления** |

### 3.3 Корень — iOS-калибровочные бэктесты и однократные diag/debug-скрипты

| Путь | Размер | git/mtime | Статус | Evidence |
|---|---|---|---|---|
| validate_loo.py | 24.7 KB | 2026-07-06 | LIVE (валидация) | Импортирует models.{common,empirical,map_model}; производит LOO-бэктест, upstream для `LOO_BAND_PCT` в ltv/config.py |
| compare.py | 10.6 KB | 2026-07-06 | LIVE (валидация) | Импортирует все 5 моделей models/*; сравнительный бэктест |
| calendar_backtest.py | 22.1 KB | 2026-07-06 | LIVE (валидация) | Импортируется `diag_portfolio_drift.py` (`CUTOFFS`, `inventory`) — единственный root-level скрипт, реально импортируемый другим root-level скриптом |
| segment_backtest.py | 16.0 KB | 2026-07-06 | LIVE (валидация) | models.{common,empirical,map_model} |
| plot_forecasts.py | 7.7 KB | 2026-07-06 | LIVE (валидация) | models.{common,empirical,map_model} → reports/plot_forecasts.md |
| diag_portfolio_drift.py | 13.9 KB | 2026-07-06 | LIVE (валидация) | Импортирует calendar_backtest.py напрямую → reports/portfolio_drift.md |
| debug_sign_pw2.py | 16.8 KB | 2026-07-06 | LIVE (диагностика, разовая) | models.{common,map_model}; вывод reports/sign_pw2_analysis.md существует и цитируется |
| debug_app_6744300418.py | 7.5 KB | 2026-07-06 | LIVE (диагностика, разовая) | models.common; вывод reports/app_6744300418.md |
| compare_map_to_local_sql_style_may_04_10.py | 24.2 KB | 2026-07-14 | **LIVE, ключевая библиотека** | Импортируется build_tables.py, build_triple_report{,_fixed}.py, reconcile.py, web_hbase_smooth_correction.py, web_boss_charts_ab.py, web_person_level_tables{,_v2}.py и ещё 6 web_*-скриптов (12 входящих импортов подтверждено grep) |
| compare_map_to_local_sql_style_may_04_10_error_chart.py | 5.1 KB | UNTRACKED | DEAD (диагностика) | Импортирует ту же библиотеку, но сам никем не импортируется; не упомянут в MODULES.md |
| compare_map_to_sql_may_04_10.py | 16.0 KB | UNTRACKED | **DEAD (явно, первоисточник)** | MODULES.md: "Still present, superseded but harmless (not imported by the live chain): compare_map_to_sql_may_04_10.py (BigQuery-SQL-export variant)" |
| diag_decline.py | 18.6 KB | UNTRACKED | DEAD (диагностика) | Не импортируется; вывод reports/decline_diag.md существует (историческая находка, не пересчитывается) |
| diag_dups.py / diag_dups2.py | 1.4/1.3 KB | UNTRACKED | DEAD (диагностика) | Не импортируются, нет соответствующего report-файла — вероятно чисто консольный вывод разового прогона |
| diag_sg_dups.py / diag_sg_dups2.py | 1.7 KB каждый | UNTRACKED | DEAD (диагностика) | То же |
| dunning_window_diag.py | 16.1 KB | UNTRACKED | DEAD (диагностика) | Вывод reports/dunning_window_diag.md существует |
| solidgate_geo_hunt.py | 12.4 KB | UNTRACKED | DEAD (диагностика) | Вывод reports/solidgate_geo_hunt.md существует |
| segment_backtest.py | (см. выше) | | | |
| export_sql_cohort_may_04_10.sql | 4.5 KB | UNTRACKED | DEAD (разовый) | Одноразовый BQ-экспорт для валидации когорты 05-04 против SQL-стиля коллеги; результат зафиксирован, не перезапускается |

### 3.4 Корень — build/pull скрипты (silver, golden, appsflyer-пул)

| Путь | Размер | git/mtime | Статус | Evidence |
|---|---|---|---|---|
| build_web_silver.py | 66.7 KB | UNTRACKED | LIVE (по WEB_SILVER_BUILD_SPEC.md) | Строит `data/silver/web_events_silver.parquet` из сырых Stripe/Solidgate; специфицирован в WEB_SILVER_BUILD_SPEC.md как отдельная (ещё не влитая в golden) стадия |
| build_golden_se_training.py | 5.2 KB | UNTRACKED | **LIVE** | Производит `data/golden/golden_all_se_training.parquet` + `golden_good_se_training.parquet`, которые читает `compare_map_to_local_sql_style_may_04_10.py` (`WEB_MATRIX_PATH`) — т.е. общая калибровочная библиотека, используемая ВСЕМИ live-цепочками, зависит от вывода этого скрипта |
| pull_web_raw.py | 2.4 KB | UNTRACKED | LIVE (разовый пул) | Пуллит сырые Stripe/Solidgate таблицы из `web-payment-orchestration` в `data/raw/*.parquet` (SNAPSHOT_TS=2026-07-07 заморожен) |
| make_manifest.py | 1.0 KB | UNTRACKED | LIVE (вспомогательный) | Строит `data/raw/SNAPSHOT_MANIFEST.json`, используется преflight-проверками WEB_SILVER_BUILD_SPEC.md |
| check_pull.py | 2.4 KB | UNTRACKED | LIVE (проверочный) | Верифицирует счётчики после pull_web_raw.py |
| reclassify_app_id_v2.py | 6.1 KB | UNTRACKED | LIVE (разовый) | Перекласс `app_id` в `data/silver/web_events_silver.parquet` (есть `..._pre_appid_v2_backup.parquet` — явный до/после снапшот) |

### 3.5 `ltv/` — golden core

| Путь | Размер | git | Статус | Evidence |
|---|---|---|---|---|
| ltv/__init__.py | 0.8 KB | 2026-07-14 | LIVE | Пакет-маркер |
| ltv/config.py | 3.9 KB | 2026-07-14 | LIVE | Импортируется cohorts.py, revenue.py, tests/test_ltv.py, web_person_level_{revenue,fix,tables_v2}.py, web_hbase_smooth_correction.py, web_boss_charts_ab.py, build_tables.py (косвенно через ltv.cohorts) |
| ltv/cohorts.py | 4.8 KB | 2026-07-14 | LIVE | build_population_5406() — фундамент И golden, И v2 (build_tables.py импортирует `ltv.cohorts` напрямую) |
| ltv/revenue.py | 5.9 KB | 2026-07-14 | LIVE | "Единственное правило дохода" golden; импортируется web_person_level_{revenue,fix}.py, tests/test_ltv.py |

### 3.6 `ltv_v2/` — appsflyer core

| Путь | Размер | git | Статус | Evidence |
|---|---|---|---|---|
| ltv_v2/__init__.py | 0.6 KB | 2026-07-14 | LIVE | Пакет-маркер, докстрока объясняет изоляцию от ltv/ |
| ltv_v2/config.py | 7.8 KB | 2026-07-14 (код), но правлен 2026-07-20 в этой сессии (RAW_EVENTS_PATH) | LIVE | RAW_EVENTS_PATH, WINDOW_START, REFUND_HAIRCUT, captured-rule, trial-rule — импортируется revenue.py, reconcile_v2.py, build_tables.py |
| ltv_v2/revenue.py | 4.4 KB | 2026-07-14 | LIVE | per_person_revenue(), per_person_week_cumulative() — импортируется build_tables.py, reconcile_v2.py |

### 3.7 `models/` — общая математика (iOS + переиспользуется веб)

| Путь | Размер | git | Статус | Evidence |
|---|---|---|---|---|
| models/__init__.py | 0 B | 2026-07-03 | LIVE | Пакет-маркер |
| models/common.py | 3.9 KB | 2026-07-03 | **LIVE, заморожен** | HMAX/K_SHRINK/load_matrix/select_apps — импортируется буквально всем (18+ скриптов) |
| models/map_model.py | 13.2 KB | 2026-07-03 | **LIVE, заморожен** | fit()/predict()/MAP_LEVERS — калибровочное ядро и iOS, и веб (golden+v2) |
| models/empirical.py | 2.8 KB | 2026-07-03 | LIVE (валидация) | Используется compare.py, calendar_backtest.py, plot_forecasts.py, segment_backtest.py, validate_loo.py — альтернативная модель для сравнения, не в deliverable-цепочке |
| models/hybrid.py | 6.0 KB | 2026-07-03 | LIVE (валидация) | compare.py |
| models/hybrid_v2.py | 8.1 KB | 2026-07-03 | LIVE (валидация) | compare.py, map_sanity.py |
| models/logreg.py | 4.0 KB | 2026-07-03 | LIVE (валидация) | compare.py |
| models/map_predict_check.py | 5.7 KB | 2026-07-03 | **UNKNOWN** | Импортирует common/map_model, сам не импортируется, есть `__main__`, не упомянут в README/MODULES/ROADMAP — вопрос человеку, актуален ли ручной прогон |
| models/map_preflight.py | 5.5 KB | 2026-07-03 | **UNKNOWN** | То же |
| models/map_sanity.py | 10.5 KB | 2026-07-03 | **UNKNOWN** | То же (плюс импортирует hybrid_v2) |

### 3.8 `pipeline/` — iOS BigQuery-пул

| Путь | Размер | git | Статус | Evidence |
|---|---|---|---|---|
| pipeline/pull.py | 1.6 KB | 2026-07-03 | LIVE | BQ-пул appsflyer conversions → data/ios_events.parquet; описан README "Как запустить" |
| pipeline/se_training.py | 5.9 KB | 2026-07-03 | LIVE | data/ios_events.parquet → data/se_training.parquet, читается models.common.load_matrix() |

### 3.9 `web_appsflyer_v2/` — v2 deliverable-скрипты

| Путь | Размер | git/mtime | Статус | Evidence |
|---|---|---|---|---|
| build_tables.py | 16.7 KB | правлен 2026-07-20 (SNAPSHOT_NOW) | LIVE | Строит table A/B + `_tmp_v2_*` промежутки (§2.2) |
| build_triple_report_fixed.py | 14.0 KB | правлен 2026-07-20 (фильтр зрелости, snapshot) | **LIVE, актуальный** | Строит table C, единственный вход для upload.py |
| build_triple_report.py | 12.6 KB | 2026-07-14 17:50, не трогался с тех пор | **DUPLICATE** builds_triple_report_fixed.py | См. §2.2 diff — идентичен по математике, отличается только path-resolution; пишет в ТЕ ЖЕ выходные файлы; риск гонки |
| build_xlsx_report.py | 22.4 KB | 2026-07-14 | LIVE | Читает table_A/B csv → LTV_v2_tables.xlsx (отдельный deliverable, не идёт в table C/BigQuery) |
| schema_ml_web_predictions.yaml | 3.2 KB | UNTRACKED | LIVE | Схема для upload.py, сверена мной побайтово с реальной BigQuery-таблицей в этой сессии — совпадает 48/48 |
| upload.py | 2.4 KB | правлен 2026-07-20 (write_disposition) | LIVE | Единственный писатель в BigQuery ml_web_predictions; **НЕ запускался в этом аудите** |

### 3.10 Гейты (корень)

| Путь | Размер | git | Статус | Evidence |
|---|---|---|---|---|
| reconcile.py | 6.7 KB | 2026-07-14 | **GATE** (golden) | Читает reports/reconcile_baseline.json + пересчитывает через compare_map_to_local_sql_style_may_04_10 + models; см. §8 |
| reconcile_v2.py | 5.1 KB | 2026-07-14 | **GATE** (v2/appsflyer) | Читает ltv_v2.revenue напрямую (в обход build_tables.py); см. §8 |

### 3.11 Root-level `web_baseline_*` / `web_person_level_*` / `web_*` — golden-цепочка и её боковые ветки

| Путь | Размер | git/mtime | Статус | Evidence |
|---|---|---|---|---|
| web_person_level_revenue.py | 3.1 KB | 2026-07-12 | **LIVE** (шаг 1 golden-цепочки) | MODULES.md §"Live deliverable chain"; импортирует ltv.revenue, ltv.config |
| web_person_level_tables.py | 12.1 KB | UNTRACKED | **LIVE** (шаг 2) | MODULES.md; импортирует compare_map_to_local_sql_style_may_04_10, models |
| web_person_level_fix.py | 6.6 KB | UNTRACKED | **LIVE** (шаг 3) | MODULES.md; импортирует ltv.{cohorts,revenue}, ltv.config |
| web_person_level_tables_v2.py | 10.1 KB | UNTRACKED | **LIVE** (шаг 4, финальный) | MODULES.md; импортирует ltv.config, compare_map_to_local_sql_style_may_04_10, models → deliverable-CSV, которые сверяет reconcile.py |
| web_person_level_ltv.py | 14.4 KB | UNTRACKED | **DEAD + DUPLICATE-риск** | Независимо хардкодит WHITELIST_FUNNELS (строка 17: "device-security-check-gate", "device-security-gate", ...) вместо импорта из ltv.config; не упомянут в MODULES.md live-chain (который называет tables_v2.py, не этот файл) — похоже на до-рефакторную версию, оставленную рядом |
| web_hbase_smooth_correction.py | 13.6 KB | 2026-07-12 | LIVE (калибровка) | MODULES.md §"Calibration path"; импортирует compare_map_to_local_sql_style_may_04_10, models, ltv.config |
| web_boss_charts_ab.py | 10.3 KB | 2026-07-12 | LIVE (калибровка) | MODULES.md §"Calibration path" |
| web_hazard_scalar_calibration.py | 10.7 KB | UNTRACKED | DEAD (диагностика) | Не упомянут в MODULES.md live/calibration путях; не импортируется |
| web_cohort_composition_vs_error.py | 4.6 KB | UNTRACKED | DEAD (диагностика) | То же |
| web_map_error_profile_all_cohorts.py | 5.3 KB | UNTRACKED | DEAD (диагностика) | То же |
| web_baseline_backtest.py | 3.3 KB | UNTRACKED | DEAD (диагностика) | Читает golden_*_se_training.parquet напрямую, но нигде не импортируется и не упомянут |
| web_baseline_chart.py | 4.0 KB | UNTRACKED | DEAD (диагностика) | То же |
| web_baseline_june_cohort_chart.py | 5.1 KB | UNTRACKED | DEAD (диагностика) | То же |
| web_baseline_ltv_chart.py | 5.9 KB | UNTRACKED | DEAD (диагностика) | То же |
| web_baseline_may_04_10_cohort_chart.py | 14.2 KB | UNTRACKED | DEAD (диагностика) | Производит reports/web_baseline_may_04_10_cohort_*.csv (существуют, вероятно разовый анализ) |
| web_baseline_may_cohort_chart.py | 7.9 KB | UNTRACKED | DEAD (диагностика) | Производит reports/web_baseline_may_cohort_survival.csv |

### 3.12 `tests/`

| Путь | Размер | git | Статус | Evidence |
|---|---|---|---|---|
| tests/test_ltv.py | 7.0 KB | 2026-07-14 | LIVE | MODULES.md: "8 synthetic tests... dependency-free runner"; импортирует ltv.{cohorts,revenue}, ltv.config |

### 3.13 `reports/` — верхний уровень (доки/диагностика, не пересчитываемые данные)

Полный список — 61 файл; группирую по происхождению вместо построчного повтора очевидного:

| Группа | Файлы | Статус | Evidence |
|---|---|---|---|
| Golden-гейт baseline | reconcile_baseline.json | **LIVE, замороженный референс** | Читается reconcile.py дословно; см. §8 |
| golden deliverable CSV | web_model/05_person_level_clean/table_{A,B}_cohort_*_v2.csv | **LIVE** | md5 сверяется reconcile.py |
| golden deliverable CSV (не-v2) | web_model/05_person_level_clean/table_{A,B}_cohort_utm.csv, table_B_cohort_funnel.csv | DATA_STALE | Предыдущая (не-v2) версия тех же таблиц от web_person_level_tables.py (шаг 2, до fix.py); не сверяется гейтом напрямую, но не мусор — промежуточный артефакт живой цепочки |
| golden calibration diagnostics | web_model/00_golden_build/golden_build.md, web_model/02_map_vs_sql_cohort/*.csv (7 файлов), web_model/04_person_level/ratio_person_vs_sub.csv | DATA_STALE (историческая диагностика) | Датированы 2026-07-09/10, не перезаписываются текущими скриптами |
| v2/appsflyer deliverable | web_appsflyer_v2/{table_A,B,C}_*.csv, table_C_*.parquet, LTV_v2_tables.xlsx, README.md | **DATA_CURRENT** | table_C датирован 2026-07-20 12:10 (эта сессия); README.md внутри этой папки — нетехническое описание v2, отдельное от корневого README.md |
| iOS 5-летний прогноз (эта сессия, ранее) | ios_5yr_*.csv/json/parquet, stablenet_chart_data.json | DATA_CURRENT | Датированы 2026-07-15, отдельная задача (5-летний форкаст 9 iOS-приложений), не часть web/golden цепочек |
| Разовая диагностика (корень reports/) | SNAPSHOT_SUMMARY.md, app_6744300418.md, calendar_backtest.md, comparison{,_detail,_prev}.{md,csv}, decline_diag.md, dunning_window_diag.md, loo_map.md, map_multipliers.md, plot_forecasts.md, portfolio_drift.md, segment_backtest.md, sign_pw2_analysis.md, silver_chains_sample.md, silver_profile.md, solidgate_geo_hunt.md | DATA_STALE, но ЦИТИРУЕТСЯ | Каждый — выход одного из DEAD-скриптов §3.3; не пересчитывается, но упоминается в README.md "Known issues"/"Результаты валидации" как историческое свидетельство — не мусор в смысле "нечитаемо", но и не живой пересчитываемый артефакт |
| forecast_chart/, growth_chart/, ltv_chart/ | build_*.py + *_numbers.md (3 пары) | DEAD (скрипты) / DATA_STALE (md) | Не импортируются; отдельные one-off чарт-билдеры, датированы 2026-07-07 |

### 3.14 `data/` — 74 файла

| Подкаталог | Статус | Evidence |
|---|---|---|
| data/golden/golden_{all,good}.parquet | **DATA_CURRENT** | Вход build_golden_se_training.py и compare_map_to_local_sql_style_may_04_10.py (GOLDEN_PATH) |
| data/golden/golden_{all,good}_se_training.parquet | **DATA_CURRENT** | Выход build_golden_se_training.py, вход в калибровочную библиотеку (WEB_MATRIX_PATH) |
| data/ios_events.parquet (165.8 MB) | **DATA_CURRENT** | Выход pipeline/pull.py |
| data/se_training.parquet (40.5 MB) | **DATA_CURRENT** | Выход pipeline/se_training.py, вход models.common.load_matrix() |
| data/silver/web_events_silver.parquet | **DATA_CURRENT** | Выход build_web_silver.py (+ reclassify_app_id_v2.py) |
| data/silver/web_events_silver_pre_appid_v2_backup.parquet | DATA_STALE (намеренный бэкап) | До-переклассификационный снапшот, оставлен для отката явно (см. имя) |
| data/raw/appsflyer_captured_events_2026-07-13.parquet | DATA_STALE (намеренный откат) | Докстрока ltv_v2/config.py: "Old freeze (no trial) kept on disk... for rollback/diff" |
| data/raw/appsflyer_captured_events_with_trial_2026-07-13.parquet | DATA_STALE (предыдущий фриз) | Предыдущее значение RAW_EVENTS_PATH, оставлено на диске нетронутым по требованию отката |
| data/raw/appsflyer_captured_events_with_trial_2026-07-20.parquet | **DATA_CURRENT** | Текущий RAW_EVENTS_PATH (эта сессия) |
| data/raw/bq_appsflyer_person_dim_2026-07-11.parquet, bq_appsflyer_person_week_2026-07-11.parquet | **DATA_CURRENT** | Вход ltv.cohorts.build_population_5406(); скрипт-пуллер для них в репо не найден (см. §9) |
| data/raw/bq_enrich_*_2026-07-09.parquet (3 файла) | DATA_STALE или DATA_CURRENT — **UNKNOWN** | Не нашёл явного читателя в scoped .py файлах в рамках этого прохода; возможно потреблены build_web_silver.py/reclassify_app_id_v2.py (не проверено построчно на этот конкретный путь) |
| data/raw/solidgate_events.parquet, solidgate_old_transactions.parquet, stripe_events.parquet, stripe_payments.parquet, stripe_subscriptions.parquet, web_conversions.parquet | **DATA_CURRENT** | Вход build_web_silver.py / ltv.cohorts.build_cus_email_map(); пуллятся pull_web_raw.py |
| data/raw/_tmp_*.parquet/csv/sql/json (≈35 файлов) | Смесь DATA_CURRENT / DATA_STALE | Многие — прямые входы reconcile.py/reconcile_v2.py (_tmp_pop_fixed.parquet, _tmp_cohort0504_emails.csv, _tmp_golden_per_email_windowed.parquet — ЖИВЫЕ); часть — одноразовые ad-hoc BQ-выгрузки прошлых сессий без постоянного скрипта-источника (_tmp_golden_chunk_*.json, _tmp_emailq_*.sql, _tmp_revq_*.sql, _tmp_sym_q*.sql) — технически ничем не читаются сейчас, но представляют собой воспроизводимость прошлых ad-hoc BQ-запросов, не мусор в чистом смысле |
| data/raw/SNAPSHOT_MANIFEST.json | **DATA_CURRENT** | Выход make_manifest.py |
| data/tmp/*.parquet (6 файлов) | **UNKNOWN** | Не нашёл явного читателя за этот проход — вероятно промежуточные stripe/solidgate parsing артефакты build_web_silver.py, не проверено построчно |

---

## 4. Constants map

| Константа | Значение | Файл:строка (определение) | Дублирование/риск |
|---|---|---|---|
| `HMAX` | 52 | `models/common.py:?` (single source) | Ре-экспортирован `ltv/config.py:17`. Независимо ЗАХАРДКОЖЕН (не импортирован) в `web_appsflyer_v2/build_tables.py`, `build_triple_report{,_fixed}.py` (`HMAX = 52`) — **риск рассинхрона** |
| `K_SHRINK` | 800 | `models/common.py:17` | Ре-экспортирован `ltv/config.py:17`. Также захардкожен (K_SHRINK=800) в 6 файлах `archive/*.py` — но archive DEAD, не риск |
| `REFUND_HAIRCUT` | 0.00913 | `ltv_v2/config.py:115` | Единственное определение; упоминается (не переопределяется) в `build_xlsx_report.py` дважды как текст в отчёте |
| `alpha, beta, k_max_reliable` (калибровка) | -0.3933196671, 0.0674853235, 11 | Пересчитывается ЖИВЬЁМ в `build_tables.py`, `build_triple_report_fixed.py`, `reconcile.py` (одна и та же формула скопирована в 3 местах, не вынесена в общую функцию) | Заморожено как reference-значение в `reports/reconcile_baseline.json`. Формула (не значение) задублирована в 3 файлах — изменение формулы в одном месте не отразится в других двух |
| `RELIABILITY_N_THRESHOLD` | 40 | `ltv/config.py:64` (заявлен как единый источник) | НЕ импортируется — независимо захардкожен в `build_tables.py`, `build_triple_report{,_fixed}.py` **(риск, вопреки докстроке ltv/config.py)** |
| `MIN_FIRST_PAYERS` | 15 | `ltv/config.py:62` | Та же ситуация — захардкожен отдельно в build_tables.py/build_triple_report{,_fixed}.py |
| `MIN_MATURE_REBILL` | 3 | `ltv/config.py:63` | Та же ситуация |
| `TAPER_WIDTH` | 3 | `ltv/config.py:65` | Та же ситуация |
| `LOW_N_CELL_THRESHOLD` | 40 | `ltv/config.py:70` | Та же ситуация |
| `H_EXT` | 104 | `ltv/config.py:69` | Та же ситуация |
| `HORIZONS_REPORT` / `HORIZONS` | (4, 12, 26, 52, 104) | `ltv/config.py:71` под именем `HORIZONS_REPORT` | В build_tables.py/build_triple_report{,_fixed}.py — под именем `HORIZONS` (другое имя, то же значение) — независимо, не импортировано |
| `MONTH_STEP` (ups-каденция) | 4 | Определён отдельно в `build_tables.py` и `build_triple_report_fixed.py` (не в конфиге вообще) | Не в ltv/config.py ни в ltv_v2/config.py — не единый источник, хотя это одна из самых важных бизнес-правил (фикс "+21%" бага) |
| `WHITELIST_FUNNELS` | 5 воронок (device-security-check-gate, device-security-gate, detect-security-warnigs-gate-now, device-security, device-security-check) | `ltv/config.py:49-55` | Импортирован правильно в `ltv/cohorts.py`. Но **независимо задублирован** в `web_person_level_ltv.py:17` (тот же набор, другим литералом) |
| `ORGANIC_SENTINELS` | {None, "", "unknown", "none"} | `ltv/config.py:57` | Импортирован в ltv/cohorts.py |
| `BQ_PROJECT` (iOS) | `appsflyer-data-411716` | `pipeline/pull.py:6` (литерал в `bigquery.Client(project=...)`) | Тот же литерал в `ltv_v2/config.py:16` (`BQ_PROJECT`) и `web_appsflyer_v2/upload.py:18` (`PROJECT`) — 3 независимых литерала одного project id, ни один не импортирует другой |
| `BASE_PRICE` | 9.99 | `ltv/config.py:20` | Импортирован в ltv/revenue.py, web_person_level_{revenue,fix}.py |
| `TARGET_STRIPE_PRICE_ID` | `price_1RVWUuJzVYkL7XCuWyUpC9bH` | `ltv/config.py:23` | Импортирован в compare_map_to_local_sql_style_may_04_10.py (проверено grep) |
| `WINDOW_START` (v2) | 2026-04-13 | `ltv_v2/config.py:118` | Совпадает по значению с `POP_START_DATE` в `ltv/config.py:42` — два РАЗНЫХ имени для одной и той же границы, в разных изолированных конфигах (осознанная изоляция, не случайное дублирование) |
| `SNAPSHOT_TS` (golden) | 2026-07-07 | `ltv/config.py:31` | `GOLDEN_SNAPSHOT_TS` в `ltv_v2/config.py:122` — то же значение под другим именем (переиспользуется reconcile_v2.py для симметричного окна) |
| `SNAPSHOT_NOW` (build_tables.py) | 2026-07-20 (текущая, обновляется вручную при каждом прогоне) | `web_appsflyer_v2/build_tables.py` (правил в этой сессии) | Не в конфиге — это единственная "дата прогона", которую нужно руками поднимать при каждом ребилде; явный источник ошибки при рефакторинге (легко забыть обновить) |
| `SNAPSHOT_DATE` (build_triple_report_fixed.py) | 2026-07-20 | `web_appsflyer_v2/build_triple_report_fixed.py` | Должен совпадать с `SNAPSHOT_NOW` выше вручную — НЕ читается из него программно, второй независимый ручной ввод той же даты |
| Абсолютные пути `C:\Users\...` | — | Не найдено ни одного в .py-файлах области аудита | Хорошая новость — весь путь-резолюшен идёт через `Path(__file__)`/relative paths |
| Даты в именах файлов | `_2026-07-11`, `_2026-07-13`, `_2026-07-20`, `_2026-07-09` и т.д. | `data/raw/*.parquet` (см. §3.14) | Осознанный паттерн заморозки (freeze-then-diff), не случайность — но НИГДЕ не задокументирован централизованно список "какая дата сейчас актуальна для какого файла", кроме докстрок в самих config.py |

---

## 5. Data files

Полная таблица — см. §3.14 (data/) и §3.13 (reports/, где лежат CSV/JSON/XLSX-диливерables). Сводка по статусу:

- **DATA_CURRENT** (читается живой цепочкой прямо сейчас): 22 файла — все golden/se_training parquet, оба appsflyer captured-events parquet (07-20 текущий + 07-13 для отката), bq_appsflyer_person_{dim,week}, все Stripe/Solidgate raw, table_A/B/C appsflyer csv+parquet, LTV_v2_tables.xlsx, reconcile_baseline.json, оба golden deliverable v2-csv.
- **DATA_STALE, намеренно (rollback/backup)**: appsflyer_captured_events_2026-07-13.parquet (без trial), appsflyer_captured_events_with_trial_2026-07-13.parquet (предыдущий фриз), web_events_silver_pre_appid_v2_backup.parquet, archive/se_training_cens.parquet.
- **DATA_STALE, диагностика прошлых сессий** (не мусор, но не пересчитывается): reports/*.md одноразовых diag_*/debug_*/web_baseline_* скриптов, reports/web_model/00_golden_build/, /02_map_vs_sql_cohort/, /04_person_level/.
- **UNKNOWN**: data/raw/bq_enrich_*_2026-07-09.parquet (3 файла), data/tmp/*.parquet (6 файлов) — не нашёл явного читателя в этом проходе, см. §9.
- Большая россыпь `data/raw/_tmp_*.sql`/`_tmp_*.json` (≈15 файлов) — застывшие ad-hoc BigQuery-запросы прошлых сессий (golden↔appsflyer email-matching), без постоянного скрипта-источника; читаются ли ещё чем-то — не проверено (не нашёл ссылок), вероятно чисто архивные следы ручной работы.

---

## 6. Duplicates & dead code — кандидаты (СПИСКОМ, ничего не удалено)

1. **`web_appsflyer_v2/build_triple_report.py`** — дублирует `build_triple_report_fixed.py` (единственное отличие — path-resolution), пишет в те же выходные файлы, устарел (не содержит фильтр зрелости из этой сессии). Самый опасный кандидат — риск гонки/перезаписи.
2. **`compare_map_to_sql_may_04_10.py`** — явно назван superseded в MODULES.md.
3. **`archive/*` (11 .py + 1 .parquet)** — явно назван историческим референсом в README/MODULES, ничего не импортирует его.
4. **`archive/forecast_demo.py`** — уже удалён из рабочего дерева (не закоммичено), но всё ещё упомянут в README.md — сам README нуждается в правке (не делаю, вне scope этого аудита).
5. **`web_person_level_ltv.py`** — вероятный до-рефакторный дубль (независимо хардкодит WHITELIST_FUNNELS, не упомянут как часть цепочки в MODULES.md, который явно называет 4 других web_person_level_*.py как живую цепочку).
6. **`compare_map_to_local_sql_style_may_04_10_error_chart.py`** — диагностика поверх основной библиотеки, никем не импортируется, не упомянута в документации.
7. **Однотипные root-level `web_baseline_*.py` (6 файлов)** и **`web_hazard_scalar_calibration.py`, `web_cohort_composition_vs_error.py`, `web_map_error_profile_all_cohorts.py`** — ни один не импортируется, ни один не упомянут в MODULES.md ни в live-chain, ни в calibration-path секциях (та секция называет только web_hbase_smooth_correction.py + web_boss_charts_ab.py).
8. **`reports/forecast_chart/`, `reports/growth_chart/`, `reports/ltv_chart/`** — по одному build_*.py каждый, не импортируются, отдельные one-off чарт-билдеры (2026-07-07), не подключены ни к одному пайплайну.
9. **Диагностические root-level `diag_*.py`/`debug_*.py` (9 файлов)** — MODULES.md прямо называет их "superseded but harmless... not imported by the live chain".

---

## 7. iOS model location

Найдена, целиком в репозитории:

- **`models/map_model.py`** — ядро карты рычагов: `fit()` (портфельный `h_base`, множители по `MAP_LEVERS=["geo","media_source","billday_bin"]`), `predict()` (персональный множитель × app-specific `hr = exp(log_hr·n/(n+K_SHRINK))`), `_subs_with_levers`, `personal_multiplier`.
- **`models/common.py`** — общие константы модели (`HMAX=52`, `K_SHRINK=800`, `MIN_PAYERS`, `MIN_MATURE`, `HORIZONS`, `PRED_WEEKS_LIST`), `load_matrix()`, `select_apps()`, `empirical_hbase`.
- **`pipeline/pull.py` + `pipeline/se_training.py`** — источник данных для карты (`data/se_training.parquet`).
- **`MAP_MODEL_SPEC.md`** — спецификация модели текстом.
- Переиспользуется веб-пайплайном ЦЕЛИКОМ без модификации (и golden, и v2 вызывают `map_model.fit/predict` на iOS-матрице для реконструкции формы роста, затем накладывают веб-специфичную alpha/beta коррекцию поверх).

NOT FOUND IN REPO: неприменимо — модель присутствует полностью.

---

## 8. Anchor validation

Оба якоря воспроизводятся кодом, не хардкодом-совпадением:

- **$55.16** (`per_sub_payer_ltv_rebill7 = 55.162174`, N=92) и **$67.80** (`per_payer_base_plus_ups = 67.804382`, 89 payers) — оба произведены **`reconcile.py`**, функциями `recompute_anchor_and_calibration()` (когорта 2026-05-04, через `compare_map_to_local_sql_style_may_04_10.sql_style_summary`) и `recompute_per_payer_anchor()` (читает `data/raw/_tmp_pop_fixed.parquet` + `_tmp_cohort0504_emails.csv`), сверенными построчно с `reports/reconcile_baseline.json` (`tolerance.float_abs=1e-05`). Прочитан код полностью, не пересказ.
- **$68.13** — appsflyer-версия того же 05-04 когорты, произведена **`reconcile_v2.py`** (`CHECK 2`): берёт `ltv_v2.revenue.per_person_revenue()`, фильтрует по `data/raw/_tmp_cohort0504_emails.csv`, сравнивает с **хардкоженным** `golden_anchor = 67.804382` (строка 80 reconcile_v2.py — то же число, что и golden per_payer выше, переписанное вручную как константа, не импортированное из reconcile_baseline.json).
- Оба гейта — независимые сущности; `reconcile_v2.py` НЕ читает вывод `reconcile.py`, только повторяет число руками. Это рабочая, но хрупкая связь: если golden-якорь когда-нибудь пересчитается и изменится, `reconcile_v2.py` не узнает об этом автоматически.

---

## 9. Questions for human

1. **`data/raw/bq_appsflyer_person_dim_2026-07-11.parquet` и `bq_appsflyer_person_week_2026-07-11.parquet`** (вход `ltv.cohorts.build_population_5406()`, т.е. фундамент ОБЕИХ веб-цепочек) — не нашёл в репозитории постоянного скрипта, который их пуллит (есть только россыпь `_tmp_*.sql`/`.json` в data/raw/, явно ad-hoc). Это сознательный пробел (пуллится вручную через MCP/консоль каждый раз) или скрипт где-то потерян? Нужно для рефакторинга — иначе "с чистого клона" эта цепочка не воспроизводима.
2. **`models/map_predict_check.py`, `map_sanity.py`, `map_preflight.py`** — самостоятельные, с `__main__`, но не упомянуты ни в README, ни в MODULES.md, ни в ROADMAP.md. Ещё используются вручную (preflight перед каждым запуском карты) или можно считать DEAD?
3. **`data/raw/bq_enrich_*_2026-07-09.parquet` (3 файла) и `data/tmp/*.parquet` (6 файлов)** — не нашёл явного читателя за этот проход статического анализа (возможно, потребляются `build_web_silver.py`/`reclassify_app_id_v2.py` по путям, которые я не сопоставил построчно с учётом возможных f-string/переменных путей — не хочу гадать). Нужно подтвердить.
4. **README.md стал неполным**: не описывает `build_triple_report_fixed.py`/`upload.py`/table C/`ml_web_predictions` вообще (самая свежая ветка) и ссылается на уже удалённый (по MODULES.md) `archive/forecast_demo.py`. Обновлять README — отдельная задача, или это ожидаемо (README пишется реже, чем код)?
5. **`web_person_level_ltv.py`** — это до-рефакторный дубль web_person_level_tables_v2.py, безопасно кандидат в DEAD, или он всё ещё зачем-то запускается отдельно (например, для более старого формата отчёта)? Его независимый хардкод WHITELIST_FUNNELS — риск, если он ещё жив.
6. **`build_triple_report.py` vs `_fixed`** — можно ли считать первый безопасным к удалению/архивации сейчас, раз он technically ничем не отличается кроме path-resolution и не запускался с 07-14? (Не удалял — жду вашего решения, как и просили.)
7. Часть `data/raw/_tmp_*.sql`/`.json` (email-matching чанки golden↔appsflyer) — это одноразовые следы прошлой ручной сверки или их предполагается когда-нибудь оформить в переиспользуемый скрипт (аналогично тому, как `pull_web_raw.py` уже оформлен для Stripe/Solidgate)?

---

Ничего не удалено, не перемещено, не переименовано, не отредактировано. BigQuery не запрашивался. Ни один пайплайн/скрипт не запускался, включая `upload.py`. Единственный созданный файл — этот.
