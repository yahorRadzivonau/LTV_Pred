"""
МОДЕЛЬ MAP — карта множителей риска (h_base x residual-цепочка множителей x hr).

Логика взята из MAP_MODEL_SPEC.md, Шаги 1-3 ("fit: построение карты
множителей", "predict"). Интерфейс fit(mx, apps_info)->state,
predict(state, app_rows, weeks)->pd.Series идентичен остальным моделям
в compare.py.

fit() строит h_base (форма кривой риска по ступеням, эмпирика на сырой
матрице) и цепочку residual-множителей по рычагам MAP_LEVERS, посчитанную в
чистой зоне (step_k <= CLEAN_STEP_MAX, без crash-аппов).

predict() (Шаг 3): личный множитель комбо (personal_multiplier, Шаг 3.1) x
h_base x hr аппа (Шаг 3.2, посчитан ПРОТИВ h_base*личный_множитель, не
голого h_base — иначе задвоение эффекта рычагов), кривая = средневзвешенная
личных кривых (Шаг 3.3, дискретное ожидание, не симуляция).

Порядок рычагов в residual-цепочке важен и не должен меняться: каждый
следующий рычаг считается на "остатке" риска после применения предыдущих
(mult = obs/expected, где expected уже учитывает предыдущие рычаги) — это
защита от задвоения эффекта у скоррелированных рычагов (например, канал
закупает трафик в конкретных гео).

plan_interval ИСКЛЮЧЁН из MAP_LEVERS (был в Шаге 1 изначально, убран после
находки владельца): это утечка таргета, не содержательный рычаг. plan_interval
в pipeline/se_training.py выводится из МЕДИАНЫ ЗАЗОРА МЕЖДУ ПЛАТЕЖАМИ юзера —
у юзера с одним платежом зазора ещё не существует, поэтому он попадает в
категорию "unknown". То есть "unknown" в значительной мере означает "умер
после первого платежа" — это готовый ответ (исход), просочившийся в фичу.
Зеркально "week" получает дутое (заниженное) значение риска: попасть в эту
категорию можно, только дожив минимум до 2-го платежа. В бэктесте это
подглядывание в будущее (интервал юзера посчитан по платежам ПОСЛЕ недели,
на которой мы якобы делаем прогноз), а в проде у свежего аппа вообще все
юзеры были бы "unknown" — рычаг был бы бесполезен именно там, где нужнее
всего. Честная версия этого рычага — из метаданных продукта (цена/интервал
из каталога), это Этап 2 по ROADMAP.md, не переменная из исходов подписки.
"""
import numpy as np
import pandas as pd

from core.common import HMAX, K_SHRINK, MIN_PAYERS, MIN_MATURE, HORIZONS, PRED_WEEKS_LIST, empirical_hbase

MAP_LEVERS = ["geo", "media_source", "billday_bin"]  # порядок = порядок residual-цепочки, НЕ менять. plan_interval исключён (утечка таргета, см. докстринг модуля)
MAP_MIN_ROWS = 2000      # минимум строк категории в чистой зоне, иначе категория -> "other"
MAP_CLIP = (0.4, 2.5)    # клип каждого множителя И итогового произведения
CLEAN_STEP_MAX = 8       # граница чистой зоны
SANITY_TOL = (0.9, 1.1)  # допустимое взвешенное среднее множителей рычага (используется в Шаге 2)


def _prep(mx):
    """Общая подготовка колонок рычагов — используется и в fit(), и в
    personal_multiplier(), чтобы обе стороны видели одни и те же категории."""
    mx = mx.copy()
    mx["billday_bin"] = pd.cut(mx["billing_day_of_month"],
        bins=[0, 10, 20, 31], labels=["d01_10", "d11_20", "d21_31"]).astype(str)
    for col in ["geo", "media_source"]:
        mx[col] = mx[col].fillna("(none)").replace("", "(none)")
    return mx


