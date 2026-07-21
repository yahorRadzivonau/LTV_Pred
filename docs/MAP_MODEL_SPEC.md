# MAP_MODEL_SPEC — имплементация models/map_model.py

> Спека для Claude Code. Выполнять шаги строго по порядку, после каждого
> шага — стоп-репорт владельцу и ожидание "ок". Все решения уже приняты,
> ничего не выбирать самостоятельно. Если реальность противоречит спеке
> (колонка отсутствует, ассерт падает) — СТОП и вопрос владельцу, не чинить
> самостоятельно и не ослаблять ассерты.

## Железные правила (действуют на все шаги)

- BigQuery НЕ трогать. `pipeline/pull.py` НЕ запускать.
- Файлы в `data/` НЕ перезаписывать.
- `models/empirical.py`, `models/logreg.py`, `models/hybrid.py`,
  `models/hybrid_v2.py`, `models/common.py` НЕ менять ни на символ.
- Перед перезаписью `reports/comparison.md` сделать бэкап в
  `reports/comparison_prev.md` (механизм уже есть в compare.py).
- Числа не подгонять. Неожиданный результат фиксировать как есть.
- Все новые константы — в начале `models/map_model.py`, не в common.py.

## Термины (использовать ровно эти)

| Термин | Значение |
|---|---|
| матрица | `data/se_training.parquet`, загруженная через `common.load_matrix()` |
| чистая зона | строки матрицы с `step_k <= 8` И `weeks_obs >= step_k` |
| чистые аппы | аппы с `crash_flag == False` из `common.select_apps` |
| рычаг | категориальная колонка с множителем риска |
| множитель | число, на которое умножается базовый риск, клип (0.4, 2.5) |
| h_base | пошаговый базовый риск, `common.empirical_hbase(mx)` на СЫРОЙ матрице |

## Константы модели

```python
MAP_LEVERS = ["geo", "media_source", "plan_interval", "billday_bin"]  # порядок = порядок residual-цепочки, НЕ менять
MAP_MIN_ROWS = 2000      # минимум строк категории в чистой зоне, иначе категория -> "other"
MAP_CLIP = (0.4, 2.5)    # клип каждого множителя И итогового произведения
CLEAN_STEP_MAX = 8       # граница чистой зоны
SANITY_TOL = (0.9, 1.1)  # допустимое взвешенное среднее множителей рычага
```

`K_SHRINK`, `HMAX`, `MIN_PAYERS`, `MIN_MATURE`, `HORIZONS`,
`PRED_WEEKS_LIST` — брать из `common.py`, не дублировать.

---

## Шаг 0 — префлайт-проверки данных

Создать `models/map_preflight.py`, запускаемый отдельно
(`python -m models.map_preflight`). Все проверки — ассерты с содержательным
сообщением. Ничего не чинить при падении — падать.

```python
mx = common.load_matrix()

# 0.1 структура
assert set(["sub_id","step_k","outcome","weeks_obs","app_id","geo",
    "media_source","plan_interval","billing_day_of_month",
    "survived"]).issubset(mx.columns)

# 0.2 объёмы (ориентиры текущего снапшота, допуск +-20% на будущие пересборки)
assert 2_000_000 < len(mx) < 3_500_000
assert 250_000 < mx["sub_id"].nunique() < 450_000

# 0.3 значения
assert mx["step_k"].min() == 1
assert set(mx["outcome"].unique()) <= {"renewed","recovered",
    "voluntary_cancel","billing_issue","churned"}
assert (mx["weeks_obs"] >= 0).all()
assert mx["billing_day_of_month"].between(1,31).all()

# 0.4 чистая зона достаточна
clean_zone = mx[(mx["step_k"] <= 8) & (mx["weeks_obs"] >= mx["step_k"])]
assert len(clean_zone) > 1_000_000, "чистая зона подозрительно мала"

# 0.5 survived согласован с outcome
assert (mx["survived"] == mx["outcome"].isin(["renewed","recovered"]).astype(int)).all()
```

Дополнительно напечатать (не ассерты, для глаз владельца):
доли `(none)`/`unknown` в geo и media_source; распределение plan_interval;
число строк чистой зоны по каждой ступени 1-8.

DONE Шага 0: скрипт отработал без падений, распечатка приложена к репорту.
СТОП, ждать "ок".

---

## Шаг 1 — fit: построение карты множителей

`models/map_model.py`, функция `fit(mx, apps_info) -> state`.
`apps_info` — результат `common.select_apps` (уже посчитанные crash_flags),
НЕ пересчитывать внутри.

### 1.1 Подготовка

```python
mx = mx.copy()
mx["billday_bin"] = pd.cut(mx["billing_day_of_month"],
    bins=[0,10,20,31], labels=["d01_10","d11_20","d21_31"]).astype(str)
for col in ["geo","media_source","plan_interval"]:
    mx[col] = mx[col].fillna("(none)").replace("", "(none)")
```

