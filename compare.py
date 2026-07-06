"""
Прогоняет LTV-модели (models/empirical.py, models/logreg.py, models/hybrid.py,
models/hybrid_v2.py, models/map_model.py) на одной и той же обучающей матрице
(empirical и map — на сырой mx, logreg/hybrid/hybrid_v2 — на mx_clean без
обвальных аппов, см. комментарий в main()), считает точность по сетке (недели
видимых данных 1-8) x (горизонты 12/26/52 недель) и пишет:
- reports/comparison.md         — агрегированная таблица (медианы по 35 аппам)
- reports/comparison_detail.csv — полные данные: по каждому app_id/неделе/горизонту/модели
Предыдущие версии обоих файлов перед перезаписью сохраняются как *_prev.*.

Запуск: python compare.py
"""
import csv
import shutil
from pathlib import Path

import numpy as np

from models import common, empirical, hybrid, hybrid_v2, logreg, map_model

ROOT = Path(__file__).resolve().parent
REPORT_PATH = ROOT / "reports" / "comparison.md"
REPORT_PREV_PATH = ROOT / "reports" / "comparison_prev.md"
DETAIL_PATH = ROOT / "reports" / "comparison_detail.csv"
DETAIL_PREV_PATH = ROOT / "reports" / "comparison_detail_prev.csv"

MODELS = {"empirical": empirical, "logreg": logreg, "hybrid": hybrid, "hybrid_v2": hybrid_v2, "map": map_model}


def score_model(model, state, apps, facts, matures, crash_flag, mx):
    """results[pw][horizon] = (медиана |отн.ошибки| %, медиана знаковой отн.ошибки %, кол-во аппов)
       detail_rows = список строк по каждому (app_id, pw, horizon), из которых собраны results —
       полные данные без агрегации, на случай если нужно посмотреть по конкретному аппу."""
    results = {}
    detail_rows = []
    for pw in common.PRED_WEEKS_LIST:
        errs = {h: [] for h in common.HORIZONS}
        signed = {h: [] for h in common.HORIZONS}
        n_apps = 0
        for app in apps:
            if crash_flag[app]:
                continue
            app_rows = mx[mx["app_id"] == app]
            pred = model.predict(state, app_rows, pw)
            used = False
            for h in common.HORIZONS:
                if matures[app].get(h, 0) >= common.MIN_MATURE and not np.isnan(facts[app].get(h, np.nan)):
                    f, pr = facts[app][h], pred[h]
                    if f > 0:
                        abs_err = abs(pr - f) / f
                        signed_err = (pr - f) / f
                        errs[h].append(abs_err)
                        signed[h].append(signed_err)
                        used = True
                        detail_rows.append({
                            "app_id": app, "pw_weeks": pw, "horizon": h,
                            "predicted": round(pr, 4), "fact": round(f, 4),
                            "abs_err_pct": round(abs_err * 100, 2),
                            "signed_err_pct": round(signed_err * 100, 2),
                        })
            if used:
                n_apps += 1
        results[pw] = {
            h: (
                np.median(errs[h]) * 100 if errs[h] else float("nan"),
                np.median(signed[h]) * 100 if signed[h] else float("nan"),
                n_apps,
            )
            for h in common.HORIZONS
        }
    return results, detail_rows


def write_detail_csv(all_detail_rows):
    """Полные (не агрегированные) результаты каждой модели: одна строка на
       (модель, app_id, неделя данных, горизонт) — предсказание, факт, ошибки.
       Старый файл, если был, сохраняется в comparison_detail_prev.csv."""
    DETAIL_PATH.parent.mkdir(exist_ok=True)
    if DETAIL_PATH.exists():
        shutil.copyfile(DETAIL_PATH, DETAIL_PREV_PATH)
        print(f"старая детализация сохранена: {DETAIL_PREV_PATH}")
    fieldnames = ["model", "app_id", "pw_weeks", "horizon", "predicted", "fact", "abs_err_pct", "signed_err_pct"]
    with open(DETAIL_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_detail_rows)
    print(f"детализация записана: {DETAIL_PATH} ({len(all_detail_rows)} строк)")


