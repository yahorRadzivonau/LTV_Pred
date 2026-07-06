import pyarrow.parquet as pq
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import OneHotEncoder
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline

SRC   = r"C:\Users\yahor\PycharmProjects\LTV\se_training.parquet"
APPS  = ["id6473738977", "id6469790837", "id6499426756"]  # большой / липкий / средний
HMAX  = 52          # горизонт недель
WEEKS = [1, 2, 3, 4, 5]   # по скольким неделям данных делаем прогноз
K_SHRINK = 400      # сила притяга поправки аппа к среднему (n/(n+K))

# ---------- загрузка матрицы ----------
mx = pd.read_parquet(SRC)
mx["survived"] = mx["outcome"].isin(["renewed", "recovered"]).astype(int)
mx["step_bin"] = mx["step_k"].clip(upper=13).astype(int).astype(str).replace("13", "13+")
for col in ["geo", "media_source", "attribution_source"]:
    top = mx[col].value_counts().nlargest(30).index
    mx[col] = mx[col].where(mx[col].isin(top), "other")
mx["trial_days"] = mx["trial_days"].fillna(-1)

CAT = ["step_bin", "geo", "media_source", "attribution_source", "plan_interval", "cohort_month"]
NUM = ["billing_day_of_month", "trial_days"]

def base_hazard_curve(df):
    """реальная доля выживших шаг-к-шагу 1..HMAX по данному набору строк"""
    g = df[df["step_k"] <= HMAX].groupby("step_k")["survived"].mean()
    return g.reindex(range(1, HMAX + 1))

def survival_from_steps(step_surv):
    """перемножаем пошаговые P(пережить) -> кривая дожития (дискретное ожидание)"""
    s = np.cumprod(step_surv.ffill().values)
    return s

for app in APPS:
    app_rows = mx[mx["app_id"] == app]

    # ---------- 1. БАЗОВАЯ модель на всех ДРУГИХ аппах (апп не подсматриваем) ----------
    other = mx[mx["app_id"] != app]
    pre = ColumnTransformer([
        ("c", OneHotEncoder(handle_unknown="ignore", min_frequency=50), CAT),
        ("n", "passthrough", NUM),
    ])
    base_model = Pipeline([("pre", pre), ("lr", LogisticRegression(max_iter=1000))])
    base_model.fit(other[CAT + NUM], other["survived"])

    # базовый прогноз пошагового дожития для "типичной" строки этого аппа,
    # считаем по реальному составу юзеров аппа, но БЕЗ его собственной поправки
    def base_step_surv(rows):
        if len(rows) == 0:
            return pd.Series(np.nan, index=range(1, HMAX + 1))
        p = base_model.predict_proba(rows[CAT + NUM])[:, 1]
        tmp = rows.assign(p=p)
        return tmp[tmp["step_k"] <= HMAX].groupby("step_k")["p"].mean().reindex(range(1, HMAX + 1))

    # ---------- 2. ФАКТ: реальная кривая аппа по зрелым юзерам ----------
    fact_steps = base_hazard_curve(app_rows)
    fact_curve = survival_from_steps(fact_steps)

    # ---------- 3. прогнозы по N неделям ----------
    plt.figure(figsize=(9, 5.5))
    plt.plot(range(1, HMAX + 1), fact_curve, color="black", lw=3, label="ФАКТ (реальная кривая)")

    portfolio_step = other.groupby("step_k")["survived"].mean()  # средняя по портфелю

    for w in WEEKS:
        # обрезаем историю аппа: видим только ступеньки <= w
        seen = app_rows[app_rows["step_k"] <= w]
        n_seen = len(seen)

        # поправка аппа = (реальное дожитие аппа на виденных ступенях) - (база модели на тех же),
        # усаженная к нулю по объёму данных: вес = n/(n+K)
        shrink = n_seen / (n_seen + K_SHRINK)
        app_seen_surv  = seen.groupby("step_k")["survived"].mean()
        base_seen_surv = base_step_surv(seen)
        # поправка в вероятностной шкале, усаженная
        delta = (app_seen_surv - base_seen_surv).reindex(range(1, HMAX + 1))
        delta_filled = delta.fillna(0) * shrink  # неизвестные ступени -> поправка 0 (=как портфель)

        # прогноз пошагового дожития = база модели (на всех ступенях) + усаженная поправка
        base_all = base_step_surv(app_rows)         # форма по составу юзеров аппа
        pred_step = (base_all + delta_filled).clip(0.01, 0.999)
        pred_curve = survival_from_steps(pred_step)

        plt.plot(range(1, HMAX + 1), pred_curve, lw=1.6, alpha=0.8,
                 label=f"прогноз по {w} нед (n={n_seen}, shrink={shrink:.2f})")

    plt.title(f"Дожитие: факт vs прогноз по N неделям — {app}")
    plt.xlabel("неделя (= номер платежа)")
    plt.ylabel("доля доживших")
    plt.ylim(0, 1)
    plt.grid(alpha=0.25)
    plt.legend(fontsize=8)
    plt.tight_layout()
    out = rf"C:\Users\yahor\PycharmProjects\LTV\forecast_{app}.png"
    plt.savefig(out, dpi=130)
    print("сохранил:", out)

plt.show()