"""
Юнит-проверки Шага 3 (predict) из MAP_MODEL_SPEC.md — DONE-условие обязательно
запускать перед подключением map в compare.py (Шаг 4).

Запуск (из корня репозитория):
    python -m models.map_predict_check

Проверки:
1. Синтетический апп: ВСЕ рычаги в state — только "__default__" (ни одна
   реальная категория не обучена), данные полностью цензурированы (n_seen=0,
   значит hr=1 при любом weeks). Ожидание: кривая должна СОВПАСТЬ поэлементно
   с cumprod(1 - h_base).
2. Монотонность и диапазон на реальном аппе: кривая не возрастает по шагам
   и все значения в (0, 1].
3. Защита хвоста от underflow: на реальном аппе с минимальным фактическим
   дожитием на нед52 (самый текучий) кривая на 52-й неделе > 0.0.
"""
import numpy as np
import pandas as pd

from models import common, map_model
from models.common import HMAX


def check_default_categories_hr1():
    h_base = pd.Series(np.linspace(0.30, 0.03, HMAX), index=range(1, HMAX + 1))
    state = {
        "h_base": h_base,
        "multipliers": {lever: {"__default__": 1.0} for lever in map_model.MAP_LEVERS},
        "crash_apps": set(),
        "small_categories": {lever: [] for lever in map_model.MAP_LEVERS},
    }
    n = 50
    app_rows = pd.DataFrame({
        "sub_id": [f"synth_{i}" for i in range(n)],
        "app_id": ["synth_app"] * n,
        "step_k": [1] * n,
        "weeks_obs": [0.1] * n,   # < step_k+1=2 у ВСЕХ строк -> obs всегда пуст -> hr=1 при любом weeks
        "survived": [1] * n,
        "died": [0] * n,
        "geo": ["ZZ_UNSEEN"] * n,           # не встречается в multipliers и не в small_categories -> __default__
        "media_source": ["ZZ_UNSEEN"] * n,  # аналогично
        "billing_day_of_month": [15] * n,   # попадёт в реальный billday_bin, но multipliers["billday_bin"] пуст -> тоже __default__
    })

    for weeks in (1, 4, 8):
        curve = map_model.predict(state, app_rows, weeks)
        expected = pd.Series(np.cumprod(1 - h_base.values), index=range(1, HMAX + 1))
        assert np.allclose(curve.values, expected.values, atol=1e-12), (
            f"юнит-тест 1 (все __default__, hr=1, weeks={weeks}) не совпал с cumprod(1-h_base): "
            f"max abs diff={np.max(np.abs(curve.values - expected.values))}"
        )
    print("Юнит-тест 1 пройден: при всех __default__-категориях и hr=1 "
          "predict() == cumprod(1-h_base) поэлементно (проверено для weeks=1,4,8).")


def check_monotonic_and_range(mx, apps_info, state):
    apps, facts, matures, crash_flag, crash_apps = apps_info
    sample_app = next(a for a in apps if not crash_flag[a])
    app_rows = mx[mx["app_id"] == sample_app]

    for weeks in common.PRED_WEEKS_LIST:
        curve = map_model.predict(state, app_rows, weeks)
        assert len(curve) == HMAX, f"длина кривой {len(curve)} != HMAX={HMAX}"
        assert (curve.values > 0).all() and (curve.values <= 1.0).all(), (
            f"значения кривой вне (0,1] для {sample_app}, weeks={weeks}: "
            f"min={curve.min()}, max={curve.max()}"
        )
        diffs = np.diff(curve.values)
        assert (diffs <= 1e-12).all(), (
            f"кривая не монотонно невозрастающая для {sample_app}, weeks={weeks}: "
            f"максимальный рост {diffs.max()}"
        )
    print(f"Юнит-тест 2 пройден: кривая монотонно невозрастающая и в (0,1] "
          f"(апп {sample_app}, недели данных 1/4/8).")


def check_tail_no_underflow(mx, apps_info, state):
    apps, facts, matures, crash_flag, crash_apps = apps_info
    candidates = [
        a for a in apps
        if not crash_flag[a] and matures[a].get(HMAX, 0) >= common.MIN_MATURE
        and not np.isnan(facts[a].get(HMAX, np.nan))
    ]
    assert candidates, "нет ни одного не-crash аппа со зрелыми данными на нед52 для проверки хвоста"
    leakiest_app = min(candidates, key=lambda a: facts[a][HMAX])
    app_rows = mx[mx["app_id"] == leakiest_app]

    curve = map_model.predict(state, app_rows, weeks=1)  # минимум данных -> самый рискованный случай для underflow
    tail_value = curve.iloc[-1]
    assert tail_value > 0.0, (
        f"хвост кривой схлопнулся в 0.0 для самого текучего аппа {leakiest_app} "
        f"(факт нед52={facts[leakiest_app][HMAX]:.4f})"
    )
    print(f"Юнит-тест 3 пройден: самый текучий апп {leakiest_app} "
          f"(факт дожития нед52={facts[leakiest_app][HMAX]:.4f}) -> "
          f"predict(weeks=1).iloc[-1]={tail_value:.6g} > 0.0")


def main():
    check_default_categories_hr1()

    mx = common.load_matrix()
    apps_info = common.select_apps(mx)
    state = map_model.fit(mx, apps_info)

    check_monotonic_and_range(mx, apps_info, state)
    check_tail_no_underflow(mx, apps_info, state)

    print("\nВсе юнит-проверки Шага 3 пройдены.")


if __name__ == "__main__":
    main()
