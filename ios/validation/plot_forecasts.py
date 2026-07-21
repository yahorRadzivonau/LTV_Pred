"""
Задача C: визуальные графики "прогноз vs факт" для модели map. Только
matplotlib, сохранение в PNG (backend Agg), plt.show() не вызывается.

models/map_model.py и models/empirical.py НЕ меняются — здесь только
читаются их fit()/predict() (та же сигнатура, что и в compare.py).

Запуск: python plot_forecasts.py
"""
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from core import common, map_model
from ios.alt_models import empirical

PLOTS_DIR = ROOT / "reports" / "ios" / "plots"
OUT_PATH = ROOT / "reports" / "ios" / "plot_forecasts.md"

PW_LINES = [1, 2, 4]
PW_COLORS = {1: "tab:blue", 2: "tab:orange", 4: "tab:green"}
VLINES = [12, 26, 52]
CENSOR_MIN_MATURE = 50
# "выброс по множителям" (reports/ios/map_multipliers.md) и "самый текучий" (сильнейшая
# недооценка факта картой, см. reports/ios/sign_pw2_analysis.md)
SPECIAL_APPS = ["id6760619107", "id6744300418"]


def pick_apps(clean_apps, counts):
    """6 аппов: 2 крупнейших, 2 ближайших к медиане, 2 фиксированных особых."""
    ranked = counts.sort_values(ascending=False)
    largest = ranked.index[:2].tolist()
    remaining = ranked.drop(index=largest)
    median_val = ranked.median()
    near_median = (remaining - median_val).abs().sort_values().index[:2].tolist()
    chosen = largest + near_median
    for app in SPECIAL_APPS:
        if app not in clean_apps:
            raise SystemExit(f"{app} не входит в 39 чистых аппов — спека Задачи C нарушена, СТОП")
        if app in chosen:
            raise SystemExit(f"{app} уже выбран как крупнейший/медианный — коллизия набора, нужна ручная правка")
        chosen.append(app)
    return chosen


def censored_fact(fact, matures, min_mature=CENSOR_MIN_MATURE):
    """(x, y) факта direct_survival, обрезанного там, где mature < min_mature
    (mature монотонно не растёт с k, поэтому обрыв на первом провале ниже порога корректен)."""
    xs, ys = [], []
    for k in range(1, common.HMAX + 1):
        if matures.get(k, 0) < min_mature:
            break
        v = fact.get(k, np.nan)
        if np.isnan(v):
            break
        xs.append(k)
        ys.append(v)
    return xs, ys


def plot_app(app, app_rows, fact, matures, map_state, emp_state, subs_count):
    xs_fact, ys_fact = censored_fact(fact, matures)

    fig, ax = plt.subplots(figsize=(9, 5.5))
    ax.plot(xs_fact, ys_fact, color="black", linewidth=2.5, label="факт (direct_survival, censored)")

    preds = {}
    for pw in PW_LINES:
        pred = map_model.predict(map_state, app_rows, pw)
        preds[pw] = pred
        ax.plot(pred.index, pred.values, color=PW_COLORS[pw], linewidth=1.5,
                label=f"map, {pw} нед данных")

    pred_emp4 = empirical.predict(emp_state, app_rows, 4)
    ax.plot(pred_emp4.index, pred_emp4.values, color="gray", linestyle="--", linewidth=1.5,
            label="empirical, 4 нед данных")

    for v in VLINES:
        ax.axvline(v, color="gray", linewidth=0.6, alpha=0.5)

    fact52 = fact.get(52, np.nan)
    pred_map4_52 = preds[4].get(52, np.nan)
    if not np.isnan(fact52) and fact52 > 0:
        signed_err = (pred_map4_52 - fact52) / fact52 * 100
        err_txt = f"зн.ошибка map@нед52(4нед данных)={signed_err:+.0f}%"
    else:
        signed_err = None
        err_txt = "зн.ошибка map@нед52: н/д (факт недозрел)"

    ax.set_title(f"{app} | подписчиков: {subs_count} | {err_txt}", fontsize=10)
    ax.set_xlabel("неделя подписки (платёж)")
    ax.set_ylabel("доля доживших")
    ax.set_ylim(0, 1)
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8)
    fig.tight_layout()

    out_path = PLOTS_DIR / f"{app}.png"
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    return out_path, signed_err


