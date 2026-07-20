import pyarrow.parquet as pq
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

MX     = r"C:\Users\yahor\PycharmProjects\LTV\se_training.parquet"
APPS   = ["id6473738977", "id6469790837", "id6499426756"]
HMAX   = 52
WEEKS  = [1, 2, 3, 4, 5]
K_SHRINK = 800

mx = pd.read_parquet(MX)
assert "weeks_obs" in mx.columns, "пересобери se_training.py"
mx["survived"] = mx["outcome"].isin(["renewed", "recovered"]).astype(int)
mx["died"] = 1 - mx["survived"]

# ---------- ФАКТ прямым способом: доля доживших до ступени k ----------
# у юзера "максимально достигнутая ступень" = max(step_k). дожил до k, если max_step >= k.
# в риск на ступени k берём только тех, у кого было время (weeks_obs >= k).
def direct_survival(rows):
    per_sub = rows.groupby("sub_id").agg(max_step=("step_k", "max"),
                                         weeks_obs=("weeks_obs", "first"))
    out = []
    for k in range(1, HMAX + 1):
        at_risk = per_sub[per_sub["weeks_obs"] >= k]
        if len(at_risk) == 0:
            out.append(np.nan)
        else:
            # пережил ступень k = дошёл ДО СЛЕДУЮЩЕЙ (max_step >= k+1), как и hazard-прогноз
            out.append((at_risk["max_step"] >= k + 1).mean())
    return pd.Series(out, index=range(1, HMAX + 1))

# ---------- hazard портфеля (censored) для прогноза ----------
def step_hazard_per_app(rows):
    """пошаговый РИСК (1 - удержание) по КАЖДОМУ аппу: вернёт DataFrame [app_id, step_k, p_step, risk]"""
    r = rows[rows["weeks_obs"] >= rows["step_k"]].copy()
    # дожил до k = survived на этой ступени; at-risk = был на ступени k
    g = r.groupby(["app_id", "step_k"]).agg(
        survived=("survived", "sum"),
        n=("survived", "size")).reset_index()
    g["p_step"] = g["survived"] / g["n"]      # доля переживших шаг
    return g

def portfolio_hazard_median(rows, min_risk=30):
    """риск портфеля = 1 - медиана(по аппам) пошагового удержания. Устойчиво к обвалам."""
    g = step_hazard_per_app(rows)
    g = g[g["n"] >= min_risk]                  # только аппы с объёмом на ступени
    med = g.groupby("step_k")["p_step"].median()   # медиана удержания по аппам
    p = med.reindex(range(1, HMAX + 1))
    return (1 - p)
    # возвращаем РИСК (hazard), как старая функция
def survival_from_hazard(haz):
    return np.cumprod((1 - haz.fillna(0)).values)

for app in APPS:
    app_rows = mx[mx["app_id"] == app]
    other    = mx[mx["app_id"] != app]

    h_port = portfolio_hazard_median(other)
    fact_curve = direct_survival(app_rows)          # <-- ПРЯМОЙ факт, не cumprod

    plt.figure(figsize=(9, 5.5))
    plt.plot(range(1, HMAX + 1), fact_curve.values, color="black", lw=3, label="ФАКТ (прямой)")

    print(f"\n=== {app} ===")
    for w in WEEKS:
        seen = app_rows[app_rows["step_k"] <= w]
        n_seen = len(seen)
        h_app_seen = portfolio_hazard_median(seen)
        common = (~h_app_seen.isna()) & (~h_port.isna()) & (h_port > 0)
        if common.sum() > 0:
            ratio = (h_app_seen[common] / h_port[common]).clip(0.2, 5.0)
            wts = seen[seen["weeks_obs"] >= seen["step_k"]].groupby("step_k").size().reindex(ratio.index).fillna(0)
            log_hr = np.average(np.log(ratio), weights=wts) if wts.sum() > 0 else 0.0
        else:
            log_hr = 0.0
        hr = np.exp(log_hr * (n_seen / (n_seen + K_SHRINK)))
        pred_curve = survival_from_hazard((h_port * hr).clip(0.001, 0.999))
        plt.plot(range(1, HMAX + 1), pred_curve, lw=1.6, alpha=0.85,
                 label=f"прогноз по {w} нед (n={n_seen}, hr={hr:.2f})")
        if w == 5:
            for wk in [12, 26, 52]:
                f, p = fact_curve.iloc[wk-1], pred_curve[wk-1]
                err = (p-f)/f*100 if f > 0 else float("nan")
                print(f"  нед {wk}: факт={f:.3f} прогноз={p:.3f} ошибка={p-f:+.3f} ({err:+.0f}%)")

    plt.title(f"Дожитие: факт(прямой) vs прогноз — {app}")
    plt.xlabel("неделя (= платёж)"); plt.ylabel("доля доживших")
    plt.ylim(0, 1); plt.grid(alpha=0.25); plt.legend(fontsize=8)
    plt.tight_layout()
    out = rf"C:\Users\yahor\PycharmProjects\LTV\fc_direct_{app}.png"
    plt.savefig(out, dpi=130); print("сохранил:", out)

plt.show()