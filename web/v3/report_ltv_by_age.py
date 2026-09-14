"""
Two cuts the owner asked for.

1. OBSERVED average LTV week by week, with the sample size behind every week.
   This is pure fact -- no model anywhere -- so it shows where the data actually
   thins out, which is the honest limit on anything downstream.

2. PREDICTED LTV at weeks 52 and 104, grouped by how old the cohort is.
   The diagnostic value is in reading DOWN the column: if 1-week-old cohorts
   forecast a very different year-two number than 13-week-old ones, that is
   either portfolio drift or the model leaning on thin evidence. Stable columns
   mean the anchoring is doing its job.

Writes: reports/web_v3/ltv_by_age.md
Run: .venv/Scripts/python.exe web/v3/report_ltv_by_age.py
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

from ltv_v3.config import (  # noqa: E402
    OUT_DIR, DATA_DIR, POPULATION_GLOB, RETURN_WINDOW_DAYS, MIN_COHORT_AGE_WEEKS,
)
from ltv_v3 import revenue as R  # noqa: E402

OUT_PATH = ROOT / OUT_DIR / "ltv_by_age.md"
RETURN_WINDOW_WEEKS = RETURN_WINDOW_DAYS / 7.0


def newest(pat):
    return sorted(Path(DATA_DIR).glob(pat))[-1]


def main():
    pop = pd.read_parquet(newest(POPULATION_GLOB))
    events = R.load_events()
    max_week = int(pop["age_weeks_now"].max())
    cum = R.per_person_week_cumulative(events, pop, max_week)
    pop_idx = pop.set_index("email")
    base_idx = pop_idx[pop_idx["has_base"]]

    tbl_c = pd.read_csv(sorted(Path(OUT_DIR).glob("table_C_v3_*.csv"))[-1])

    lines = ["# LTV по неделям и по возрасту когорт", "",
             f"Снапшот: {pop['snapshot_now'].iloc[0].date()}. "
             f"Цензурирование {RETURN_WINDOW_DAYS} дней — человек засчитывается на неделе w,",
             "только если его неделя w уже успела «устояться».", ""]

    # ================================================== 1. observed LTV by week
    lines += [
        "## 1. Наблюдённое LTV по неделям (ЧИСТЫЙ ФАКТ, без модели)",
        "",
        "| неделя | LTV/привлечённого | людей | LTV/подписчика | подписчиков | прирост/нед |",
        "|---|---|---|---|---|---|",
    ]
    print("неделя | LTV/привл | людей | LTV/подписч | подписч | прирост")
    prev = None
    for w in range(0, max_week + 1):
        mature_all = pop_idx[pop_idx["age_weeks_now"] >= w + RETURN_WINDOW_WEEKS]
        mature_base = base_idx[base_idx["age_weeks_now"] >= w + RETURN_WINDOW_WEEKS]
        if len(mature_all) < 30:
            break
        v_all = float(cum.reindex(mature_all.index)[w].mean())
        v_base = float(cum.reindex(mature_base.index)[w].mean()) if len(mature_base) else np.nan
        inc = "" if prev is None else f"${v_base - prev:+.2f}"
        lines.append(f"| {w} | ${v_all:.2f} | {len(mature_all)} | ${v_base:.2f} | "
                     f"{len(mature_base)} | {inc} |")
        print(f"  {w:2d}   | ${v_all:8.2f} | {len(mature_all):5d} | ${v_base:8.2f} | "
              f"{len(mature_base):5d} | {inc}")
        prev = v_base

    lines += [
        "",
        "Где обрывается таблица — там перестаёт хватать людей (порог 30). Это и есть",
        "фактическая граница наблюдений: дальше любые числа модельные.",
        "",
    ]

    # ================================================== 2. forecast by cohort age
    lines += [
        "## 2. Прогноз на нед 52 и 104 в разрезе возраста когорты",
        "",
        "Средневзвешенное по ячейкам таблицы C (веса — люди/плательщики).",
        "Читать НАДО ПО СТОЛБЦУ: если молодые когорты предсказывают год иначе, чем",
        "зрелые, это либо дрейф портфеля, либо модель опирается на тонкие данные.",
        "",
        "| возраст, нед | ячеек | привлечено | подписчиков | конв. | LTV/привл 52 | LTV/привл 104 | LTV/подписч 52 | LTV/подписч 104 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    print("\nвозраст | ячеек | привл | подписч | конв | /привл 52 | /привл 104 | /подписч 52 | /подписч 104")
    for age, g in tbl_c.groupby("avg_age_weeks"):
        n_t, n_b = g["n_trial"].sum(), g["n_base_payer"].sum()
        if n_t == 0:
            continue
        wt = lambda col, w: float((g[col] * g[w]).sum() / g[w].sum()) if g[w].sum() else np.nan
        row = (f"| {int(age)} | {len(g)} | {int(n_t)} | {int(n_b)} | {n_b/n_t:.1%} | "
               f"${wt('ltv_per_trial_52','n_trial'):.2f} | ${wt('ltv_per_trial_104','n_trial'):.2f} | "
               f"${wt('ltv_per_base_payer_52','n_base_payer'):.2f} | "
               f"${wt('ltv_per_base_payer_104','n_base_payer'):.2f} |")
        lines.append(row)
        print(f"  {int(age):2d}    | {len(g):4d}  | {int(n_t):5d} | {int(n_b):5d}   | {n_b/n_t:.0%}  | "
              f"${wt('ltv_per_trial_52','n_trial'):7.2f} | ${wt('ltv_per_trial_104','n_trial'):7.2f} | "
              f"${wt('ltv_per_base_payer_52','n_base_payer'):7.2f} | "
              f"${wt('ltv_per_base_payer_104','n_base_payer'):7.2f}")

    lines += [
        "",
        f"Когорты младше {MIN_COHORT_AGE_WEEKS} нед в таблицу не попадают.",
        "",
        "### Оговорки",
        "",
        "- Колонки 52 и 104 целиком модельные: свои веб-данные держатся до ступени 8,",
        "  дальше форма заимствована у iOS (`ltv_{N}_evidence = ios_tail_extrapolation`).",
        "- Разброс между строками смешивает ДВА эффекта: реальную разницу качества",
        "  когорт и разное количество данных под прогнозом. Разделить их без",
        "  календарного бэктеста нельзя, а его у веба пока нет.",
        "- Конверсия меняется по строкам, поэтому `/привл` и `/подписч` двигаются",
        "  не синхронно: первое — про закупку, второе — про продукт.",
    ]

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {OUT_PATH}")


if __name__ == "__main__":
    main()
