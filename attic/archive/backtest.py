import pyarrow.parquet as pq
import pandas as pd
import numpy as np

MX = r"C:\Users\yahor\PycharmProjects\LTV\se_training.parquet"
HMAX = 52
K_SHRINK = 800
MIN_PAYERS = 500
MIN_MATURE = 200
HORIZONS = [12, 26, 52]
PRED_WEEKS_LIST = [1, 2, 3, 4, 5, 6, 7, 8]
CRASH_RET_THRESHOLD = 0.65   # пошаговое удержание ниже этого = обвал (>35% ушло за шаг)
CRASH_MIN_RISK = 50          # столько юзеров должно быть на ступени, чтобы судить об обвале

mx = pd.read_parquet(MX)
mx["survived"] = mx["outcome"].isin(["renewed", "recovered"]).astype(int)

def portfolio_hazard_median(rows, min_risk=30):
    r = rows[rows["weeks_obs"] >= rows["step_k"]]
    g = r.groupby(["app_id", "step_k"]).agg(surv=("survived","sum"), n=("survived","size")).reset_index()
    g = g[g["n"] >= min_risk]
    med = g.groupby("step_k").apply(lambda d: (d["surv"]/d["n"]).median()).reindex(range(1, HMAX+1))
    return 1 - med

H_PORT = portfolio_hazard_median(mx)

def direct_survival(rows):
    per = rows.groupby("sub_id").agg(max_step=("step_k","max"), weeks_obs=("weeks_obs","first"))
    fact, mature = {}, {}
    for k in range(1, HMAX+1):
        ar = per[per["weeks_obs"] >= k]
        mature[k] = len(ar)
        fact[k] = (ar["max_step"] >= k+1).mean() if len(ar) > 0 else np.nan
    return pd.Series(fact), mature, per

def detect_crash(per):
    """Обвал = была ступенька, где пошаговое удержание разом упало ниже порога
       (>35% ушло за один шаг). Считаем по ВСЕЙ кривой, без привязки к конкретной неделе.
       ВАЖНО: классификация ПОСТ-ФАКТУМ по полной истории — нужна только для отчёта
       'модель не отвечает за обвалы цены'. В сам прогноз эта инфа НЕ попадает."""
    worst = 1.0
    for k in range(2, HMAX+1):
        at_risk = (per["weeks_obs"] >= k) & (per["max_step"] >= k-1)  # дошёл до k-1, было время на k
        if at_risk.sum() >= CRASH_MIN_RISK:
            ret = (per.loc[at_risk, "max_step"] >= k).mean()          # пережил ли шаг k
            worst = min(worst, ret)
    return worst < CRASH_RET_THRESHOLD

def predict_curve(app_rows, weeks):
    seen = app_rows[app_rows["step_k"] <= weeks]
    n_seen = len(seen)
    r = seen[seen["weeks_obs"] >= seen["step_k"]]
    g = r.groupby("step_k").agg(surv=("survived","sum"), n=("survived","size"))
    if len(g) == 0:
        return pd.Series(np.cumprod((1-H_PORT.fillna(0)).values), index=range(1,HMAX+1))
    g["h_app"] = 1 - g["surv"]/g["n"]
    common = [k for k in g.index if k <= HMAX and not np.isnan(H_PORT.get(k,np.nan))
              and H_PORT.get(k,0) > 0 and g.loc[k,"n"] >= 20]
    if len(common) > 0:
        ratio = np.clip(g.loc[common,"h_app"].values / H_PORT.loc[common].values, 0.2, 5.0)
        log_hr = np.average(np.log(ratio), weights=g.loc[common,"n"].values)
    else:
        log_hr = 0.0
    hr = np.exp(log_hr * (n_seen/(n_seen+K_SHRINK)))
    h = (H_PORT * hr).clip(0.001, 0.999)
    return pd.Series(np.cumprod((1-h.fillna(0)).values), index=range(1,HMAX+1))

# ---------- предрасчёт факта + классификация обвалов ----------
payers = mx.groupby("app_id")["sub_id"].nunique()
apps = payers[payers >= MIN_PAYERS].index.tolist()

facts, matures, crash_flag = {}, {}, {}
for app in apps:
    ar = mx[mx["app_id"]==app]
    f, mt, per = direct_survival(ar)
    facts[app], matures[app] = f, mt
    crash_flag[app] = detect_crash(per)

n_normal = sum(not crash_flag[a] for a in apps)
n_crash  = sum(crash_flag[a] for a in apps)
print(f"аппов всего: {len(apps)}  |  нормальных: {n_normal}  |  обвальных (исключены): {n_crash}\n")

# ---------- развёртка по неделям данных ----------
print("=== ТОЧНОСТЬ по неделям данных (только НОРМАЛЬНЫЕ аппы) ===")
print("формат ячейки: |отн.ошибка|  (знак: + завышаем / - занижаем)\n")
header = "данных | " + " | ".join(f"нед{h:<8}" for h in HORIZONS) + " | аппов"
print(header)
for pw in PRED_WEEKS_LIST:
    errs   = {h: [] for h in HORIZONS}
    signed = {h: [] for h in HORIZONS}
    n_apps = 0
    for app in apps:
        if crash_flag[app]:
            continue
        pred = predict_curve(mx[mx["app_id"]==app], pw)
        used = False
        for h in HORIZONS:
            if matures[app].get(h,0) >= MIN_MATURE and not np.isnan(facts[app].get(h,np.nan)):
                f, p = facts[app][h], pred[h]
                if f > 0:
                    errs[h].append(abs(p-f)/f)
                    signed[h].append((p-f)/f)
                    used = True
        if used:
            n_apps += 1
    line = f"  {pw:2d}нед |"
    for h in HORIZONS:
        med = np.median(errs[h])*100   if errs[h]   else float("nan")
        sgn = np.median(signed[h])*100 if signed[h] else float("nan")
        line += f"  |{med:3.0f}%| зн{sgn:+3.0f}% |"
    line += f"  {n_apps}"
    print(line)

print("\n|N%| = медиана абсолютной отн.ошибки; зн = медиана знаковой (куда врём)")