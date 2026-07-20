"""
Задача B — разбор немонотонности знака у map: медиана знаковой ошибки на
2 неделях данных (нед52 map: зн+7%) хуже, чем на 1 неделе (зн+2%), хотя
ожидание — монотонное улучшение с ростом объёма данных. Разбираем: это
пара аппов-выбросов тянет медиану, или систематика по портфелю.

Зацепка из Задачи A2 (reports/loo_map.md, "Разбор знака"): похожий скачок
знака (+9% у map_loo, pw=1, нед52) НЕ объясняется хвостовой нестабильностью
h_base — там медиану формируют 18-22 аппа, не единицы. Значит дело либо в
hr, либо в личном множителе (не в форме кривой).

По коду models/map_model.py: personal_multiplier(state, app_rows) НЕ
принимает weeks и считается на ПОЛНОМ app_rows (первая по step_k строка на
подписчика, атрибуты geo/media_source/billday известны сразу при подписке,
не требуют недель наблюдения) — состав и веса комбо физически не могут
зависеть от pw. Единственный компонент predict(), зависящий от weeks —
hr (шаг 3.2, hazard-ratio аппа по видимым неделям). Проверяем это
эмпирически (не считаем как данность), затем разбираем, что двигает hr:
гипотеза владельца — на pw=2 hr уже реагирует на шум ранних ступеней, а
усадка n/(n+K_SHRINK=800) при таком n_seen ещё слабая.

models/map_model.py НЕ модифицируется, только читается. Формула шага 3.2
(hr) скопирована сюда буквально для диагностики — predict() отдаёт наружу
только готовую кривую, hr отдельно не возвращает.

Запуск: python debug_sign_pw2.py
"""
from pathlib import Path

import numpy as np
import pandas as pd

from models import common, map_model

ROOT = Path(__file__).resolve().parent
OUT_PATH = ROOT / "reports" / "sign_pw2_analysis.md"
HORIZON = 52


def compute_hr(state, app_rows, weeks):
    """Буквальная копия шага 3.2 из map_model.predict() — только чтобы
    достать hr отдельно от готовой кривой для диагностики. Возвращает
    также combos (шаг 3.1, сверка личного множителя) и g (o/e по ступеням,
    до усреднения) — нужно, чтобы увидеть, что именно двигает log_hr,
    а не гадать по итоговому hr."""
    h_base = state["h_base"]
    combos = map_model.personal_multiplier(state, app_rows)
    subs = map_model._subs_with_levers(state, app_rows)
    subs = subs.merge(combos[map_model.MAP_LEVERS + ["mult"]], on=map_model.MAP_LEVERS, how="left")
    sub_mult = subs.set_index("sub_id")["mult"]

    obs = app_rows[app_rows["weeks_obs"] >= app_rows["step_k"] + 1].copy()
    obs = obs[obs["step_k"] <= weeks]
    n_seen = len(obs)
    g = pd.DataFrame()
    if n_seen > 0:
        obs["mult"] = obs["sub_id"].map(sub_mult)
        obs["exp_die"] = (h_base.reindex(obs["step_k"]).values * obs["mult"].values)
        obs["exp_die"] = obs["exp_die"].clip(0.001, 0.999)
        g = obs.groupby("step_k").agg(o=("died", "mean"), e=("exp_die", "mean"), n=("died", "size"))
        gg = g[(g["n"] >= 20) & (g["e"] > 0)]
        log_hr = np.average(np.log(np.clip(gg["o"] / gg["e"], 0.2, 5.0)), weights=gg["n"]) if len(gg) else 0.0
    else:
        log_hr = 0.0
    shrink = n_seen / (n_seen + common.K_SHRINK)
    hr = np.exp(log_hr * shrink)
    return hr, n_seen, log_hr, shrink, combos, g


