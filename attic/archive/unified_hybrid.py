import pandas as pd, numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import OneHotEncoder
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline

MX = r"C:\Users\yahor\PycharmProjects\LTV\se_training.parquet"
HMAX = 52
K_SHRINK = 800
MIN_PAYERS = 500
MIN_MATURE = 200
HORIZONS = [12, 26, 52]
PRED_WEEKS_LIST = [1, 2, 3, 4, 5, 6, 7, 8]
CRASH_RET_THRESHOLD = 0.65
CRASH_MIN_RISK = 50
RR_CLIP = (0.4, 2.5)   # ограничение множителя рычагов, чтобы не улетал

mx = pd.read_parquet(MX)
mx["survived"] = mx["outcome"].isin(["renewed", "recovered"]).astype(int)
mx["died"] = 1 - mx["survived"]
mx["step_bin"] = mx["step_k"].clip(upper=HMAX).astype(int).astype(str)
for col in ["geo", "media_source", "attribution_source"]:
    top = mx[col].value_counts().nlargest(30).index
    mx[col] = mx[col].where(mx[col].isin(top), "other")
mx["trial_days"] = mx["trial_days"].fillna(-1)

CAT = ["step_bin", "geo", "media_source", "attribution_source", "plan_interval", "cohort_month"]
NUM = ["billing_day_of_month", "trial_days"]
COMBO = ["geo", "media_source", "attribution_source", "plan_interval",
         "cohort_month", "billing_day_of_month", "trial_days"]

# ---------------- функции ----------------

def direct_survival(rows):
    per = rows.groupby("sub_id").agg(max_step=("step_k", "max"), weeks_obs=("weeks_obs", "first"))
    fact, mature = {}, {}
    for k in range(1, HMAX + 1):
        ar = per[per["weeks_obs"] >= k]
        mature[k] = len(ar)
        fact[k] = (ar["max_step"] >= k + 1).mean() if len(ar) > 0 else np.nan
    return pd.Series(fact), mature, per

def detect_crash(per):
    worst = 1.0
    for k in range(2, HMAX + 1):
        at_risk = (per["weeks_obs"] >= k) & (per["max_step"] >= k - 1)
        if at_risk.sum() >= CRASH_MIN_RISK:
            worst = min(worst, (per.loc[at_risk, "max_step"] >= k).mean())
    return worst < CRASH_RET_THRESHOLD

def empirical_hbase(rows, min_risk=30):
    """ФОРМА: медианный по аппам РИСК смерти на каждой ступени (censored). Держит хвост."""
    r = rows[rows["weeks_obs"] >= rows["step_k"]]
    g = r.groupby(["app_id", "step_k"]).agg(surv=("survived", "sum"), n=("survived", "size")).reset_index()
    g = g[g["n"] >= min_risk]
    med_surv = g.groupby("step_k").apply(lambda d: (d["surv"] / d["n"]).median()).reindex(range(1, HMAX + 1))
    return (1 - med_surv)   # h_base[k]

def portfolio_step_hazard_from_model(model):
    """средний РИСК портфеля по ступеням, как его видит logreg — знаменатель для rr.
       усредняем предсказание модели по реальному распределению рычагов обучающих строк."""
    samp = TRAIN.sample(min(50000, len(TRAIN)), random_state=0).copy()
    base = pd.DataFrame(np.repeat(samp[COMBO].values, 1, axis=0), columns=COMBO)
    # для каждой ступени прогоняем этот же набор рычагов и берём средний риск
    out = {}
    reps = pd.concat([samp[COMBO].assign(step_k=k) for k in range(1, HMAX + 1)], ignore_index=True)
    reps["step_bin"] = reps["step_k"].clip(upper=HMAX).astype(int).astype(str)
    risk = 1 - model.predict_proba(reps[CAT + NUM])[:, 1]
    reps["risk"] = risk
    m = reps.groupby("step_k")["risk"].mean().reindex(range(1, HMAX + 1))
    return m   # h_port_model[k]

