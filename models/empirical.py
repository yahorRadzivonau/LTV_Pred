"""
МОДЕЛЬ A — эмпирическая (эталон, самая устойчивая на дальних горизонтах).

Форма кривой = портфельный hazard, посчитанный как медиана по приложениям на
каждой платёжной ступени (censored-aware, устойчиво к выбросам/обвалам).
Поверх формы — hazard-ratio (hr) конкретного приложения, оценённый по фактически
видимым неделям данных, с усадкой к 1.0 при малом объёме данных (K_SHRINK).

Никакого ML внутри — чистая эмпирика. Перенесено дословно из archive/backtest.py.
Последние известные цифры (медиана |отн. ошибки|): нед12 3-7% | нед26 5-12% | нед52 7-18%.

ВАЖНО (отличие от logreg/hybrid): fit() здесь ожидает СЫРУЮ матрицу (со всеми
аппами, включая обвальные), а не mx_clean — именно так считал backtest.py.
Портфельный медианный hazard там никогда не исключал обвальные аппы (в отличие
от unified_model.py/unified_hybrid.py). Подтверждено регрессионным тестом:
с mx_clean цифры расходятся со старым backtest.py на 1-2 п.п., с сырой mx —
совпадают. compare.py учитывает это и передаёт сюда именно сырую mx.
"""
import numpy as np
import pandas as pd

from models.common import HMAX, K_SHRINK, empirical_hbase


def fit(mx):
    H_PORT = empirical_hbase(mx)
    return {"H_PORT": H_PORT}


def predict(state, app_rows, weeks):
    H_PORT = state["H_PORT"]
    seen = app_rows[app_rows["step_k"] <= weeks]
    n_seen = len(seen)
    r = seen[seen["weeks_obs"] >= seen["step_k"]]
    g = r.groupby("step_k").agg(surv=("survived", "sum"), n=("survived", "size"))
    if len(g) == 0:
        return pd.Series(np.cumprod((1 - H_PORT.fillna(0)).values), index=range(1, HMAX + 1))
    g["h_app"] = 1 - g["surv"] / g["n"]
    common_steps = [k for k in g.index if k <= HMAX and not np.isnan(H_PORT.get(k, np.nan))
                     and H_PORT.get(k, 0) > 0 and g.loc[k, "n"] >= 20]
    if len(common_steps) > 0:
        ratio = np.clip(g.loc[common_steps, "h_app"].values / H_PORT.loc[common_steps].values, 0.2, 5.0)
        log_hr = np.average(np.log(ratio), weights=g.loc[common_steps, "n"].values)
    else:
        log_hr = 0.0
    hr = np.exp(log_hr * (n_seen / (n_seen + K_SHRINK)))
    h = (H_PORT * hr).clip(0.001, 0.999)
    return pd.Series(np.cumprod((1 - h.fillna(0)).values), index=range(1, HMAX + 1))