`(none)` и `unknown` — полноценные категории, НЕ выбрасывать и НЕ сливать
между собой.

### 1.2 h_base

```python
h_base = common.empirical_hbase(mx)   # на СЫРОЙ матрице, включая crash-аппы
```

Это решение принято и регрессионно проверено (empirical использует сырую mx).
НЕ переключать на clean.

### 1.3 Чистая зона для множителей

```python
crash_apps = {a for a, flag in apps_info.crash_flags.items() if flag}
cz = mx[(mx["step_k"] <= CLEAN_STEP_MAX)
        & (mx["weeks_obs"] >= mx["step_k"])
        & (~mx["app_id"].isin(crash_apps))].copy()
cz["expected"] = h_base.reindex(cz["step_k"]).values   # ожидаемый риск строки
cz["died"] = 1 - cz["survived"]
```

Множители считаются ТОЛЬКО на `cz` (без crash-аппов). h_base — на сырой.
Это разные базы намеренно; причина: обвалы — управленческие события,
медиана h_base к ним робастна, а средние риски категорий — нет.

### 1.4 Residual-цепочка

Для каждого рычага в порядке `MAP_LEVERS`:

```python
multipliers = {}
for lever in MAP_LEVERS:
    counts = cz[lever].value_counts()
    small = counts[counts < MAP_MIN_ROWS].index
    cz[lever] = cz[lever].where(~cz[lever].isin(small), "other")

    g = cz.groupby(lever).agg(obs=("died","mean"), exp=("expected","mean"),
                              n=("died","size"))
    m = (g["obs"] / g["exp"]).clip(*MAP_CLIP)
    multipliers[lever] = m.to_dict()          # {категория: множитель}
    multipliers[lever]["__default__"] = 1.0   # для незнакомых категорий в predict

    cz["expected"] = cz["expected"] * cz[lever].map(m).fillna(1.0)  # residual
```

Обязательно: `expected` обновляется ПОСЛЕ расчёта множителя рычага и ДО
расчёта следующего — это и есть残 residual-механика, защита от задвоения
скоррелированных рычагов (канал закупает в конкретных гео).

### 1.5 Состав state

```python
state = {
    "h_base": h_base,                 # pd.Series index 1..HMAX
    "multipliers": multipliers,       # {lever: {category: mult, "__default__": 1.0}}
    "crash_apps": crash_apps,
    "small_categories": {...},        # {lever: список категорий, ушедших в other} — для отчёта
}
```

DONE Шага 1: fit отрабатывает без ошибок на реальной матрице.
НЕ переходить к бэктесту — сначала Шаг 2.

---

## Шаг 2 — санити множителей (ДО любого бэктеста)

Урок бага rr-нормировки: сначала проверяем вменяемость, потом меряем точность.

### 2.1 Нормировка каждого рычага

Для каждого рычага: взвешенное (по числу строк категории в cz) среднее
множителей. Требование:

```python
for lever in MAP_LEVERS:
    w_mean = sum(mult[cat] * n[cat] for cat in cats) / sum(n[cat] for cat in cats)
    assert SANITY_TOL[0] <= w_mean <= SANITY_TOL[1], f"{lever}: {w_mean}"
```

Если ассерт падает — СТОП, распечатать полную таблицу множителей рычага
с n, показать владельцу. НЕ перенормировать самостоятельно.

### 2.2 Осмысленность на глаз

Распечатать в `reports/map_multipliers.md`:
- по каждому рычагу все категории: множитель, n строк, доля cz;
- топ-10 по модулю отклонения от 1.0 среди категорий с n >= 10000;
- сколько категорий ушло в "other" по каждому рычагу.

### 2.3 Согласие с hybrid_v2

hybrid_v2 нашёл аппы-выбросы по rr: `id6760619107` (1.914),
`id6761661333` (1.655). Посчитать для этих двух аппов средний личный
множитель их юзеров по карте (механика из Шага 3.1). Ожидание: он тоже
заметно > 1. Напечатать рядом: `rr_hybrid_v2` vs `map_mean_mult` по этим
аппам. Расхождение — не ошибка, но обязательно показать владельцу.

DONE Шага 2: ассерты 2.1 прошли, map_multipliers.md создан, сравнение 2.3
напечатано. СТОП, ждать "ок".

---

## Шаг 3 — predict

`predict(state, app_rows, weeks) -> pd.Series` индекс 1..HMAX.
Сигнатура и семантика идентичны остальным моделям в compare.py.

### 3.1 Личный множитель комбо

```python
subs = app_rows.sort_values("step_k").groupby("sub_id").first().reset_index()
# препроцессинг как в fit: billday_bin, fillna("(none)")
combos = subs.groupby(MAP_LEVERS).size().rename("w").reset_index()

def combo_mult(row):
    p = 1.0
    for lever in MAP_LEVERS:
        p *= state["multipliers"][lever].get(row[lever],
             state["multipliers"][lever]["__default__"])
    return float(np.clip(p, *MAP_CLIP))   # клип ИТОГОВОГО произведения

combos["mult"] = combos.apply(combo_mult, axis=1)
```