def precompute_app(app_rows):
    """rr аппа = (риск по рычагам его юзеров) / (средний риск портфеля по модели), усреднён по ступеням.
       Один множитель на весь состав юзеров аппа (не зависит от недели — устойчиво)."""
    subs = app_rows.sort_values("step_k").groupby("sub_id").first().reset_index()
    combos = subs.groupby(COMBO).size().rename("w").reset_index()
    # риск юзеров аппа по модели, усреднённый по ступеням (та же сетка, что h_port_model)
    reps = pd.concat([combos[COMBO].assign(step_k=k) for k in range(1, HMAX + 1)], ignore_index=True)
    reps["step_bin"] = reps["step_k"].clip(upper=HMAX).astype(int).astype(str)
    reps["risk"] = 1 - BASE.predict_proba(reps[CAT + NUM])[:, 1]
    reps = reps.merge(combos[COMBO + ["w"]], on=COMBO, how="left")
    app_risk_by_step = (reps.assign(rw=reps["risk"] * reps["w"])
                        .groupby("step_k").apply(lambda d: d["rw"].sum() / d["w"].sum()))
    rr_series = (app_risk_by_step / H_PORT_MODEL).reindex(range(1, HMAX + 1))
    rr = float(np.clip(rr_series.mean(), *RR_CLIP))   # один множитель рычагов на апп

    # hr (липкость) — как раньше: наблюдаемая смертность vs h_base на виденных ступенях
    obs = app_rows[app_rows["weeks_obs"] >= app_rows["step_k"] + 1].copy()
    return combos["w"].values, rr, obs

def predict(w, rr, obs, weeks):
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
    haz = np.clip(H_BASE.values * rr * hr, 0.001, 0.999)   # форма × рычаги × липкость
    return pd.Series(np.cumprod(1 - haz), index=range(1, HMAX + 1)), rr, hr

# ---------- 1. факт + обвалы ----------
payers = mx.groupby("app_id")["sub_id"].nunique()
apps = payers[payers >= MIN_PAYERS].index.tolist()
facts, matures, crash_flag = {}, {}, {}
for app in apps:
    f, mt, per = direct_survival(mx[mx["app_id"] == app])
    facts[app], matures[app] = f, mt
    crash_flag[app] = detect_crash(per)
crash_apps = {a for a in apps if crash_flag[a]}
print(f"аппов: {len(apps)} | нормальных: {len(apps)-len(crash_apps)} | обвальных: {len(crash_apps)}")

# ---------- 2. форма (медиана, без обвалов) + logreg (без обвалов) ----------
clean = mx[~mx["app_id"].isin(crash_apps)]
H_BASE = empirical_hbase(clean)     # форма кривой из эмпирики

TRAIN = clean[clean["weeks_obs"] >= clean["step_k"] + 1].copy()
pre = ColumnTransformer([
    ("c", OneHotEncoder(handle_unknown="ignore", min_frequency=50), CAT),
    ("n", "passthrough", NUM)])
BASE = Pipeline([("pre", pre), ("lr", LogisticRegression(max_iter=1000))])
print("обучаю logreg для рычагов...")
BASE.fit(TRAIN[CAT + NUM], TRAIN["survived"])
H_PORT_MODEL = portfolio_step_hazard_from_model(BASE)   # знаменатель для rr
print("готово\n")

# ---------- 3. предрасчёт ----------
cache = {}
for i, app in enumerate(apps):
    if not crash_flag[app]:
        cache[app] = precompute_app(mx[mx["app_id"] == app])
    if (i + 1) % 10 == 0: print(f"  предрасчёт {i+1}/{len(apps)}")
print()

# ---------- 4. развёртка ----------
print("=== ГИБРИД (форма=медиана, рычаги=logreg rr, липкость=hr) ===")
print("данных | " + " | ".join(f"нед{h:<8}" for h in HORIZONS) + " | аппов")
for pw in PRED_WEEKS_LIST:
    errs = {h: [] for h in HORIZONS}; signed = {h: [] for h in HORIZONS}; n_apps = 0
    for app in apps:
        if crash_flag[app]:
            continue
        w, rr, obs = cache[app]
        pred, _, _ = predict(w, rr, obs, pw)
        used = False
        for h in HORIZONS:
            if matures[app].get(h, 0) >= MIN_MATURE and not np.isnan(facts[app].get(h, np.nan)):
                f, pr = facts[app][h], pred[h]
                if f > 0:
                    errs[h].append(abs(pr - f) / f); signed[h].append((pr - f) / f); used = True
        if used: n_apps += 1
    line = f"  {pw:2d}нед |"
    for h in HORIZONS:
        med = np.median(errs[h]) * 100 if errs[h] else float("nan")
        sgn = np.median(signed[h]) * 100 if signed[h] else float("nan")
        line += f"  |{med:3.0f}%| зн{sgn:+3.0f}% |"
    line += f"  {n_apps}"
    print(line)

print("\nэмпирика: нед12 3-7% | нед26 5-12% | нед52 7-18%")
print("logreg-unified: нед12 2-5% | нед26 13-17% | нед52 20-30%")