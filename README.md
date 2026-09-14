# LTV — прогноз retention/LTV подписок

Два продукта в одном репозитории:

- **iOS** (ниже) — retention/LTV мобильных подписчиков на основе событий
  AppsFlyer (`silver_layer.conversions`). Модели предсказывают не разовую
  цифру LTV, а всю **кривую дожития по платёжным ступеням** (1-й, 2-й, 3-й…
  платёж), из которой выводится retention/LTV на любом горизонте.
- **Web** (раздел «Web LTV pipeline» ниже) — LTV веб-подписок
  Stripe/Solidgate: golden-референс + кандидат-пайплайн на appsflyer-источнике.

## Схема пайплайна

```
pipeline/pull.py          BigQuery (silver_layer.conversions) -> data/ios_events.parquet
        |
pipeline/se_training.py   события -> платёжные ступени с исходами          -> data/se_training.parquet
        |
models/common.py          общие константы + разметка аппов/фактов/обвалов (select_apps)
        |
        +-- models/empirical.py   МОДЕЛЬ A: медиана по аппам x hazard-ratio
        +-- models/logreg.py      МОДЕЛЬ B: логрег по рычагам x hazard-ratio (закрыта)
        +-- models/hybrid.py      МОДЕЛЬ C: медиана x logreg-рычаг (rr) x hazard-ratio (закрыта)
        +-- models/hybrid_v2.py   МОДЕЛЬ D: доработанный hybrid
        +-- models/map_model.py   МОДЕЛЬ E: карта residual-множителей x hazard-ratio (БАЗОВАЯ)
        |
compare.py                 фитит все 5 на одних данных, считает точность -> reports/comparison.md
```

## Модели

Медиана |отн. ошибки| по неделям видимых данных 1-8, полные цифры и знаковая
ошибка — в `reports/comparison.md` (пересчитывается при каждом запуске
`compare.py`).

| Модель | нед12 | нед26 | нед52 | Статус |
|---|---|---|---|---|
| `models/map_model.py` | 4% | 7% | 10% | **базовая**. Карта residual-множителей по рычагам (гео/канал/день оплаты) x портфельный hazard x hazard-ratio аппа. Лучшая точность на всех горизонтах, интерпретируема («Бразилия x1.3»), рассчитана на малые данные — см. `MAP_MODEL_SPEC.md`. |
| `models/empirical.py` | 4% | 8% | 11% | эталон/бейзлайн. Портфельная медиана hazard x hazard-ratio аппа, без рычагов. Самая простая и устойчивая модель, map должна быть не хуже неё — см. `reports/loo_map.md`. |
| `models/hybrid_v2.py` | 4% | 9% | 10% | жива, идёт нос к носу с empirical/map. Держим в запасе, базовой не назначена (map даёт то же качество и явные множители по рычагам). |
| `models/logreg.py` | 5% | 15% | 24% | **закрыта**. Логрег по рычагам поверх hazard-ratio — смещение хвоста на дальних горизонтах фундаментально (3 прогона подтвердили), не чинить. |
| `models/hybrid.py` | 9% | 13% | 16% | **закрыта**. Баг нормировки `rr` (числитель/знаменатель на разных базах) сделал рычаги бесполезными; оставлена для истории, замена — `hybrid_v2.py`. |

## Результаты валидации

Кроме обычного (степенного) бэктеста `compare.py`, карта прошла три отдельные
проверки — каждая отвечает на свой вопрос:

- **`reports/comparison.md`** — степенной бэктест: сетка (недели видимых данных
  1-8) x (горизонты 12/26/52) для всех 5 моделей на одних и тех же данных.
  Обучение и предсказание не разнесены во времени (модель может видеть
  современников предсказываемого аппа) — см. ограничение ниже
  (`reports/calendar_backtest.md`).
- **`reports/loo_map.md`** — leave-one-out: не объясняется ли точность map тем,
  что множители рычагов учились в том числе на юзерах предсказываемого аппа.
  И map, и empirical переобучены без аппа (`map_loo`/`empirical_loo`) —
  честное сравнение на равных условиях. Итог: на app-уровне map и empirical в
  LOO практически неотличимы (дельта в пределах ±0.3 п.п. на всех
  горизонтах) — отрыва в точности LOO не показывает, map остаётся базовой за
  счёт интерпретируемости и явных множителей по рычагам, а не за счёт голой
  точности.
