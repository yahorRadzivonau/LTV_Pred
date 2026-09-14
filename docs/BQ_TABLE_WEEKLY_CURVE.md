# Задание: новая таблица в BigQuery для графиков

Создать в существующем терраформе ещё одну таблицу в датасете `ad_hock_tables`,
рядом с уже имеющейся `ml_web_predictions`. Существующие таблицы не трогать.

```
project : appsflyer-data-411716
dataset : ad_hock_tables
table   : ml_web_weekly_curve
```

## Что это за данные

Понедельная кривая жизни когорт для построения графиков. Одна строка =
одна ячейка (когорта × воронка × источник) × одна неделя жизни, от 0 до 104.

Существующая `ml_web_predictions` даёт **итог на нескольких горизонтах**
(4/12/26/52/104). Эта таблица даёт **всю кривую понедельно** — то, из чего строится
график. Разбивка ячеек в обеих таблицах одинаковая, ключи совпадают.

Строк: **~35 700** на текущий момент (340 ячеек × 105 недель). Будет расти по мере
появления новых когорт, примерно +105 строк на каждую новую ячейку.

Таблица перезаписывается целиком (`WRITE_TRUNCATE`) при каждом обновлении —
инкрементальной догрузки не будет.

## Схема — 14 колонок, все NULLABLE

| колонка | тип | описание |
|---|---|---|
| `cohort_date` | DATE | понедельник недели входа когорты |
| `funnel` | STRING | воронка первого касания |
| `utm_source` | STRING | источник трафика |
| `week` | INTEGER | неделя жизни когорты, 0..104 |
| `n_base_payer` | INTEGER | сколько человек в ячейке купили подписку (размер ячейки, одинаков во всех 105 строках ячейки) |
| `rebills_base` | FLOAT | платежей по базовой подписке в эту неделю |
| `rebills_ups` | FLOAT | платежей по апселлу в эту неделю. Ноль в большинстве недель — апселл списывается раз в 4 недели, на неделях 2, 6, 10, … |
| `rebills_base_cum` | FLOAT | накопленным итогом `rebills_base` с недели 0 |
| `rebills_ups_cum` | FLOAT | накопленным итогом `rebills_ups` |
| `active_share` | FLOAT | `rebills_base / n_base_payer` — доля ячейки, ещё платящая в эту неделю. Основная метрика для графика удержания |
| `revenue_week` | FLOAT | выручка в эту неделю, USD |
| `revenue_cum` | FLOAT | выручка накопленным итогом, USD |
| `source` | STRING | `fact` — неделя уже прожита и наблюдена; `model` — прогноз; `no_payers` — в ячейке никто не купил подписку, всё по нулям |
| `evidence` | STRING | `web_data` до недели 8 включительно; `ios_tail_extrapolation` дальше — там форма кривой заимствована с iOS, а не измерена на вебе |

## Схема в JSON (для поля `schema` в terraform)

```json
[
  {"name": "cohort_date",      "type": "DATE",    "mode": "NULLABLE", "description": "Понедельник недели входа когорты"},
  {"name": "funnel",           "type": "STRING",  "mode": "NULLABLE", "description": "Воронка первого касания"},
  {"name": "utm_source",       "type": "STRING",  "mode": "NULLABLE", "description": "Источник трафика"},
  {"name": "week",             "type": "INTEGER", "mode": "NULLABLE", "description": "Неделя жизни когорты, 0..104"},
  {"name": "n_base_payer",     "type": "INTEGER", "mode": "NULLABLE", "description": "Размер ячейки: сколько человек купили подписку"},
  {"name": "rebills_base",     "type": "FLOAT",   "mode": "NULLABLE", "description": "Платежей по базовой подписке в эту неделю"},
  {"name": "rebills_ups",      "type": "FLOAT",   "mode": "NULLABLE", "description": "Платежей по апселлу в эту неделю; ноль вне чекпоинтов 2/6/10/..."},
  {"name": "rebills_base_cum", "type": "FLOAT",   "mode": "NULLABLE", "description": "Базовые платежи накопленным итогом"},
  {"name": "rebills_ups_cum",  "type": "FLOAT",   "mode": "NULLABLE", "description": "Платежи по апселлу накопленным итогом"},
  {"name": "active_share",     "type": "FLOAT",   "mode": "NULLABLE", "description": "Доля ячейки, ещё платящая в эту неделю"},
  {"name": "revenue_week",     "type": "FLOAT",   "mode": "NULLABLE", "description": "Выручка за неделю, USD"},
  {"name": "revenue_cum",      "type": "FLOAT",   "mode": "NULLABLE", "description": "Выручка накопленным итогом, USD"},
  {"name": "source",           "type": "STRING",  "mode": "NULLABLE", "description": "fact | model | no_payers"},
  {"name": "evidence",         "type": "STRING",  "mode": "NULLABLE", "description": "web_data до недели 8, дальше ios_tail_extrapolation"}
]
```

## Настройки таблицы

- **Партиционирование: не нужно.** Таблица маленькая (~36k строк, единицы МБ) и
  переписывается целиком.
- **Кластеризация: `cohort_date, funnel`.** Удешевляет фильтры по когорте и
  воронке — именно так таблицу и будут читать.

  Важная оговорка, проверенная на соседней таблице: **кластеризация НЕ сортирует
  вывод `SELECT *`.** Она про отсечение блоков, а не про порядок строк. Если
  нужен предсказуемый порядок — только `ORDER BY` в запросе или во вью.

- `deletion_protection` — по усмотрению, но учтите: загрузчик работает через
  `WRITE_TRUNCATE`, что защите не мешает, а вот пересоздание таблицы при смене
  кластеризации потребует её снять.

## Пример: как таблица будет читаться

```sql
-- кривая удержания одной ячейки
SELECT week, active_share, source
FROM `appsflyer-data-411716.ad_hock_tables.ml_web_weekly_curve`
WHERE cohort_date = '2026-07-13'
  AND funnel = 'device-security-check-gate'
  AND utm_source = 'fbbohdan'
ORDER BY week

-- портфель целиком
SELECT week,
       SUM(rebills_base) / SUM(n_base_payer) AS active_share,
       SUM(revenue_cum)                      AS revenue_cum
FROM `appsflyer-data-411716.ad_hock_tables.ml_web_weekly_curve`
WHERE n_base_payer > 0
GROUP BY week
ORDER BY week
```

## Чего делать НЕ надо

- Не менять `ml_web_predictions` и её схему — это отдельная работающая таблица.
- Не заводить партиционирование по `cohort_date`: когорт мало (15), партиции
  вышли бы по паре сотен строк, это только замедлит.
- Не делать `week` обязательным (`REQUIRED`) — загрузчик пишет весь набор
  колонок как NULLABLE, чтобы схема совпадала побитово.

После создания таблицы сообщите — под неё будет написан загрузчик по образцу
`web/v3/upload_v3.py` (по умолчанию превью, запись только с явным флагом).