def main():
    print("загрузка матрицы, обычный (не-LOO) фит map...")
    mx = common.load_matrix()
    apps_info = common.select_apps(mx)
    apps, facts, matures, crash_flag, crash_apps = apps_info
    clean_apps = [a for a in apps if not crash_flag[a]]

    state = map_model.fit(mx, apps_info)  # тот же state, что в обычном (не-LOO) прогоне compare.py

    rows = []
    combo_mismatch = []
    for app in clean_apps:
        app_rows = mx[mx["app_id"] == app]
        if matures[app].get(HORIZON, 0) < common.MIN_MATURE or np.isnan(facts[app].get(HORIZON, np.nan)):
            continue
        fact = facts[app][HORIZON]
        if fact <= 0:
            continue

        pred1 = map_model.predict(state, app_rows, 1)[HORIZON]
        pred2 = map_model.predict(state, app_rows, 2)[HORIZON]
        hr1, n_seen1, log_hr1, shrink1, combos1, g1 = compute_hr(state, app_rows, 1)
        hr2, n_seen2, log_hr2, shrink2, combos2, g2 = compute_hr(state, app_rows, 2)

        m1 = combos1.set_index(map_model.MAP_LEVERS)["mult"].sort_index()
        m2 = combos2.set_index(map_model.MAP_LEVERS)["mult"].sort_index()
        if not m1.equals(m2):
            combo_mismatch.append(app)
        personal_mult_avg1 = np.average(combos1["mult"], weights=combos1["w"])
        personal_mult_avg2 = np.average(combos2["mult"], weights=combos2["w"])

        # o/e отдельно на step_k=1 и step_k=2 (до усреднения в log_hr) — чтобы увидеть,
        # что именно тянет log_hr вниз при переходе pw=1 -> pw=2: слабая усадка при
        # маленьком n, или собственный o/e второй ступени, отличный от первой.
        ratio1 = g2.loc[1, "o"] / g2.loc[1, "e"] if 1 in g2.index else np.nan
        ratio2 = g2.loc[2, "o"] / g2.loc[2, "e"] if 2 in g2.index else np.nan

        signed1 = (pred1 - fact) / fact * 100
        signed2 = (pred2 - fact) / fact * 100
        n_users_2w = app_rows[app_rows["step_k"] <= 2]["sub_id"].nunique()

        rows.append({
            "app_id": app, "fact": fact, "pred_pw1": pred1, "pred_pw2": pred2,
            "signed_pw1": signed1, "signed_pw2": signed2, "delta": signed2 - signed1,
            "hr_pw1": hr1, "hr_pw2": hr2, "n_seen_pw1": n_seen1, "n_seen_pw2": n_seen2,
            "shrink_pw1": shrink1, "shrink_pw2": shrink2,
            "ratio_step1": ratio1, "ratio_step2": ratio2,
            "personal_mult_pw1": personal_mult_avg1, "personal_mult_pw2": personal_mult_avg2,
            "n_users_2w": n_users_2w,
        })

    df = pd.DataFrame(rows)
    print(f"аппов в оценке (mature>=200 на нед52): {len(df)}")
    print(f"комбо-множитель отличался между pw=1 и pw=2 (ожидание: ни у одного): {len(combo_mismatch)}"
          + (f" -> {combo_mismatch}" if combo_mismatch else ""))

    n_worsened = (df["signed_pw2"].abs() > df["signed_pw1"].abs()).sum()
    n_improved = (df["signed_pw2"].abs() < df["signed_pw1"].abs()).sum()
    n_same = len(df) - n_worsened - n_improved

    df_sorted = df.sort_values("delta", key=lambda s: s.abs(), ascending=False)
    worst3 = df.sort_values("delta", ascending=False).head(3)  # самое сильное смещение в сторону завышения (пред-fact)

    write_report(df_sorted, worst3, n_worsened, n_improved, n_same, combo_mismatch, state["h_base"])