- **`reports/segment_backtest.md`** — то, для чего карта вообще нужна: точность
  ВНУТРИ аппа по гео-сегментам (этого не умеет empirical — у неё одна кривая на
  весь апп). Главный тест — совпадение знака (сегмент лучше/хуже своего аппа)
  между фактом и прогнозом: **88% (43/49)** на сегментах, где карта делает явную
  ставку (|множитель гео − 1| > 0.05), при пороге 60% — карта работает.
- **`reports/calendar_backtest.md`** — симуляция прода: обучение только на
  данных ДО даты отсечки T, прогноз аппов, запущенных незадолго до T. Нашёл
  **известную проблему**: систематическое завышение факта нед12 на
  свежезапущенных аппах, **+16.2 п.п.** (n=12, выборка малая, но направление
  устойчивое) — заметно хуже, чем в степенном тесте (+0.6 п.п.), то есть это не
  артефакт смешения эпох в степенном тесте, а отдельный реальный эффект.
  Диагностика (`reports/portfolio_drift.md`) нашла временной тренд портфеля
  вниз и разрыв «дожившие/молодые», но вместе они объясняют лишь ~48% эффекта
  — **чинится сейчас, механизм поправки не выбран**. Критично перед вебом
  (Этап 5 в `ROADMAP.md`) — там все аппы будут «новичками».

## Known issues

(полный список с обоснованием — `ROADMAP.md`, раздел «Known issues»)

- **h_base[2] недооценивает удержание на 2-й ступени для зрелых аппов** — из-за
  этого знак ошибки map ухудшается при переходе с 1 на 2 недели видимых данных
  вместо улучшения. Причина — смешивание разнородных когорт в портфельной
  медиане h_base. Не чинится отдельно — то же явление честно вскрыл календарный
  бэктест (пункт ниже). Подробности: `reports/sign_pw2_analysis.md`.
- **Систематическое завышение на свежезапущенных аппах** (+16.2 п.п. на нед12,
  календарный бэктест) — см. раздел «Результаты валидации» выше. В работе,
  механизм поправки ещё не выбран.

## Как запустить

Все команды — из корня репозитория, с активным venv (`.venv`).

**1. Выгрузка сырых событий из BigQuery** (`pipeline/pull.py`)
```
python pipeline/pull.py
```
⚠️ Ходит в BigQuery. Перед реальной выгрузкой делает `dry-run` (показывает объём
в ГБ), останавливается сам, если объём больше `LIMIT_GB = 2.0`, и **спрашивает
подтверждение в консоли (yes/no)** перед скачиванием. Результат пишется в
`data/ios_events.parquet`.

**2. Разметка платёжных ступеней** (`pipeline/se_training.py`)
```
python pipeline/se_training.py
```
Читает `data/ios_events.parquet`, пишет `data/se_training.parquet`.

**3. Сравнение моделей** (`compare.py`)
```
python compare.py
```
Читает `data/se_training.parquet`, обучает все 5 моделей, печатает вердикт в
консоль и пишет полную таблицу в `reports/comparison.md` (+ `reports/comparison_detail.csv`
с построчными данными по каждому аппу/неделе/горизонту/модели).

**4. Отдельные скрипты валидации** (запускать после `compare.py`, читают
`reports/comparison_detail.csv`; ничего в `models/*.py` не меняют)

| Скрипт | Что делает | Отчёт |
|---|---|---|
| `validate_loo.py` | LOO-валидация map vs empirical (см. выше) | `reports/loo_map.md` |
| `debug_sign_pw2.py` | разбор знака ошибки на 1 vs 2 неделях данных | `reports/sign_pw2_analysis.md` |
| `debug_app_6744300418.py` | разбор конкретного аппа-аутлаера (пропущенный обвал или честный аутлаер) | `reports/app_6744300418.md` |
| `plot_forecasts.py` | графики прогноз vs факт для 6 аппов + портфельный scatter | `reports/plots/*.png`, `reports/plot_forecasts.md` |
| `segment_backtest.py` | точность map внутри аппа по гео-сегментам (см. выше) | `reports/segment_backtest.md` |
| `calendar_backtest.py` | календарный бэктест, симуляция прода (см. выше) | `reports/calendar_backtest.md` |
| `diag_portfolio_drift.py` | диагностика причины завышения на новых аппах | `reports/portfolio_drift.md`, `reports/plots/portfolio_drift.png` |

