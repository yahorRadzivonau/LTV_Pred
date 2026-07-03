"""
Общие константы и функции для всех LTV-моделей (empirical, logreg, hybrid).

Перенесено дословно (без изменения формул) из archive/backtest.py,
archive/unified_backtest.py и archive/unified_hybrid.py — эти три скрипта
почти всегда содержали одинаковый код для этой части, здесь он живёт в одном месте.
"""
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
MATRIX_PATH = ROOT / "data" / "se_training.parquet"

HMAX = 52
K_SHRINK = 800
MIN_PAYERS = 500
MIN_MATURE = 200
HORIZONS = [12, 26, 52]
PRED_WEEKS_LIST = [1, 2, 3, 4, 5, 6, 7, 8]
CRASH_RET_THRESHOLD = 0.65
CRASH_MIN_RISK = 50
RR_CLIP = (0.4, 2.5)   # ограничение множителя рычагов, чтобы не улетал


def load_matrix(path: Path = MATRIX_PATH) -> pd.DataFrame:
    """Читает data/se_training.parquet, добавляет survived/died. Используется всеми моделями."""
    mx = pd.read_parquet(path)
    mx["survived"] = mx["outcome"].isin(["renewed", "recovered"]).astype(int)
    mx["died"] = 1 - mx["survived"]
    return mx


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


def empirical_hbase(rows, min_risk=30):
    """ФОРМА: медианный по аппам РИСК смерти на каждой ступени (censored). Держит хвост."""
    r = rows[rows["weeks_obs"] >= rows["step_k"]]
    g = r.groupby(["app_id", "step_k"]).agg(surv=("survived", "sum"), n=("survived", "size")).reset_index()
    g = g[g["n"] >= min_risk]
    med_surv = g.groupby("step_k").apply(lambda d: (d["surv"] / d["n"]).median()).reindex(range(1, HMAX + 1))
    return (1 - med_surv)   # h_base[k]


def select_apps(mx):
    """Считается ОДИН раз на сыром mx (без фильтрации по crash): список аппов
       с >=MIN_PAYERS платящих, факт дожития (direct_survival) и флаг обвала
       (detect_crash) для каждого. Используется всеми тремя моделями и compare.py,
       чтобы не пересчитывать одно и то же трижды."""
    payers = mx.groupby("app_id")["sub_id"].nunique()
    apps = payers[payers >= MIN_PAYERS].index.tolist()
    facts, matures, crash_flag = {}, {}, {}
    for app in apps:
        f, mt, per = direct_survival(mx[mx["app_id"] == app])
        facts[app], matures[app] = f, mt
        crash_flag[app] = detect_crash(per)
    crash_apps = {a for a in apps if crash_flag[a]}
    return apps, facts, matures, crash_flag, crash_apps