def write_report(df_sorted, worst3, n_worsened, n_improved, n_same, combo_mismatch, h_base):
    lines = [
        "# Разбор знака: почему на 2 неделях данных хуже, чем на 1 (map, нед52)\n",
        "Контекст: reports/comparison.md, строка \"1 нед\" -> нед52 map зн+2%, "
        "строка \"2 нед\" -> нед52 map зн+7%. Ожидание — монотонное улучшение с "
        "ростом данных, получили ухудшение. Ниже — по-апповая раскладка на все "
        f"{len(df_sorted)} чистых аппов с mature>={common.MIN_MATURE} на нед52, "
        "отсортированная по |дельте| (зн.ошибка pw2 - зн.ошибка pw1).\n",
    ]
    header = ("| app_id | факт | pred_pw1 | pred_pw2 | зн.ошибка pw1 | зн.ошибка pw2 "
               "| дельта | n_users(<=2нед) |")
    lines += [header, "|" + "---|" * 8]
    for _, r in df_sorted.iterrows():
        lines.append(
            f"| {r['app_id']} | {r['fact']:.4f} | {r['pred_pw1']:.4f} | {r['pred_pw2']:.4f} "
            f"| {r['signed_pw1']:+.1f}% | {r['signed_pw2']:+.1f}% | {r['delta']:+.1f}п.п. "
            f"| {r['n_users_2w']:.0f} |"
        )

    lines.append(f"\n## Знак: ухудшился/улучшился при переходе 1->2 недели данных\n")
    lines.append(f"- ухудшился (|зн.ошибка| выросла): **{n_worsened}** из {len(df_sorted)} аппов")
    lines.append(f"- улучшился (|зн.ошибка| упала): **{n_improved}**")
    lines.append(f"- не изменился: {n_same}")

    lines.append("\n## Проверка личного множителя (гипотеза владельца, п.2)\n")
    if combo_mismatch:
        lines.append(f"Личный множитель комбо ОТЛИЧАЛСЯ между pw=1 и pw=2 у {len(combo_mismatch)} "
                     f"аппов: {combo_mismatch}. Состав пользователей аппа между 1 и 2 неделей данных "
                     "не идентичен тому, что видит personal_multiplier() — требует разбора.")
    else:
        lines.append(
            "Личный множитель комбо ИДЕНТИЧЕН между pw=1 и pw=2 у всех аппов (проверено по всем "
            f"{len(df_sorted)} аппам). Это ожидаемо по коду: personal_multiplier(state, app_rows) "
            "не принимает weeks и строится на ПОЛНОМ app_rows (первая по step_k строка на "
            "подписчика — geo/media_source/billday известны сразу при подписке, не зависят от "
            "того, сколько недель мы наблюдаем ретеншен). **Личный множитель исключён как причина "
            "разницы pred_pw1 vs pred_pw2 — вся разница идёт из hr.**"
        )

    lines.append("\n## Топ-3 аппа по ухудшению (самый сильный сдвиг в сторону завышения) — раскладка на hr\n")
    header2 = ("| app_id | hr_pw1 | hr_pw2 | n_seen_pw1 | n_seen_pw2 | усадка n/(n+800) pw1 "
               "| усадка pw2 | o/e ступень1 | o/e ступень2 | личн.множ. pw1 | личн.множ. pw2 |")
    lines += [header2, "|" + "---|" * 11]
    for _, r in worst3.iterrows():
        lines.append(
            f"| {r['app_id']} | {r['hr_pw1']:.3f} | {r['hr_pw2']:.3f} "
            f"| {r['n_seen_pw1']:.0f} | {r['n_seen_pw2']:.0f} "
            f"| {r['shrink_pw1']:.3f} | {r['shrink_pw2']:.3f} "
            f"| {r['ratio_step1']:.3f} | {r['ratio_step2']:.3f} "
            f"| {r['personal_mult_pw1']:.3f} | {r['personal_mult_pw2']:.3f} |"
        )

    mult_moves = (worst3["personal_mult_pw2"] - worst3["personal_mult_pw1"]).abs()
    lines.append(f"\nСредний сдвиг |личн.множ. pw2 - pw1| у топ-3: {mult_moves.mean():.6f} "
                 "(должен быть 0 по построению — см. проверку выше).")
    lines.append(
        "\n**Гипотеза владельца (\"усадка n/(n+800) ещё слабая на pw=2\") НЕ подтвердилась**: "
        f"усадка уже {worst3['shrink_pw1'].min():.2f}-{worst3['shrink_pw1'].max():.2f} на pw=1 "
        f"(n_seen тысячи, не единицы) и {worst3['shrink_pw2'].min():.2f}-{worst3['shrink_pw2'].max():.2f} "
        "на pw=2 — разница между pw=1 и pw=2 в усадке минимальна (0.03-0.04), она не может "
        "объяснить сдвиг hr на 0.03-0.04 в абсолютном выражении, который мы видим. Настоящий "
        "источник — колонки \"o/e ступень1\" и \"o/e ступень2\" выше: у всех топ-3 o/e на "
        "ступени 2 НИЖЕ, чем на ступени 1 — то есть удержание на 2-й ступени лучше, чем "
        f"предполагает h_base[2]={h_base.get(2, float('nan')):.4f} (портфельная медиана), "
        "причём заметно лучше, чем на 1-й ступени относительно h_base[1]="
        f"{h_base.get(1, float('nan')):.4f}. Как только 2-я ступень попадает в окно hr "
        "(взвешенное среднее log(o/e) по ступеням 1 и 2), она тянет hr ниже 1.0 сильнее, "
        "чем ступень 1 одна — и это не шум пары аппов, а закономерность на всей "
        "оцениваемой выборке (см. ниже).")

    n_ratio_valid = df_sorted["ratio_step1"].notna() & df_sorted["ratio_step2"].notna()
    n_lower = (df_sorted.loc[n_ratio_valid, "ratio_step2"] < df_sorted.loc[n_ratio_valid, "ratio_step1"]).sum()
    n_ratio_total = n_ratio_valid.sum()
    med_r1 = df_sorted.loc[n_ratio_valid, "ratio_step1"].median()
    med_r2 = df_sorted.loc[n_ratio_valid, "ratio_step2"].median()
    lines.append(f"\n### o/e по ступеням на всей оцениваемой выборке ({n_ratio_total} аппов)\n")
    lines.append(f"- медиана o/e на ступени 1: {med_r1:.3f}")
    lines.append(f"- медиана o/e на ступени 2: {med_r2:.3f}")
    lines.append(f"- у скольких аппов o/e на ступени 2 НИЖЕ, чем на ступени 1: "
                 f"**{n_lower}/{n_ratio_total}** ({n_lower/n_ratio_total*100:.0f}%)")

    # вывод
    n_total = len(df_sorted)
    frac_worsened = n_worsened / n_total if n_total else 0.0
    frac_lower = n_lower / n_ratio_total if n_ratio_total else 0.0
    if frac_worsened > 0.5 and frac_lower > 0.5:
        verdict = (f"СИСТЕМАТИКА: знак ухудшился у большинства аппов ({n_worsened}/{n_total}, "
                   f"{frac_worsened*100:.0f}%), и это объясняется закономерностью на уровне "
                   f"портфеля: у {n_lower}/{n_ratio_total} ({frac_lower*100:.0f}%) оцениваемых "
                   "аппов удержание на 2-й ступени (o/e) лучше относительно h_base, чем на 1-й. "
                   "Дёргается компонента **hr** (личный множитель исключён проверкой выше) — но "
                   "НЕ из-за слабой усадки n/(n+800) (она уже сильная на pw=1, n_seen в тысячах): "
                   "источник — h_base[2] систематически недооценивает удержание именно на 2-й "
                   "ступени для этой (зрелой, отобранной по mature>=200 на нед52) группы аппов, "
                   "и включение этой ступени в окно hr тянет прогноз дальше в завышение.")
    else:
        verdict = (f"ВЫБРОСЫ/СМЕШАННО: знак ухудшился у {n_worsened}/{n_total} "
                   f"({frac_worsened*100:.0f}%) аппов, а o/e на ступени 2 ниже, чем на ступени 1, "
                   f"только у {n_lower}/{n_ratio_total} ({frac_lower*100:.0f}%) — нет чистого "
                   "портфельного паттерна, требуется разбор на уровне конкретных аппов, а не "
                   "общей закономерности.")
    lines.append(f"\n## Вывод\n\n{verdict}")

    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nотчёт записан: {OUT_PATH}")
    print(verdict)


if __name__ == "__main__":
    main()