`validate_loo.py` и `calendar_backtest.py` кэшируют дорогие фиты
(`reports/loo_cache/`, `reports/calendar_cache/`) с fingerprint-проверкой —
при изменении `models/*.py` или данных кэш инвалидируется автоматически.

## Структура

```
LTV/
├── README.md
├── ROADMAP.md                  # план развития, зафиксированные решения, known issues
├── MAP_MODEL_SPEC.md           # подробная спека модели map (базовой)
├── compare.py                  # степенной бэктест всех 5 моделей -> reports/comparison.md
├── validate_loo.py             # LOO-валидация -> reports/loo_map.md
├── debug_sign_pw2.py           # разбор знака ошибки pw1 vs pw2 -> reports/sign_pw2_analysis.md
├── debug_app_6744300418.py     # разбор аппа-аутлаера -> reports/app_6744300418.md
├── plot_forecasts.py           # графики прогноз vs факт -> reports/plots/, reports/plot_forecasts.md
├── segment_backtest.py         # точность внутри аппа по гео -> reports/segment_backtest.md
├── calendar_backtest.py        # календарный бэктест -> reports/calendar_backtest.md
├── diag_portfolio_drift.py     # диагностика дрейфа портфеля -> reports/portfolio_drift.md
├── data/
│   ├── ios_events.parquet      # сырые события из BigQuery (не в git, см. .gitignore)
│   └── se_training.parquet     # матрица платёжных ступеней (не в git)
├── pipeline/
│   ├── pull.py                 # BigQuery -> ios_events.parquet
│   └── se_training.py          # события -> матрица ступеней
├── models/
│   ├── common.py                # константы, load_matrix, select_apps, empirical_hbase
│   ├── empirical.py             # МОДЕЛЬ A (бейзлайн)
│   ├── logreg.py                # МОДЕЛЬ B (закрыта)
│   ├── hybrid.py                # МОДЕЛЬ C (закрыта)
│   ├── hybrid_v2.py             # МОДЕЛЬ D
│   └── map_model.py             # МОДЕЛЬ E (базовая), общая с web-частью
├── reports/                     # все отчёты + reports/plots/ (PNG — в git); CSV/XLSX с данными — НЕ в git
├── archive/                     # устаревшие/экспериментальные скрипты и данные (см. ниже)
│
│   # --- web-часть (Stripe/Solidgate + appsflyer), см. раздел "Web LTV pipeline" ---
├── ltv/                          # golden: config.py, revenue.py, cohorts.py
├── ltv_v2/                        # v2/appsflyer: config.py, revenue.py -- изолирован от ltv/
├── web_appsflyer_v2/               # build_tables.py, build_xlsx_report.py
├── reconcile.py                    # гейт golden (бит-в-бит vs reports/reconcile_baseline.json)
├── reconcile_v2.py                 # гейт v2 (симметричное окно vs golden + якорь)
├── compare_map_to_local_sql_style_may_04_10.py  # форма роста, общая калибровка golden/v2
└── tests/                          # test_ltv.py
```

### Что в `archive/`

Ничего не удалено — всё, что заменено новой структурой, лежит здесь как
референс:

- `narezka.py` — старая версия `se_training.py` (без `weeks_obs`/цензурирования)
- `censored.py` + `se_training_cens.parquet` — отдельный расчёт флага `censored`
- `handls.py` — baseline logreg-проверка фич (AUC/калибровка)
- `explore.py`, `trial_scan.py`, `cycl.py` — разведочный анализ сырых событий
- `forecast_demo.py/2/3.py` — три ранние итерации метода прогноза кривой дожития
- `backtest.py` — оригинал, из которого перенесена логика `models/empirical.py`
- `unified_model.py` — logreg-версия с `sample_weight` (менее стабильна)
- `unified_backtest.py` — logreg-версия без `sample_weight`, из которой перенесена логика `models/logreg.py`
- `unified_hybrid.py` — оригинал, из которого перенесена логика `models/hybrid.py`

### Регрессионная проверка переноса

При переносе `backtest.py` в `models/empirical.py` цифры на 1-2 п.п. разошлись
со старым скриптом. Причина: `backtest.py` считал портфельный медианный hazard
по **всем** аппам, включая обвальные (управленческие события вроде поднятия
цены), — в отличие от `unified_model.py`/`unified_hybrid.py`, которые уже
исключали обвальные аппы из формы кривой. Поэтому `models/empirical.fit()`
и `models/map_model.fit()` — единственные, кто принимает на вход сырую `mx`
(не `mx_clean`) — это задокументировано в их docstring и в `compare.py`.