def write_report(results):
    lines = [
        "# Сравнение моделей LTV\n",
        "Ячейка: медиана |отн.ошибки| (зн = медиана знаковой отн.ошибки, "
        "+ завышаем / - занижаем, n = число аппов, попавших в оценку). "
        f"Только не-crash аппы, mature >= {common.MIN_MATURE}.\n",
        "**Сноска про n:** n — общий счётчик строки (аппов, у которых есть оценка хотя бы на "
        "ОДНОМ из горизонтов нед12/26/52), одинаковый для всех горизонтов в одной строке. На "
        "нед52 реально голосует меньше аппов, чем показывает n (обычно ~17 из 35) — не "
        "переоценивайте выборку хвоста по этому n.\n",
    ]
    header = "| данных | " + " | ".join(
        f"нед{h} {name}" for h in common.HORIZONS for name in MODELS
    ) + " |"
    sep = "|" + "---|" * (1 + len(common.HORIZONS) * len(MODELS))
    lines += [header, sep]
    for pw in common.PRED_WEEKS_LIST:
        row = [f"{pw} нед"]
        for h in common.HORIZONS:
            for name in MODELS:
                med, sgn, n = results[name][pw][h]
                row.append(f"{med:.0f}% (зн{sgn:+.0f}%, n={n})")
        lines.append("| " + " | ".join(row) + " |")

    lines.append("\n## Вердикт\n")
    for h in common.HORIZONS:
        scores = {}
        for name in MODELS:
            vals = [results[name][pw][h][0] for pw in common.PRED_WEEKS_LIST
                    if not np.isnan(results[name][pw][h][0])]
            scores[name] = np.median(vals) if vals else float("nan")
        winner = min(scores, key=lambda k: scores[k])
        summary = ", ".join(f"{k}={v:.0f}%" for k, v in scores.items())
        lines.append(f"- **нед{h}**: {summary} -> лучшая модель: **{winner}**")

    REPORT_PATH.parent.mkdir(exist_ok=True)
    if REPORT_PATH.exists():
        # старый отчёт не затираем — переносим в comparison_prev.md перед перезаписью
        shutil.copyfile(REPORT_PATH, REPORT_PREV_PATH)
        print(f"старый отчёт сохранён: {REPORT_PREV_PATH}")
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"отчёт записан: {REPORT_PATH}")


def print_verdict(results):
    print("\n=== ВЕРДИКТ (медиана по всем неделям данных) ===")
    for h in common.HORIZONS:
        scores = {}
        for name in MODELS:
            vals = [results[name][pw][h][0] for pw in common.PRED_WEEKS_LIST
                    if not np.isnan(results[name][pw][h][0])]
            scores[name] = np.median(vals) if vals else float("nan")
        winner = min(scores, key=lambda k: scores[k])
        summary = ", ".join(f"{k}={v:.0f}%" for k, v in scores.items())
        print(f"нед{h}: {summary}  -> лучшая: {winner}")


def main():
    print("загрузка матрицы...")
    mx = common.load_matrix()
    print("матрица:", mx.shape)

    print("select_apps (один раз для всех моделей)...")
    apps, facts, matures, crash_flag, crash_apps = common.select_apps(mx)
    print(f"аппов: {len(apps)} | нормальных: {len(apps) - len(crash_apps)} | обвальных: {len(crash_apps)}")

    mx_clean = mx[~mx["app_id"].isin(crash_apps)].copy()
    apps_info = (apps, facts, matures, crash_flag, crash_apps)  # для map_model.fit(mx, apps_info)

    # empirical и map получают СЫРУЮ mx (не mx_clean):
    # - empirical: так было в archive/backtest.py — портфельный медианный hazard
    #   там никогда не исключал обвальные аппы (в отличие от unified_model.py/
    #   unified_hybrid.py, которые уже строили форму на clean). Подтверждено
    #   регрессионным тестом (см. README): с mx_clean цифры на 1-2 п.п. расходятся
    #   со старым backtest.py, с mx — совпадают день в день.
    # - map: h_base считается через common.empirical_hbase(mx) на сырой матрице
    #   (то же решение, что и для empirical, см. MAP_MODEL_SPEC.md Шаг 1.2) —
    #   множители по рычагам map строит сама внутри fit(), выделяя чистую зону
    #   и исключая crash-аппы через apps_info, поэтому mx_clean ей не нужен и не
    #   передаётся (иначе h_base потерял бы часть портфеля, как разбирали для empirical).
    fit_data = {"empirical": mx, "logreg": mx_clean, "hybrid": mx_clean, "hybrid_v2": mx_clean}

    results = {}
    all_detail_rows = []
    for name, model in MODELS.items():
        print(f"\nобучаю {name}...")
        # map_model.fit имеет другую сигнатуру: fit(mx, apps_info), а не fit(mx_clean) —
        # ей нужен доступ к apps_info (crash_apps) для собственного построения чистой зоны.
        state = model.fit(mx, apps_info) if name == "map" else model.fit(fit_data[name])
        print(f"считаю точность {name} по сетке недель x горизонтов...")
        res, detail_rows = score_model(model, state, apps, facts, matures, crash_flag, mx)
        results[name] = res
        for row in detail_rows:
            row["model"] = name
        all_detail_rows.extend(detail_rows)

    write_report(results)
    write_detail_csv(all_detail_rows)
    print_verdict(results)


if __name__ == "__main__":
    main()
