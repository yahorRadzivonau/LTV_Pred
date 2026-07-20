"""
МОДЕЛЬ C — гибрид (рекомендуемая production-модель).

Три слоя: форма (h_base — эмпирическая медиана по аппам, как в models/empirical.py),
рычаги (rr — множитель уровня приложения из logreg-модели по составу юзеров:
гео/сорс/план/когорта, один множитель на весь апп, зажат в RR_CLIP), липкость
(hr — hazard-ratio по фактически видимым неделям данных, с усадкой к 1.0 через
K_SHRINK). hazard[k] = h_base[k] * rr * hr.

Перенесено дословно из archive/unified_hybrid.py.
Последние известные цифры (медиана |отн. ошибки|): нед12 3-7% | нед26 5-12% | нед52 12-16%.
"""
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from core.common import HMAX, K_SHRINK, RR_CLIP, empirical_hbase

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


def _portfolio_step_hazard_from_model(model, train):
    """средний РИСК портфеля по ступеням, как его видит logreg — знаменатель для rr.
       усредняем предсказание модели по реальному распределению рычагов обучающих строк."""
    samp = train.sample(min(50000, len(train)), random_state=0).copy()
    reps = pd.concat([samp[COMBO].assign(step_k=k) for k in range(1, HMAX + 1)], ignore_index=True)
    reps["step_bin"] = reps["step_k"].clip(upper=HMAX).astype(int).astype(str)
    risk = 1 - model.predict_proba(reps[CAT + NUM])[:, 1]
    reps["risk"] = risk
    return reps.groupby("step_k")["risk"].mean().reindex(range(1, HMAX + 1))


def fit(mx_clean):
    tops = _fit_tops(mx_clean)
    prepped = _prep_features(mx_clean, tops)
    H_BASE = empirical_hbase(prepped)     # форма кривой из эмпирики

    train = prepped[prepped["weeks_obs"] >= prepped["step_k"] + 1].copy()
    pre = ColumnTransformer([
        ("c", OneHotEncoder(handle_unknown="ignore", min_frequency=50), CAT),
        ("n", "passthrough", NUM)])
    base = Pipeline([("pre", pre), ("lr", LogisticRegression(max_iter=1000))])
    base.fit(train[CAT + NUM], train["survived"])
    H_PORT_MODEL = _portfolio_step_hazard_from_model(base, train)   # знаменатель для rr

    return {"H_BASE": H_BASE, "BASE": base, "H_PORT_MODEL": H_PORT_MODEL, "tops": tops, "_cache": {}}


def _precompute_app(state, app_rows):
    """rr аппа = (риск по рычагам его юзеров) / (средний риск портфеля по модели), усреднён по ступеням.
       Один множитель на весь состав юзеров аппа (не зависит от недели — устойчиво)."""
    app_rows = _prep_features(app_rows, state["tops"])
    subs = app_rows.sort_values("step_k").groupby("sub_id").first().reset_index()
    combos = subs.groupby(COMBO).size().rename("w").reset_index()
    reps = pd.concat([combos[COMBO].assign(step_k=k) for k in range(1, HMAX + 1)], ignore_index=True)
    reps["step_bin"] = reps["step_k"].clip(upper=HMAX).astype(int).astype(str)
    reps["risk"] = 1 - state["BASE"].predict_proba(reps[CAT + NUM])[:, 1]
    reps = reps.merge(combos[COMBO + ["w"]], on=COMBO, how="left")
    app_risk_by_step = (reps.assign(rw=reps["risk"] * reps["w"])
                        .groupby("step_k").apply(lambda d: d["rw"].sum() / d["w"].sum()))
    rr_series = (app_risk_by_step / state["H_PORT_MODEL"]).reindex(range(1, HMAX + 1))
    rr = float(np.clip(rr_series.mean(), *RR_CLIP))   # один множитель рычагов на апп

    # hr (липкость) — наблюдаемая смертность vs h_base на виденных ступенях
    obs = app_rows[app_rows["weeks_obs"] >= app_rows["step_k"] + 1].copy()
    return combos["w"].values, rr, obs


def predict(state, app_rows, weeks):
    key = app_rows["app_id"].iat[0]
    if key not in state["_cache"]:
        state["_cache"][key] = _precompute_app(state, app_rows)
    w, rr, obs = state["_cache"][key]
    H_BASE = state["H_BASE"]

    seen = obs[obs["step_k"] <= weeks]
    n_seen = len(seen)
    if n_seen > 0:
        g = seen.groupby("step_k").agg(obs_die=("died", "mean"), n=("died", "size"))
        g = g[g["n"] >= 20]
        # ожидаемый риск на виденных ступенях = h_base * rr; hr добивает остаток
        exp_die = (H_BASE.reindex(g.index) * rr).clip(0.001, 0.999)
        valid = exp_die > 0
        if valid.sum() > 0:
            log_hr = np.average(np.log(np.clip(g["obs_die"][valid] / exp_die[valid], 0.2, 5.0)),
                                weights=g["n"][valid])
        else:
            log_hr = 0.0
    else:
        log_hr = 0.0
    hr = np.exp(log_hr * (n_seen / (n_seen + K_SHRINK)))
    haz = np.clip(H_BASE.values * rr * hr, 0.001, 0.999)   # форма x рычаги x липкость
    return pd.Series(np.cumprod(1 - haz), index=range(1, HMAX + 1))