def _collapse_small(df, small_categories):
    """Схлопывает редкие категории (state["small_categories"][lever], решённые
    в fit() по MAP_MIN_ROWS) в "other" для каждого рычага, присутствующего в
    df. ОБЯЗАТЕЛЬНО применять к любым данным ДО поиска в state["multipliers"]:
    словарь множителей не содержит ключей для редких категорий (они были
    схлопнуты в "other" при обучении) — без этого шага .get(cat, __default__)
    промахивается мимо "other" и тихо подставляет __default__=1.0 вместо
    верного значения. Раньше это было пропущено в personal_multiplier() —
    баг найден адверсариальной проверкой Шага 2, здесь исправлен."""
    df = df.copy()
    for lever, small in small_categories.items():
        if lever in df.columns:
            df[lever] = df[lever].where(~df[lever].isin(small), "other")
    return df


def _clean_zone(mx_prepped, crash_apps):
    """Чистая зона: step_k<=CLEAN_STEP_MAX, weeks_obs>=step_k, без crash-аппов.
    Вынесено отдельно, чтобы Шаг 2 (санити) мог пересчитать те же строки для
    n по категориям, не дублируя условие фильтрации в другом месте."""
    cz = mx_prepped[(mx_prepped["step_k"] <= CLEAN_STEP_MAX)
            & (mx_prepped["weeks_obs"] >= mx_prepped["step_k"])
            & (~mx_prepped["app_id"].isin(crash_apps))].copy()
    cz["died"] = 1 - cz["survived"]
    return cz


def fit(mx, apps_info):
    """Строит state для модели MAP: h_base + residual-цепочка множителей по MAP_LEVERS.

    apps_info — результат common.select_apps(mx): простой 5-кортеж
    (apps, facts, matures, crash_flag, crash_apps). Не пересчитываем его здесь,
    просто распаковываем и берём готовое множество crash_apps (последний элемент).
    """
    # 1.1 Подготовка
    mx = _prep(mx)

    # 1.2 h_base — на СЫРОЙ матрице, включая crash-аппы
    h_base = empirical_hbase(mx)

    # 1.3 Чистая зона для множителей
    # apps_info = (apps, facts, matures, crash_flag, crash_apps) — обычный tuple,
    # НЕ объект с атрибутом .crash_flags. crash_apps уже готовое множество —
    # берём его напрямую как последний (5-й) элемент кортежа.
    crash_apps = apps_info[4]
    cz = _clean_zone(mx, crash_apps)
    cz["expected"] = h_base.reindex(cz["step_k"]).values   # ожидаемый риск строки

    # 1.4 Residual-цепочка
    multipliers = {}
    small_categories = {}
    for lever in MAP_LEVERS:
        counts = cz[lever].value_counts()
        small = counts[counts < MAP_MIN_ROWS].index
        cz[lever] = cz[lever].where(~cz[lever].isin(small), "other")
        small_categories[lever] = small.tolist()

        g = cz.groupby(lever).agg(obs=("died", "mean"), exp=("expected", "mean"),
                                  n=("died", "size"))
        m = (g["obs"] / g["exp"]).clip(*MAP_CLIP)
        multipliers[lever] = m.to_dict()          # {категория: множитель}
        multipliers[lever]["__default__"] = 1.0   # для незнакомых категорий в predict

        cz["expected"] = cz["expected"] * cz[lever].map(m).fillna(1.0)  # residual

    # 1.5 Состав state
    state = {
        "h_base": h_base,                 # pd.Series index 1..HMAX
        "multipliers": multipliers,       # {lever: {category: mult, "__default__": 1.0}}
        "crash_apps": crash_apps,
        "small_categories": small_categories,  # {lever: список категорий, ушедших в other}
    }
    return state