Незнакомая категория (не встречалась в fit, включая "other"-логику) ->
`__default__` = 1.0. НЕ падать, НЕ угадывать ближайшую.

### 3.2 hr аппа — относительно карты, не голого h_base

Механика как в empirical (взвешенный лог-ratio, усадка K_SHRINK), но
ожидаемая смертность строки = `h_base[step_k] * mult(комбо этой подписки)`:

```python
obs = app_rows[app_rows["weeks_obs"] >= app_rows["step_k"] + 1].copy()
obs = obs[obs["step_k"] <= weeks]
# join личного множителя по sub_id (через first-строку подписки)
obs["exp_die"] = (h_base.reindex(obs["step_k"]).values * obs["mult"]).clip(0.001, 0.999)
g = obs.groupby("step_k").agg(o=("died","mean"), e=("exp_die","mean"), n=("died","size"))
g = g[(g["n"] >= 20) & (g["e"] > 0)]
log_hr = np.average(np.log(np.clip(g["o"]/g["e"], 0.2, 5.0)), weights=g["n"]) if len(g) else 0.0
hr = np.exp(log_hr * (n_seen / (n_seen + K_SHRINK)))   # n_seen = len(obs)
```

Причина: если hr считать против голого h_base, он повторно выучит эффект
рычагов и задвоит его.

### 3.3 Кривая аппа

```python
haz = np.clip(np.outer(combos["mult"].values, h_base.values) * hr, 0.001, 0.999)  # (n_combos, HMAX)
curves = np.cumprod(1 - haz, axis=1)
w = combos["w"].values[:, None]
return pd.Series((curves * w).sum(axis=0) / w.sum(), index=range(1, HMAX + 1))
```

Дискретное ожидание, средневзвешенная личных кривых. НЕ симуляция.

DONE Шага 3: predict возвращает Series 1..52, значения монотонно невозрастают,
все в (0,1]. Юнит-проверка: для синтетического аппа, где все юзеры
"__default__"-категорий и hr=1, кривая == cumprod(1-h_base) поэлементно.
Дополнительный ассерт (защита хвоста от underflow): выбрать реальный апп
с минимальным фактическим дожитием на нед52 (самый текучий) и проверить
`assert predict(...).iloc[-1] > 0.0` — хвост кривой не схлопнулся в 0.0.
СТОП, ждать "ок".

---

## Шаг 4 — подключение в compare.py и бэктест

1. map — шестая колонка (после hybrid_v2). Интерфейс тот же fit/predict.
2. Запустить `python compare.py`.
3. Регрессия: колонки empirical/logreg/hybrid/hybrid_v2 совпадают с
   `reports/comparison_prev.md` ячейка в ячейку. Если нет — искать свою
   ошибку, map-цифры не репортить до совпадения.

DONE Шага 4: comparison.md с 6 колонками, регрессия 0 расхождений.

---

## Шаг 5 — отчёт владельцу

Обязательные блоки:

1. Таблица нед12/26/52 (медиана 1-8 недель данных): empirical vs hybrid_v2
   vs map, с знаковой ошибкой.
2. Отдельно срез "1-2 недели данных" по знаковой ошибке (бизнес-кейс
   раннего решения о закупке) — те же три модели.
3. Ссылка на reports/map_multipliers.md + 5 самых липких и 5 самых текучих
   категорий словами ("гео X снижает риск на Y%").
4. Вердикт по развилке из ROADMAP.md Этап 1 (какая строка таблицы развилки
   сработала), без рекомендаций сверх таблицы.

СТОП. Ничего после Шага 5 не делать.

---

## Известные ловушки (НЕ повторять)

- НЕ считать знаменатель и числитель множителя на разных базах строк
  (баг rr в hybrid v1). В map обе части считаются на одних строках cz —
  сохранить это свойство при любых правках.
- НЕ включать crash-аппы в расчёт множителей (управленческие события).
- НЕ считать hr против голого h_base (задвоение рычагов, см. Шаг 3.2).
- НЕ добавлять attribution_source и trial_days рычагами (решение v1:
  attribution_source дублирует media_source, trial_days грязный —
  527 отрицательных значений от переподписок).
- НЕ ослаблять ассерты Шага 0 и Шага 2 ради прохождения. Падение ассерта =
  вопрос владельцу.
- НЕ использовать mx["died"] из load_matrix, если его нет — создать локально
  `1 - survived` (проверить, что уже даёт common).
- НЕ использовать plan_interval рычагом, пока он выводится из зазоров
  ребиллов — это утечка таргета (unknown = умер после 1 платежа).
  Честная версия рычага — из метаданных продукта, Этап 2.
