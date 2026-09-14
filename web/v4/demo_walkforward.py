"""
Walk-forward demo: what the model predicts for a cell as data arrives week by week.

For each chosen (cohort x funnel x utm_source) cell the model is re-run with 1
week of visible data, then 2, then 3 ... and each run forecasts the same fixed
horizons. Reading down a column shows the forecast settling as evidence
accumulates; the FACT row at the bottom is what actually happened wherever the
cell is old enough to know.

Honest by construction: the state is refitted LEAVE-ONE-COHORT-OUT for every
cell, so the model has never seen the cohort it is predicting. hr comes from the
cohort at that visible depth, multipliers from the cell's own people.

Writes: reports/web_v4/demo_walkforward.md
Run: .venv/Scripts/python.exe web/v4/demo_walkforward.py
"""
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
os.chdir(ROOT)

from core import common  # noqa: E402
from ltv_v4.config import OUT_DIR, DATA_DIR, MATRIX_GLOB, POPULATION_GLOB, SESSION_COL  # noqa: E402
from ltv_v4 import se_training_web as S, map_web as M, money as MON, revenue as R  # noqa: E402

HORIZONS = [4, 8, 12, 26, 52]
CELLS = [
    ("2026-04-20", "device-security", "fbbohdan"),
    ("2026-05-11", "device-security-check", "fbbohdan"),
]
OUT_PATH = ROOT / OUT_DIR / "demo_walkforward.md"


def newest(pat):
    return sorted(Path(DATA_DIR).glob(pat))[-1]


def main():
    mx = S.add_survived(pd.read_parquet(newest(MATRIX_GLOB)))
    train_all = S.training_rows(mx)
    pop = pd.read_parquet(newest(POPULATION_GLOB))
    events = R.load_events()
    max_week = int(pop["age_weeks_now"].max())
    cum = R.per_person_week_cumulative(events, pop, max_week)
    ios_h = common.empirical_hbase(common.load_matrix())
    pop_idx = pop.set_index(SESSION_COL)

    lines = [
        "# Прогон week-by-week: как прогноз меняется по мере поступления данных",
        "",
        "Для каждой ячейки модель запускается заново, видя 1 неделю данных, потом 2,",
        "потом 3 — и каждый раз прогнозирует ОДНИ И ТЕ ЖЕ горизонты. Читая колонку",
        "сверху вниз, видно, как прогноз сходится по мере накопления фактов.",
        "",
        "Состояние переобучается **leave-one-cohort-out** на каждой ячейке: модель",
        "никогда не видела когорту, которую предсказывает. `hr` берётся от когорты на",
        "той же глубине видимости, множители — от людей самой ячейки.",
        "",
        "Все числа — **LTV на базового плательщика** (купившего $9.99/нед), в долларах.",
        "",
    ]

    for cohort, funnel, utm in CELLS:
        people = pop_idx[
            (pop_idx["cohort_date"].dt.date.astype(str) == cohort)
            & (pop_idx["first_funnel"] == funnel)
            & (pop_idx["utm_source"] == utm)
        ]
        base_people = people[people["has_base"]]
        age = int(people["age_weeks_now"].mean())
        cell_rows = mx[mx["sub_id"].isin(set(base_people.index))]
        if cell_rows.empty:
            continue

        cohort_rows_all = mx[mx["app_id"] == cohort]
        loo_train = train_all[train_all["app_id"] != cohort]
        state = M.fit(loo_train, ios_h_base=ios_h)

        header = (f"## {cohort} × `{funnel}` × `{utm}`")
        meta = (f"привлечено {len(people)}, из них купили подписку {len(base_people)} "
                f"(конверсия {len(base_people)/len(people):.1%}), возраст когорты {age} нед")
        print(f"\n{header}\n{meta}")
        lines += ["", header, "", meta, ""]

        cols = "| видит недель | " + " | ".join(f"нед {N}" for N in HORIZONS) + " | hr когорты |"
        sep = "|---" * (len(HORIZONS) + 2) + "|"
        lines += [cols, sep]
        print(cols)

        for weeks in range(1, min(age, 8) + 1):
            vis_cohort = visible(cohort_rows_all, weeks)
            vis_cell = visible(cell_rows, weeks)
            if vis_cell.empty or vis_cohort.empty:
                continue
            hr = M.cohort_hr(state, vis_cohort, weeks)
            curve = M.cell_curve(state, vis_cell, hr)
            anchor_obs = float(cum.reindex(base_people.index)[min(weeks, max_week)].mean())

            vals = []
            for N in HORIZONS:
                if N <= weeks:
                    # Horizon already behind the visible edge: report the observed
                    # revenue AT THAT WEEK, not at the visible edge. Reporting
                    # cum[weeks] here made the "week 4" column appear to grow as
                    # the model saw more, which is just week-8 money in a week-4
                    # column.
                    v = float(cum.reindex(base_people.index)[min(N, max_week)].mean())
                else:
                    v = MON.cell_ltv(curve, anchor_obs, weeks, N, base_people)
                vals.append(f"${v:,.2f}")
            row = f"| {weeks} | " + " | ".join(vals) + f" | {hr:.3f} |"
            lines.append(row)
            print(row)

        fact_vals = []
        for N in HORIZONS:
            if N <= age and N <= max_week:
                fact_vals.append(f"**${float(cum.reindex(base_people.index)[N].mean()):,.2f}**")
            else:
                fact_vals.append("—")
        fact_row = "| **ФАКТ** | " + " | ".join(fact_vals) + " | |"
        lines.append(fact_row)
        print(fact_row)
        lines.append("")
        lines.append(f"«—» в строке ФАКТ означает, что когорте {age} нед и до этого горизонта "
                     f"она ещё не дожила.")

    lines += [
        "",
        "## Как это читать",
        "",
        "- Прогноз на **уже пройденные** недели равен факту — модель туда не лезет,",
        "  он заякорен на наблюдённой выручке.",
        "- Колонки нед 26 и нед 52 держатся на хвосте, заимствованном у iOS: свои",
        "  веб-данные кончаются на ступени 8. Это не измерение, а допущение.",
        "- `hr` когорты меняется по мере поступления данных: на 1 неделе он ещё",
        "  сильно усажен к 1.0 (мало наблюдений), дальше расходится.",
    ]

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {OUT_PATH}")


def visible(rows: pd.DataFrame, weeks: int) -> pd.DataFrame:
    """The rows as they looked after `weeks` weeks -- later steps unseen."""
    eligible = rows.loc[rows["weeks_obs"] >= weeks, "sub_id"].unique()
    vis = rows[rows["sub_id"].isin(eligible) & (rows["step_k"] <= weeks)].copy()
    vis["weeks_obs"] = weeks
    return vis


if __name__ == "__main__":
    main()