## Web LTV pipeline (подписки Stripe/Solidgate)

Второй продукт в этом репозитории: LTV веб-подписок (не мобильных, iOS-часть
выше — отдельный продукт, отдельные данные). Два пайплайна:

- **golden** (`ltv/`, `reconcile.py`) — замороженный референс на сырых
  событиях Stripe/Solidgate, snapshot `SNAPSHOT_TS=2026-07-07`. Источник
  истины для сверки; `reconcile.py` — гейт, проверяет бит-в-бит совпадение с
  `reports/reconcile_baseline.json`.
- **v2 / appsflyer** (`ltv_v2/`, `web_appsflyer_v2/`, `reconcile_v2.py`) —
  кандидат на замену источника данных: тот же продукт, но события берутся из
  `appsflyer-data-411716.silver_layer.web_conversions` (BigQuery) вместо сырых
  Stripe/Solidgate. Полностью изолирован от golden — ничего в `ltv/` не читает
  и не пишет `ltv_v2/`, и наоборот. `reconcile_v2.py` — отдельный гейт.

### golden (`ltv/`)

| Модуль | Что делает |
|---|---|
| `ltv/config.py` | Константы: `BASE_PRICE`, `TARGET_STRIPE_PRICE_ID`, `PAID_EVENTS`, `SNAPSHOT_TS`, `POP_START_DATE`, whitelist воронок. |
| `ltv/revenue.py` | **Единственное правило дохода**: captured money = `subscription_renewed` + `trial_converted`, минус дедуп/капнутые рефанды, без `billing_issue`. |
| `ltv/cohorts.py` | Популяция 5406 человек + `customer_user_id`↔`email` карта (email — единственный надёжный ключ склейки, см. ниже). |

Подробная карта модулей и история рефакторинга — `MODULES.md`.

**Известная проблема golden (не пофикшена, задокументирована)**: событие
`trial_converted` у части подписок в апстриме содержит цену ПОЛНОГО тарифа
($9.99/$11.99) вместо реальной цены платного триала ($0.99) — то есть golden
теряет реальный триал-платёж так же, как раньше терял его appsflyer, только
другим механизмом. Проверено на живых примерах (raw Stripe): из 3600 человек с
реальным appsflyer-триалом 1810 совпадают с golden корректно, но 814 — golden
кредитует по полной цене вместо реальной. Живёт выше по стеку (silver layer),
чинить вне scope `ltv_v2`/`reconcile_v2.py` — см. докстринг `reconcile_v2.py`.

### v2 / appsflyer (`ltv_v2/`, `web_appsflyer_v2/`)

| Модуль | Что делает |
|---|---|
| `ltv_v2/config.py` | Источник, окно (`WINDOW_START=2026-04-13`), правило captured-дохода, refund haircut (см. ниже), правило платного триала. Каждая константа — с комментарием, откуда взялась. |
| `ltv_v2/revenue.py` | Per-person доход из фиксированного локального пула событий (без live BigQuery на каждый прогон). |
| `web_appsflyer_v2/build_tables.py` | Строит таблицы A (`cohort_date x utm_source`) и B (`cohort_date x first_funnel`): 3 уровня LTV на каждый горизонт (4/12/26/52/104 нед) — `base` (только $9.99/нед, надёжно), `ups_factonly` (факт, без прогноза апсела), `ups_projected` (апсел спрогнозирован месячной каденцией, ⚠ мало данных). |
| `web_appsflyer_v2/build_xlsx_report.py` | То же самое в `.xlsx` с цветовым кодированием зон доверия (fact/model/low_n) и листом Summary (живые формулы SUMIF/SUMPRODUCT, не хардкод). |

Зависимости (нужны, чтобы `build_tables.py` запустился с чистого клона):
`compare_map_to_local_sql_style_may_04_10.py` (форма роста, переиспользуется
из golden-калибровки) и `models/` (тот же `map_model.py`, что и в iOS-части —
одна калибровка на оба продукта).

**Ключевые решения, зафиксированные при построении v2:**

- **Ключ склейки — только `LOWER(email)`.** `customer_user_id` ненадёжен:
  Solidgate переиздаёт `customer_account_id` при ресабе/ретрае (подтверждено
  живым примером — один email под 3 разными cus_id в двух системах).
