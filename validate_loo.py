"""
LOO-валидация модели map (Задача A + Задача A2).

Задача A: проверяет, что отрыв map от других моделей в reports/comparison.md
не объясняется тем, что множители рычагов учились в том числе на юзерах
предсказываемого аппа. Для каждого из чистых (не-crash) аппов map_model.fit()
вызывается на матрице БЕЗ строк этого аппа, а predict() — на этом состоянии
для выкинутого аппа. h_base внутри fit() тоже пересчитывается без аппа (это
правильно, не баг: h_base — часть модели, которая по-честному не должна
видеть целевой апп).

Задача A2: у Задачи A был методологически некорректный критерий — map_loo
(без аппа) сравнивался с обычной empirical, чей H_PORT считался ВКЛЮЧАЯ
предсказываемый апп. Это сравнение на неравных условиях. Честная версия:
empirical тоже переобучается без строк аппа (empirical_loo), hr при этом
остаётся как обычно — по видимым неделям самого предсказываемого аппа, это
часть прод-логики и не выкидывается ни для map, ни для empirical.

common.select_apps() считается ОДИН раз на полной матрице — apps/facts/matures
не пересчитываются между обычным прогоном и LOO, меняется только то, что видит
fit() при обучении. Модели models/map_model.py и models/empirical.py не
изменяются, только читаются.

Обычная (не-LOO) колонка "map" берётся не пересчётом, а напрямую из уже
посчитанного reports/comparison_detail.csv — тех же чисел, что в
reports/comparison.md (файл уже прошёл регрессионную проверку).

State по каждому LOO-аппу кэшируется в reports/loo_cache/ (map: <app_id>.pkl,
empirical: emp_<app_id>.pkl), чтобы повторный запуск не пересчитывал фиты
заново. Кэш привязан к fingerprint (sha256 map_model.py+common.py + mtime
se_training.parquet) в reports/loo_cache/_fingerprint.txt — при расхождении
скрипт падает с понятным сообщением вместо тихого использования устаревшего
state.

Запуск: python validate_loo.py
"""
import hashlib
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from models import common, empirical, map_model

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "reports" / "loo_cache"
FP_PATH = CACHE_DIR / "_fingerprint.txt"
DETAIL_PATH = ROOT / "reports" / "comparison_detail.csv"
OUT_PATH = ROOT / "reports" / "loo_map.md"

DEGRADATION_OK = 1.0        # <= этого -> утечки нет (старый, снятый критерий Задачи A)
DEGRADATION_MODERATE = 3.0  # <= этого -> умеренная, > -> красный флаг

SIGN_TAIL_STEPS = list(range(40, common.HMAX + 1))   # 40..52, для раздела "разбор знака"
SIGN_SUPPORT_STEPS = list(range(45, common.HMAX + 1))  # 45..52, для подсчёта n аппов-опор


# ---------------------------------------------------------------------------
# fingerprint кэша (Задача A2, п.4)
# ---------------------------------------------------------------------------

def _compute_fingerprint():
    h = hashlib.sha256()
    for rel in ("models/map_model.py", "models/common.py"):
        h.update((ROOT / rel).read_bytes())
    h.update(str((ROOT / "data" / "se_training.parquet").stat().st_mtime).encode())
    return h.hexdigest()


def check_fingerprint():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fp = _compute_fingerprint()
    if FP_PATH.exists():
        old = FP_PATH.read_text(encoding="utf-8").strip()
        if old != fp:
            raise SystemExit(
                "кэш устарел, почисти reports/loo_cache/ "
                "(fingerprint models/map_model.py + models/common.py + mtime data/se_training.parquet "
                "изменился с прошлого запуска)"
            )
        print(f"fingerprint кэша совпадает с текущим состоянием ({fp[:12]}...) — переиспользую.")
    else:
        FP_PATH.write_text(fp, encoding="utf-8")
        print(f"fingerprint кэша установлен впервые ({fp[:12]}...): код и данные с этого момента "
              f"защищены от тихой подмены кэша.")


# ---------------------------------------------------------------------------
# обычная (не-LOO) колонка map — из уже посчитанного comparison_detail.csv
# ---------------------------------------------------------------------------