def _subs_with_levers(state, app_rows):
    """Одна строка на подписчика (первая по step_k), с уже применённым
    препроцессингом (_prep + _collapse_small) и колонками MAP_LEVERS.

    Общая точка входа для personal_multiplier() (Шаг 3.1) и для join'а
    личного множителя обратно на построчные данные в predict() (Шаг 3.2).
    И fit(), и это место ОБЯЗАНЫ идти через один и тот же _prep/_collapse_small
    — иначе препроцессинг может разъехаться между обучением и предиктом
    (типовой источник тихих багов)."""
    app_rows = _prep(app_rows)
    app_rows = _collapse_small(app_rows, state["small_categories"])
    return app_rows.sort_values("step_k").groupby("sub_id").first().reset_index()


def _combo_mult(state, row):
    p = 1.0
    for lever in MAP_LEVERS:
        p *= state["multipliers"][lever].get(row[lever], state["multipliers"][lever]["__default__"])
    return float(np.clip(p, *MAP_CLIP))


def personal_multiplier(state, app_rows):
    """Личный множитель каждой уникальной комбинации рычагов пользователей
    аппа (Шаг 3.1 из MAP_MODEL_SPEC.md). Возвращает DataFrame с колонками
    MAP_LEVERS, "w" (число подписчиков с этой комбинацией) и "mult" (личный
    множитель, клип итогового произведения)."""
    subs = _subs_with_levers(state, app_rows)
    combos = subs.groupby(MAP_LEVERS).size().rename("w").reset_index()
    combos["mult"] = combos.apply(lambda row: _combo_mult(state, row), axis=1)
    return combos


def predict(state, app_rows, weeks):
    """Шаг 3 из MAP_MODEL_SPEC.md. Сигнатура и семантика идентичны остальным
    моделям в compare.py: predict(state, app_rows, weeks) -> pd.Series индекс
    1..HMAX, дискретное ожидание (средневзвешенная личных кривых), НЕ симуляция.
    """
    h_base = state["h_base"]

    # 3.1 личный множитель каждой уникальной комбинации рычагов
    combos = personal_multiplier(state, app_rows)

    # join личного множителя обратно на подписчиков (по sub_id, через
    # первую строку подписки — тот же _subs_with_levers, что и в 3.1, ни
    # одного отдельного препроцессинга здесь не заводим)
    subs = _subs_with_levers(state, app_rows)
    subs = subs.merge(combos[MAP_LEVERS + ["mult"]], on=MAP_LEVERS, how="left")
    sub_mult = subs.set_index("sub_id")["mult"]

    # 3.2 hr аппа — относительно h_base * личный множитель, НЕ голого h_base
    # (иначе hr повторно выучит эффект рычагов и задвоит его)
    obs = app_rows[app_rows["weeks_obs"] >= app_rows["step_k"] + 1].copy()
    obs = obs[obs["step_k"] <= weeks]
    n_seen = len(obs)
    if n_seen > 0:
        obs["mult"] = obs["sub_id"].map(sub_mult)
        obs["exp_die"] = (h_base.reindex(obs["step_k"]).values * obs["mult"].values)
        obs["exp_die"] = obs["exp_die"].clip(0.001, 0.999)
        g = obs.groupby("step_k").agg(o=("died", "mean"), e=("exp_die", "mean"), n=("died", "size"))
        g = g[(g["n"] >= 20) & (g["e"] > 0)]
        log_hr = np.average(np.log(np.clip(g["o"] / g["e"], 0.2, 5.0)), weights=g["n"]) if len(g) else 0.0
    else:
        log_hr = 0.0
    hr = np.exp(log_hr * (n_seen / (n_seen + K_SHRINK)))

    # 3.3 кривая аппа — дискретное ожидание, средневзвешенная личных кривых
    haz = np.clip(np.outer(combos["mult"].values, h_base.values) * hr, 0.001, 0.999)  # (n_combos, HMAX)
    curves = np.cumprod(1 - haz, axis=1)
    w = combos["w"].values[:, None]
    return pd.Series((curves * w).sum(axis=0) / w.sum(), index=range(1, HMAX + 1))
