import pyarrow.parquet as pq
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

SRC   = r"C:\Users\yahor\PycharmProjects\LTV\se_training.parquet"
APPS  = ["id6473738977", "id6469790837", "id6499426756"]
HMAX  = 52
WEEKS = [1, 2, 3, 4, 5]
K_SHRINK = 800   # сила притяга hazard-множителя аппа к 1.0 (портфелю)

mx = pd.read_parquet(SRC)
mx["survived"] = mx["outcome"].isin(["renewed", "recovered"]).astype(int)
mx["died"] = 1 - mx["survived"]

def step_hazard(df):
    """P(умереть) на каждой ступени 1..HMAX по набору строк"""
    h = df[df["step_k"] <= HMAX].groupby("step_k")["died"].mean()
    return h.reindex(range(1, HMAX + 1))

def survival_from_hazard(haz):
    """кривая дожития = произведение (1 - hazard) по ступеням"""
    surv_step = (1 - haz.fillna(0)).values
    return np.cumprod(surv_step)

for app in APPS:
    app_rows = mx[mx["app_id"] == app]
    other    = mx[mx["app_id"] != app]

    # базовый риск портфеля по ступеням (без этого аппа)
    h_port = step_hazard(other)

    # ФАКТ: реальная кривая аппа
    fact_curve = survival_from_hazard(step_hazard(app_rows))

    plt.figure(figsize=(9, 5.5))
    plt.plot(range(1, HMAX + 1), fact_curve, color="black", lw=3, label="ФАКТ")

    for w in WEEKS:
        seen = app_rows[app_rows["step_k"] <= w]
        n_seen = len(seen)

        # hazard-множитель аппа = (его риск на виденных ступенях) / (риск портфеля на тех же),
        # усреднённый по виденным ступеням, в лог-шкале (чтобы был мультипликативным)
        h_app_seen  = step_hazard(seen)
        # отношение риска на виденных ступенях
        common = (~h_app_seen.isna()) & (~h_port.isna()) & (h_port > 0)
        if common.sum() > 0:
            ratio = (h_app_seen[common] / h_port[common]).clip(0.2, 5.0)
            # средний лог-множитель, взвешенный по числу наблюдений на ступени
            wts = seen.groupby("step_k").size().reindex(ratio.index).fillna(0)
            log_hr = np.average(np.log(ratio), weights=wts) if wts.sum() > 0 else 0.0
        else:
            log_hr = 0.0

        # усадка к 1.0 (log_hr -> 0) по объёму данных
        shrink = n_seen / (n_seen + K_SHRINK)
        hr = np.exp(log_hr * shrink)

        # прогноз: риск портфеля × множитель аппа, на ВСЕХ ступенях
        h_pred = (h_port * hr).clip(0.001, 0.999)
        pred_curve = survival_from_hazard(h_pred)

        plt.plot(range(1, HMAX + 1), pred_curve, lw=1.6, alpha=0.85,
                 label=f"прогноз по {w} нед (n={n_seen}, hr={hr:.2f})")

    plt.title(f"Дожитие: факт vs прогноз — {app}")
    plt.xlabel("неделя (= платёж)"); plt.ylabel("доля доживших")
    plt.ylim(0, 1); plt.grid(alpha=0.25); plt.legend(fontsize=8)
    plt.tight_layout()
    out = rf"C:\Users\yahor\PycharmProjects\LTV\fc2_{app}.png"
    plt.savefig(out, dpi=130); print("сохранил:", out, "| hr-факт-нет, см. линии")

plt.show()