def normal_model_column(model_name):
    """Агрегация уже посчитанных строк reports/comparison_detail.csv для модели
    model_name, той же логикой, что compare.py.score_model(): медиана по
    строкам на (pw, horizon), n_apps — число аппов, у которых есть хотя бы
    одна строка для этой pw (любой horizon)."""
    detail = pd.read_csv(DETAIL_PATH)
    d = detail[detail["model"] == model_name]
    results = {}
    for pw in common.PRED_WEEKS_LIST:
        dp = d[d["pw_weeks"] == pw]
        n_apps = dp["app_id"].nunique()
        results[pw] = {}
        for h in common.HORIZONS:
            dh = dp[dp["horizon"] == h]
            med = dh["abs_err_pct"].median() if len(dh) else float("nan")
            sgn = dh["signed_err_pct"].median() if len(dh) else float("nan")
            results[pw][h] = (med, sgn, n_apps)
    return results


def overall_by_horizon(results, idx=0):
    """Медиана по неделям данных (idx=0 -> |отн.ошибка|, idx=1 -> знаковая), как в
    вердикте compare.py / print_verdict()."""
    out = {}
    for h in common.HORIZONS:
        vals = [results[pw][h][idx] for pw in common.PRED_WEEKS_LIST if not np.isnan(results[pw][h][idx])]
        out[h] = np.median(vals) if vals else float("nan")
    return out


# ---------------------------------------------------------------------------
# LOO-фиты (общие для map и empirical) + скоринг
# ---------------------------------------------------------------------------

def build_states(mx, apps_info, clean_apps, model_name):
    states = {}
    n = len(clean_apps)
    for i, app in enumerate(clean_apps, 1):
        prefix = "emp_" if model_name == "empirical" else ""
        cache_path = CACHE_DIR / f"{prefix}{app}.pkl"
        if cache_path.exists():
            with open(cache_path, "rb") as f:
                states[app] = pickle.load(f)
            print(f"[{model_name} {i}/{n}] {app}: state из кэша")
        else:
            print(f"[{model_name} {i}/{n}] {app}: фит {model_name} БЕЗ этого аппа...")
            mx_loo = mx[mx["app_id"] != app]
            state = map_model.fit(mx_loo, apps_info) if model_name == "map" else empirical.fit(mx_loo)
            with open(cache_path, "wb") as f:
                pickle.dump(state, f)
            states[app] = state
    return states


