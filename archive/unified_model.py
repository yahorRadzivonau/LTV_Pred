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

mx = pd.read_parquet(MX)
mx["survived"] = mx["outcome"].isin(["renewed", "recovered"]).astype(int)
mx["step_bin"] = mx["step_k"].clip(upper=HMAX).astype(int).astype(str)   # каждая ступень = категория
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
    """Обвал = была ступенька, где пошаговое удержание разом упало ниже порога
       (>35% ушло за один шаг). Считаем по ВСЕЙ кривой.
       Классификация ПОСТ-ФАКТУМ по полной истории — для отчёта и чистки базы
       от известных управленческих событий (поднятие цены). В прогноз не попадает."""
    worst = 1.0
    for k in range(2, HMAX + 1):
        at_risk = (per["weeks_obs"] >= k) & (per["max_step"] >= k - 1)
        if at_risk.sum() >= CRASH_MIN_RISK:
            worst = min(worst, (per.loc[at_risk, "max_step"] >= k).mean())
    return worst < CRASH_RET_THRESHOLD

def precompute_app(app_rows):
    """один раз на апп: кривые p(пережить шаг) для каждого комбо + веса + exp-смертность"""
    subs = app_rows.sort_values("step_k").groupby("sub_id").first().reset_index()
    combos = subs.groupby(COMBO).size().rename("w").reset_index()
    n = len(combos)
    reps = combos.loc[combos.index.repeat(HMAX)].reset_index(drop=True)
    reps["step_k"] = np.tile(np.arange(1, HMAX + 1), n)
    reps["step_bin"] = reps["step_k"].clip(upper=HMAX).astype(int).astype(str)  # как в обучении
    p = BASE.predict_proba(reps[CAT + NUM])[:, 1].reshape(n, HMAX)
    obs = app_rows[app_rows["weeks_obs"] >= app_rows["step_k"] + 1].copy()
    if len(obs):
        obs["exp_die"] = 1 - BASE.predict_proba(obs[CAT + NUM])[:, 1]
        obs["obs_die"] = 1 - obs["survived"]
    return p, combos["w"].values, obs

def predict_from_cache(p, w, obs, weeks):
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

# ---------- 1. факт + классификация обвалов (ДО обучения базы) ----------
payers = mx.groupby("app_id")["sub_id"].nunique()
apps = payers[payers >= MIN_PAYERS].index.tolist()

facts, matures, crash_flag = {}, {}, {}
for app in apps:
    f, mt, per = direct_survival(mx[mx["app_id"] == app])
    facts[app], matures[app] = f, mt
    crash_flag[app] = detect_crash(per)

crash_apps = {a for a in apps if crash_flag[a]}
n_normal = len(apps) - len(crash_apps)
print(f"аппов: {len(apps)} | нормальных: {n_normal} | обвальных (исключены из базы и оценки): {len(crash_apps)}")

# ---------- 2. базовая модель: ТОЛЬКО нормальные аппы + равный голос аппа ----------
# Обвалы = известные управленческие события (поднятие цены), не органический отток.
# База моделирует органику; поднятие цены — отдельная надстройка (overlay).
# sample_weight = 1/строк_аппа: каждый апп вносит равный вклад
# (аналог "1 апп = 1 голос" из эмпирической медианы).
train = mx[(~mx["app_id"].isin(crash_apps)) & (mx["weeks_obs"] >= mx["step_k"] + 1)].copy()
app_sizes = train.groupby("app_id")["sub_id"].transform("size")
train_w = (1.0 / app_sizes).values

pre = ColumnTransformer([
    ("c", OneHotEncoder(handle_unknown="ignore", min_frequency=50), CAT),
    ("n", "passthrough", NUM)])
BASE = Pipeline([("pre", pre), ("lr", LogisticRegression(max_iter=1000))])
print("обучаю базовую модель (без обвальных аппов, равный голос)...")
BASE.fit(train[CAT + NUM], train["survived"], lr__sample_weight=train_w)
print("готово\n")

# ---------- 3. предрасчёт по аппам ----------
cache = {}
for i, app in enumerate(apps):
    if not crash_flag[app]:
        cache[app] = precompute_app(mx[mx["app_id"] == app])
    if (i + 1) % 10 == 0:
        print(f"  предрасчёт {i + 1}/{len(apps)}")
print()

# ---------- 4. развёртка по неделям данных ----------
print("=== UNIFIED (без бинов, чистая база, равный голос): точность ===")
print("данных | " + " | ".join(f"нед{h:<8}" for h in HORIZONS) + " | аппов")
for pw in PRED_WEEKS_LIST:
    errs   = {h: [] for h in HORIZONS}
    signed = {h: [] for h in HORIZONS}
    n_apps = 0
    for app in apps:
        if crash_flag[app]:
            continue
        p, w, obs = cache[app]
        pred = predict_from_cache(p, w, obs, pw)
        used = False
        for h in HORIZONS:
            if matures[app].get(h, 0) >= MIN_MATURE and not np.isnan(facts[app].get(h, np.nan)):
                f, pr = facts[app][h], pred[h]
                if f > 0:
                    errs[h].append(abs(pr - f) / f)
                    signed[h].append((pr - f) / f)
                    used = True
        if used:
            n_apps += 1
    line = f"  {pw:2d}нед |"
    for h in HORIZONS:
        med = np.median(errs[h]) * 100   if errs[h]   else float("nan")
        sgn = np.median(signed[h]) * 100 if signed[h] else float("nan")
        line += f"  |{med:3.0f}%| зн{sgn:+3.0f}% |"
    line += f"  {n_apps}"
    print(line)

print("\nориентир (эмпирика): нед12 3-7% | нед26 5-12% | нед52 7-18%, знак -0..-6")