- **Captured-доход**: `subscription_started` + `upsale_converted` + реальный
  платный триал (`trial_started`, сумма ≠ null и ≠ ровно $1.00). Исключены:
  `billing_issue`, `sub_cancelled`, `upsale_created`, `trial_cancelled`
  (echo уже посчитанной транзакции).
- **Платный триал vs плейсхолдер** — различитель подтверждён через сырой
  Stripe/Solidgate (не предположение): реальная активация — `payment_action=
  auth_settle`, `invoice.amount>0`; плейсхолдер (ровно $1.00 у Solidgate) —
  `auth_0_amount`, `invoice.amount=0`. 100% совпадений на полной локальной
  выборке (7848 строк). Реальный триал считается в `ups` (не в `base`, чтобы
  не портить недельную форму роста), помечен `is_trial`.
- **base vs ups — два ЦЕЛЬНЫХ потока, не декомпозиция.** `base`=
  `subscription_started` ($9.99/нед), `ups`=`upsale_converted` ($11.99/мес) +
  реальный триал — разные продукты/каденции, подтверждено на живом примере
  (два независимых Stripe-subscription_id).
- **Прогноз апсела — месячной каденцией, не недельной.** Старый баг (+21%):
  апсел растягивался той же недельной hazard-кривой, что и база, хотя
  апселится раз в ~4 недели. Фикс: та же retention-кривая, но сэмплирована
  на месячных шагах (нед 4/8/12…).
- **Refund haircut = 0.00913 — ВРЕМЕННЫЙ костыль.** В appsflyer нет
  событий рефанда вообще; константа выведена из доли рефандов golden на
  симметричном окне. Заменить на реальный сигнал, как только появится.

**Гейт `reconcile_v2.py`**: симметричное окно (совпадает с золотым по верхней
границе, т.к. appsflyer — live, а golden — заморожен) + якорь на когорте
2026-05-04. Текущее состояние (2026-07-13, после фикса триала): CHECK1
(разрыв net vs golden) **+3.5%** — выше цели 1.5%, объяснено проблемой golden
`trial_converted` выше, не багом v2. CHECK2 (якорь) в норме.

**Известные оговорки v2 (см. `reports/web_appsflyer_v2/README.md` для полной
версии для нетехнического читателя):**

- `ups_projected` — ~3 месяца истории апсела, низкая точность, не для решений
  без проверки `low_n`/`ltv_{N}_ups_projection_quality`.
- На горизонтах 26/52/104 нед — 0% факта во всех ячейках обеих таблиц (ни
  одна когорта ещё не дожила) — целиком модельный прогноз.
- low_n (n_payers<40) — 64-78% ячеек детальных таблиц. Для читаемой сводки
  без микро-ячеек — лист Summary в `.xlsx`.
- 21%/24% population-разрыв между golden и appsflyer (appsflyer иногда не
  логирует `subscription_started` для реального плательщика) — 1.44% от
  дохода golden, ниже порога блокировки, задокументировано как оговорка к
  источнику.
- `SNAPSHOT_NOW` в `build_tables.py` — текущая дата на момент прогона
  (appsflyer live, в отличие от заморожённого golden) — обновлять при
  каждом перестроении таблиц.

### Как запустить (web)

```
python web/golden/reconcile.py     # гейт golden — воспроизводит замороженный baseline бит-в-бит
python web/v2/build_tables.py      # строит table_A/table_B на appsflyer-источнике
python web/v2/build_triple_report_fixed.py  # строит table_C (cohort x funnel x utm), с фильтром зрелости
python web/v2/reconcile_v2.py      # гейт v2 — печатает CHECK1/CHECK2
python web/v2/build_xlsx_report.py # .xlsx с цветовыми зонами из уже готовых CSV
```

`reconcile.py` сверяет уже посчитанные golden-таблицы с заморженным
baseline — сами golden-таблицы (`web_person_level_*.py`) в этот репозиторий
пока не включены (см. `MODULES.md` за их описанием); для полного
пересчёта golden с нуля они понадобятся отдельно.

## data_repo-prod/

Соседний репозиторий — инфраструктура данных (Terraform, Cloud Functions,
BigQuery датасеты, документация по `silver_layer.conversions`). Источник данных
для `pipeline/pull.py`, но сам по себе не часть модели прогноза. Не менялся и
не должен меняться в рамках этого проекта. **Не публикуется вместе с этим
репозиторием** (см. `.gitignore`) — это внутренняя инфраструктура, а не код
модели.
