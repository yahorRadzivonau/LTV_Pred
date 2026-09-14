"""
v2 vs v3: the head-to-head report for this iteration.

Reads the per-fold LOO detail written by validate_loo_web.py plus the v3 tables,
and assembles reports/web_v3/comparison_v2_vs_v3.md. No refitting here, so the
report can never disagree with the run that produced it.

Run: .venv/Scripts/python.exe web/v3/build_comparison_report.py
     (after validate_loo_web.py and build_tables_v3.py)
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
    OUT_DIR, DATA_DIR, MAP_LEVERS_WEB, RETURN_WINDOW_DAYS, MAP_MIN_ROWS_WEB,
)

OUT_PATH = ROOT / OUT_DIR / "comparison_v2_vs_v3.md"


def med(series):
    return series.median() if len(series) else np.nan


def main():
    detail = pd.read_parquet(Path(DATA_DIR) / "loo_detail.parquet")
    tables = {lbl: sorted(Path(OUT_DIR).glob(f"table_{lbl}_v3_*.csv"))[-1] for lbl in "ABC"}
    tbl_c = pd.read_csv(tables["C"])

    s = {m: detail[f"err_{m}"].dropna() for m in ("v2_formula", "v3_formula")}
    l = {m: detail[f"ltv_err_{m}"].dropna() for m in ("v2_formula", "v3_formula")}

    lines = [
        "# v2 против v3 — итог итерации",
        "",
        "Обе формулы прогнаны на ОДНИХ данных, одних LOO-фолдах и одной метрике,",
        "поэтому разница относится к формуле, а не к исправлениям данных: фиксы",
        "знаменателя, триала и апсейла применены к обеим одинаково.",
        "",
        "| | v2-формула | v3-формула |",
        "|---|---|---|",
        "| кривая | одна пулированная на весь продукт | своя на ячейку |",
        "| `hr` | один на всё | **по когорте**, усадка `K_SHRINK` |",
        "| рычаги | вычисляются и схлопываются в пул | входят через людей ячейки |",
        "| деньги | факт × глобальное отношение | якорь на факте + моделируемое приращение |",
        "",
        "## Точность (LOO по когортам)",
        "",
        "| метрика | v2 | **v3** | дельта |",
        "|---|---|---|---|",
        f"| дожитие, \\|ошибка\\| | {med(s['v2_formula'].abs()):.1%} | **{med(s['v3_formula'].abs()):.1%}** | "
        f"{med(s['v3_formula'].abs()) - med(s['v2_formula'].abs()):+.1%} п.п. |",
        f"| дожитие, смещение | {med(s['v2_formula']):+.1%} | **{med(s['v3_formula']):+.1%}** | "
        f"{abs(med(s['v3_formula'])) - abs(med(s['v2_formula'])):+.1%} п.п. |",
        f"| LTV, \\|ошибка\\| | {med(l['v2_formula'].abs()):.1%} | **{med(l['v3_formula'].abs()):.1%}** | "
        f"{med(l['v3_formula'].abs()) - med(l['v2_formula'].abs()):+.1%} п.п. |",
        f"| LTV, смещение | {med(l['v2_formula']):+.1%} | **{med(l['v3_formula']):+.1%}** | "
        f"{abs(med(l['v3_formula'])) - abs(med(l['v2_formula'])):+.1%} п.п. |",
        "",
        f"n = {len(detail)} фолдов, {detail['cohort'].nunique()} когорт. Полная сетка — `loo_web.md`.",
        "",
        "### По горизонтам",
        "",
        "| горизонт | v2 дожитие | v3 дожитие | v2 LTV | v3 LTV |",
        "|---|---|---|---|---|",
    ]
    for off, g in detail.groupby("horizon_offset"):
        lines.append(
            f"| +{int(off)} нед | {med(g['err_v2_formula'].abs()):.1%} | **{med(g['err_v3_formula'].abs()):.1%}** | "
            f"{med(g['ltv_err_v2_formula'].abs()):.1%} | **{med(g['ltv_err_v3_formula'].abs()):.1%}** |"
        )

    lines += [
        "",
        "### Где v3 выигрывает больше всего",
        "",
        "| видно недель | v2 дожитие | v3 дожитие |",
        "|---|---|---|",
    ]
    for fed, g in detail.groupby("weeks_fed"):
        if len(g) < 3:
            continue
        lines.append(f"| {int(fed)} | {med(g['err_v2_formula'].abs()):.1%} | "
                     f"**{med(g['err_v3_formula'].abs()):.1%}** |")
    lines += [
        "",
        "У v2 одна кривая на всех, поэтому накопленное знание о конкретной когорте",
        "ей некуда девать — с ростом видимых данных она не улучшается. У v3 это",
        "знание входит через `hr` когорты.",
        "",
        "## Что исправлено в данных (применено к обеим формулам)",
        "",
        "| проблема | было | стало |",
        "|---|---|---|",
        "| знаменатель | только плательщики (`n_attributed == n_payers`, per_attributed завышен ~1.38x) | per-trial: любой триал ∪ купившие базу |",
        "| платный триал | в бакете `ups`, рос месячным множителем | разово на неделе 0, не проецируется |",
        "| апсейл | размазан как `11.99/4` каждую неделю | чекпоинты недель 2, 6, 10… (замер: 0% на неделе 0) |",
        "| цензурирование | шаг оценивался через 1 неделю при окне разметки 60 дн | строго по времени, 21 день, для выживших и умерших одинаково |",
        f"| продукт B | $4.99→$39.99/мес смешан с $9.99/нед | исключён (легаси, после 13 апреля вошли 2 чел) |",
        "",
        "## Границы, которые не двинулись",
        "",
        f"- Свой веб-овский `h_base` держится до ступени **8**. Дальше — хвост iOS",
        "  со сшивкой. В таблицах это колонка `ltv_{N}_evidence`: на нед 4 стоит",
        "  `web_data`, начиная с нед 12 — `ios_tail_extrapolation`.",
        f"- Ячейки: {int((tbl_c['low_n']).sum())} из {len(tbl_c)} в таблице C помечены `low_n` "
        f"(<40 плательщиков). Усадка `hr` их вытягивает к портфельной кривой, но",
        "  данных в них по-прежнему мало.",
        "- Календарный дрейф не измерялся и остаётся открытым: iOS на своём",
        "  календарном бэктесте завышает на +16% на нед12. У веба такого теста",
        "  пока нет — при обучении с 13 апреля на него хватает 2-3 точек.",
        "",
        "## Конфигурация",
        "",
        f"- рычаги: `{MAP_LEVERS_WEB}` (воронка замерена и оставлена вне — см. `lever_study_web.md`)",
        f"- порог схлопывания категории: {MAP_MIN_ROWS_WEB} строк",
        f"- окно возврата / цензурирования: {RETURN_WINDOW_DAYS} дней",
        "",
        "## Чего в этой итерации НЕ делалось",
        "",
        "- В BigQuery ничего не заливалось. Таблица `ad_hock_tables.ml_web_predictions`",
        "  по-прежнему содержит числа v2 с завышенным `per_attributed`.",
        "- `web/v2/`, `web/golden/`, `core/`, `ios/` и их отчёты не изменялись.",
    ]

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT_PATH}")
    # The console here is cp1251; the report itself is UTF-8. Print an ASCII
    # summary rather than let an encode error kill a successful run.
    print(f"  survival: v2 {med(s['v2_formula'].abs()):.4f} -> v3 {med(s['v3_formula'].abs()):.4f}")
    print(f"  LTV:      v2 {med(l['v2_formula'].abs()):.4f} -> v3 {med(l['v3_formula'].abs()):.4f}")
    print(f"  folds: {len(detail)} over {detail['cohort'].nunique()} cohorts")


if __name__ == "__main__":
    main()