def plot_portfolio(clean_apps, facts, mx, map_state):
    xs, ys, labels = [], [], []
    for app in clean_apps:
        app_rows = mx[mx["app_id"] == app]
        pred = map_model.predict(map_state, app_rows, 4)
        fact52 = facts[app].get(52, np.nan)
        pred52 = pred.get(52, np.nan)
        if np.isnan(fact52) or fact52 <= 0 or np.isnan(pred52):
            continue
        xs.append(fact52)
        ys.append(pred52)
        labels.append(app)

    xs = np.array(xs)
    ys = np.array(ys)
    rel_err = np.abs(ys - xs) / xs
    worst_idx = np.argsort(-rel_err)[:3]

    fig, ax = plt.subplots(figsize=(7, 7))
    ax.scatter(xs, ys, color="tab:blue", alpha=0.7)
    lim = [0, max(xs.max(), ys.max()) * 1.05]
    ax.plot(lim, lim, color="gray", linestyle="--", linewidth=1, label="y=x")
    for i in worst_idx:
        ax.annotate(labels[i], (xs[i], ys[i]), fontsize=7, xytext=(4, 4), textcoords="offset points")
    ax.set_xlabel("факт нед52")
    ax.set_ylabel("прогноз map (4 нед данных) нед52")
    ax.set_title(f"Портфель: факт vs прогноз map@нед52 (n={len(xs)} аппов)")
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8)
    fig.tight_layout()

    out_path = PLOTS_DIR / "portfolio.png"
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    return out_path, [labels[i] for i in worst_idx], len(xs)


def main():
    PLOTS_DIR.mkdir(parents=True, exist_ok=True)

    mx = common.load_matrix()
    apps, facts, matures, crash_flag, crash_apps = common.select_apps(mx)
    apps_info = (apps, facts, matures, crash_flag, crash_apps)
    clean_apps = [a for a in apps if not crash_flag[a]]

    counts = mx[mx["app_id"].isin(clean_apps)].groupby("app_id")["sub_id"].nunique()
    chosen = pick_apps(clean_apps, counts)
    print("выбранные аппы:", chosen)

    map_state = map_model.fit(mx, apps_info)
    emp_state = empirical.fit(mx)

    lines = [
        "# Прогноз vs реальность — графики (map)\n",
        "6 аппов (2 крупнейших, 2 около медианы по числу подписчиков, "
        "id6760619107 — выброс по множителям, id6744300418 — самый текучий) "
        "+ 1 портфельный scatter. models/map_model.py и models/empirical.py "
        "не менялись — только чтение fit()/predict().\n",
    ]

    for app in chosen:
        app_rows = mx[mx["app_id"] == app]
        out_path, signed_err = plot_app(app, app_rows, facts[app], matures[app],
                                          map_state, emp_state, int(counts[app]))
        rel = out_path.relative_to(ROOT).as_posix()
        err_str = f"{signed_err:+.0f}%" if signed_err is not None else "н/д"
        lines.append(f"- `{rel}` — {app}, {int(counts[app])} подписчиков, "
                      f"зн.ошибка map@нед52(4нед)={err_str}.")

    portfolio_path, worst3, n_apps = plot_portfolio(clean_apps, facts, mx, map_state)
    rel = portfolio_path.relative_to(ROOT).as_posix()
    lines.append(f"- `{rel}` — портфель, {n_apps} аппов, топ-3 худших по |отн.ошибке| подписаны: "
                  f"{', '.join(worst3)}.")

    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"отчёт записан: {OUT_PATH}")
    print(f"графики в {PLOTS_DIR}")


if __name__ == "__main__":
    main()
