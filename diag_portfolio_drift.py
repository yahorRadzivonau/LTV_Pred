"""
Диагностика систематического завышения на молодых аппах, найденного
календарным бэктестом (reports/calendar_backtest.md: map/empirical
переоценивают факт нед12 на +16.2%/+19.5% на 12 оценках). Две гипотезы
владельца, ПЕРЕД любым фиксом:
(а) дрейф портфеля вниз во времени — новые аппы органически хуже держат;
(б) эффект выжившего на уровне аппов — h_base/H_PORT построены на
    портфеле, где непропорционально представлены аппы, которые В ИТОГЕ
    дожили до зрелости, а не типичный только что запущенный апп.

Только диагностика: НИЧЕГО не фиксит, models/*.py и data/ не меняются.
CUTOFFS/inventory() переиспользованы из calendar_backtest.py (уже
написан и проверен в этой же сессии) — чтобы список из 12 оцениваемых
(T, апп) пар не пришлось пересчитывать заново другим способом.

Выход: reports/portfolio_drift.md + reports/plots/portfolio_drift.png.
Запуск: python diag_portfolio_drift.py
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from models import common
from calendar_backtest import CUTOFFS, inventory

ROOT = Path(__file__).resolve().parent
PLOTS_DIR = ROOT / "reports" / "plots"
OUT_PATH = ROOT / "reports" / "portfolio_drift.md"

CALENDAR_GAP_PP = 16.2   # наблюдённое завышение map, нед12, "все T" (reports/calendar_backtest.md)
SNAPSHOT = pd.Timestamp("2026-06-29", tz="UTC")  # последняя дата в data/se_training.parquet


def build_portfolio_table(mx):
    rows = []
    for app, app_rows in mx.groupby("app_id"):
        launch = app_rows["pay_ts"].min()
        fact, mature, _ = common.direct_survival(app_rows)
        fact4 = fact.get(4) if mature.get(4, 0) >= common.MIN_MATURE else np.nan
        fact12 = fact.get(12) if mature.get(12, 0) >= common.MIN_MATURE else np.nan
        fact26 = fact.get(26) if mature.get(26, 0) >= common.MIN_MATURE else np.nan
        weeks_since_launch = (SNAPSHOT - launch).days / 7
        rows.append({
            "app": app,
            "launch": launch,
            "launch_month": launch.strftime("%Y-%m"),
            "mature4": mature.get(4, 0), "fact4": fact4,
            "mature12": mature.get(12, 0), "fact12": fact12,
            "mature26": mature.get(26, 0), "fact26": fact26,
            "weeks_since_launch": weeks_since_launch,
            "matured": not np.isnan(fact26),
            # апп уже мог бы дозреть до нед26 по календарю, но mature26<200 — сильнее похоже на "убит"
            "old_enough_but_not_matured": (weeks_since_launch >= 26) and np.isnan(fact26),
        })
    return pd.DataFrame(rows)


def trend_regression(df):
    d = df.dropna(subset=["fact12"]).copy()
    d["launch_months"] = (d["launch"] - d["launch"].min()).dt.total_seconds() / (86400 * 30.44)
    slope, intercept = np.polyfit(d["launch_months"], d["fact12"] * 100, 1)
    r = np.corrcoef(d["launch_months"], d["fact12"] * 100)[0, 1]
    return slope, intercept, r, d


def plot_scatter(df, slope, intercept, r, d_reg, out_path):
    fig, ax = plt.subplots(figsize=(9, 5.5))
    d = df.dropna(subset=["fact12"])
    colors = np.where(d["matured"], "tab:blue", "tab:orange")
    ax.scatter(d["launch"], d["fact12"] * 100, c=colors, alpha=0.75)
    ax.scatter([], [], c="tab:blue", label="дожил до зрелости (есть факт нед26)")
    ax.scatter([], [], c="tab:orange", label="молодой/убитый (нет факта нед26)")

    x_months = np.array([d_reg["launch_months"].min(), d_reg["launch_months"].max()])
    y_line = slope * x_months + intercept
    launch_min = d_reg["launch"].min()
    x_dates = [launch_min + pd.Timedelta(days=float(v) * 30.44) for v in x_months]
    ax.plot(x_dates, y_line, color="red", linestyle="--", label=f"тренд ({slope:+.2f} п.п./мес, r={r:+.2f})")

    ax.set_xlabel("месяц запуска аппа (первый платёж)")
    ax.set_ylabel("факт нед12, %")
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8)
    ax.set_title("Дрейф портфеля: факт нед12 vs месяц запуска аппа")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def write_report(df, slope, intercept, r, d_reg, calendar_apps, trend_pp, surv_pp, plot_path):
    matured = df[df["matured"]]
    young_or_killed = df[~df["matured"]]
    old_not_matured = df[df["old_enough_but_not_matured"]]
    still_young = young_or_killed[~young_or_killed["old_enough_but_not_matured"]]

    lines = [
        "# Дрейф портфеля: диагностика завышения на молодых аппах\n",
        "Контекст: reports/calendar_backtest.md — map/empirical переоценивают факт нед12 "
        f"молодых аппов на **+{CALENDAR_GAP_PP:.1f}п.п.** (n=12). Две гипотезы владельца: "
        "(а) дрейф портфеля вниз во времени, (б) эффект выжившего на уровне аппов. "
        "Это ТОЛЬКО диагностика — фикс не выбирается здесь, models/*.py и data/ не менялись.\n",
    ]

    lines.append(f"Всего аппов в портфеле: {len(df)}. С фактом нед12 (mature≥200): "
                 f"{df['fact12'].notna().sum()}. С фактом нед4: {df['fact4'].notna().sum()}.\n")

    lines.append("## 1-2. По-апповая таблица + scatter\n")
    lines.append(f"![дрейф портфеля]({plot_path.relative_to(ROOT).as_posix()})\n")
    lines.append("| app | месяц запуска | mature4 | факт4 | mature12 | факт12 | mature26 | факт26 | дожил до зрелости |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for _, row in df.sort_values("launch").iterrows():
        def fmt(v):
            return f"{v:.4f}" if v == v else "н/д"
        lines.append(f"| {row['app']} | {row['launch_month']} | {row['mature4']} | {fmt(row['fact4'])} | "
                      f"{row['mature12']} | {fmt(row['fact12'])} | {row['mature26']} | {fmt(row['fact26'])} | "
                      f"{'да' if row['matured'] else 'нет'} |")
    lines.append("")

    def group_row(label, g):
        total = len(g)
        have_fact = int(g["fact12"].notna().sum())
        mean = f"{g['fact12'].mean() * 100:.1f}%" if have_fact > 0 else "н/д"
        return f"| {label} | {total} (из них с фактом12: {have_fact}) | {mean} |"

    lines.append("## 3. Группы: дожившие до зрелости vs молодые/убитые\n")
    lines.append("| группа | n всего (с фактом12) | средний факт нед12 |")
    lines.append("|---|---|---|")
    lines.append(group_row("дожившие до зрелости (факт нед26 есть)", matured))
    lines.append(group_row("молодые/убитые (факт нед26 нет) — всего", young_or_killed))
    lines.append(group_row("...из них: ещё физически молоды (<26 нед с запуска)", still_young))
    lines.append(group_row("...из них: возраст ≥26 нед, но mature26<200 (похоже на 'убит')", old_not_matured))
    lines.append("")
    lines.append(
        "Разбивка 'молодые/убитые' на подгруппы — чтобы не путать 'ещё рано мерить нед26' "
        "(это нормально, апп может быть прекрасным) с 'дожил бы до нед26 по календарю, но не "
        "набрал mature — похоже на реально прекращённый/не взлетевший апп' (эта подгруппа "
        "ближе к настоящему 'выживанию').\n"
    )

    lines.append("## 4. Вывод\n")
    trend_dir = "ВНИЗ" if slope < 0 else "вверх/нет"
    lines.append(f"**(а) Временной тренд**: наклон {slope:+.2f} п.п./мес (r={r:+.2f}) — тренд направлен "
                  f"{trend_dir}. " + ("Подтверждает гипотезу дрейфа портфеля." if slope < 0 and abs(r) > 0.2
                  else "Слабый/неочевидный тренд — не подтверждает уверенно гипотезу дрейфа." ) + "\n")

    surv_gap_pp = (matured["fact12"].mean() - young_or_killed["fact12"].mean()) * 100
    n_killed_with_fact = int(old_not_matured["fact12"].notna().sum())
    lines.append(f"**(б) Разрыв выжившие vs остальные**: {surv_gap_pp:+.1f}п.п. "
                  f"(дожившие минус молодые/убитые). " +
                  ("Подтверждает гипотезу выжившего — дожившие держат заметно лучше." if surv_gap_pp > 3
                   else "Разрыв небольшой — гипотеза выжившего слабо подтверждается этой метрикой.") +
                  f" ВАЖНАЯ ОГОВОРКА: в группе 'возраст≥26нед, но не дозрел' (похожей на настоящих "
                  f"'убитых') {n_killed_with_fact} аппов с измеримым фактом12 — весь разрыв "
                  f"{surv_gap_pp:+.1f}п.п. фактически образован подгруппой 'ещё физически молоды' "
                  f"(п.3 выше). То есть на этих данных гипотезы (а) и (б) НЕ разделяются чисто: "
                  f"'молодые/убитые' здесь почти целиком означает буквально 'молодые', разрыв групп "
                  f"по сути измеряет тот же временной эффект, что и тренд в (а), а не независимый "
                  f"эффект отбора выживших.\n")

    lines.append(
        "**(в) Грубая оценка вклада в +16.2п.п. календарного завышения** (без подгонки, "
        "оценки не обязаны складываться ровно в 16.2 — эффекты не независимы и не аддитивны "
        "по построению, это ориентир, а не точная декомпозиция; см. оговорку в (б) — оценки "
        "(а) и (б) ниже, скорее всего, ЧАСТИЧНО ДУБЛИРУЮТ один и тот же временной эффект, а "
        "не складываются в две независимые причины):\n"
    )
    lines.append(f"- Тренд: {len(calendar_apps)} календарных аппов запущены в среднем позже "
                  f"портфеля, использованного в регрессии (сдвиг по времени учтён в формуле "
                  f"слоуп x разница месяцев запуска) -> ~{trend_pp:+.1f}п.п. "
                  f"({trend_pp/CALENDAR_GAP_PP*100:.0f}% от {CALENDAR_GAP_PP}п.п.).")
    lines.append(f"- Выживший: разрыв групп {surv_gap_pp:+.1f}п.п. -> "
                  f"{surv_pp/CALENDAR_GAP_PP*100:.0f}% от {CALENDAR_GAP_PP}п.п. (использован "
                  f"напрямую как оценка эффекта, см. группы выше).")
    lines.append(
        "\nСТОП — по этому отчёту владелец выбирает механизм поправки."
    )

    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"отчёт записан: {OUT_PATH}")


def main():
    mx = common.load_matrix()
    df = build_portfolio_table(mx)

    slope, intercept, r, d_reg = trend_regression(df)
    print(f"тренд: {slope:+.3f} п.п./мес, r={r:+.3f}, n={len(d_reg)}")

    PLOTS_DIR.mkdir(parents=True, exist_ok=True)
    plot_path = PLOTS_DIR / "portfolio_drift.png"
    plot_scatter(df, slope, intercept, r, d_reg, plot_path)

    # 12 оцениваемых (T, апп) пар из календарного бэктеста — переиспользуем inventory()
    inv_rows = inventory(mx)
    calendar_apps = sorted({app for r in inv_rows for app in r["evaluable_apps"]})
    launch_calendar = df[df["app"].isin(calendar_apps)]["launch"]
    launch_calendar_months = (launch_calendar - d_reg["launch"].min()).dt.total_seconds() / (86400 * 30.44)
    portfolio_ref_months = d_reg["launch_months"].median()
    months_gap = launch_calendar_months.mean() - portfolio_ref_months
    trend_pp = -slope * months_gap
    print(f"календарные аппы: {len(calendar_apps)}, сдвиг во времени={months_gap:+.1f} мес, "
          f"trend_pp={trend_pp:+.2f}")

    matured = df[df["matured"]]
    young_or_killed = df[~df["matured"]]
    surv_pp = (matured["fact12"].mean() - young_or_killed["fact12"].mean()) * 100
    print(f"survivor gap pp={surv_pp:+.2f}")

    write_report(df, slope, intercept, r, d_reg, calendar_apps, trend_pp, surv_pp, plot_path)


if __name__ == "__main__":
    main()
