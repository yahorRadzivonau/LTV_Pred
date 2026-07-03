"""
МОДЕЛЬ B — logreg (чистая версия, без sample_weight по аппам).

Форма кривой предсказывается логистической регрессией по рычагам
(step_bin/geo/media_source/attribution_source/plan_interval/cohort_month/
billing_day_of_month/trial_days). Поверх модельной формы — hazard-ratio (hr)
конкретного приложения по видимым неделям данных, с усадкой к 1.0 (K_SHRINK).

Перенесено дословно из archive/unified_backtest.py — версия БЕЗ sample_weight,
она стабильнее варианта с "равным голосом аппа" из archive/unified_model.py.
Последние известные цифры (медиана |отн. ошибки|): нед12 2-5% | нед26 13-17% | нед52 20-30%.
"""
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from models.common import HMAX, K_SHRINK

CAT = ["step_bin", "geo", "media_source", "attribution_source", "plan_interval", "cohort_month"]
NUM = ["billing_day_of_month", "trial_days"]
COMBO = ["geo", "media_source", "attribution_source", "plan_interval",
         "cohort_month", "billing_day_of_month", "trial_days"]


def _fit_tops(mx):
    return {col: mx[col].value_counts().nlargest(30).index
            for col in ["geo", "media_source", "attribution_source"]}


def _prep_features(mx, tops):
    mx = mx.copy()
    mx["step_bin"] = mx["step_k"].clip(upper=HMAX).astype(int).astype(str)
    for col, top in tops.items():
        mx[col] = mx[col].where(mx[col].isin(top), "other")
    mx["trial_days"] = mx["trial_days"].fillna(-1)
    return mx


def fit(mx_clean):
    tops = _fit_tops(mx_clean)
    prepped = _prep_features(mx_clean, tops)
    train = prepped[prepped["weeks_obs"] >= prepped["step_k"] + 1].copy()

    pre = ColumnTransformer([
        ("c", OneHotEncoder(handle_unknown="ignore", min_frequency=50), CAT),
        ("n", "passthrough", NUM)])
    base = Pipeline([("pre", pre), ("lr", LogisticRegression(max_iter=1000))])
    base.fit(train[CAT + NUM], train["survived"])

    return {"BASE": base, "tops": tops, "_cache": {}}


def _precompute_app(state, app_rows):
    app_rows = _prep_features(app_rows, state["tops"])
    subs = app_rows.sort_values("step_k").groupby("sub_id").first().reset_index()
    combos = subs.groupby(COMBO).size().rename("w").reset_index()
    n = len(combos)
    reps = combos.loc[combos.index.repeat(HMAX)].reset_index(drop=True)
    reps["step_k"] = np.tile(np.arange(1, HMAX + 1), n)
    reps["step_bin"] = reps["step_k"].clip(upper=HMAX).astype(int).astype(str)
    p = state["BASE"].predict_proba(reps[CAT + NUM])[:, 1].reshape(n, HMAX)
    obs = app_rows[app_rows["weeks_obs"] >= app_rows["step_k"] + 1].copy()
    if len(obs):
        obs["exp_die"] = 1 - state["BASE"].predict_proba(obs[CAT + NUM])[:, 1]
        obs["obs_die"] = 1 - obs["survived"]
    return p, combos["w"].values, obs


def predict(state, app_rows, weeks):
    key = app_rows["app_id"].iat[0]
    if key not in state["_cache"]:
        state["_cache"][key] = _precompute_app(state, app_rows)
    p, w, obs = state["_cache"][key]

    seen = obs[obs["step_k"] <= weeks]
    n_seen = len(seen)
    if n_seen > 0:
        g = seen.groupby("step_k").agg(o=("obs_die", "mean"), e=("exp_die", "mean"), n=("obs_die", "size"))
        g = g[(g["n"] >= 20) & (g["e"] > 0)]
        log_hr = (np.average(np.log(np.clip(g["o"] / g["e"], 0.2, 5.0)), weights=g["n"])
                  if len(g) else 0.0)
    else:
        log_hr = 0.0
    hr = np.exp(log_hr * (n_seen / (n_seen + K_SHRINK)))
    haz = np.clip((1 - p) * hr, 0.001, 0.999)
    curves = np.cumprod(1 - haz, axis=1)
    ww = w[:, None]
    return pd.Series((curves * ww).sum(axis=0) / ww.sum(), index=range(1, HMAX + 1))