def score_loo(predict_fn, states, mx, clean_apps, facts, matures):
    """Идентична по семантике score_model() из compare.py, только state — свой
    на каждый апп (LOO), а не общий для всех. Возвращает results (та же форма,
    что normal_model_column) и detail_rows (по app/pw/horizon — нужны для
    разбора знака в Задаче A2 п.5)."""
    results = {}
    detail_rows = []
    for pw in common.PRED_WEEKS_LIST:
        errs = {h: [] for h in common.HORIZONS}
        signed = {h: [] for h in common.HORIZONS}
        n_apps = 0
        for app in clean_apps:
            app_rows = mx[mx["app_id"] == app]
            pred = predict_fn(states[app], app_rows, pw)
            used = False
            for h in common.HORIZONS:
                if matures[app].get(h, 0) >= common.MIN_MATURE and not np.isnan(facts[app].get(h, np.nan)):
                    f_val, pr = facts[app][h], pred[h]
                    if f_val > 0:
                        abs_err = abs(pr - f_val) / f_val
                        signed_err = (pr - f_val) / f_val
                        errs[h].append(abs_err)
                        signed[h].append(signed_err)
                        used = True
                        detail_rows.append({
                            "app_id": app, "pw": pw, "horizon": h,
                            "pred": pr, "fact": f_val,
                            "abs_err_pct": abs_err * 100, "signed_err_pct": signed_err * 100,
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


# ---------------------------------------------------------------------------
# раздел 1 (Задача A, как было) — таблица map/map_loo + старый (снятый) вердикт
# ---------------------------------------------------------------------------

def write_old_section(lines, map_results, loo_results, empirical_overall):
    lines += [
        "# LOO-валидация модели map\n",
        "Проверка честности отрыва map: множители рычагов на каждом LOO-фите обучены "
        "БЕЗ строк аппа, для которого делается прогноз (h_base внутри fit() тоже "
        "пересчитан без него). Агрегация ячеек идентична reports/comparison.md — "
        "медиана |отн.ошибки| (зн = медиана знаковой отн.ошибки, n = число аппов, "
        f"попавших в оценку), только не-crash аппы, mature >= {common.MIN_MATURE}. "
        "Колонка map взята из reports/comparison_detail.csv (без пересчёта), "
        "map_loo — из этого прогона.\n",
    ]
    header = "| данных | " + " | ".join(
        f"нед{h} {name}" for h in common.HORIZONS for name in ("map", "map_loo")
    ) + " |"
    sep = "|" + "---|" * (1 + len(common.HORIZONS) * 2)
    lines += [header, sep]
    for pw in common.PRED_WEEKS_LIST:
        row = [f"{pw} нед"]
        for h in common.HORIZONS:
            for res in (map_results, loo_results):
                med, sgn, n = res[pw][h]
                row.append(f"{med:.0f}% (зн{sgn:+.0f}%, n={n})")
        lines.append("| " + " | ".join(row) + " |")

    lines.append("\n## Деградация (медиана по неделям данных, map_loo - map, п.п.)\n")
    map_overall = overall_by_horizon(map_results)
    loo_overall = overall_by_horizon(loo_results)
    degradations = {h: loo_overall[h] - map_overall[h] for h in common.HORIZONS}
    for h in common.HORIZONS:
        lines.append(f"- **нед{h}**: map={map_overall[h]:.1f}%, map_loo={loo_overall[h]:.1f}%, "
                      f"деградация={degradations[h]:+.1f}п.п. (empirical={empirical_overall[h]:.1f}%)")

    lines.append("\n## Вердикт (Задача A, снят — см. \"Честное сравнение\" ниже)\n")
    max_degr = max(degradations.values())
    worse_than_empirical = [h for h in common.HORIZONS if loo_overall[h] > empirical_overall[h]]
    if worse_than_empirical or max_degr > DEGRADATION_MODERATE:
        verdict = ("КРАСНЫЙ ФЛАГ: " +
                    (f"map_loo хуже empirical на горизонтах {worse_than_empirical}. "
                     if worse_than_empirical else "") +
                    (f"деградация {max_degr:.1f}п.п. > {DEGRADATION_MODERATE}п.п. "
                     if max_degr > DEGRADATION_MODERATE else "") +
                    "СТОП, требуется разбор.")
    elif max_degr > DEGRADATION_OK:
        verdict = (f"Умеренная деградация (макс. {max_degr:.1f}п.п. в диапазоне "
                   f"{DEGRADATION_OK}-{DEGRADATION_MODERATE}п.п.) — отметить, обсудить с владельцем.")
    else:
        verdict = (f"Деградация <= {DEGRADATION_OK}п.п. на всех горизонтах "
                   f"(макс. {max_degr:.1f}п.п.) -> утечки нет, отрыв map честный.")
    lines.append(verdict)
    lines.append(
        "\n**Примечание владельца (Задача A2):** этот вердикт снят — критерий сравнивал "
        "map_loo (обучен без аппа) с обычной empirical (H_PORT которой считался ВКЛЮЧАЯ "
        "предсказываемый апп). Сравнение на неравных условиях. Актуальный вердикт — в "
        "разделе \"Честное сравнение\" ниже."
    )
    return map_overall, loo_overall


# ---------------------------------------------------------------------------
# раздел 2 (Задача A2, п.1-3) — честное сравнение map_loo vs empirical_loo
# ---------------------------------------------------------------------------

def write_fair_section(lines, loo_results, emp_loo_results):
    lines.append("\n---\n")
    lines.append("## Честное сравнение: map_loo vs empirical_loo (Задача A2)\n")
    lines.append(
        "И map, и empirical переобучены БЕЗ строк предсказываемого аппа (для map — вся "
        "residual-цепочка множителей и h_base, для empirical — H_PORT). hr/hazard-ratio "
        "аппа при этом посчитан как обычно — по видимым неделям самого аппа (это часть "
        "прод-логики обеих моделей, не источник утечки, не выкидывается).\n"
    )
    header = "| данных | " + " | ".join(
        f"нед{h} {name}" for h in common.HORIZONS for name in ("map_loo", "empirical_loo")
    ) + " |"
    sep = "|" + "---|" * (1 + len(common.HORIZONS) * 2)
    lines += [header, sep]
    for pw in common.PRED_WEEKS_LIST:
        row = [f"{pw} нед"]
        for h in common.HORIZONS:
            for res in (loo_results, emp_loo_results):
                med, sgn, n = res[pw][h]
                row.append(f"{med:.0f}% (зн{sgn:+.0f}%, n={n})")
        lines.append("| " + " | ".join(row) + " |")

    map_loo_overall = overall_by_horizon(loo_results)
    emp_loo_overall = overall_by_horizon(emp_loo_results)
    deltas = {h: emp_loo_overall[h] - map_loo_overall[h] for h in common.HORIZONS}

    lines.append("\n### Итог (медиана |отн.ошибки| по неделям данных)\n")
    for h in common.HORIZONS:
        side = "в пользу map" if deltas[h] > 0 else ("в пользу empirical" if deltas[h] < 0 else "ничья")
        lines.append(f"- **нед{h}**: map_loo={map_loo_overall[h]:.1f}%, "
                      f"empirical_loo={emp_loo_overall[h]:.1f}%, "
                      f"дельта(empirical_loo - map_loo)={deltas[h]:+.1f}п.п. ({side})")

    lines.append("\n## Вердикт (актуальный, заменяет вердикт Задачи A)\n")
    if all(d > 0 for d in deltas.values()):
        verdict = (f"Отрыв map честный: на всех горизонтах map_loo точнее empirical_loo "
                   f"при равных условиях LOO (дельта от {min(deltas.values()):+.1f} до "
                   f"{max(deltas.values()):+.1f}п.п. в пользу map).")
    elif all(d < 0 for d in deltas.values()):
        verdict = (f"Отрыв map НЕ подтверждён при равных условиях: на всех горизонтах "
                   f"empirical_loo точнее map_loo (дельта от {min(deltas.values()):+.1f} до "
                   f"{max(deltas.values()):+.1f}п.п.).")
    else:
        favor_map = [h for h in common.HORIZONS if deltas[h] > 0]
        favor_emp = [h for h in common.HORIZONS if deltas[h] <= 0]
        verdict = (f"Смешанный результат: отрыв map честный на горизонтах {favor_map}, "
                   f"но НЕ подтверждён на {favor_emp} — там при равных условиях LOO "
                   f"empirical_loo не хуже или лучше map_loo.")
    lines.append(verdict)
    return verdict


# ---------------------------------------------------------------------------
# раздел 3 (Задача A2, п.5) — разбор знака +9% на нед52/1 нед данных у map_loo
# ---------------------------------------------------------------------------

def hbase_support_counts(mx, steps, min_risk=30):
    """Сколько app_id реально формируют медиану empirical_hbase на каждой ступени
    steps (те же условия, что внутри common.empirical_hbase: n>=min_risk строк
    censored-aware риска на (app_id, step_k))."""
    r = mx[mx["weeks_obs"] >= mx["step_k"]]
    g = r.groupby(["app_id", "step_k"]).agg(surv=("survived", "sum"), n=("survived", "size")).reset_index()
    g = g[g["n"] >= min_risk]
    g = g[g["step_k"].isin(steps)]
    counts = g.groupby("step_k")["app_id"].nunique().reindex(steps).fillna(0).astype(int)
    voters = {k: set(g.loc[g["step_k"] == k, "app_id"]) for k in steps}
    return counts, voters


def write_sign_section(lines, mx, map_loo_detail, map_states):
    lines.append("\n---\n")
    lines.append("## Разбор знака +9% (нед52, 1 нед данных, map_loo) — Задача A2, п.5\n")

    detail = pd.DataFrame(map_loo_detail)
    cell = detail[(detail["pw"] == 1) & (detail["horizon"] == 52)].sort_values(
        "signed_err_pct", ascending=False)
    worst3 = cell.head(3)["app_id"].tolist()
    lines.append(f"3 аппа с худшим (самым положительным, т.е. завышающим) знаком на "
                 f"этой ячейке: {', '.join(worst3)}.\n")

    h_base_full = common.empirical_hbase(mx)

    header = ["step"] + ["h_base (с аппом, портфель целиком)"]
    for app in worst3:
        header += [f"h_base_loo без {app}", f"дельта без {app}"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * len(header))
    for step in SIGN_TAIL_STEPS:
        row = [str(step), f"{h_base_full.get(step, float('nan')):.4f}"]
        for app in worst3:
            h_loo = map_states[app]["h_base"]
            v = h_loo.get(step, float("nan"))
            row.append(f"{v:.4f}")
            row.append(f"{v - h_base_full.get(step, float('nan')):+.4f}")
        lines.append("| " + " | ".join(row) + " |")

    counts_full, voters_full = hbase_support_counts(mx, SIGN_SUPPORT_STEPS)
    lines.append("\n### Сколько аппов формируют медиану h_base на ступенях "
                 f"{SIGN_SUPPORT_STEPS[0]}-{SIGN_SUPPORT_STEPS[-1]} (n с >=30 строк риска там)\n")
    lines.append("| step | n аппов-опор (полный портфель) | " +
                 " | ".join(f"{app} — сам был опорой?" for app in worst3) + " |")
    lines.append("|" + "---|" * (2 + len(worst3)))
    for step in SIGN_SUPPORT_STEPS:
        row = [str(step), str(counts_full.get(step, 0))]
        for app in worst3:
            row.append("да" if app in voters_full.get(step, set()) else "нет")
        lines.append("| " + " | ".join(row) + " |")

    min_voters = min(counts_full.get(s, 0) for s in SIGN_SUPPORT_STEPS)
    any_self_voter = any(app in voters_full.get(s, set()) for app in worst3 for s in SIGN_SUPPORT_STEPS)
    if min_voters <= 5 and any_self_voter:
        conclusion = ("ВЫБРОСЫ: на хвосте (ступени 45-52) медиану формируют буквально "
                      f"считаные аппы (минимум {min_voters} на некоторых ступенях), и минимум "
                      "один из 3 худших по знаку сам был среди этих немногих голосующих — "
                      "удаление такого аппа заметно двигает медиану, которую он же формировал. "
                      "Это структурная неустойчивость хвостовой оценки при малом n, а не "
                      "систематическая ошибка модели.")
    else:
        conclusion = ("СИСТЕМАТИКА: опора медианы на хвосте не настолько узкая (n аппов "
                      f">{min_voters}), и/или худшие по знаку аппы не входили в число голосующих "
                      "на этих ступенях — сдвиг знака не объясняется единичным выбросом, "
                      "нужен более глубокий разбор источника.")
    lines.append(f"\n**Вывод:** {conclusion}")


# ---------------------------------------------------------------------------

def main():
    check_fingerprint()

    print("загрузка матрицы...")
    mx = common.load_matrix()
    print("select_apps (один раз, на полной матрице)...")
    apps_info = common.select_apps(mx)
    apps, facts, matures, crash_flag, crash_apps = apps_info
    clean_apps = [a for a in apps if not crash_flag[a]]
    print(f"чистых (не-crash) аппов: {len(clean_apps)}")

    print("\nобычная колонка map (из comparison_detail.csv, без пересчёта)...")
    map_results = normal_model_column("map")
    empirical_overall = overall_by_horizon(normal_model_column("empirical"))

    print("\nLOO map...")
    map_states = build_states(mx, apps_info, clean_apps, "map")
    map_loo_results, map_loo_detail = score_loo(map_model.predict, map_states, mx, clean_apps, facts, matures)

    print("\nLOO empirical...")
    emp_states = build_states(mx, apps_info, clean_apps, "empirical")
    emp_loo_results, _ = score_loo(empirical.predict, emp_states, mx, clean_apps, facts, matures)

    lines = []
    write_old_section(lines, map_results, map_loo_results, empirical_overall)
    verdict = write_fair_section(lines, map_loo_results, emp_loo_results)
    write_sign_section(lines, mx, map_loo_detail, map_states)

    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nотчёт записан: {OUT_PATH}")
    print("Актуальный вердикт:", verdict)


if __name__ == "__main__":
    main()
